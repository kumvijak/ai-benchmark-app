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
            remote,
            command,
        ]
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
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


VM_SETUP_SCRIPT = r"""python3 - <<'PY'
import json, os, re, shutil, subprocess, time

log = []
VLLM_VERSION = "0.30.0"
GUIDELLM_VERSION = "0.8.0"

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
}

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
sh("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip python3-venv curl libtcmalloc-minimal4", timeout=900)
result["python_path"] = shutil.which("python3")
result["pip_path"] = shutil.which("pip3")
result["curl_path"] = shutil.which("curl")

venv_dir = os.path.expanduser("~/.aig-vllm")
venv_python = os.path.join(venv_dir, "bin", "python")
os.environ["PATH"] = os.path.join(venv_dir, "bin") + os.pathsep + os.path.expanduser("~/.local/bin") + os.pathsep + os.environ.get("PATH", "")

def vllm_cpu_ready():
    code, _ = sh(
        f"{venv_python} - <<'VERIFY'\n"
        "import importlib\n"
        "import vllm\n"
        "import vllm.platforms\n"
        f"assert vllm.__version__.split('+', 1)[0] == {VLLM_VERSION!r}, vllm.__version__\n"
        "assert vllm.platforms.current_platform.device_name == 'cpu', vllm.platforms.current_platform.device_name\n"
        "importlib.import_module('vllm._C')\n"
        "VERIFY\n"
        "VLLM_TARGET_DEVICE=cpu vllm serve --help >/dev/null",
        timeout=120,
    )
    return code == 0

if not shutil.which("vllm") or not vllm_cpu_ready():
    sh("python3 -m pip install --user --break-system-packages -U uv", timeout=900)
    sh(f"rm -rf {venv_dir}", timeout=300)
    sh(f"python3 -m venv {venv_dir}", timeout=300)
    uv_path = os.path.expanduser("~/.local/bin/uv")
    if os.path.exists(uv_path):
        sh(
            f"{uv_path} pip install --python {venv_python} --force-reinstall vllm=={VLLM_VERSION} "
            f"--extra-index-url https://wheels.vllm.ai/{VLLM_VERSION}/cpu "
            "--extra-index-url https://download.pytorch.org/whl/cpu "
            "--index-strategy unsafe-best-match --torch-backend cpu",
            timeout=2400,
        )
    if not vllm_cpu_ready():
        sh(
            f"{venv_python} -m pip install --no-cache-dir --force-reinstall vllm=={VLLM_VERSION} "
            f"--extra-index-url https://wheels.vllm.ai/{VLLM_VERSION}/cpu "
            "--extra-index-url https://download.pytorch.org/whl/cpu",
            timeout=2400,
        )
vllm_path = shutil.which("vllm")
if vllm_path and vllm_cpu_ready():
    result["vllm_status"] = "installed"
    result["vllm_version"] = output(f"{venv_python} -c 'import vllm; print(getattr(vllm, \"__version__\", \"unknown\"))'") or output("vllm --version")
else:
    result["vllm_status"] = "missing"

benchmark_python = venv_python if os.path.exists(venv_python) else "python3"
guide_code, guide_out = sh(f"{benchmark_python} -m pip show guidellm", timeout=120)
guide_version = None
if guide_code == 0:
    for line in guide_out.splitlines():
        if line.lower().startswith("version:"):
            guide_version = line.split(":", 1)[1].strip()
            break
if guide_code != 0 or guide_version != GUIDELLM_VERSION:
    sh(f"{benchmark_python} -m pip install --no-cache-dir --force-reinstall guidellm=={GUIDELLM_VERSION}", timeout=1800)
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
result["ok"] = result["benchmark_status"] == "installed" and result["vllm_status"] == "installed"
result["log"] = log[-30:]
print(json.dumps(result))
PY"""


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
