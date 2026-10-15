from __future__ import annotations

import base64
import shlex
import json
from dataclasses import asdict, dataclass

from .runtime import build_runtime_check_script, build_runtime_launcher


@dataclass(frozen=True)
class VllmProfile:
    model_ref: str
    dtype: str
    max_model_len: int
    max_num_batched_tokens: int | None
    max_num_seqs: int
    cpu_kvcache_space_gib: int | None
    reserved_cpu: int
    prefix_cache_enabled: bool
    chunked_prefill_enabled: bool
    request_timeout_seconds: int
    port: int
    quantization: str = 'none'
    model_revision: str | None = None


def build_vllm_command(profile: VllmProfile, checkpoint_path: str | None = None, launch_script: str | None = None) -> str:
    if profile.quantization not in {'none', 'int8', 'int4'}:
        raise ValueError('Unsupported CPU quantization mode')
    if profile.quantization != 'none' and not checkpoint_path:
        raise ValueError('Quantized runtime requires a verified compressed checkpoint')
    args = [
        "vllm",
        "serve",
        checkpoint_path or profile.model_ref,
        "--dtype",
        profile.dtype,
        "--max-model-len",
        str(profile.max_model_len),
        "--max-num-seqs",
        str(profile.max_num_seqs),
        "--host",
        "0.0.0.0",
        "--port",
        str(profile.port),
    ]
    if launch_script:
        args[:1] = ['python', launch_script]
    if profile.quantization != 'none':
        args.extend(['--quantization', 'compressed-tensors', '--served-model-name', profile.model_ref])
    elif profile.model_revision:
        args.extend(['--revision', profile.model_revision])
    if profile.max_num_batched_tokens is not None:
        args.extend(["--max-num-batched-tokens", str(profile.max_num_batched_tokens)])
    if profile.prefix_cache_enabled:
        args.append("--enable-prefix-caching")
    if profile.chunked_prefill_enabled:
        args.append("--enable-chunked-prefill")
    env = {
        "PATH": "$HOME/.aig-vllm/bin:$HOME/.local/bin:$PATH",
        "VLLM_TARGET_DEVICE": "cpu",
        "TORCHINDUCTOR_FREEZING": "1",
        "VLLM_USE_AOT_COMPILE": "0",
        "TORCHINDUCTOR_AUTOGRAD_CACHE": "0",
        "VLLM_CPU_NUM_OF_RESERVED_CPU": str(profile.reserved_cpu),
    }
    if profile.cpu_kvcache_space_gib is not None:
        env['VLLM_CPU_KVCACHE_SPACE'] = str(profile.cpu_kvcache_space_gib)
    if profile.quantization == 'int4':
        env['VLLM_CPU_INT4_W4A8'] = '0'
    exports = " ".join(f"{k}={v if k == 'PATH' else shlex.quote(v)}" for k, v in env.items())
    quoted = " ".join(shlex.quote(str(a)) for a in args)
    return f"{exports} {quoted}"


def build_start_script(profile: VllmProfile, checkpoint_path: str | None = None, quantization_evidence: dict | None = None) -> str:
    launch_path = f"/tmp/aig-benchmark-control/vllm-launch-{profile.port}.py"
    launcher = base64.b64encode(build_runtime_launcher().encode()).decode()
    command = build_vllm_command(profile, checkpoint_path, launch_path)
    quoted_command = shlex.quote(f"exec env {command}")
    return (
        "set -e\n"
        "export PATH=$HOME/.aig-vllm/bin:$HOME/.local/bin:$PATH\n" +
        "unset VLLM_CPU_SGL_KERNEL\n" +
        ("unset VLLM_CPU_KVCACHE_SPACE\n" if profile.cpu_kvcache_space_gib is None else "") +
        "command -v vllm >/dev/null || { echo 'vllm is not installed or not on PATH' >&2; exit 127; }\n"
        f"if ss -ltn 2>/dev/null | awk '{{print $4}}' | grep -Eq '(^|:){profile.port}$'; then "
        f"echo 'vLLM is already running on port {profile.port}; stop it before starting this profile' >&2; exit 98; fi\n"
        "mkdir -p /tmp/aig-benchmark-control\n" +
        build_runtime_check_script().replace(
            " - <<'VERIFY'", f" - > /tmp/aig-benchmark-control/runtime-{profile.port}.json <<'VERIFY'"
        ) +
        f"export AIG_RUNTIME_PORT={profile.port}\n"
        f"export AIG_SERVING_CONFIG={shlex.quote(json.dumps(asdict(profile)))}\n"
        f"export AIG_QUANTIZATION_EVIDENCE={shlex.quote(json.dumps(quantization_evidence or {}))}\n"
        "\"$HOME/.aig-vllm/bin/python\" - <<'LAUNCHER'\n"
        "import base64, pathlib\n"
        f"pathlib.Path({launch_path!r}).write_bytes(base64.b64decode({launcher!r}))\n"
        "LAUNCHER\n"
        f"nohup bash -c {quoted_command} > /tmp/aig-benchmark-control/vllm-{profile.port}.log 2>&1 &\n"
        "echo $! > /tmp/aig-benchmark-control/vllm.pid\n"
        "sleep 1\n"
        "cat /tmp/aig-benchmark-control/vllm.pid\n"
    )
