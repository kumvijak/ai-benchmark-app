"""Evidence-first, offline report generation. No cloud or SSH access."""
from __future__ import annotations

import base64
import csv
import hashlib
import html
import json
import math
import re
from pathlib import Path
import zipfile

from .benchmark import _guidellm_benchmark_rows
from .timing import audit_timing

REPORT_VERSION = '1.2.1'
LEASE_SECONDS = 60
MAX_ATTEMPTS = 3

REPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 owner_user_id INTEGER NOT NULL REFERENCES users(id),
 title TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued',
 stage TEXT NOT NULL DEFAULT 'queued',
 progress INTEGER NOT NULL DEFAULT 0,
 snapshot TEXT NOT NULL,
 summary TEXT,
 error_message TEXT,
 attempts INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT,
 lease_until REAL,
 artifact_token TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 finished_at TEXT,
 UNIQUE(owner_user_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS report_queue_status ON report_jobs(status, id);
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sanitized(value):
    """Export structural evidence; exclude credentials, prompts and generated text."""
    if isinstance(value, dict):
        return {str(k): sanitized(v) for k, v in value.items()
                if not re.search(r'password|passphrase|private.?key|secret|authorization|api.?key|access.?token|credential|prompt_text|output_text|response_text|^(?:token|hf_token|api_token|headers|cookies|prompt|response|completion|messages|text|generated_text|body)$', str(k), re.I)}
    if isinstance(value, list):
        return [sanitized(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[REDACTED]', value, flags=re.S)
        value = re.sub(r'\b(?:hf_[A-Za-z0-9]+|Bearer\s+[^\s"\']+)', '[REDACTED]', value)
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def native_json(result):
    if isinstance(result.get('guidellm_json'), dict):
        return result['guidellm_json']
    artifact = (result.get('artifacts') or {}).get('json') or {}
    try:
        raw = artifact.get('content')
        if raw is None and artifact.get('content_b64'):
            raw = base64.b64decode(artifact['content_b64'], validate=True).decode()
        value = json.loads(raw) if raw else None
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError):
        return None


def snapshot_job(job):
    result = json.loads(job['result']) if job.get('result') else {}
    payload = json.loads(job['payload']) if job.get('payload') else {}
    # Never capture the entire payload or fetch mutable profiles as historical fact.
    spec = result.get('spec') or {k: payload[k] for k in (
        'workload_name', 'input_tokens', 'output_tokens', 'max_requests', 'max_errors',
        'benchmark_mode', 'max_duration_seconds', 'concurrencies', 'seed') if k in payload}
    native = native_json(result)
    source = sanitized(dict(job_id=job['id'], status=job['status'], created_at=job['created_at'],
        finished_at=job.get('finished_at'), spec=spec, native=native,
        timing_policy=result.get('timing_policy') or {},
        runtime_evidence=result.get('runtime_evidence') or {},
        execution_snapshot=result.get('execution_snapshot') or {},
        model_precision=result.get('model_precision') or {},
        vm_profile_id=payload.get('vm_profile_id'),
        normalized_rows=result.get('normalized_rows') or []))
    source['source_sha256'] = digest(source)
    return source


def enqueue_report(db, user_id, title, job_ids):
    ids = sorted(set(job_ids))
    if not ids or len(ids) > 100:
        raise ValueError('Select between 1 and 100 benchmark jobs')
    sources = []
    for job_id in ids:
        job = db.one("SELECT * FROM jobs WHERE id=? AND owner_user_id=? AND job_type='run_benchmark'", (job_id, user_id))
        if not job:
            raise LookupError('Benchmark job not found')
        if job['status'] not in ('succeeded', 'failed'):
            raise ValueError('Only finished benchmark jobs can be included')
        sources.append(snapshot_job(job))
    snapshot = dict(version=REPORT_VERSION, title=title, sources=sources)
    encoded = canonical(snapshot)
    if len(encoded.encode()) > 50 * 1024 * 1024:
        raise ValueError('Report evidence exceeds 50 MiB; select fewer jobs')
    fingerprint = digest(snapshot)
    db.execute('INSERT OR IGNORE INTO report_jobs(owner_user_id,title,fingerprint,snapshot) VALUES(?,?,?,?)',
               (user_id, title, fingerprint, encoded))
    return db.one('SELECT id,status FROM report_jobs WHERE owner_user_id=? AND fingerprint=?', (user_id, fingerprint))


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def groups_from_snapshot(snapshot):
    groups = {}
    for source in snapshot['sources']:
        spec = source['spec']
        execution = source['execution_snapshot']
        runtime = source['runtime_evidence']
        profile = execution.get('profile') or runtime.get('serving_config') or source['model_precision']
        system = execution.get('system') or {'vm_profile_id': source['vm_profile_id'], 'description': 'Historical hardware not recorded'}
        identity = dict(model=profile, system=system,
            workload={k: v for k, v in spec.items() if k not in ('job_id', 'concurrencies', 'concurrency', 'runtime_evidence')},
            software={k: runtime.get(k) for k in ('backend','platform','cpu_vendor','vllm_version','torch_version','zentorch_version','zendnn_version','triton_version')})
        quantization = runtime.get('quantization_evidence') or {}
        identity['quantization'] = {key:quantization.get(key) for key in ('bits','format','recipe_sha256','source_revision','validation')}
        identity['software']['guidellm_version'] = execution.get('guidellm_version')
        key = digest(identity)
        if key not in groups:
            groups[key] = dict(id=key[:12], configuration=identity, runtime=sanitized(runtime), points=[], attempts=[])
        group = groups[key]
        group['attempts'].append(source['job_id'])
        raw = source.get('native')
        benchmarks = raw.get('benchmarks', []) if raw else []
        rows = _guidellm_benchmark_rows(raw, spec) if raw else source.get('normalized_rows', [])
        if not rows:
            rows = [{}]
        for index, row in enumerate(rows):
            benchmark = benchmarks[index] if index < len(benchmarks) else {}
            warnings = []
            if not execution.get('serving_identity_verified'):
                warnings.append('Historical serving identity/precision not verified')
            if profile.get('quantization') in ('int8', 'int4') and quantization.get('validation', {}).get('status') != 'PASS':
                warnings.append('Quantized checkpoint validation not recorded')
            if not (profile.get('model_revision') or profile.get('source_revision')):
                warnings.append('Source model revision not pinned or not recorded')
            if not raw:
                warnings.append('Native JSON unavailable; normalized values are historical and unverified')
            metrics = benchmark.get('metrics') or {}
            totals = metrics.get('request_totals') or {}
            total = number(totals.get('total', row.get('requests')))
            success = number(totals.get('successful', row.get('success_requests')))
            errored = number(totals.get('errored'))
            incomplete = number(totals.get('incomplete'))
            failed = (errored + incomplete) if errored is not None and incomplete is not None else number(row.get('failed_requests'))
            if None in (total, success, failed) or (success is not None and total is not None and success > total):
                warnings.append('Request counts missing or inconsistent')
            elif success + failed != total:
                warnings.append('Successful/error/incomplete counts do not reconcile')
            target = number(spec.get('max_requests'))
            outcome = ('complete' if source['status'] == 'succeeded' and target is not None and success is not None
                       and success >= target and failed == 0 and total == success else
                       'failed' if source['status'] == 'failed' or (success == 0 and failed) else 'incomplete')
            if outcome != 'complete':
                warnings.append('Configured request target not completed without errors/incomplete requests')
            # Native rates are retained for inspection. Do not reconstruct from summary duration:
            # retained request timing is required to validate the measurement window.
            native_tps = number(row.get('output_tokens_per_second'))
            timing = audit_timing(benchmark)
            warnings.extend(timing['native_timing_findings'])
            if timing['native_timing_status'] == 'not_checked':
                warnings.append('Native timing boundaries unavailable for consistency checks')
            warnings.extend(timing['full_run_findings'])
            latency = {}
            for name, native_name in [('ttft','time_to_first_token_ms'),('tpot','time_per_output_token_ms'),('latency','request_latency')]:
                successful = (metrics.get(native_name) or {}).get('successful') or {}
                values = {**(successful.get('percentiles') or {}), **{k: successful.get(k) for k in ('min','mean','max')}}
                latency[name] = {k: (number(v) * (1000 if name == 'latency' else 1) if number(v) is not None else None) for k, v in values.items()}
            concurrency = number(row.get('concurrency'))
            if not concurrency:
                warnings.append('Concurrency unavailable; point excluded from concurrency charts')
            point = dict(job_id=source['job_id'], point_index=index + 1, run_date=source.get('finished_at') or source['created_at'],
                concurrency=concurrency, outcome=outcome, total_requests=total, successful_requests=success,
                errored_requests=errored, incomplete_requests=incomplete, failed_requests=failed,
                native_system_tps=native_tps, reconstructed_system_tps=timing['full_run_output_tps'],
                full_run_tps_per_concurrency=timing['full_run_output_tps']/concurrency if timing['full_run_output_tps'] is not None and concurrency else None,
                aggregate_tps_per_concurrency=native_tps / concurrency if native_tps is not None and concurrency else None,
                ttft_p95_ms=number(latency['ttft'].get('p95', row.get('ttft_ms_p95'))),
                duration_seconds=number(benchmark.get('duration', row.get('duration_seconds'))),
                latency_ms=latency, warnings=warnings, evidence_status='warning' if warnings else 'checked', **timing)
            group['points'].append(point)
        group['points'].sort(key=lambda p: (p['concurrency'] or 0, p['job_id'], p['point_index']))
    return list(groups.values())


def fmt(value):
    if value is None:
        return 'Not measured'
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return f'{value:,.3f}'.rstrip('0').rstrip('.') if isinstance(value, float) else str(value)


def esc(value):
    return html.escape(fmt(value), quote=True)


def chart(points, x_key, y_key, title, x_label, y_label):
    valid = [p for p in points if number(p.get(x_key)) is not None and number(p.get(y_key)) is not None]
    if not valid:
        return f'<section class="card chart"><h3>{esc(title)}</h3><p>Not measured — no usable points.</p></section>'
    xmax = max([p[x_key] for p in valid] + [1]); ymax = max([p[y_key] for p in valid] + [1])
    circles = ''
    for point in valid:
        x = 68 + 430 * point[x_key] / xmax; y = 240 - 190 * point[y_key] / ymax
        label = f"Job {point['job_id']}, point {point['point_index']} · C{fmt(point['concurrency'])} · {x_label}: {fmt(point[x_key])} · {y_label}: {fmt(point[y_key])} · {point['outcome']} · {point['evidence_status']} evidence"
        circles += f'<circle cx="{x:.2f}" cy="{y:.2f}" r="6" tabindex="0" aria-label="{esc(label)}" data-job="{point["job_id"]}" class="{point["outcome"]}"><title>{esc(label)}</title></circle>'
    ticks = ''
    for i in range(5):
        x = 68 + 430 * i / 4; y = 240 - 190 * i / 4
        ticks += f'<path d="M68 {y}H498" stroke="#e5eaf1"/><text x="58" y="{y+4}" text-anchor="end">{esc(round(ymax*i/4,2))}</text><text x="{x}" y="260" text-anchor="middle">{esc(round(xmax*i/4,2))}</text>'
    return f'''<section class="card chart"><h3>{esc(title)}</h3><svg viewBox="0 0 560 300" role="group" aria-label="{esc(title)}">{ticks}<path d="M68 40V240H510" fill="none" stroke="#98a5b8"/>{circles}<text x="280" y="292" text-anchor="middle">{esc(x_label)}</text><text x="68" y="20">{esc(y_label)}</text></svg><p class="chart-readout note" aria-live="polite">Hover or focus a point for measurements.</p><p class="muted">Measured points only · no interpolation</p></section>'''


CSS = """
:root{--ink:#17253b;--muted:#64748b;--blue:#1466c3;--border:#dce3ed;--bg:#f5f7fa;--fail:#c63843;--pass:#1d8066;--inc:#a76700}

*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;font:14px/1.55 system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--ink);background:var(--bg)}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,circle:focus-visible{outline:3px solid var(--blue);outline-offset:4px}
.top{background:white;border-bottom:1px solid var(--border)}
.top-inner{max-width:1280px;margin:auto;padding:17px 28px;display:flex;align-items:center;justify-content:space-between;gap:20px}
.brand{font-weight:750;font-size:17px}
.brand small{display:block;font-size:11px;color:var(--muted);font-weight:500;letter-spacing:.08em;text-transform:uppercase}
main{max-width:1280px;margin:auto;padding:28px}
.hero{margin:18px 0 22px}
.eyebrow{text-transform:uppercase;letter-spacing:.1em;color:var(--muted);font-size:11px;font-weight:700}
h1{font-size:32px;line-height:1.2;margin:8px 0;overflow-wrap:anywhere}
h2{font-size:21px;margin:0 0 16px}
h3{font-size:16px;margin:0 0 10px}
.subtitle{color:var(--muted);font-size:16px}
.muted,.note{font-size:12px;color:var(--muted)}
.badge{display:inline-block;padding:3px 10px;font-size:11px;border-radius:20px;font-weight:750;background:#edf2f8;color:var(--muted);white-space:nowrap}
.badge.complete{background:#e5f4ed;color:var(--pass)}
.badge.failed{background:#fff0f1;color:var(--fail)}
.badge.incomplete,.badge.warning{background:#fff3dc;color:var(--inc)}
.card{background:white;border:1px solid var(--border);border-radius:10px;padding:22px;margin-bottom:22px;box-shadow:0 2px 5px #102a4310;min-width:0}
.grid,.report-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}
.grid .card{margin:0}
.section{margin:26px 0}
.facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:15px 26px}
.item span{display:block;color:var(--muted);font-size:12px;margin-bottom:3px}
.item strong{font-size:14px;font-weight:600;overflow-wrap:anywhere}
.notice,.callout{background:#edf5ff;border-left:3px solid var(--blue);padding:12px 16px;margin:18px 0;color:#244568;font-size:13px}
.stats{display:flex;gap:28px;flex-wrap:wrap;margin-top:16px}
.stats strong{font-size:24px;display:block}
.stats span{font-size:12px;color:var(--muted)}
nav,.links,.counts,.attempt-controls{display:flex;gap:15px;flex-wrap:wrap}
.links{font-size:13px}
.report-link{color:var(--ink);display:block;border:1px solid var(--border);border-radius:10px;padding:20px;background:white;overflow-wrap:anywhere}
.report-link:hover{text-decoration:none;border-color:#8cb5e2;box-shadow:0 4px 16px #163a6520}
.report-link h3{font-size:17px;margin:9px 0}
.counts{font-size:12px;color:var(--muted);margin-top:15px}
.filters{display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin:22px 0}
.filters label{display:grid;gap:4px;font-size:12px;color:var(--muted)}
button,input,select{font:inherit;padding:9px 12px;border:1px solid var(--border);border-radius:7px;background:white;color:var(--ink);max-width:100%}
button{cursor:pointer;color:var(--blue)}
input[type=checkbox]{accent-color:var(--blue)}
.scroll{overflow:auto;max-width:100%}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--border);vertical-align:top;min-width:135px;overflow-wrap:anywhere}
th{background:#f3f6fa}
tbody th{position:sticky;left:0}
caption{text-align:left;color:var(--muted);padding:0 0 12px}
.run-table thead th{white-space:nowrap}
.run-table tbody tr:nth-child(even) td{background:#f8fafc}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
svg{display:block;width:100%;height:auto}
svg text{font:11px system-ui;fill:#526780}
circle{fill:var(--pass);stroke:white;stroke-width:2;cursor:help}
circle.incomplete{fill:var(--inc)}
circle.failed{fill:var(--fail)}
details{padding:10px 0;border-bottom:1px solid var(--border)}
summary{cursor:pointer}
footer{padding:20px 0 8px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:20px}
.attempt-summary:target{outline:3px solid var(--blue);outline-offset:4px}
.attempt-summary{scroll-margin-top:20px}
.empty{padding:30px;text-align:center}
.definitions p{font-size:13px;color:#475569}

@media(max-width:760px){main{padding:18px}
.top-inner{padding:14px 18px;flex-direction:column;align-items:stretch}
h1{font-size:25px}
.grid,.report-grid{grid-template-columns:1fr}
.facts{grid-template-columns:repeat(2,minmax(0,1fr))}
.card{padding:17px}
.filters label{flex:1 1 145px;min-width:0}
.filters input,.filters select{width:100%}
footer{display:block}
}

@media print{.top nav,.filters,.attempt-controls,button{display:none}
body{background:white}
main{max-width:none;padding:0}
.card{box-shadow:none;break-inside:avoid}
.scroll{overflow:visible}
tbody th{position:static}
.run-table{font-size:9px}
.run-table th,.run-table td{min-width:0;padding:5px}
a{color:inherit}
.grid{gap:12px}
}

"""


def document(title, body, script=''):
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title><style>{CSS}</style></head><body><header class="top"><div class="top-inner"><div class="brand">CPU inference benchmark reports<small>GuideLLM · MLCommons-style presentation</small></div><nav aria-label="Report downloads"><a href="index.html">All benchmarks</a><a href="metrics.csv">Metrics CSV</a><a href="report.json">Report JSON</a><a href="manifest.json">Evidence manifest</a></nav></div></header><main>{body}<footer><span>GuideLLM measurements · Not an official MLPerf submission</span><span>Report pipeline {REPORT_VERSION}</span></footer></main>{script}</body></html>'''


def configuration_labels(configuration):
    profile = configuration['model']
    dtype = {'bfloat16': 'BF16', 'float16': 'FP16', 'float32': 'FP32'}.get(profile.get('dtype'), profile.get('dtype') or 'Unverified dtype')
    quantization = profile.get('quantization')
    precision = dtype if quantization == 'none' else str(quantization).upper() if quantization else f'{dtype} · quantization unverified'
    return dict(model=profile.get('model_ref') or profile.get('source_model') or configuration['workload'].get('model_ref') or 'Model not recorded',
                shape=configuration['system'].get('shape') or 'Host not recorded', precision=precision,
                workload=configuration['workload'].get('workload_name') or 'Workload not recorded')


def attempt_summaries(group):
    """Summarize each attempt without averaging retries or guessing absent counts."""
    attempts = []
    for jid in group['attempts']:
        points = [p for p in group['points'] if p['job_id'] == jid]
        outcome = 'failed' if any(p['outcome'] == 'failed' for p in points) else 'complete' if points and all(p['outcome'] == 'complete' for p in points) else 'incomplete'
        dates = sorted({str(p['run_date'])[:10] for p in points if p.get('run_date')})
        counts = {}
        for key in ('successful_requests', 'errored_requests', 'incomplete_requests'):
            values = [number(p.get(key)) for p in points]
            counts[key] = sum(values) if values and all(v is not None for v in values) else None
        attempts.append(dict(job_id=jid, points=points, outcome=outcome, date=', '.join(dates) or 'Date not recorded', **counts))
    return attempts


def badge(outcome):
    return f'<span class="badge {esc(outcome)}">{esc(outcome.upper())}</span>'


def facts(values):
    return '<div class="facts">' + ''.join(f'<div class="item"><span>{esc(label)}</span><strong>{esc(value)}</strong></div>' for label, value in values) + '</div>'


def metadata_card(title, values):
    labels = {'model_ref':'Model', 'model_revision':'Source revision', 'quantization':'Weight quantization',
              'dtype':'Floating-point dtype', 'cpu_kvcache_space_gib':'KV cache (GiB)',
              'max_model_len':'Maximum context tokens', 'max_num_seqs':'Maximum sequences',
              'ocpus':'OCPUs', 'exposed_vcpus':'Exposed vCPUs', 'memory_gb':'Memory (GiB)',
              'input_tokens':'Input token budget', 'output_tokens':'Output token budget',
              'max_requests':'Request target', 'max_errors':'Error stop limit',
              'max_duration_seconds':'Duration limit (s)', 'backend':'Inference backend'}
    def rows(data, prefix=''):
        output = ''
        for key,value in data.items():
            label = prefix + labels.get(key, key.replace('_',' ').capitalize())
            if isinstance(value,dict):
                output += rows(value, label+' · ')
            else:
                if value is None and key == 'cpu_kvcache_space_gib':
                    value = 'Automatic'
                elif value is None and key == 'max_errors':
                    value = 'No error stop'
                output += '<tr><th>'+esc(label)+'</th><td>'+esc(value)+'</td></tr>'
        return output
    content = '<table><tbody>'+rows(values)+'</tbody></table>' if values else '<p>Not recorded</p>'
    return '<section class="card"><h2>'+esc(title)+'</h2><div class="scroll">'+content+'</div></section>'


def render_detail(group, title):
    c = group['configuration']; profile = c['model']; points = group['points']
    labels = configuration_labels(c)
    model = profile.get('model_ref') or profile.get('source_model') or c['workload'].get('model_ref') or 'Model not recorded'
    charts = ''.join(chart(points, *args) for args in [
        ('full_run_tps_per_concurrency','reconstructed_system_tps','Full-run TPS vs. tokens/s per concurrency','Full-run TPS / concurrency','Successful output tokens/s'),
        ('concurrency','reconstructed_system_tps','Full-run system TPS vs. concurrency','Concurrency','Successful output tokens/s'),
        ('concurrency','ttft_p95_ms','Time to first token P95 vs. concurrency','Concurrency','TTFT P95 (ms)'),
        ('concurrency','full_run_tps_per_concurrency','Full-run TPS per concurrency vs. concurrency','Concurrency','Full-run TPS / concurrency')])
    fields = [('Run date','run_date'),('Concurrency','concurrency'),('Completion','outcome'),('Evidence','evidence_status'),
              ('GuideLLM native output tokens/s (audit)','native_system_tps'),('Full-run successful output tokens/s','reconstructed_system_tps'),
              ('Native timing consistency','native_timing_status'),('Full-run record check','full_run_status'),
              ('Full request window (s)','full_run_duration_seconds'),('Successful output tokens','full_run_successful_output_tokens'),
              ('Full-run TPS / concurrency','full_run_tps_per_concurrency'),
              ('Native TPS / concurrency (audit)','aggregate_tps_per_concurrency'),('TTFT P95 (ms)','ttft_p95_ms'),
              ('Total requests','total_requests'),('Successful requests','successful_requests'),('Errored requests','errored_requests'),
              ('Incomplete requests','incomplete_requests'),('Failed + incomplete','failed_requests'),('Native measurement duration (s)','duration_seconds')]
    attempt_numbers = {jid: index+1 for index, jid in enumerate(group['attempts'])}
    multiple_attempts = len(attempt_numbers) > 1
    table_identity = ' · '.join(
        (f'Attempt {attempt_numbers[jid]}: Job {jid}' if multiple_attempts else f'Job {jid}')
        for jid in group['attempts'])
    head = '<tr><th scope="col">Field</th>' + ''.join(
        '<th scope="col">' + (f'Concurrency {esc(p["concurrency"])}' if p['concurrency'] is not None else 'Concurrency not recorded')
        + (f'<br><span class="note">Attempt {attempt_numbers[p["job_id"]]}</span>' if multiple_attempts else '')
        + '</th>' for p in points) + '</tr>'
    rows = ''.join('<tr><th scope="row">'+esc(label)+'</th>'+''.join('<td>'+(badge(p[key]) if key == 'outcome' else esc(p[key]))+'</td>' for p in points)+'</tr>' for label,key in fields)
    for name,label in [('ttft','Time to first token'),('tpot','Time per output token'),('latency','Query latency')]:
        for pct in ('min','mean','p50','p90','p95','p99','p999','max'):
            rows += '<tr><th>'+esc(f'{label} {pct} (ms)')+'</th>'+''.join('<td>'+esc(p['latency_ms'][name].get(pct))+'</td>' for p in points)+'</tr>'
    warnings = ''.join(f'<details><summary>Job {p["job_id"]} · point {p["point_index"]}: {len(p["warnings"])} findings</summary><ul>'+''.join('<li>'+esc(w)+'</li>' for w in p['warnings'])+'</ul></details>' for p in points)
    cards = ''.join(metadata_card(label, value) for label,value in [
        ('Model and precision',dict(profile, checkpoint=c['quantization'])),('Workload — synthetic text, single turn',c['workload']),('System under test',c['system']),('Software and backend',c['software'])])
    attempt_controls = ''.join(f'<label><input type="checkbox" data-attempt="{jid}" checked> Job {jid}</label> ' for jid in group['attempts'])
    evidence_links = ' · '.join(f'<a href="evidence/job-{jid}.json">Job {jid} evidence</a>' for jid in group['attempts'])
    attempts = attempt_summaries(group)
    attempt_cards = ''
    for attempt in attempts:
        jid = attempt['job_id']
        counts = facts([('Run date', attempt['date']), ('Successful requests', attempt['successful_requests']),
                        ('Errored requests', attempt['errored_requests']), ('Incomplete requests', attempt['incomplete_requests']),
                        ('Load points', len(attempt['points'])),
                        ('Timing checks', f"{sum(p['full_run_status']=='reconciled' for p in attempt['points'])}/{len(attempt['points'])} full-run points reconciled; {sum(p['native_timing_status']=='inconsistent' for p in attempt['points'])} native window inconsistencies")])
        attempt_cards += f'<article class="card attempt-summary" id="job-{jid}"><h3>Job {jid} · {badge(attempt["outcome"])}</h3>{counts}<p class="note">Counts across retained load points · <a href="evidence/job-{jid}.json">Sanitized source evidence</a></p></article>'
    overview = facts([('Test profile', labels['workload']), ('Precision / quantization', labels['precision']),
                      ('Floating-point dtype', profile.get('dtype')), ('Host', labels['shape']),
                      ('Request target per load point', c['workload'].get('max_requests')),
                      ('Configuration', group['id'])])
    body = f'''<div class="hero"><div class="eyebrow">{esc(labels['workload'])} · Configuration report</div><h1>{esc(model)}</h1><div class="subtitle">{esc(labels['shape'])} · {esc(labels['precision'])}</div><p class="note">{esc(title)} · {len(group['attempts'])} attempt(s) · {len(points)} load point(s)</p></div><section class="card"><h2>Report details</h2>{overview}</section><div class="callout">GuideLLM measurements. Not an official MLPerf result or certification. Accuracy not measured. Throughput charts use successful output tokens divided by the full observed request window only when retained counts, tokens and timestamps reconcile. Native values remain in the audit table with their timing status. TPS / concurrency is a diagnostic ratio, not a measured per-request percentile.</div><section class="section"><h2>Performance curves</h2><div class="attempt-controls">{attempt_controls}</div><p class="note">Toggle attempts to inspect overlapping points. Green: complete; amber: incomplete; red: failed. Completion does not validate performance evidence.</p><div class="grid">{charts}</div></section><section class="section"><h2>Workload attempts</h2><div class="grid">{attempt_cards}</div></section><section class="section"><h2>System and benchmark configuration</h2><p class="note">Serving fields record the saved execution configuration; cache flags do not independently establish effective runtime defaults. Different configurations remain separate reports.</p><div class="grid">{cards}</div></section><section class="card"><h2>Run data</h2><p class="note">{esc(table_identity)}</p><div class="scroll" tabindex="0" role="region" aria-label="Run data table"><table class="run-table"><caption>Each column is one retained load point. Missing measurements are not inferred; retries remain separate.</caption><thead>{head}</thead><tbody>{rows}</tbody></table></div></section><section class="card"><h2>Evidence findings and source files</h2><p>{evidence_links}</p><div class="links"><a href="metrics.csv">Combined metrics CSV</a><a href="report.json">Presentation data JSON</a><a href="manifest.json">Source and file hashes</a></div>{warnings}</section><section class="card definitions"><h2>Measurement definitions and review notes</h2><p>Full-run output tokens/s is actual successful output tokens divided by latest request end minus earliest request start across successful, errored and incomplete requests. It includes fill, drain and scheduling gaps, and does not establish steady-state capacity or latency-qualified goodput. Incomplete or sampled evidence withholds this rate. GuideLLM native rates are preserved, even when inconsistent, for audit and are not used in throughput charts. Tokens/s per concurrency is aggregate full-run output TPS divided by configured concurrency; it is not an individual request percentile. TTFT and TPOT use native milliseconds; query latency is converted from seconds to milliseconds. GuideLLM TPOT semantics are retained. Missing values are not inferred. Retried attempts remain separate columns. Findings identify missing evidence and detected inconsistencies; timing consistency does not certify model accuracy or kernel execution.</p><p>Complete means the source job succeeded and every retained load point reached its request target with reconciled counts and no errors or incomplete requests. Request counts on attempt cards sum retained points. No joint latency SLO goodput, accuracy, model quality or production cost was measured. Unequal host, cache, batching or workload settings prevent a controlled host-only or precision-only ranking.</p><p>AMX capability, ZenDNN installation and backend selection do not prove kernel execution. Unknown host resources and model revisions remain unverified.</p></section>'''
    script = '''<script>document.querySelectorAll('[data-attempt]').forEach(input=>input.addEventListener('change',()=>{document.querySelectorAll('circle[data-job]').forEach(point=>{if(point.dataset.job===input.dataset.attempt)point.style.display=input.checked?'':'none'});document.querySelectorAll('.chart-readout').forEach(p=>p.textContent='Hover or focus a point for measurements.')}));document.querySelectorAll('circle[data-job]').forEach(point=>{const show=()=>point.closest('.chart').querySelector('.chart-readout').textContent=point.getAttribute('aria-label');point.addEventListener('mouseenter',show);point.addEventListener('focus',show)});</script>'''

    return document(model, body, script)


def render_index(report):
    records = [(g, configuration_labels(g['configuration']), a) for g in report['groups'] for a in attempt_summaries(g)]
    stats = [(len(records), 'Workload attempts'), (sum(len(g['points']) for g in report['groups']), 'Load points')]
    stats += [(sum(a['outcome'] == status for _, _, a in records), status.capitalize() + ' attempts') for status in ('complete', 'failed', 'incomplete')]
    summary = '<div class="stats">' + ''.join(f'<div><strong>{count}</strong><span>{label}</span></div>' for count, label in stats) + '</div>'
    filters = '<label>Search<input id="filter" type="search" placeholder="Model, host or software"></label>'
    for key, label, all_label in [('model','Model','All models'), ('shape','Host','All hosts'), ('precision','Precision','All precisions'), ('workload','Test profile','All test profiles'), ('outcome','Completion','All outcomes'), ('date','Run date','All dates')]:
        values = sorted({str(labels[key] if key in labels else attempt[key]) for _, labels, attempt in records})
        options = ''.join(f'<option value="{esc(value)}">{esc(value.upper() if key == "outcome" else value)}</option>' for value in values)
        filters += f'<label>{label}<select class="filter" data-key="{key}" aria-label="Filter {label.lower()}"><option value="">{all_label}</option>{options}</select></label>'
    cards = ''
    for group, labels, attempt in records:
        attrs = dict(labels, outcome=attempt['outcome'], date=attempt['date'])
        attributes = ' '.join(f'data-{key}="{esc(value)}"' for key, value in attrs.items())
        system = group['configuration']['system']
        host = f"{labels['shape']} · {fmt(system.get('ocpus'))} OCPU · {fmt(system.get('memory_gb'))} GiB"
        counts = ''.join(f'<span>{esc(attempt[key])} {label}</span>' for key, label in [('successful_requests','successful'), ('errored_requests','errors'), ('incomplete_requests','incomplete')])
        cards += f'''<a class="report-link report-card" {attributes} data-search="{esc(canonical(group['configuration']).lower())}" href="{group['id']}.html#job-{attempt['job_id']}"><div class="eyebrow">{esc(labels['workload'])} · {badge(attempt['outcome'])}</div><h3>{esc(labels['model'])}</h3><div class="note">{esc(host)}</div><div class="note">{esc(labels['precision'])} · {esc(attempt['date'])}</div><div class="counts">{counts}</div><p class="note">Job {attempt['job_id']} · {len(attempt['points'])} load point(s) · Configuration {group['id']}</p><span class="badge">{sum(bool(p["warnings"]) for p in attempt["points"])} point(s) with findings</span></a>'''
    body = f'''<div class="hero"><div class="eyebrow">Report #{report['id']} · {len(report['groups'])} configuration(s)</div><h1>{esc(report['title'])}</h1><div class="subtitle">Model, host, precision and test-profile benchmark results</div>{summary}</div><div class="callout">MLCommons-style presentation of GuideLLM results. Not an official MLPerf submission or certification. Completion and evidence validity are assessed separately. Throughput uses reconciled full-run request records; missing or sampled records withhold the rate. Native values are retained for audit. Unequal configurations do not establish a controlled host or precision ranking.</div><div class="filters" role="search" aria-label="Filter benchmark attempts">{filters}<button id="reset" type="button">Reset filters</button></div><p id="filterCount" class="note" role="status" aria-live="polite">{len(records)} of {len(records)} attempts</p><div class="report-grid">{cards}</div><p id="empty" class="card empty" hidden>No attempts match these filters. Reset filters to see all results.</p><section class="card definitions section"><h2>Coverage and preserved evidence</h2><p>Each card represents one workload attempt. Matching configurations share a detail page; retries retain separate columns and chart points. Request counts sum retained load points within an attempt; missing counts remain Not measured. Completed means the configured target was reached without errors or incomplete requests. Accuracy and kernel execution are not established by completion.</p><div class="links"><a href="metrics.csv">Combined measurements (CSV)</a><a href="report.json">All report metadata</a><a href="manifest.json">File hashes and evidence policy</a></div></section>'''
    script = '''<script>(()=>{const query=document.getElementById('filter'),filters=[...document.querySelectorAll('select.filter')],cards=[...document.querySelectorAll('.report-card')];function apply(){let n=0;const q=query.value.trim().toLowerCase();for(const card of cards){card.hidden=!(card.dataset.search.includes(q)&&filters.every(f=>!f.value||card.dataset[f.dataset.key]===f.value));if(!card.hidden)n++}document.getElementById('filterCount').textContent=n+' of '+cards.length+' attempts';document.getElementById('empty').hidden=n!==0}query.addEventListener('input',apply);filters.forEach(f=>f.addEventListener('change',apply));document.getElementById('reset').addEventListener('click',()=>{query.value='';filters.forEach(f=>f.value='');apply()});apply()})();</script>'''
    return document(report['title'], body, script)


def build_bundle(snapshot, report_id, destination, progress=lambda stage, percent: None):
    root = Path(destination); root.mkdir(parents=True, exist_ok=False)
    progress('validating', 15)
    groups = groups_from_snapshot(snapshot)
    progress('calculating', 40)
    report = dict(id=report_id, version=REPORT_VERSION, title=snapshot['title'], groups=groups,
                  evidence_status='warning' if any(p['warnings'] for g in groups for p in g['points']) else 'checked', methodology='full-request-window-v1')
    (root/'report.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    evidence = root/'evidence'; evidence.mkdir()
    for source in snapshot['sources']:
        (evidence/f'job-{source["job_id"]}.json').write_text(json.dumps(source, indent=2, allow_nan=False))
    columns = ['configuration','job_id','point_index','run_date','concurrency','outcome','evidence_status',
               'total_requests','successful_requests','errored_requests','incomplete_requests','failed_requests',
               'native_system_tps','reconstructed_system_tps','aggregate_tps_per_concurrency','ttft_p95_ms','duration_seconds',
               'native_timing_status','full_run_status','full_run_duration_seconds','full_run_successful_output_tokens','full_run_tps_per_concurrency','warnings']
    with (root/'metrics.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns); writer.writeheader()
        for group in groups:
            for p in group['points']:
                writer.writerow({k: (group['id'] if k == 'configuration' else '; '.join(p[k]) if k == 'warnings' else p.get(k)) for k in columns})
    progress('rendering', 65)
    (root/'index.html').write_text(render_index(report))
    for group in groups:
        (root/f'{group["id"]}.html').write_text(render_detail(group, report['title']))
    manifest = dict(pipeline_version=REPORT_VERSION, snapshot_sha256=digest(snapshot), evidence_policy='Sanitized structural evidence; credentials and textual prompts/responses excluded',
                    files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()})
    (root/'manifest.json').write_text(json.dumps(manifest, indent=2))
    progress('packaging', 90)
    with zipfile.ZipFile(root/'report.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for p in sorted(root.rglob('*')):
            if p.is_file() and p.name != 'report.zip':
                archive.write(p, str(p.relative_to(root)))
    return dict(configurations=len(groups), attempts=len(snapshot['sources']), points=sum(len(g['points']) for g in groups), evidence_status=report['evidence_status'])
