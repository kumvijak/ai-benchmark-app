from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class VmProfile:
    host: str
    port: int
    username: str
    private_key: str
    passphrase: str | None


class SshClient:
    def __init__(self, profile: VmProfile, timeout: int = 20):
        self.profile = profile
        self.timeout = timeout
        self._key_path: str | None = None

    def __enter__(self) -> "SshClient":
        if self.profile.passphrase:
            raise RuntimeError("Passphrase-protected keys are not supported in this initial container build")
        fd, path = tempfile.mkstemp(prefix="aig-ssh-key-", text=True)
        with os.fdopen(fd, "w") as fh:
            fh.write(self.profile.private_key)
            if not self.profile.private_key.endswith("\n"):
                fh.write("\n")
        os.chmod(path, 0o600)
        self._key_path = path
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._key_path:
            try:
                os.remove(self._key_path)
            finally:
                self._key_path = None

    def run(self, command: str, timeout: int = 60) -> tuple[int, str, str]:
        if not self._key_path:
            raise RuntimeError("SSH client is not connected")
        remote = f"{self.profile.username}@{self.profile.host}"
        args = [
            "ssh",
            "-i",
            self._key_path,
            "-p",
            str(self.profile.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={self.timeout}",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            remote,
            command,
        ]
        try:
            proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return 124, exc.stdout or "", exc.stderr or f"SSH command timed out after {timeout} seconds"
        return proc.returncode, proc.stdout, proc.stderr

    def run_checked(self, command: str, timeout: int = 60) -> str:
        code, out, err = self.run(command, timeout=timeout)
        if code != 0:
            raise RuntimeError(f"Remote command failed with exit {code}: {err or out}")
        return out


HEALTH_SCRIPT = r"""python3 - <<'PY'
import json, os, shutil, subprocess

os.environ["PATH"] = os.path.expanduser("~/.aig-vllm/bin") + os.pathsep + os.path.expanduser("~/.local/bin") + os.pathsep + os.environ.get("PATH", "")

def sh(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT).strip()
    except subprocess.CalledProcessError as exc:
        return exc.output.strip()

payload = {
  "os": sh("uname -a"),
  "python3": shutil.which("python3"),
  "curl": shutil.which("curl"),
  "vllm": shutil.which("vllm"),
  "cpu": sh("lscpu | sed -n '1,20p'"),
  "memory": sh("free -h"),
  "disk": sh("df -h /"),
  "time": sh("date -Iseconds"),
}
missing = [name for name in ["python3", "curl"] if not payload[name]]
payload["ok"] = len(missing) == 0
payload["missing_required"] = missing
print(json.dumps(payload))
PY"""


import base64

from .runtime import build_runtime_check_script


VM_SETUP_SCRIPT = r"""python3 - <<'PY'
import base64, json, os, re, shutil, subprocess, sys, time

log = []
VLLM_VERSION = "0.31.0"
GUIDELLM_VERSION = "0.8.0"
RUNTIME_CHECK = base64.b64decode("__RUNTIME_CHECK__").decode()
CONSTRAINTS = "vllm==0.31.0+cpu\ntorch==2.13.0+cpu\ntriton==3.8.0+cpu\ntransformers==5.17.0\nnumpy==2.3.5\nsetuptools==77.0.3\ncompressed-tensors==0.17.0\n"
os.environ.pop("LD_PRELOAD", None)

def sh(cmd, timeout=900):
    started = time.time()
    proc = subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=timeout)
    elapsed = time.time() - started
    out = (proc.stdout or "") + (proc.stderr or "")
    log.append({"cmd": cmd, "code": proc.returncode, "seconds": round(elapsed, 2), "output": out[-4000:]})
    return proc.returncode, out.strip()

def output(cmd):
    code, out = sh(cmd)
    return out if code == 0 else ""

def root_device_parts(source):
    if source.startswith("/dev/mapper/"):
        return None, None
    match = re.match(r"^(/dev/nvme\d+n\d+)p(\d+)$", source)
    if match:
        return match.group(1), match.group(2)
    match = re.match(r"^(/dev/[a-zA-Z]+)(\d+)$", source)
    if match:
        return match.group(1), match.group(2)
    return None, None

result = {
    "root_expand_status": "unknown",
    "root_device": None,
    "root_usage_before": output("df -h / | tail -n 1"),
    "root_usage_after": None,
    "vllm_status": "unknown",
    "vllm_version": None,
    "benchmark_status": "unknown",
    "benchmark_version": None,
    "python_path": shutil.which("python3"),
    "pip_path": shutil.which("pip3"),
    "curl_path": shutil.which("curl"),
    "cpu_acceleration": {},
    "vllm_install_variant": None,
    "ld_preload": None,
}

cpu_info = output("lscpu")
cpu_flags = output("lscpu | awk -F: '/Flags|Features/ {print tolower($2)}' | tr ' ' '\\n' | sort -u | tr '\\n' ' '")
cpu_vendor = output("lscpu | awk -F: '/Vendor ID|Model name/ {print $2; exit}'").strip()
is_amd = "authenticamd" in cpu_info.lower() or " amd " in f" {cpu_info.lower()} "
is_intel = "genuineintel" in cpu_info.lower() or " intel" in cpu_info.lower()
zen_eligible = is_amd and "avx512f" in cpu_flags
amx_eligible = is_intel and "amx_tile" in cpu_flags and "amx_bf16" in cpu_flags
result["cpu_acceleration"] = {
    "vendor": cpu_vendor,
    "amd_zen_eligible": zen_eligible,
    "intel_amx_eligible": amx_eligible,
    "has_avx512f": "avx512f" in cpu_flags,
    "has_avx2": "avx2" in cpu_flags,
    "has_amx_tile": "amx_tile" in cpu_flags,
    "has_amx_bf16": "amx_bf16" in cpu_flags,
}

intel_eligible = is_intel and 'avx2' in cpu_flags
if not (zen_eligible or intel_eligible):
    result.update(ok=False, vllm_status="unsupported_hardware",
                  error="AMD requires AVX-512; Intel requires AVX2 exposed to the guest", log=log)
    result["runtime_evidence"] = {"error": result["error"]}
    print(json.dumps(result))
    raise SystemExit(0)

if is_amd:
    CONSTRAINTS += "zentorch==2.13.0.1\n"
zentorch_requirement = "'zentorch==2.13.0.1' " if is_amd else ""

if sys.version_info[:2] != (3, 12):
    result.update(ok=False, vllm_status="unsupported_python",
                  error="The pinned default runtime requires Python 3.12 (Ubuntu 24.04)", log=log)
    result["runtime_evidence"] = {"error": result["error"]}
    print(json.dumps(result))
    raise SystemExit(0)

source = output("findmnt -n -o SOURCE /").splitlines()[-1:] or [""]
source = source[0]
disk, part = root_device_parts(source)
result["root_device"] = source
if disk and part:
    if not shutil.which("growpart"):
        sh("sudo DEBIAN_FRONTEND=noninteractive apt-get update", timeout=900)
        sh("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y cloud-guest-utils", timeout=900)
    grow_code, _ = sh(f"sudo growpart {disk} {part}", timeout=600)
    fs_type = output("findmnt -n -o FSTYPE /")
    if fs_type == "xfs":
        resize_code, _ = sh("sudo xfs_growfs /", timeout=600)
    else:
        resize_code, _ = sh(f"sudo resize2fs {source}", timeout=600)
    result["root_expand_status"] = "ok" if resize_code == 0 or grow_code in (0, 1) else "failed"
else:
    result["root_expand_status"] = "skipped_unsupported_root_device"

sh("sudo DEBIAN_FRONTEND=noninteractive apt-get update", timeout=900)
sh("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip python3-venv curl libtcmalloc-minimal4 libomp5-18 libnuma-dev ffmpeg libsm6 libxext6 libgl1 lsof", timeout=900)
result["python_path"] = shutil.which("python3")
result["pip_path"] = shutil.which("pip3")
result["curl_path"] = shutil.which("curl")

venv_dir = os.path.expanduser("~/.aig-vllm")
venv_python = os.path.join(venv_dir, "bin", "python")
os.environ["PATH"] = os.path.join(venv_dir, "bin") + os.pathsep + os.path.expanduser("~/.local/bin") + os.pathsep + os.environ.get("PATH", "")

def vllm_cpu_ready():
    code, out = sh(RUNTIME_CHECK, timeout=120)
    if code != 0:
        result["runtime_error"] = out[-4000:]
        return False
    try:
        result["runtime_evidence"] = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        result["runtime_error"] = "Missing runtime verification evidence"
        return False
    result.pop("runtime_error", None)
    return True

if not vllm_cpu_ready():
    os.environ.pop("LD_PRELOAD", None)
    sh(f"python3 -m venv {venv_dir}", timeout=300)
    constraints_path = os.path.join(venv_dir, "runtime-constraints.txt")
    with open(constraints_path, "w") as constraints_file:
        constraints_file.write(CONSTRAINTS)
    cpu_index_url = f"https://wheels.vllm.ai/{VLLM_VERSION}/cpu"
    result["vllm_install_variant"] = "vllm-cpu-zendnn-pinned" if is_amd else "vllm-cpu-intel-pinned"
    sh(
        f"{venv_python} -m pip install --upgrade pip && "
        f"{venv_python} -m pip install --no-cache-dir --force-reinstall 'vllm=={VLLM_VERSION}+cpu' "
        f"'torch==2.13.0+cpu' {zentorch_requirement}'transformers==5.17.0' "
        "'numpy==2.3.5' 'setuptools==77.0.3' 'compressed-tensors==0.17.0' "
        f"-c {constraints_path} --extra-index-url {cpu_index_url} "
        "--extra-index-url https://download.pytorch.org/whl/cpu && "
        f"{venv_python} -m pip check",
        timeout=2400,
    )
vllm_path = shutil.which("vllm")
if vllm_path and vllm_cpu_ready():
    result["vllm_status"] = "installed"
    result["vllm_version"] = result["runtime_evidence"]["vllm_version"]
else:
    result["vllm_status"] = "missing"

benchmark_python = venv_python
constraints_path = os.path.join(venv_dir, "runtime-constraints.txt")
if os.path.isdir(venv_dir):
    with open(constraints_path, "w") as constraints_file:
        constraints_file.write(CONSTRAINTS)
guide_code, guide_out = sh(f"{benchmark_python} -m pip show guidellm", timeout=120)
guide_version = None
if guide_code == 0:
    for line in guide_out.splitlines():
        if line.lower().startswith("version:"):
            guide_version = line.split(":", 1)[1].strip()
            break
if guide_code != 0 or guide_version != GUIDELLM_VERSION:
    sh(f"{benchmark_python} -m pip install --no-cache-dir guidellm=={GUIDELLM_VERSION} "
       f"'torch==2.13.0+cpu' {zentorch_requirement}'transformers==5.17.0' 'numpy==2.3.5' "
       "'setuptools==77.0.3' 'compressed-tensors==0.17.0' "
       f"-c {constraints_path} --extra-index-url https://wheels.vllm.ai/{VLLM_VERSION}/cpu "
       "--extra-index-url https://download.pytorch.org/whl/cpu", timeout=1800)
    guide_code, guide_out = sh(f"{benchmark_python} -m pip show guidellm", timeout=120)
if guide_code == 0:
    result["benchmark_status"] = "installed"
    for line in guide_out.splitlines():
        if line.lower().startswith("version:"):
            result["benchmark_version"] = line.split(":", 1)[1].strip()
            break
else:
    result["benchmark_status"] = "missing"
result["root_usage_after"] = output("df -h / | tail -n 1")
pip_code, _ = sh(f"{venv_python} -m pip check", timeout=120)
result["ok"] = result["benchmark_version"] == GUIDELLM_VERSION and result["vllm_status"] == "installed" and pip_code == 0 and vllm_cpu_ready()
if not result["ok"]:
    result.setdefault("runtime_evidence", {})["error"] = result.get("runtime_error", "Pinned runtime or GuideLLM dependency verification failed")
result["log"] = log[-30:]
print(json.dumps(result))
PY"""

VM_SETUP_SCRIPT = VM_SETUP_SCRIPT.replace(
    "__RUNTIME_CHECK__", base64.b64encode(build_runtime_check_script().encode()).decode()
)


def parse_health(output: str) -> dict:
    return json.loads(output.strip().splitlines()[-1])


def parse_setup(output: str) -> dict:
    return json.loads(output.strip().splitlines()[-1])


def wait_for_http(ssh: SshClient, port: int, timeout_seconds: int) -> dict:
    deadline = time.time() + timeout_seconds
    last = ""
    while time.time() < deadline:
        code, out, err = ssh.run(f"curl -fsS --max-time 3 http://127.0.0.1:{port}/v1/models", timeout=8)
        if code == 0:
            return {"ready": True, "response": out[-4000:]}
        last = err or out
        time.sleep(3)
    raise RuntimeError(f"vLLM endpoint did not become ready on port {port}: {last[-1000:]}")
