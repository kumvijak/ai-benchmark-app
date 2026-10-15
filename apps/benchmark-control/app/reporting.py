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

REPORT_VERSION = '1.0.0'
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
            warnings.append('Native timing window not independently validated; reconstructed throughput withheld')
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
                native_system_tps=native_tps, reconstructed_system_tps=None,
                aggregate_tps_per_concurrency=native_tps / concurrency if native_tps is not None and concurrency else None,
                ttft_p95_ms=number(latency['ttft'].get('p95', row.get('ttft_ms_p95'))),
                duration_seconds=number(benchmark.get('duration', row.get('duration_seconds'))),
                latency_ms=latency, warnings=warnings, evidence_status='warning')
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
        return f'<section class="card"><h3>{esc(title)}</h3><p>Not measured — no usable points.</p></section>'
    xmax = max([p[x_key] for p in valid] + [1]); ymax = max([p[y_key] for p in valid] + [1])
    def xy(p): return 68 + 430 * p[x_key] / xmax, 240 - 190 * p[y_key] / ymax
    circles = ''.join(f'<circle cx="{xy(p)[0]:.2f}" cy="{xy(p)[1]:.2f}" r="{7 if p["outcome"] == "complete" else 4}" data-job="{p["job_id"]}" class="{p["outcome"]}"><title>Job {p["job_id"]}, C{esc(p["concurrency"])}: {esc(p[y_key])}; {esc(p["outcome"])}; evidence warning</title></circle>' for p in valid)
    return f'''<section class="card"><h3>{esc(title)}</h3><svg viewBox="0 0 560 300" role="img" aria-label="{esc(title)}"><path d="M68 40V240H510" fill="none" stroke="#98a5b8"/><text x="68" y="260">0</text><text x="475" y="260">{esc(round(xmax,2))}</text><text x="5" y="50">{esc(round(ymax,2))}</text>{circles}<text x="280" y="292" text-anchor="middle">{esc(x_label)}</text><text x="68" y="20">{esc(y_label)}</text></svg><p class="muted">Measured points only. Hover for run details. Native rates; timing unvalidated.</p></section>'''


CSS = '''*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#17283f;font:15px/1.55 system-ui,sans-serif}main{max-width:1450px;margin:auto;padding:32px}h1{font-size:30px;margin:8px 0}h2{margin-top:0}h3{font-size:16px}a{color:#245f9f}.eyebrow{color:#42658d;text-transform:uppercase;letter-spacing:.12em;font-size:12px}.card{background:white;border:1px solid #dde3ec;border-radius:12px;padding:22px;margin:18px 0}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.grid .card{margin:0}.muted{color:#627187;font-size:13px}.notice{padding:14px;background:#fff3d6;border-left:4px solid #c68a17}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:10px 14px;border-bottom:1px solid #e0e5ed;vertical-align:top;min-width:135px}th{background:#f3f6fa}tbody th{position:sticky;left:0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}svg{width:100%;height:auto}svg text{font:11px system-ui;fill:#526780}circle{fill:#2760a0;stroke:white;stroke-width:2}circle.incomplete{fill:#c28b20}circle.failed{fill:#b5443a}button,input,select{font:inherit;padding:8px;border:1px solid #cbd5e1;border-radius:6px}nav{display:flex;gap:15px;flex-wrap:wrap}.tag{display:inline-block;background:#eaf0f8;padding:4px 9px;border-radius:5px} @media(max-width:800px){main{padding:15px}.grid{grid-template-columns:1fr}}@media print{.card{break-inside:avoid}input,select{display:none}}'''


def document(title, body, script=''):
    return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title><style>{CSS}</style></head><body><main>{body}</main>{script}</body></html>'


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
    model = profile.get('model_ref') or profile.get('source_model') or c['workload'].get('model_ref') or 'Model not recorded'
    charts = ''.join(chart(points, *args) for args in [
        ('aggregate_tps_per_concurrency','native_system_tps','System TPS vs. tokens/s per concurrency','Aggregate TPS / concurrency','Native output tokens/s'),
        ('concurrency','native_system_tps','System TPS vs. concurrency','Concurrency','Native output tokens/s'),
        ('concurrency','ttft_p95_ms','Time to first token P95 vs. concurrency','Concurrency','TTFT P95 (ms)'),
        ('concurrency','aggregate_tps_per_concurrency','Tokens/s per concurrency vs. concurrency','Concurrency','Aggregate TPS / concurrency')])
    fields = [('Run date','run_date'),('Concurrency','concurrency'),('Completion','outcome'),('Evidence','evidence_status'),
              ('Native system tokens/s','native_system_tps'),('Reconstructed system tokens/s','reconstructed_system_tps'),
              ('Aggregate TPS / concurrency','aggregate_tps_per_concurrency'),('TTFT P95 (ms)','ttft_p95_ms'),
              ('Total requests','total_requests'),('Successful requests','successful_requests'),('Errored requests','errored_requests'),
              ('Incomplete requests','incomplete_requests'),('Failed + incomplete','failed_requests'),('Run duration (s)','duration_seconds')]
    head = '<tr><th>Field</th>' + ''.join(f'<th>Job {p["job_id"]} · point {p["point_index"]}</th>' for p in points) + '</tr>'
    rows = ''.join('<tr><th>'+esc(label)+'</th>'+''.join('<td>'+esc(p[key])+'</td>' for p in points)+'</tr>' for label,key in fields)
    for name,label in [('ttft','Time to first token'),('tpot','Time per output token'),('latency','Query latency')]:
        for pct in ('min','mean','p50','p90','p95','p99','p999','max'):
            rows += '<tr><th>'+esc(f'{label} {pct} (ms)')+'</th>'+''.join('<td>'+esc(p['latency_ms'][name].get(pct))+'</td>' for p in points)+'</tr>'
    warnings = ''.join(f'<details><summary>Job {p["job_id"]} · point {p["point_index"]}: {len(p["warnings"])} findings</summary><ul>'+''.join('<li>'+esc(w)+'</li>' for w in p['warnings'])+'</ul></details>' for p in points)
    cards = ''.join(metadata_card(label, value) for label,value in [
        ('Model and precision',dict(profile, checkpoint=c['quantization'])),('Workload — synthetic text, single turn',c['workload']),('System under test',c['system']),('Software and backend',c['software'])])
    attempt_controls = ''.join(f'<label><input type="checkbox" data-attempt="{jid}" checked> Job {jid}</label> ' for jid in group['attempts'])
    evidence_links = ' · '.join(f'<a href="evidence/job-{jid}.json">Job {jid} evidence</a>' for jid in group['attempts'])
    body = f'''<nav><a href="index.html">← Report catalogue</a><a href="metrics.csv">CSV</a><a href="report.json">JSON</a><a href="manifest.json">Evidence manifest</a></nav><p class="eyebrow">CPU inference · MLCommons-style presentation</p><h1>{esc(model)}</h1><p>{esc(title)} · Configuration {group['id']} · {len(group['attempts'])} attempt(s)</p><p class="notice">GuideLLM measurements. Not an official MLPerf result or certification. Accuracy not measured. Native timing is unvalidated; reconstructed throughput is withheld.</p><section class="card"><h2>Attempts</h2>{attempt_controls}<p class="muted">Toggle attempts to inspect overlapping chart points. Blue: complete; amber: incomplete; red: failed.</p></section><div class="grid">{charts}</div><div class="grid">{cards}</div><section class="card"><h2>Run data</h2><div class="scroll"><table><thead>{head}</thead><tbody>{rows}</tbody></table></div></section><section class="card"><h2>Evidence findings</h2><p>{evidence_links}</p>{warnings}</section><section class="card"><h2>Methodology</h2><p>Native output tokens/s comes from GuideLLM summary metrics. Tokens/s per concurrency is aggregate native output TPS divided by configured concurrency; it is not an individual request percentile. TTFT and TPOT use native milliseconds; query latency is converted from seconds to milliseconds. GuideLLM TPOT semantics are retained. Missing values are not inferred. Retried attempts remain separate columns. Blue: completed target without errors; amber: incomplete; red: failed. All points carry evidence warnings.</p><p>AMX capability/backend selection does not prove AMX kernel execution. Unknown host resources and model revisions remain unverified.</p></section>'''
    script = '''<script>document.querySelectorAll('[data-attempt]').forEach(input=>input.addEventListener('change',()=>{document.querySelectorAll('circle[data-job]').forEach(point=>{if(point.dataset.job===input.dataset.attempt)point.style.display=input.checked?'':'none'})}));</script>'''
    return document(model, body, script)


def render_index(report):
    cards = ''
    for g in report['groups']:
        c = g['configuration']; p = c['model']
        label = p.get('model_ref') or p.get('source_model') or c['workload'].get('model_ref') or 'Model not recorded'
        description = f"{label} · {p.get('quantization', 'unverified')} · {c['workload'].get('workload_name', 'Workload not recorded')}"
        cards += f'<article class="card report-card" data-search="{esc(canonical(c).lower())}"><h2><a href="{g["id"]}.html">{esc(description)}</a></h2><p>{len(g["attempts"])} attempt(s) · {len(g["points"])} load point(s) · evidence warnings</p><p class="muted">Configuration {g["id"]} · Jobs {esc(g["attempts"])}</p></article>'
    body = f'''<p class="eyebrow">CPU inference reports</p><h1>{esc(report['title'])}</h1><p>Report #{report['id']} · {len(report['groups'])} configuration(s) · Pipeline {REPORT_VERSION}</p><p class="notice">MLCommons-style presentation of GuideLLM results. Not an official MLPerf submission. Completion and evidence validity are assessed separately.</p><nav><a href="metrics.csv">Download metrics CSV</a><a href="report.json">Report JSON</a><a href="manifest.json">Evidence manifest</a></nav><section class="card"><label>Filter by model, precision, workload, host or software <input id="filter" type="search" placeholder="Search configurations"></label><p id="count" aria-live="polite"></p></section>{cards}'''
    script = '''<script>const q=document.getElementById('filter');q.addEventListener('input',()=>{let n=0;document.querySelectorAll('.report-card').forEach(c=>{c.hidden=!c.dataset.search.includes(q.value.toLowerCase());if(!c.hidden)n++});document.getElementById('count').textContent=n+' configuration(s)'});</script>'''
    return document(report['title'], body, script)


def build_bundle(snapshot, report_id, destination, progress=lambda stage, percent: None):
    root = Path(destination); root.mkdir(parents=True, exist_ok=False)
    progress('validating', 15)
    groups = groups_from_snapshot(snapshot)
    progress('calculating', 40)
    report = dict(id=report_id, version=REPORT_VERSION, title=snapshot['title'], groups=groups,
                  evidence_status='warning', methodology='native-guidellm-unvalidated-timing')
    (root/'report.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    evidence = root/'evidence'; evidence.mkdir()
    for source in snapshot['sources']:
        (evidence/f'job-{source["job_id"]}.json').write_text(json.dumps(source, indent=2, allow_nan=False))
    columns = ['configuration','job_id','point_index','run_date','concurrency','outcome','evidence_status',
               'total_requests','successful_requests','errored_requests','incomplete_requests','failed_requests',
               'native_system_tps','reconstructed_system_tps','aggregate_tps_per_concurrency','ttft_p95_ms','duration_seconds','warnings']
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
    return dict(configurations=len(groups), attempts=len(snapshot['sources']), points=sum(len(g['points']) for g in groups), evidence_status='warning')
