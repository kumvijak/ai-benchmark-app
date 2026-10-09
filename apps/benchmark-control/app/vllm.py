from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class VllmProfile:
    model_ref: str
    dtype: str
    max_model_len: int
    max_num_batched_tokens: int | None
    max_num_seqs: int
    cpu_kvcache_space_gib: int
    reserved_cpu: int
    prefix_cache_enabled: bool
    chunked_prefill_enabled: bool
    request_timeout_seconds: int
    port: int


def build_vllm_command(profile: VllmProfile) -> str:
    args = [
        "vllm",
        "serve",
        profile.model_ref,
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
    if profile.max_num_batched_tokens is not None:
        args.extend(["--max-num-batched-tokens", str(profile.max_num_batched_tokens)])
    if profile.prefix_cache_enabled:
        args.append("--enable-prefix-caching")
    if profile.chunked_prefill_enabled:
        args.append("--enable-chunked-prefill")
    env = {
        "PATH": "$HOME/.aig-vllm/bin:$HOME/.local/bin:$PATH",
        "VLLM_TARGET_DEVICE": "cpu",
        "VLLM_CPU_KVCACHE_SPACE": str(profile.cpu_kvcache_space_gib),
        "VLLM_CPU_NUM_OF_RESERVED_CPU": str(profile.reserved_cpu),
    }
    exports = " ".join(f"{k}={v if k == 'PATH' else shlex.quote(v)}" for k, v in env.items())
    quoted = " ".join(shlex.quote(str(a)) for a in args)
    return f"{exports} {quoted}"


def build_start_script(profile: VllmProfile) -> str:
    command = build_vllm_command(profile)
    quoted_command = shlex.quote(f"exec env {command}")
    return (
        "set -e\n"
        "export PATH=$HOME/.aig-vllm/bin:$HOME/.local/bin:$PATH\n"
        "command -v vllm >/dev/null || { echo 'vllm is not installed or not on PATH' >&2; exit 127; }\n"
        f"if ss -ltn 2>/dev/null | awk '{{print $4}}' | grep -Eq '(^|:){profile.port}$'; then "
        f"echo 'vLLM is already running on port {profile.port}; stop it before starting this profile' >&2; exit 98; fi\n"
        "mkdir -p /tmp/aig-benchmark-control\n"
        f"nohup bash -lc {quoted_command} > /tmp/aig-benchmark-control/vllm-{profile.port}.log 2>&1 &\n"
        "echo $! > /tmp/aig-benchmark-control/vllm.pid\n"
        "sleep 1\n"
        "cat /tmp/aig-benchmark-control/vllm.pid\n"
    )
