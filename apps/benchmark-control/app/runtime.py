"""Pinned remote CPU runtime; this module does not import inference packages locally."""
from __future__ import annotations

import base64

VLLM_VERSION = '0.31.0+cpu'
TORCH_VERSION = '2.13.0+cpu'
ZENTORCH_VERSION = '2.13.0.1'
ZENDNN_VERSION = '6.0.1'

# Shared by setup validation and every launch. Resolve libraries deterministically
# rather than picking an arbitrary OpenMP installation under /usr.
RUNTIME_PREAMBLE = r'''set -e
export PATH="$HOME/.aig-vllm/bin:$HOME/.local/bin:$PATH"
export VLLM_TARGET_DEVICE=cpu
unset VLLM_CPU_SGL_KERNEL
export TORCHINDUCTOR_FREEZING=1
export VLLM_USE_AOT_COMPILE=0
export TORCHINDUCTOR_AUTOGRAD_CACHE=0
unset LD_PRELOAD
TCMALLOC=$(ldconfig -p 2>/dev/null | awk '/libtcmalloc_minimal\.so\.4/ {print $NF; exit}')
OMP=$(ldconfig -p 2>/dev/null | awk '/libomp\.so\.5/ {print $NF; exit}')
[ -f "$TCMALLOC" ] || { echo 'Required tcmalloc library is missing' >&2; exit 1; }
[ -f "$OMP" ] || { echo 'Required LLVM OpenMP library is missing' >&2; exit 1; }
export LD_PRELOAD="$TCMALLOC:$OMP"
'''

# The same verifier runs in the server process before invoking vLLM's CLI.
RUNTIME_VERIFY_PY = r'''
import hashlib, importlib, importlib.metadata, json, os, pathlib, re

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

def verify_runtime():
    cpu_info = pathlib.Path('/proc/cpuinfo').read_text().split('\n\n', 1)[0]
    match = re.search(r'^flags\s*:\s*(.*)$', cpu_info, re.M)
    flags = set(match.group(1).split()) if match else set()
    vendor = 'amd' if 'AuthenticAMD' in cpu_info else 'intel' if 'GenuineIntel' in cpu_info else 'unknown'
    require(vendor in ('amd', 'intel'), 'Only AMD and Intel x86 CPU runtimes are supported')
    require('avx512f' in flags if vendor == 'amd' else 'avx2' in flags,
            'AMD requires AVX-512; Intel requires AVX2 exposed to the guest')
    amx_eligible = vendor == 'intel' and {'amx_tile', 'amx_bf16'} <= flags

    import torch
    import vllm
    from vllm.platforms import current_platform
    # The CPU wheel's module version omits its local ``+cpu`` build suffix.
    # Distribution metadata retains the exact installed artifact identity.
    versions = {'vllm_version': importlib.metadata.version('vllm'), 'torch_version': torch.__version__,
                'zentorch_version': None, 'zendnn_version': None}
    for key, expected in {'vllm_version': '0.31.0+cpu', 'torch_version': '2.13.0+cpu'}.items():
        require(versions[key] == expected, f'{key}: expected {expected}, detected {versions[key]!r}')
    require(torch.version.cuda is None, 'A CPU-only PyTorch build is required')
    amx_probe = getattr(getattr(torch, 'cpu', None), '_is_amx_tile_supported', None)
    amx_runtime_supported = bool(amx_probe()) if vendor == 'intel' and callable(amx_probe) else False
    versions['triton_version'] = importlib.metadata.version('triton')
    require(versions['triton_version'] == '3.8.0+cpu', 'CPU Triton 3.8.0+cpu is required')
    importlib.import_module('vllm._C')
    config = ''
    hashes = {}
    if vendor == 'amd':
        import zentorch
        config = str(zentorch.__config__)
        match = re.search(r'\b(?:AMD\s+)?ZENDNNL?\s+(?:version\s*[:=]?\s*)?[vV]?\s*["\']?(\d+\.\d+\.\d+)\b', config, re.I)
        versions['zendnn_version'] = match.group(1) if match else None
        versions['zentorch_version'] = importlib.metadata.version('zentorch')
        for key, expected in {'zentorch_version': '2.13.0.1', 'zendnn_version': '6.0.1'}.items():
            require(versions[key] == expected, f'{key}: expected {expected}, detected {versions[key]!r}; build config: {config}')
        require(not getattr(zentorch, '__stable_abi_only__', False), 'ZenTorch full backend is required')
        require(zentorch._C.is_avx512_supported(), 'ZenTorch requires supported AVX-512 instructions')
        require(current_platform.is_zen_cpu(), f'ZenDNN required; detected {type(current_platform).__name__}')
        root = pathlib.Path(zentorch.__file__).parent
        for library in sorted(root.rglob('*.so')):
            with library.open('rb') as native_file:
                hashes[str(library.relative_to(root))] = hashlib.file_digest(native_file, 'sha256').hexdigest()
        backend = 'zendnn'
    else:
        require(type(current_platform).__module__ == 'vllm.platforms.cpu' and
                type(current_platform).__name__ == 'CpuPlatform',
                f'Intel requires standard CpuPlatform; detected {type(current_platform).__name__}')
        backend = 'intel_cpu'
    return dict(versions, backend=backend, cpu_vendor=vendor,
        platform=f'{type(current_platform).__module__}.{type(current_platform).__name__}',
        build_config=config, native_library_sha256=hashes,
        ld_preload=os.environ.get('LD_PRELOAD', ''), cpu_info=cpu_info,
        cpu_capabilities={flag: flag in flags for flag in ('avx2', 'avx512f', 'avx512_bf16', 'amx_tile', 'amx_bf16')},
        amx_eligible=amx_eligible, amx_runtime_supported=amx_runtime_supported,
        amx_policy='library_auto_dispatch',
        environment={key: os.environ.get(key) for key in ('VLLM_TARGET_DEVICE',
            'VLLM_CPU_SGL_KERNEL', 'ZENDNNL_ENABLE_PROFILER', 'ZENDNNL_API_LOG_LEVEL', 'TORCHINDUCTOR_FREEZING', 'VLLM_USE_AOT_COMPILE', 'TORCHINDUCTOR_AUTOGRAD_CACHE')})

if __name__ == '__main__':
    print(json.dumps(verify_runtime()))
'''


def build_runtime_check_script() -> str:
    encoded = base64.b64encode(RUNTIME_VERIFY_PY.encode()).decode()
    return RUNTIME_PREAMBLE + (
        '\"$HOME/.aig-vllm/bin/python\" - <<\'VERIFY\'\n'
        "import base64, json, subprocess, sys\n"
        f"code = base64.b64decode('{encoded}').decode()\n"
        "proc = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True)\n"
        "if proc.returncode:\n"
        "    print(proc.stdout + proc.stderr, file=sys.stderr)\n"
        "    raise SystemExit(proc.returncode)\n"
        "evidence = json.loads(proc.stdout.strip().splitlines()[-1])\n"
        "evidence['verification_log'] = (proc.stdout.rsplit('\\n', 2)[0] + proc.stderr)[-4000:]\n"
        "print(json.dumps(evidence))\nVERIFY\n"
    )


def build_runtime_launcher() -> str:
    # This file must survive multiprocessing imports without invoking the CLI.
    definitions = RUNTIME_VERIFY_PY.split("if __name__ == '__main__':", 1)[0]
    return definitions + r'''
if __name__ == '__main__':
    import sys
    evidence = verify_runtime()
    evidence['serving_config'] = json.loads(os.environ.get('AIG_SERVING_CONFIG', '{}'))
    evidence['quantization_evidence'] = json.loads(os.environ.get('AIG_QUANTIZATION_EVIDENCE', '{}'))
    evidence['server_pid'] = os.getpid()
    evidence['server_start_ticks'] = pathlib.Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()[19]
    path = pathlib.Path('/tmp/aig-benchmark-control') / f"runtime-{os.environ['AIG_RUNTIME_PORT']}.json"
    pending = path.with_suffix('.json.part')
    pending.write_text(json.dumps(evidence))
    pending.replace(path)
    print('AIG_RUNTIME_BACKEND=' + json.dumps(evidence), flush=True)
    sys.argv[0] = 'vllm'
    from vllm.entrypoints.cli.main import main
    main()
'''
