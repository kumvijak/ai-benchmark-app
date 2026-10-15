from __future__ import annotations

import base64
import csv
import io
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuideLLMBenchmarkSpec:
    model_ref: str
    port: int
    workload_name: str
    input_tokens: int
    output_tokens: int
    concurrencies: list[int]
    max_requests: int
    request_timeout_seconds: int
    max_errors: int | None = 5
    mode: str = "concurrent"
    max_duration_seconds: int = 600
    cooldown_seconds: int = 5
    seed: int = 42
    sweep_size: int = 10
    sweep_strategy_type: str = "poisson"
    runtime_evidence: dict[str, Any] | None = None


GUIDELLM_REMOTE_SCRIPT = r"""
import base64, importlib.metadata, json, os, subprocess, sys, time

spec = json.loads(sys.stdin.read())
job_id = spec["job_id"]
job_dir = f"/tmp/aig-benchmark-control/guidellm-{job_id}"
os.makedirs(job_dir, exist_ok=True)

backend = {
    "kind": "openai_http",
    "target": f"http://127.0.0.1:{spec['port']}",
    "model": spec["model_ref"],
    "request_format": "/v1/completions",
    "stream": True,
    "timeout": spec["request_timeout_seconds"],
    "extras": {"body": {"temperature": 0}},
}
data = {
    "kind": "synthetic_text",
    "prompt_tokens": spec["input_tokens"],
    "output_tokens": spec["output_tokens"],
}

if spec["mode"] == "sweep":
    profile = {
        "kind": "sweep",
        "sweep_size": spec["sweep_size"],
        "rampup_duration": 10,
        "strategy_type": spec["sweep_strategy_type"],
        "max_concurrency": max(spec["concurrencies"]),
        "cooldown": {"mode": "duration", "value": spec["cooldown_seconds"]},
    }
else:
    profile = {
        "kind": "concurrent",
        "streams": spec["concurrencies"],
        "cooldown": {"mode": "duration", "value": spec["cooldown_seconds"]},
    }

json_path = f"{job_dir}/benchmarks.json"
csv_path = f"{job_dir}/benchmarks.csv"
html_path = f"{job_dir}/benchmarks.html"
log_path = f"{job_dir}/guidellm.log"

args = [
    "guidellm", "run",
    "--backend", json.dumps(backend, separators=(",", ":")),
    "--data", json.dumps(data, separators=(",", ":")),
    "--profile", json.dumps(profile, separators=(",", ":")),
    "--constraint", f"kind=max_requests,count={spec['max_requests']}",
    "--constraint", f"kind=max_duration,seconds={spec['max_duration_seconds']}",
    "--seed", f"kind=static,value={spec['seed']}",
    "--output", f"kind=json,path={json_path}",
    "--output", f"kind=csv,path={csv_path}",
    "--output", f"kind=html,path={html_path}",
    "--label", f"workload={spec['workload_name']}",
    "--label", f"benchmark_mode={spec['mode']}",
]
for key in ('backend', 'cpu_vendor', 'amx_eligible', 'amx_runtime_supported', 'amx_policy', 'vllm_version', 'torch_version', 'zentorch_version', 'zendnn_version', 'platform'):
    value = (spec.get('runtime_evidence') or {}).get(key)
    if value is not None:
        args += ['--label', f'{key}={value}']
if spec.get('max_errors') is not None:
    args += ['--constraint', f"kind=max_errors,count={spec['max_errors']}"]
if spec["mode"] == "sweep":
    args += ["--constraint", "kind=over_saturation,mode=enforce"]

env = os.environ.copy()
home = os.path.expanduser("~")
env["PATH"] = f"{home}/.aig-vllm/bin:{home}/.local/bin:" + env.get("PATH", "")
env["GUIDELLM__DEFAULT_RESULTS_DIR"] = job_dir
env["GUIDELLM__LOGGING__LOG_FILE"] = log_path
env["GUIDELLM__LOGGING__LOG_FILE_LEVEL"] = "INFO"

started = time.time()
proc = subprocess.run(args, text=True, capture_output=True, env=env)
duration = time.time() - started

artifacts = {}
for name, path, binary in [
    ("json", json_path, False),
    ("csv", csv_path, False),
    ("html", html_path, False),
    ("log", log_path, False),
]:
    if os.path.exists(path):
        with open(path, "rb") as fh:
            raw = fh.read()
        artifacts[name] = {
            "path": path,
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "bytes": len(raw),
        }

payload = {
    "returncode": proc.returncode,
    "guidellm_version": importlib.metadata.version("guidellm"),
    "duration_seconds": duration,
    "command": args,
    "job_dir": job_dir,
    "stdout": proc.stdout[-20000:],
    "stderr": proc.stderr[-20000:],
    "artifacts": artifacts,
    "spec": spec,
    "backend": backend,
    "data": data,
    "profile": profile,
}
print(json.dumps(payload))
raise SystemExit(proc.returncode)
"""


def remote_guidellm_command(spec: GuideLLMBenchmarkSpec, job_id: int) -> str:
    payload = guide_spec_payload(spec, job_id)
    encoded_script = base64.b64encode(GUIDELLM_REMOTE_SCRIPT.encode("utf-8")).decode("ascii")
    return (
        "python3 - <<'PY'\n"
        "import base64, subprocess, sys\n"
        f"script = base64.b64decode({encoded_script!r}).decode('utf-8')\n"
        f"payload = {json.dumps(payload, separators=(',', ':'))!r}\n"
        "proc = subprocess.run([sys.executable, '-c', script], input=payload, text=True)\n"
        "raise SystemExit(proc.returncode)\n"
        "PY"
    )


def guide_spec_payload(spec: GuideLLMBenchmarkSpec, job_id: int) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "model_ref": spec.model_ref,
        "port": spec.port,
        "workload_name": spec.workload_name,
        "input_tokens": spec.input_tokens,
        "output_tokens": spec.output_tokens,
        "concurrencies": spec.concurrencies,
        "max_requests": spec.max_requests,
        "max_errors": spec.max_errors,
        "request_timeout_seconds": spec.request_timeout_seconds,
        "mode": spec.mode,
        "max_duration_seconds": spec.max_duration_seconds,
        "cooldown_seconds": spec.cooldown_seconds,
        "seed": spec.seed,
        "sweep_size": spec.sweep_size,
        "sweep_strategy_type": spec.sweep_strategy_type,
        "runtime_evidence": spec.runtime_evidence,
    }


def decode_guidellm_artifacts(result: dict[str, Any]) -> dict[str, str]:
    decoded: dict[str, str] = {}
    for name, artifact in (result.get("artifacts") or {}).items():
        content = artifact.get("content")
        if isinstance(content, str):
            decoded[name] = content
            continue
        encoded = artifact.get("content_b64")
        if isinstance(encoded, str):
            decoded[name] = base64.b64decode(encoded).decode("utf-8", "replace")
    return decoded


def report_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(result.get("normalized_rows"), list):
        return result["normalized_rows"]
    artifacts = decode_guidellm_artifacts(result)
    raw = result.get("guidellm_json")
    if raw is None and artifacts.get("json"):
        try:
            raw = json.loads(artifacts["json"])
        except json.JSONDecodeError:
            raw = None
    rows = _rows_from_json(raw, result.get("spec") or {})
    if rows:
        return rows
    if artifacts.get("csv"):
        return _rows_from_csv(artifacts["csv"])
    if not result.get("artifacts") and not result.get("guidellm_json") and any(key in result for key in ("requests", "concurrency", "workload_name")):
        return [_compact_row(result)]
    return []


def report_markdown(result: dict[str, Any]) -> str:
    rows = report_rows(result)
    columns = _report_columns(rows)
    if not columns:
        return "No GuideLLM report rows were found."
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = ["| " + " | ".join(_fmt(row.get(col)) for col in columns) + " |" for row in rows]
    table = "\n".join([header, sep, *body])
    evidence = result.get('runtime_evidence')
    if not evidence:
        return table
    summary = (f"Backend: {evidence['backend']} | Platform: {evidence['platform']} | "
               f"Intel AMX runtime support: {'available' if evidence.get('amx_runtime_supported') else 'unavailable'} | "
               f"AMX selection: automatic | "
               f"vLLM: {evidence['vllm_version']} | PyTorch: {evidence['torch_version']}")
    if evidence.get('zendnn_version'):
        summary += f" | ZenTorch: {evidence['zentorch_version']} | ZenDNN: {evidence['zendnn_version']}"
    return summary + "\n\n" + table


def report_csv(result: dict[str, Any]) -> str:
    artifacts = decode_guidellm_artifacts(result)
    if artifacts.get("csv"):
        return artifacts["csv"]
    rows = report_rows(result)
    columns = _report_columns(rows)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _fmt(row.get(column)) for column in columns})
    return output.getvalue()


def report_html(result: dict[str, Any]) -> str:
    artifacts = decode_guidellm_artifacts(result)
    return artifacts.get("html") or "<p>No GuideLLM HTML report was generated.</p>"


def normalize_guidellm_result(envelope: dict[str, Any]) -> dict[str, Any]:
    artifacts = decode_guidellm_artifacts(envelope)
    guidellm_json = envelope.get("guidellm_json")
    if artifacts.get("json"):
        try:
            guidellm_json = json.loads(artifacts["json"])
        except json.JSONDecodeError:
            guidellm_json = None
    normalized_rows = report_rows({**envelope, "guidellm_json": guidellm_json})
    result = dict(envelope)
    result["benchmark_command_kind"] = "guidellm"
    result["guidellm_json"] = guidellm_json
    result["normalized_rows"] = normalized_rows
    for artifact in (result.get("artifacts") or {}).values():
        artifact.pop("content", None)
    return result


def _rows_from_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    rows = [dict(row) for row in reader]
    return [_compact_row(row) for row in rows]


def _rows_from_json(raw: Any, spec: dict[str, Any]) -> list[dict[str, Any]]:
    guidellm_rows = _guidellm_benchmark_rows(raw, spec)
    if guidellm_rows:
        return guidellm_rows
    found: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            keys = {str(key).lower() for key in value}
            if keys & {"concurrency", "streams", "rate"} and keys & {"requests", "successful_requests", "success_requests", "completed_requests"}:
                found.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(raw)
    if found:
        return [_compact_row(row, spec) for row in found]
    if spec:
        return [
            {
                "workload_name": spec.get("workload_name"),
                "profile": spec.get("mode"),
                "prompt_tokens": spec.get("input_tokens"),
                "output_tokens": spec.get("output_tokens"),
                "requests": spec.get("max_requests"),
                "concurrency": ",".join(str(v) for v in spec.get("concurrencies", [])),
            }
        ]
    return []


def _guidellm_benchmark_rows(raw: Any, spec: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("benchmarks"), list):
        return []
    labels = (((raw.get("config") or {}).get("metadata") or {}).get("labels") or {})
    spec_config = ((raw.get("config") or {}).get("spec") or {})
    data = spec_config.get("data") or []
    data0 = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    rows = []
    for benchmark in raw["benchmarks"]:
        if not isinstance(benchmark, dict):
            continue
        config = benchmark.get("config") or {}
        strategy = config.get("strategy") or {}
        metrics = benchmark.get("metrics") or {}
        totals = metrics.get("request_totals") or {}

        def percentile(metric: str, pct: str) -> Any:
            return ((((metrics.get(metric) or {}).get("successful") or {}).get("percentiles") or {}).get(pct))

        def mean(metric: str) -> Any:
            return ((metrics.get(metric) or {}).get("successful") or {}).get("mean")

        streams = strategy.get("streams")
        row = {
            "workload_name": labels.get("workload") or spec.get("workload_name"),
            "profile": strategy.get("type_") or labels.get("benchmark_mode") or spec.get("mode"),
            "concurrency": streams or strategy.get("max_concurrency") or strategy.get("rate"),
            "requests": totals.get("total"),
            "success_requests": totals.get("successful"),
            "failed_requests": (totals.get("errored") or 0) + (totals.get("incomplete") or 0),
            "request_rate": mean("requests_per_second"),
            "prompt_tokens": mean("prompt_token_count") or data0.get("prompt_tokens") or spec.get("input_tokens"),
            "output_tokens": mean("output_token_count") or data0.get("output_tokens") or spec.get("output_tokens"),
            "ttft_ms_p50": percentile("time_to_first_token_ms", "p50"),
            "ttft_ms_p95": percentile("time_to_first_token_ms", "p95"),
            "itl_ms_p50": percentile("inter_token_latency_ms", "p50"),
            "itl_ms_p95": percentile("inter_token_latency_ms", "p95"),
            "tpot_ms_p50": percentile("time_per_output_token_ms", "p50"),
            "e2e_ms_p50": (percentile("request_latency", "p50") * 1000 if percentile("request_latency", "p50") is not None else None),
            "e2e_ms_p95": (percentile("request_latency", "p95") * 1000 if percentile("request_latency", "p95") is not None else None),
            "output_tokens_per_second": mean("output_tokens_per_second"),
            "total_tokens_per_second": mean("tokens_per_second"),
            "duration_seconds": benchmark.get("duration"),
        }
        rows.append({key: value for key, value in row.items() if value not in (None, "")})
    return rows


def _compact_row(row: dict[str, Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = spec or {}
    lower = {str(k).lower(): v for k, v in row.items()}

    def pick(*names: str) -> Any:
        for name in names:
            if name in row:
                return row[name]
            if name.lower() in lower:
                return lower[name.lower()]
        return None

    compact = {
        "workload_name": pick("workload_name", "workload") or spec.get("workload_name"),
        "profile": pick("profile", "profile_kind", "strategy", "strategy_kind") or spec.get("mode"),
        "concurrency": pick("concurrency", "streams", "max_concurrency", "rate"),
        "requests": pick("requests", "total_requests", "request_count", "num_requests"),
        "success_requests": pick("success_requests", "successful_requests", "completed_requests"),
        "failed_requests": pick("failed_requests", "errored_requests", "error_count", "errors"),
        "request_rate": pick("request_rate", "achieved_rps", "request_throughput", "requests_per_second"),
        "ttft_ms_p50": pick("ttft_ms_p50", "ttft_p50_ms", "time_to_first_token_p50_ms"),
        "ttft_ms_p95": pick("ttft_ms_p95", "ttft_p95_ms", "time_to_first_token_p95_ms"),
        "itl_ms_p50": pick("itl_ms_p50", "itl_p50_ms", "inter_token_latency_p50_ms"),
        "itl_ms_p95": pick("itl_ms_p95", "itl_p95_ms", "inter_token_latency_p95_ms"),
        "tpot_ms_p50": pick("tpot_ms_p50", "tpot_p50_ms", "time_per_output_token_p50_ms"),
        "e2e_ms_p50": pick("e2e_ms_p50", "latency_p50_ms", "request_latency_p50_ms"),
        "e2e_ms_p95": pick("e2e_ms_p95", "latency_p95_ms", "request_latency_p95_ms"),
        "output_tokens_per_second": pick("output_tokens_per_second", "output_token_throughput", "decode_throughput"),
        "total_tokens_per_second": pick("total_tokens_per_second", "total_token_throughput"),
        "duration_seconds": pick("duration_seconds", "duration", "elapsed_seconds"),
    }
    extras = {k: v for k, v in row.items() if k not in compact and k.lower() not in {c.lower() for c in compact}}
    compact.update({k: v for k, v in extras.items() if k not in compact})
    return {k: v for k, v in compact.items() if v not in (None, "")}


def _report_columns(rows: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "workload_name", "profile", "concurrency", "requests", "success_requests", "failed_requests",
        "request_rate", "ttft_ms_p50", "ttft_ms_p95", "itl_ms_p50", "itl_ms_p95",
        "tpot_ms_p50", "e2e_ms_p50", "e2e_ms_p95", "output_tokens_per_second",
        "total_tokens_per_second", "duration_seconds",
    ]
    present = {key for row in rows for key in row}
    columns = list(preferred)
    columns.extend(sorted(present - set(columns)))
    return columns


def _fmt(value: object) -> str:
    if value is None or value == "":
        return "N/C"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)
