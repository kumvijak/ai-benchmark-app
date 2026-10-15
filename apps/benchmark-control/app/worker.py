from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
import threading
import time
from typing import Any

from .benchmark import GuideLLMBenchmarkSpec, normalize_guidellm_result, remote_guidellm_command
from .db import Database
from .oci_ops import DOC_APPROVED_SHAPES, context_from_saved_auth, shape_config_details
from .security import SecretBox, redact
from .runtime import VLLM_VERSION, TORCH_VERSION, ZENTORCH_VERSION, ZENDNN_VERSION
from .ssh_client import HEALTH_SCRIPT, VM_SETUP_SCRIPT, SshClient, VmProfile, parse_health, parse_setup, wait_for_http
from .vllm import VllmProfile, build_start_script, build_vllm_command


class Worker:
    def __init__(self, db: Database, secrets: SecretBox):
        self.db = db
        self.secrets = secrets
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="benchmark-worker", daemon=True)

    def start(self) -> None:
        self.db.execute(
            """
            UPDATE jobs
            SET status='failed',
                error_message='Job was interrupted by an app restart and must be requeued.',
                finished_at=CURRENT_TIMESTAMP
            WHERE status='running'
            """
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self.db.one("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
            if not job:
                time.sleep(1)
                continue
            try:
                self._run_job(job)
            except Exception as exc:
                self.db.execute(
                    "UPDATE jobs SET status='failed', error_message=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                    (redact(str(exc)), job["id"]),
                )

    def _run_job(self, job: dict[str, Any]) -> None:
        self.db.execute("UPDATE jobs SET status='running', started_at=CURRENT_TIMESTAMP WHERE id=?", (job["id"],))
        payload = json.loads(job["payload"])
        payload["_job_id"] = job["id"]
        if job["job_type"] == "health_check":
            result = self._health_check(job["owner_user_id"], payload)
        elif job["job_type"] == "start_runtime":
            result = self._start_runtime(job["owner_user_id"], payload)
        elif job["job_type"] == "stop_runtime":
            result = self._stop_runtime(job["owner_user_id"], payload)
        elif job["job_type"] == "run_benchmark":
            result = self._run_benchmark(job["owner_user_id"], payload)
        elif job["job_type"] == "provision_vm":
            result = self._provision_vm(job["owner_user_id"], payload)
        elif job["job_type"] == "destroy_vm":
            result = self._destroy_vm(job["owner_user_id"], payload)
        else:
            raise RuntimeError(f"Unknown job type {job['job_type']}")
        self.db.execute(
            "UPDATE jobs SET status='succeeded', result=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(result, separators=(",", ":")), job["id"]),
        )

    def _vm(self, user_id: int, vm_profile_id: int) -> VmProfile:
        row = self.db.one("SELECT * FROM vm_profiles WHERE id=? AND owner_user_id=?", (vm_profile_id, user_id))
        if not row:
            raise RuntimeError("VM profile not found")
        return VmProfile(
            host=row["host"],
            port=int(row["port"]),
            username=row["username"],
            private_key=self.secrets.decrypt(row["encrypted_private_key"]) or "",
            passphrase=self.secrets.decrypt(row["encrypted_passphrase"]),
        )

    def _vllm(self, user_id: int, vllm_profile_id: int) -> VllmProfile:
        row = self.db.one("SELECT * FROM vllm_profiles WHERE id=? AND owner_user_id=?", (vllm_profile_id, user_id))
        if not row:
            raise RuntimeError("vLLM profile not found")
        return VllmProfile(
            model_ref=row["model_ref"],
            dtype=row["dtype"],
            max_model_len=int(row["max_model_len"]),
            max_num_batched_tokens=row["max_num_batched_tokens"],
            max_num_seqs=int(row["max_num_seqs"]),
            cpu_kvcache_space_gib=None if row.get('cpu_kvcache_auto') else int(row["cpu_kvcache_space_gib"]),
            reserved_cpu=int(row["reserved_cpu"]),
            prefix_cache_enabled=bool(row["prefix_cache_enabled"]),
            chunked_prefill_enabled=bool(row["chunked_prefill_enabled"]),
            request_timeout_seconds=int(row["request_timeout_seconds"]),
            port=int(row["port"]),
            quantization=row.get('quantization', 'none'),
            model_revision=row.get('model_revision'),
        )

    def _health_check(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        vm_id = int(payload["vm_profile_id"])
        job_id = payload.get("_job_id")
        progress_base = int(payload.get("_progress_base", 0))
        progress_span = int(payload.get("_progress_span", 100))

        def progress(relative: int) -> int:
            return progress_base + round((max(0, min(100, relative)) / 100) * progress_span)

        with SshClient(self._vm(user_id, vm_id)) as ssh:
            if job_id:
                self._set_job_progress(int(job_id), progress(20), "ssh_connecting", "Connecting to the VM over SSH")
            health_out = ssh.run_checked(HEALTH_SCRIPT, timeout=45)
            if job_id:
                self._set_job_progress(
                    int(job_id),
                    progress(45),
                    "vm_setup",
                    "Expanding root disk and installing vLLM/benchmark dependencies",
                    {"vm_profile_id": vm_id},
                )
            setup_code, setup_out, setup_err = ssh.run(VM_SETUP_SCRIPT, timeout=3600)
            if setup_code != 0:
                raise RuntimeError(f"VM setup failed: {setup_err or setup_out}")
            if job_id:
                self._set_job_progress(int(job_id), progress(85), "vm_setup_validating", "Rechecking VM readiness after setup")
            final_health_out = ssh.run_checked(HEALTH_SCRIPT, timeout=45)
        result = parse_health(final_health_out)
        result["initial_health"] = parse_health(health_out)
        result["setup"] = parse_setup(setup_out)
        self._store_vm_setup_status(user_id, vm_id, result["setup"])
        status = "healthy" if result.get("ok") and result["setup"].get("ok") else "setup_failed"
        self.db.execute(
            "UPDATE vm_profiles SET last_status=?, last_checked_at=CURRENT_TIMESTAMP WHERE id=? AND owner_user_id=?",
            (status, vm_id, user_id),
        )
        if job_id:
            self._set_job_progress(
                int(job_id),
                progress(100),
                "complete" if status == "healthy" else "setup_failed",
                "VM validation completed" if status == "healthy" else "VM validation completed with setup failures",
                {"vm_profile_id": vm_id, "status": status},
            )
        return result

    def _store_vm_setup_status(self, user_id: int, vm_id: int, setup: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO vm_setup_status(
              vm_profile_id, owner_user_id, root_expand_status, root_device,
              root_usage_before, root_usage_after, vllm_status, vllm_version,
              benchmark_status, benchmark_version, python_path, pip_path, curl_path, log, runtime_evidence, last_setup_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(vm_profile_id) DO UPDATE SET
              root_expand_status=excluded.root_expand_status,
              root_device=excluded.root_device,
              root_usage_before=excluded.root_usage_before,
              root_usage_after=excluded.root_usage_after,
              vllm_status=excluded.vllm_status,
              vllm_version=excluded.vllm_version,
              benchmark_status=excluded.benchmark_status,
              benchmark_version=excluded.benchmark_version,
              python_path=excluded.python_path,
              pip_path=excluded.pip_path,
              curl_path=excluded.curl_path,
              log=excluded.log,
              runtime_evidence=excluded.runtime_evidence,
              last_setup_at=CURRENT_TIMESTAMP
            """,
            (
                vm_id,
                user_id,
                setup.get("root_expand_status", "unknown"),
                setup.get("root_device"),
                setup.get("root_usage_before"),
                setup.get("root_usage_after"),
                setup.get("vllm_status", "unknown"),
                setup.get("vllm_version"),
                setup.get("benchmark_status", "unknown"),
                setup.get("benchmark_version"),
                setup.get("python_path"),
                setup.get("pip_path"),
                setup.get("curl_path"),
                json.dumps(setup.get("log", []), separators=(",", ":")),
                json.dumps(setup.get("runtime_evidence", {}), separators=(",", ":")),
            ),
        )

    def _start_runtime(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        vm = self._vm(user_id, int(payload["vm_profile_id"]))
        profile = self._vllm(user_id, int(payload["vllm_profile_id"]))
        with SshClient(vm) as ssh:
            quantization_evidence = None
            if profile.quantization != 'none':
                from .quantization import build_prepare_command
                if payload.get('_job_id'):
                    self._set_job_progress(int(payload['_job_id']), 10, 'preparing_quantized_checkpoint',
                                          f'Preparing and validating {profile.quantization.upper()} model weights')
                if not profile.model_revision:
                    resolve = ("~/.aig-vllm/bin/python - <<'PY'\n"
                               "from huggingface_hub import HfApi\n"
                               f"print(HfApi().model_info({profile.model_ref!r}).sha)\nPY")
                    code, out, err = ssh.run(resolve, timeout=60)
                    if code != 0:
                        raise RuntimeError(f'Cannot resolve model revision: {redact(err or out)[-1500:]}')
                    profile = replace(profile, model_revision=out.strip().splitlines()[-1])
                prepare = build_prepare_command(profile.model_ref, profile.model_revision, profile.quantization)
                source = base64.b64encode(Path(__file__).with_name('quantization.py').read_bytes()).decode('ascii')
                command = ("umask 077\nmkdir -p /tmp/aig-benchmark-control\n"
                           "python3 - <<'PY'\nimport base64,pathlib\n"
                           f"pathlib.Path('/tmp/aig-benchmark-control/quantization.py').write_bytes(base64.b64decode({source!r}))\nPY\n"
                           "export PATH=$HOME/.aig-vllm/bin:$HOME/.local/bin:$PATH\n" + prepare)
                code, out, err = ssh.run(command, timeout=7200)
                if code != 0:
                    raise RuntimeError(f'Quantized checkpoint preparation failed: {redact(err or out)[-5000:]}')
                quantization_evidence = json.loads(out.strip().splitlines()[-1])
                if quantization_evidence['bits'] != (8 if profile.quantization == 'int8' else 4):
                    raise RuntimeError('Prepared checkpoint precision does not match selected profile')
                if (quantization_evidence.get('validation', {}).get('status') != 'PASS'
                        or quantization_evidence.get('source_revision') != profile.model_revision
                        or quantization_evidence.get('model_ref') != profile.model_ref
                        or quantization_evidence.get('format') != ('int-quantized' if profile.quantization == 'int8' else 'pack-quantized')):
                    raise RuntimeError('Prepared checkpoint identity, format or validation mismatch')
                self.db.execute('UPDATE vllm_profiles SET model_revision=? WHERE id=? AND owner_user_id=?',
                                (profile.model_revision, int(payload['vllm_profile_id']), user_id))
            checkpoint_path = quantization_evidence['checkpoint_path'] if quantization_evidence else None
            script = build_start_script(profile, checkpoint_path)
            code, out, err = ssh.run(script, timeout=180)
            if code != 0:
                raise RuntimeError(f"vLLM start failed: {err or out}")
            pid = out.strip().splitlines()[-1]
            try:
                ready = wait_for_http(ssh, profile.port, int(payload.get("readiness_timeout_seconds", 900)))
            except RuntimeError as exc:
                state = self._runtime_state(ssh, profile.port, pid)
                if state.get("pid_alive"):
                    ready = {
                        "ready": False,
                        "pending": True,
                        "message": "vLLM is still loading after the readiness wait. The runtime process is still alive.",
                        "last_error": redact(str(exc)),
                        "runtime_state": state,
                    }
                else:
                    raise RuntimeError(
                        "vLLM process exited before the endpoint became ready: "
                        f"{redact(str(exc))}\n{state.get('log_tail', '')[-3000:]}"
                    ) from exc
            runtime_evidence = self._runtime_evidence(ssh, profile.port, require_activation=bool(ready.get("ready")))
        return {
            "runtime_evidence": runtime_evidence,
            "pid": pid,
            "port": profile.port,
            "model_ref": profile.model_ref,
            "vllm_command": build_vllm_command(profile, checkpoint_path, f"/tmp/aig-benchmark-control/vllm-launch-{profile.port}.py"),
            "quantization": profile.quantization,
            "quantization_evidence": quantization_evidence,
            "readiness": ready,
        }

    def _runtime_evidence(self, ssh: SshClient, port: int, require_activation: bool = True) -> dict[str, Any]:
        script = (
            "python3 - <<'PY'\n"
            "import json, pathlib\n"
            f"port = {int(port)}\n"
            "root = pathlib.Path('/tmp/aig-benchmark-control')\n"
            "evidence = json.loads((root / f'runtime-{port}.json').read_text())\n"
            "log = (root / f'vllm-{port}.log').read_text(errors='replace')\n"
            "activation = []\n"
            "for line in log.splitlines():\n"
            "    if line.startswith('AIG_RUNTIME_BACKEND='):\n"
            "        activation.append(json.loads(line.split('=', 1)[1]))\n"
            "evidence['server_activation_verified'] = any(all(item.get(key) == evidence.get(key) for key in ('server_pid', 'server_start_ticks', 'platform', 'backend', 'amx_runtime_supported', 'amx_policy')) for item in activation)\n"
            "proc = pathlib.Path('/proc') / str(evidence.get('server_pid', 'missing'))\n"
            "evidence['server_alive'] = proc.exists()\n"
            "if evidence['server_alive']:\n"
            "    evidence['server_alive'] = proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()[19] == evidence.get('server_start_ticks')\n"
            "print(json.dumps(evidence))\nPY"
        )
        evidence = json.loads(ssh.run_checked(script, timeout=30).strip().splitlines()[-1])
        expected = {'vllm_version': VLLM_VERSION, 'torch_version': TORCH_VERSION}
        if evidence.get('backend') == 'zendnn':
            expected.update(zentorch_version=ZENTORCH_VERSION, zendnn_version=ZENDNN_VERSION,
                            cpu_vendor='amd', platform='vllm.platforms.zen_cpu.ZenCpuPlatform',
                            amx_runtime_supported=False, amx_policy='library_auto_dispatch')
        elif evidence.get('backend') == 'intel_cpu':
            expected.update(cpu_vendor='intel', platform='vllm.platforms.cpu.CpuPlatform',
                            amx_policy='library_auto_dispatch')
            if evidence.get('amx_runtime_supported') and not evidence.get('amx_eligible'):
                raise RuntimeError('Intel AMX runtime support is inconsistent with the CPU capability evidence')
        else:
            raise RuntimeError('Unsupported runtime backend in server evidence')
        if any(evidence.get(key) != value for key, value in expected.items()):
            raise RuntimeError('Runtime evidence does not match the required pinned vendor backend')
        if require_activation and not (evidence.get('server_activation_verified') and evidence.get('server_alive')):
            raise RuntimeError('The launched server has not confirmed backend activation; restart it with the pinned runtime')
        return evidence

    def _runtime_state(self, ssh: SshClient, port: int, pid: str) -> dict[str, Any]:
        check = (
            "python3 - <<'PY'\n"
            "import json, os, subprocess\n"
            f"pid={pid!r}\n"
            f"port={int(port)}\n"
            "def sh(cmd):\n"
            "    try:\n"
            "        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT).strip()\n"
            "    except subprocess.CalledProcessError as exc:\n"
            "        return (exc.output or '').strip()\n"
            "pid_alive = pid.isdigit() and os.path.exists(f'/proc/{pid}')\n"
            "payload = {\n"
            "    'pid': pid,\n"
            "    'pid_alive': pid_alive,\n"
            "    'port': port,\n"
            "    'listener': sh(f\"ss -ltnp 2>/dev/null | grep ':{port} ' || true\"),\n"
            "    'process': sh(f\"ps -fp {pid} 2>/dev/null || true\") if pid.isdigit() else '',\n"
            "    'log_tail': sh(f\"tail -n 120 /tmp/aig-benchmark-control/vllm-{port}.log 2>/dev/null || true\"),\n"
            "}\n"
            "print(json.dumps(payload))\n"
            "PY"
        )
        code, out, err = ssh.run(check, timeout=20)
        if code != 0:
            return {"pid": pid, "port": port, "pid_alive": False, "error": err or out}
        return json.loads(out.strip().splitlines()[-1])

    def _run_benchmark(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        vm = self._vm(user_id, int(payload["vm_profile_id"]))
        vllm = self._vllm(user_id, int(payload["vllm_profile_id"]))
        job_id = payload.get("_job_id")
        concurrencies = payload.get("concurrencies") or [payload.get("concurrency")]
        concurrencies = [int(value) for value in concurrencies if int(value) > 0]
        if not concurrencies:
            raise RuntimeError("No benchmark concurrencies were provided")
        mode = str(payload.get("benchmark_mode") or "concurrent")
        if mode not in {"concurrent", "sweep"}:
            raise RuntimeError("benchmark_mode must be concurrent or sweep")
        with SshClient(vm) as ssh:
            runtime_evidence = self._runtime_evidence(ssh, vllm.port)
            if job_id:
                self._set_job_progress(
                    int(job_id),
                    5,
                    "running_guidellm",
                    f"Running GuideLLM {mode} benchmark",
                    {"benchmark_mode": mode, "concurrencies": concurrencies},
                )
            spec = GuideLLMBenchmarkSpec(
                model_ref=vllm.model_ref,
                port=vllm.port,
                workload_name=str(payload["workload_name"]),
                input_tokens=int(payload["input_tokens"]),
                output_tokens=int(payload["output_tokens"]),
                concurrencies=concurrencies,
                max_requests=int(payload["max_requests"]),
                max_errors=payload.get('max_errors', 5),
                request_timeout_seconds=vllm.request_timeout_seconds,
                mode=mode,
                max_duration_seconds=int(payload.get("max_duration_seconds") or 600),
                cooldown_seconds=int(payload.get("cooldown_seconds") or 5),
                seed=int(payload.get("seed") or 42),
                sweep_size=int(payload.get("sweep_size") or 10),
                sweep_strategy_type=str(payload.get("sweep_strategy_type") or "poisson"),
                runtime_evidence=runtime_evidence,
            )
            command = remote_guidellm_command(spec, int(job_id or 0))
            timeout = max(900, (spec.max_duration_seconds + 120) * max(1, len(concurrencies)))
            code, out, err = ssh.run(command, timeout=timeout)
            if code != 0:
                raise RuntimeError(f"GuideLLM benchmark failed: {err or out}")
            envelope = json.loads(out.strip().splitlines()[-1])
            result = normalize_guidellm_result(envelope)
            result['runtime_evidence'] = runtime_evidence
            result['model_precision'] = {'quantization': vllm.quantization, 'dtype': vllm.dtype,
                                         'source_model': vllm.model_ref, 'source_revision': vllm.model_revision}
        if job_id:
            self._set_job_progress(
                int(job_id),
                100,
                "complete",
                "GuideLLM benchmark completed",
                {
                    "benchmark_mode": mode,
                    "concurrencies": concurrencies,
                    "artifacts": list((result.get("artifacts") or {}).keys()),
                },
            )
        return result

    def _stop_runtime(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        vm = self._vm(user_id, int(payload["vm_profile_id"]))
        target = {"pid": payload.get("pid"), "port": payload.get("port")}
        script = f"""
import json, os, re, signal, subprocess, time
target = json.loads({json.dumps(target)!r})
matches = []
proc = subprocess.run(["pgrep", "-af", "[v]llm serve|[v]llm-launch-[0-9]+[.]py serve"], text=True, capture_output=True)
for line in proc.stdout.splitlines():
    parts = line.split(" ", 1)
    if not parts:
        continue
    pid = int(parts[0])
    command = parts[1] if len(parts) > 1 else ""
    port = None
    match = re.search(r"(?:--port\\s+|--port=)(\\d+)", command)
    if match:
        port = int(match.group(1))
    if target.get("pid") and pid == int(target["pid"]):
        matches.append({{"pid": pid, "port": port, "command": command}})
    elif target.get("port") and port == int(target["port"]):
        matches.append({{"pid": pid, "port": port, "command": command}})

def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

stopped = []
for row in matches:
    pid = int(row["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not alive(pid):
                break
            time.sleep(0.25)
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
        row["stopped"] = True
    except Exception as exc:
        row["stopped"] = False
        row["error"] = str(exc)[-500:]
    stopped.append(row)
print(json.dumps({{"target": target, "matched": len(matches), "stopped": stopped}}))
"""
        with SshClient(vm) as ssh:
            code, out, err = ssh.run(f"python3 - <<'PY'\n{script}\nPY", timeout=30)
        if code != 0:
            raise RuntimeError(f"vLLM stop failed: {err or out}")
        result = json.loads(out.strip().splitlines()[-1])
        if result.get("matched", 0) == 0:
            result["message"] = "No matching vLLM process was running"
        return result

    def _auth_context(self, user_id: int, auth_profile_id: int | None = None):
        if auth_profile_id:
            row = self.db.one(
                "SELECT * FROM oci_auth_profiles WHERE id=? AND owner_user_id=?",
                (auth_profile_id, user_id),
            )
        else:
            row = self.db.one(
                """
                SELECT * FROM oci_auth_profiles
                WHERE owner_user_id=? AND last_status='validated'
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (user_id,),
            )
        if not row:
            raise RuntimeError("No validated OCI authentication profile is available")
        return row, context_from_saved_auth(row, self.secrets.decrypt)

    def _provision_vm(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        import oci

        job_id = int(payload["_job_id"])
        self._set_job_progress(job_id, 5, "validating_request", "Validating VM deployment request")
        shape = str(payload["shape"])
        if shape not in DOC_APPROVED_SHAPES:
            raise RuntimeError(f"Shape is not approved for this benchmark guide: {shape}")

        display_name = str(payload["display_name"])
        compartment_id = str(payload["compartment_id"])
        availability_domain = str(payload["availability_domain"])
        subnet_id = str(payload["subnet_id"])
        image_id = str(payload["image_id"])
        ocpus = float(payload["ocpus"])
        memory_gb = float(payload["memory_gb"])
        boot_volume_gb = int(payload["boot_volume_gb"])
        username = str(payload.get("username") or "ubuntu")
        ssh_public_key = str(payload["ssh_public_key"]).strip()
        ssh_private_key = str(payload.get("ssh_private_key") or "").strip()
        assign_public_ip = bool(payload.get("assign_public_ip", True))

        if not ssh_public_key:
            raise RuntimeError("SSH public key is required to deploy a VM")
        if ocpus <= 0 or memory_gb <= 0 or boot_volume_gb < 50:
            raise RuntimeError("OCPU, memory, and boot volume values must be positive")

        self._set_job_progress(job_id, 12, "loading_auth", "Loading validated OCI authentication")
        auth_row, ctx = self._auth_context(user_id, payload.get("oci_auth_profile_id"))
        with ctx:
            clients = _oci_clients(ctx)
            compute = clients["compute"]
            network = clients["network"]
            block = clients["block"]
            self._set_job_progress(job_id, 20, "launching_instance", "Submitting OCI compute launch request")
            launch_details = oci.core.models.LaunchInstanceDetails(
                availability_domain=availability_domain,
                compartment_id=compartment_id,
                display_name=display_name,
                shape=shape,
                shape_config=shape_config_details(shape, ocpus, memory_gb),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=subnet_id,
                    assign_public_ip=assign_public_ip,
                    display_name=f"{display_name}-vnic",
                ),
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=image_id,
                    boot_volume_size_in_gbs=boot_volume_gb,
                ),
                metadata={"ssh_authorized_keys": ssh_public_key},
            )
            instance = compute.launch_instance(launch_details).data
            instance_id = instance.id
            self._set_job_progress(
                job_id,
                35,
                "instance_accepted",
                "OCI accepted the launch request; waiting for RUNNING",
                {"instance_id": instance_id},
            )
            self.db.execute(
                """
                INSERT INTO oci_compute_instances(
                  owner_user_id, oci_auth_profile_id, display_name, compartment_id,
                  availability_domain, subnet_id, instance_id, shape, ocpus, memory_gb,
                  boot_volume_gb, image_id, assign_public_ip, lifecycle_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, auth_row["id"], display_name, compartment_id, availability_domain,
                    subnet_id, instance_id, shape, ocpus, memory_gb, boot_volume_gb,
                    image_id, int(assign_public_ip), getattr(instance, "lifecycle_state", "PROVISIONING"),
                ),
            )
            running = oci.wait_until(
                compute,
                compute.get_instance(instance_id),
                "lifecycle_state",
                "RUNNING",
                max_wait_seconds=900,
                max_interval_seconds=20,
            ).data
            self._set_job_progress(
                job_id,
                58,
                "instance_running",
                "Instance is RUNNING; discovering network and boot volume",
                {"instance_id": instance_id},
            )
            vnic = _wait_for_primary_vnic(compute, network, compartment_id, instance_id)
            boot_volume_id = _first_boot_volume_id(compute, compartment_id, availability_domain, instance_id)
            public_ip = getattr(vnic, "public_ip", None)
            private_ip = getattr(vnic, "private_ip", None)
            self._set_job_progress(
                job_id,
                72,
                "network_ready",
                "Network and boot volume metadata discovered",
                {
                    "instance_id": instance_id,
                    "public_ip": public_ip,
                    "private_ip": private_ip,
                    "boot_volume_id": boot_volume_id,
                },
            )
            self.db.execute(
                """
                UPDATE oci_compute_instances
                SET boot_volume_id=?, public_ip=?, private_ip=?, lifecycle_state=?
                WHERE instance_id=? AND owner_user_id=?
                """,
                (boot_volume_id, public_ip, private_ip, running.lifecycle_state, instance_id, user_id),
            )

        host = public_ip if assign_public_ip else private_ip
        if not host:
            raise RuntimeError("OCI instance launched, but no reachable IP address was discovered")

        vm_profile_id = None
        health: dict[str, Any] = {
            "ok": False,
            "message": "SSH health check was skipped because no private key was provided",
        }
        if ssh_private_key:
            self._set_job_progress(job_id, 82, "creating_ssh_profile", "Creating SSH profile for the new VM")
            vm_profile_id = self._create_vm_profile_for_instance(user_id, display_name, host, username, ssh_private_key)
            self._set_job_progress(job_id, 88, "testing_ssh", "Testing SSH while cloud-init finishes", {"vm_profile_id": vm_profile_id})
            health = self._wait_and_health_check(user_id, vm_profile_id, job_id, progress_base=88, progress_span=8)
            self._set_job_progress(job_id, 96, "ssh_validated", "SSH health check completed", {"vm_profile_id": vm_profile_id})

        self.db.execute(
            "UPDATE oci_compute_instances SET vm_profile_id=? WHERE instance_id=? AND owner_user_id=?",
            (vm_profile_id, instance_id, user_id),
        )
        return {
            "progress": {"percent": 100, "stage": "complete", "message": "VM deployment completed"},
            "instance_id": instance_id,
            "display_name": display_name,
            "shape": shape,
            "ocpus": ocpus,
            "memory_gb": memory_gb,
            "boot_volume_gb": boot_volume_gb,
            "public_ip": public_ip,
            "private_ip": private_ip,
            "vm_profile_id": vm_profile_id,
            "ssh_health": health,
            "boot_volume_id": boot_volume_id,
        }

    def _destroy_vm(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        import oci

        instance_row = self.db.one(
            "SELECT * FROM oci_compute_instances WHERE id=? AND owner_user_id=?",
            (int(payload["oci_instance_id"]), user_id),
        )
        if not instance_row:
            raise RuntimeError("OCI instance record not found")
        if not instance_row.get("instance_id"):
            raise RuntimeError("OCI instance record has no instance OCID")

        _, ctx = self._auth_context(user_id, instance_row.get("oci_auth_profile_id"))
        boot_volume_id = instance_row.get("boot_volume_id")
        with ctx:
            clients = _oci_clients(ctx)
            compute = clients["compute"]
            block = clients["block"]
            compute.terminate_instance(instance_row["instance_id"], preserve_boot_volume=False)
            terminated = oci.wait_until(
                compute,
                compute.get_instance(instance_row["instance_id"]),
                "lifecycle_state",
                "TERMINATED",
                max_wait_seconds=900,
                max_interval_seconds=20,
            ).data
            boot_cleanup = _delete_boot_volume_if_present(block, boot_volume_id)

        self.db.execute(
            """
            UPDATE oci_compute_instances
            SET lifecycle_state=?, destroyed_at=CURRENT_TIMESTAMP
            WHERE id=? AND owner_user_id=?
            """,
            (terminated.lifecycle_state, instance_row["id"], user_id),
        )
        return {
            "instance_id": instance_row["instance_id"],
            "lifecycle_state": terminated.lifecycle_state,
            "boot_volume_id": boot_volume_id,
            "boot_volume_cleanup": boot_cleanup,
        }

    def _create_vm_profile_for_instance(self, user_id: int, name: str, host: str, username: str, private_key: str) -> int:
        base = f"{name}-ssh"
        candidate = base
        suffix = 2
        while self.db.one("SELECT id FROM vm_profiles WHERE owner_user_id=? AND name=?", (user_id, candidate)):
            candidate = f"{base}-{suffix}"
            suffix += 1
        cur = self.db.execute(
            """
            INSERT INTO vm_profiles(owner_user_id, name, host, port, username, encrypted_private_key, encrypted_passphrase)
            VALUES (?, ?, ?, 22, ?, ?, NULL)
            """,
            (user_id, candidate, host, username, self.secrets.encrypt(private_key)),
        )
        return int(cur.lastrowid)

    def _wait_and_health_check(
        self,
        user_id: int,
        vm_profile_id: int,
        job_id: int | None = None,
        progress_base: int = 0,
        progress_span: int = 100,
    ) -> dict[str, Any]:
        last_error = ""
        for _ in range(18):
            try:
                return self._health_check(
                    user_id,
                    {
                        "vm_profile_id": vm_profile_id,
                        "_job_id": job_id,
                        "_progress_base": progress_base,
                        "_progress_span": progress_span,
                    },
                )
            except Exception as exc:
                last_error = redact(str(exc))
                time.sleep(10)
        self.db.execute(
            "UPDATE vm_profiles SET last_status='failed', last_checked_at=CURRENT_TIMESTAMP WHERE id=? AND owner_user_id=?",
            (vm_profile_id, user_id),
        )
        return {"ok": False, "message": f"SSH health check failed after waiting for cloud-init: {last_error}"}

    def _set_job_progress(
        self,
        job_id: int,
        percent: int,
        stage: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        progress = {
            "progress": {
                "percent": max(0, min(100, percent)),
                "stage": stage,
                "message": message,
                "details": details or {},
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        }
        self.db.execute(
            "UPDATE jobs SET result=? WHERE id=? AND status='running'",
            (json.dumps(progress, separators=(",", ":")), job_id),
        )


def _oci_clients(ctx: Any) -> dict[str, Any]:
    from .oci_ops import clients

    return clients(ctx)


def _wait_for_primary_vnic(compute: Any, network: Any, compartment_id: str, instance_id: str) -> Any:
    for _ in range(60):
        attachments = compute.list_vnic_attachments(compartment_id, instance_id=instance_id).data
        if attachments:
            primary = next((item for item in attachments if getattr(item, "is_primary", False)), attachments[0])
            return network.get_vnic(primary.vnic_id).data
        time.sleep(5)
    raise RuntimeError("Timed out waiting for the instance VNIC")


def _first_boot_volume_id(compute: Any, compartment_id: str, availability_domain: str, instance_id: str) -> str | None:
    for _ in range(60):
        attachments = compute.list_boot_volume_attachments(
            availability_domain=availability_domain,
            compartment_id=compartment_id,
            instance_id=instance_id,
        ).data
        if attachments:
            return attachments[0].boot_volume_id
        time.sleep(5)
    return None


def _delete_boot_volume_if_present(block: Any, boot_volume_id: str | None) -> dict[str, Any]:
    if not boot_volume_id:
        return {"status": "not_recorded"}
    try:
        volume = block.get_boot_volume(boot_volume_id).data
    except Exception as exc:
        return {"status": "not_found_or_already_deleted", "detail": redact(str(exc))}
    if getattr(volume, "lifecycle_state", None) not in {"AVAILABLE", "FAULTY"}:
        return {"status": "preserve_false_requested", "lifecycle_state": getattr(volume, "lifecycle_state", None)}
    block.delete_boot_volume(boot_volume_id)
    return {"status": "delete_requested"}
