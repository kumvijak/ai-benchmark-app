from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Annotated, Any, Literal
import urllib.request

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .benchmark import normalize_guidellm_result, report_csv, report_html, report_markdown
from .config import get_settings
from .db import Database
from .oci_ops import DEFAULT_VM_DEPLOYMENT, DOC_APPROVED_SHAPES, context_from_saved_auth, list_accessible_compartments, principal_signer, profile_text_with_key_file, signer_tenancy_id, ubuntu_2404_images
from .security import SecretBox, create_token, hash_password, redact, verify_password
from .ssh_client import HEALTH_SCRIPT, SshClient, VmProfile, parse_health
from .worker import Worker

settings = get_settings()
db = Database(settings.db_path)
db.init()
secret_box = SecretBox(settings.app_secret)
worker = Worker(db, secret_box)

app = FastAPI(title="AI Inference Benchmark Control", version="0.2.1")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthValidationRequest(BaseModel):
    mode: str
    profile_text: str | None = None
    private_key: str | None = None
    profile_name: str | None = None


class VmProfileRequest(BaseModel):
    name: str
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
    private_key: str
    passphrase: str | None = None


class OciVmRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    oci_auth_profile_id: int | None = None
    compartment_id: str
    availability_domain: str
    shape: str
    ocpus: float = Field(default=16, gt=0)
    memory_gb: float = Field(default=64, gt=0)
    boot_volume_gb: int = Field(default=200, ge=50)
    image_id: str
    subnet_id: str
    assign_public_ip: bool = True
    username: str = "ubuntu"
    ssh_public_key: str
    ssh_private_key: str | None = None


class VllmProfileRequest(BaseModel):
    name: str
    model_ref: str
    dtype: Literal['bfloat16', 'float32', 'float16', 'auto'] = "bfloat16"
    quantization: Literal['none', 'int8', 'int4'] = 'none'
    model_revision: str | None = Field(default=None, min_length=1)
    max_model_len: int = Field(default=4096, ge=1)
    max_num_batched_tokens: int | None = Field(default=None, ge=1)
    max_num_seqs: int = Field(default=8, ge=1)
    cpu_kvcache_space_gib: int | None = Field(default=8, ge=1)
    reserved_cpu: int = Field(default=2, ge=0)
    prefix_cache_enabled: bool = False
    chunked_prefill_enabled: bool = False
    request_timeout_seconds: int = Field(default=30, ge=1)
    port: int = Field(default=8000, ge=1, le=65535)

    @model_validator(mode='after')
    def validate_quantized_dtype(self):
        if self.quantization != 'none' and self.dtype != 'bfloat16':
            raise ValueError('CPU INT8/INT4 profiles require bfloat16 for floating-point layers and outputs')
        return self


class StartRuntimeRequest(BaseModel):
    vm_profile_id: int
    vllm_profile_id: int
    readiness_timeout_seconds: int = Field(default=900, ge=10)


class StopRuntimeRequest(BaseModel):
    vm_profile_id: int
    pid: int | None = Field(default=None, ge=1)
    port: int | None = Field(default=None, ge=1, le=65535)


class BenchmarkRequest(BaseModel):
    vm_profile_id: int
    vllm_profile_id: int
    workload_name: str
    input_tokens: int = Field(ge=1)
    output_tokens: int = Field(ge=1)
    concurrency: int | None = Field(default=None, ge=1)
    concurrencies: list[int] | None = None
    max_requests: int = Field(ge=1, le=10000)
    max_errors: int | None = Field(default=5, ge=1)
    benchmark_mode: str = "concurrent"
    max_duration_seconds: int = Field(default=600, ge=1, le=86400)
    cooldown_seconds: int = Field(default=5, ge=0, le=3600)
    seed: int = 42
    sweep_size: int = Field(default=10, ge=3, le=100)
    sweep_strategy_type: str = "poisson"


def current_user(
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    if token:
        bearer = token
    elif authorization and authorization.startswith("Bearer "):
        bearer = authorization.removeprefix("Bearer ").strip()
    else:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    row = db.one(
        "SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token=?",
        (bearer,),
    )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid token")
    return row


@app.on_event("startup")
def start_worker() -> None:
    worker.start()


@app.on_event("shutdown")
def stop_worker() -> None:
    worker.stop()


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": app.version}


@app.post("/api/local-session")
def local_session() -> dict[str, Any]:
    email = settings.local_user_email.lower()
    user = db.one("SELECT * FROM users WHERE email=?", (email,))
    if not user:
        cur = db.execute(
            "INSERT INTO users(email, password_hash) VALUES (?, ?)",
            (email, hash_password(create_token())),
        )
        user = {"id": int(cur.lastrowid), "email": email, "role": "user"}
    token = create_token()
    db.execute("INSERT INTO sessions(token, user_id) VALUES (?, ?)", (token, user["id"]))
    return {"token": token, "user": {"id": user["id"], "email": user["email"], "role": user["role"]}}


@app.post("/api/auth/validate")
def validate_auth(req: AuthValidationRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if req.mode not in {"user_profile", "instance_principal"}:
        raise HTTPException(status_code=400, detail="mode must be user_profile or instance_principal")
    if req.mode == "instance_principal":
        result = _validate_instance_principal()
        if result.get("ok"):
            _save_auth_profile(user["id"], req.mode, req.profile_name or "instance-principal", None, None, result)
        return result
    if not req.profile_name:
        raise HTTPException(status_code=400, detail="profile_name is required for user profile auth")
    if not req.profile_text or not req.private_key:
        raise HTTPException(status_code=400, detail="profile_text and private_key are required for user profile auth")
    with tempfile.TemporaryDirectory(prefix="aig-oci-auth-") as tmp:
        config_path = Path(tmp) / "config"
        key_path = Path(tmp) / "oci_api_key.pem"
        key_path.write_text(req.private_key)
        os.chmod(key_path, 0o600)
        config_text = profile_text_with_key_file(req.profile_text, req.profile_name, key_path)
        config_path.write_text(config_text)
        result = _validate_user_profile(config_path, req.profile_name)
        if result.get("ok"):
            _save_auth_profile(user["id"], req.mode, req.profile_name, req.profile_text, req.private_key, result)
        return result


@app.post("/api/register")
def register(req: RegisterRequest) -> dict[str, Any]:
    try:
        cur = db.execute(
            "INSERT INTO users(email, password_hash) VALUES (?, ?)",
            (req.email.lower(), hash_password(req.password)),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail="User already exists or registration failed") from exc
    token = create_token()
    db.execute("INSERT INTO sessions(token, user_id) VALUES (?, ?)", (token, int(cur.lastrowid)))
    return {"token": token, "user": {"id": int(cur.lastrowid), "email": req.email.lower(), "role": "user"}}


@app.post("/api/login")
def login(req: LoginRequest) -> dict[str, Any]:
    user = db.one("SELECT * FROM users WHERE email=?", (req.email.lower(),))
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token()
    db.execute("INSERT INTO sessions(token, user_id) VALUES (?, ?)", (token, user["id"]))
    return {"token": token, "user": {"id": user["id"], "email": user["email"], "role": user["role"]}}


@app.get("/api/me")
def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return {"id": user["id"], "email": user["email"], "role": user["role"]}


@app.post("/api/vm-profiles")
def create_vm_profile(req: VmProfileRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    cur = db.execute(
        """
        INSERT INTO vm_profiles(owner_user_id, name, host, port, username, encrypted_private_key, encrypted_passphrase)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["id"], req.name, req.host, req.port, req.username,
            secret_box.encrypt(req.private_key), secret_box.encrypt(req.passphrase),
        ),
    )
    db.audit(user["id"], "created", "vm_profile", int(cur.lastrowid), {"name": req.name, "host": req.host})
    return {"id": int(cur.lastrowid)}


@app.get("/api/vm-profiles")
def list_vm_profiles(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    return db.all(
        "SELECT id, name, host, port, username, last_status, last_checked_at FROM vm_profiles WHERE owner_user_id=? ORDER BY id",
        (user["id"],),
    )


@app.delete("/api/vm-profiles/{profile_id}")
def delete_vm_profile(profile_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _assert_owned("vm_profiles", profile_id, user["id"])
    db.execute(
        "UPDATE oci_compute_instances SET vm_profile_id=NULL WHERE vm_profile_id=? AND owner_user_id=?",
        (profile_id, user["id"]),
    )
    db.execute("DELETE FROM vm_setup_status WHERE vm_profile_id=? AND owner_user_id=?", (profile_id, user["id"]))
    db.execute("DELETE FROM vm_profiles WHERE id=? AND owner_user_id=?", (profile_id, user["id"]))
    db.audit(user["id"], "deleted", "vm_profile", profile_id, {})
    return {"deleted": True, "id": profile_id}


@app.get("/api/vm-setup-status")
def list_vm_setup_status(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    return db.all(
        """
        SELECT s.vm_profile_id, v.name, v.host, s.root_expand_status, s.root_device,
               s.root_usage_before, s.root_usage_after, s.vllm_status, s.vllm_version,
               s.benchmark_status, s.benchmark_version, s.runtime_evidence, s.python_path, s.pip_path, s.curl_path, s.last_setup_at
        FROM vm_setup_status s
        JOIN vm_profiles v ON v.id=s.vm_profile_id
        WHERE s.owner_user_id=?
        ORDER BY s.last_setup_at DESC
        """,
        (user["id"],),
    )


@app.get("/api/oci/auth-profiles")
def list_oci_auth_profiles(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    return db.all(
        """
        SELECT id, mode, profile_name, region, tenancy_id, last_status, last_validated_at, updated_at
        FROM oci_auth_profiles WHERE owner_user_id=? ORDER BY updated_at DESC, id DESC
        """,
        (user["id"],),
    )


@app.post("/api/oci/auth-profiles/{profile_id}/validate")
def validate_saved_oci_auth_profile(profile_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    row = db.one("SELECT * FROM oci_auth_profiles WHERE id=? AND owner_user_id=?", (profile_id, user["id"]))
    if not row:
        raise HTTPException(status_code=404, detail="Authentication profile not found")
    result = _validate_saved_auth_profile(row)
    _update_auth_profile_validation(profile_id, user["id"], result)
    return result


@app.delete("/api/oci/auth-profiles/{profile_id}")
def delete_oci_auth_profile(profile_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    row = db.one("SELECT id FROM oci_auth_profiles WHERE id=? AND owner_user_id=?", (profile_id, user["id"]))
    if not row:
        raise HTTPException(status_code=404, detail="Authentication profile not found")
    db.execute(
        "UPDATE oci_compute_instances SET oci_auth_profile_id=NULL WHERE owner_user_id=? AND oci_auth_profile_id=?",
        (user["id"], profile_id),
    )
    db.execute("DELETE FROM oci_auth_profiles WHERE id=? AND owner_user_id=?", (profile_id, user["id"]))
    db.audit(user["id"], "deleted", "oci_auth_profile", profile_id, {})
    return {"deleted": True, "id": profile_id}


@app.get("/api/oci/defaults")
def oci_defaults(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return {
        "approved_shapes": list(DOC_APPROVED_SHAPES),
        "default_deployment": DEFAULT_VM_DEPLOYMENT,
    }


@app.get("/api/oci/compartments")
def oci_compartments(user: dict[str, Any] = Depends(current_user), auth_profile_id: int | None = None) -> list[dict[str, Any]]:
    auth, ctx = _auth_context(user["id"], auth_profile_id)
    with ctx:
        clients = _oci_clients(ctx)
        tenancy_id = ctx.config.get("tenancy") or auth.get("tenancy_id")
        if not tenancy_id:
            raise HTTPException(status_code=400, detail="Tenancy OCID is unavailable for this auth mode")
        return list_accessible_compartments(clients["identity"], tenancy_id)


@app.get("/api/oci/availability-domains")
def oci_availability_domains(compartment_id: str, user: dict[str, Any] = Depends(current_user), auth_profile_id: int | None = None) -> list[dict[str, Any]]:
    _, ctx = _auth_context(user["id"], auth_profile_id)
    with ctx:
        ads = _oci_clients(ctx)["identity"].list_availability_domains(compartment_id).data
        return [{"name": ad.name, "id": getattr(ad, "id", None)} for ad in ads]


@app.get("/api/oci/vcns")
def oci_vcns(compartment_id: str, user: dict[str, Any] = Depends(current_user), auth_profile_id: int | None = None) -> list[dict[str, Any]]:
    _, ctx = _auth_context(user["id"], auth_profile_id)
    with ctx:
        vcns = _oci_clients(ctx)["network"].list_vcns(compartment_id=compartment_id).data
        return [
            {"id": vcn.id, "display_name": vcn.display_name, "cidr_block": getattr(vcn, "cidr_block", None), "lifecycle_state": vcn.lifecycle_state}
            for vcn in vcns if vcn.lifecycle_state == "AVAILABLE"
        ]


@app.get("/api/oci/subnets")
def oci_subnets(compartment_id: str, vcn_id: str, user: dict[str, Any] = Depends(current_user), auth_profile_id: int | None = None) -> list[dict[str, Any]]:
    _, ctx = _auth_context(user["id"], auth_profile_id)
    with ctx:
        subnets = _oci_clients(ctx)["network"].list_subnets(compartment_id=compartment_id, vcn_id=vcn_id).data
        return [
            {
                "id": subnet.id,
                "display_name": subnet.display_name,
                "cidr_block": subnet.cidr_block,
                "prohibit_public_ip_on_vnic": subnet.prohibit_public_ip_on_vnic,
                "lifecycle_state": subnet.lifecycle_state,
            }
            for subnet in subnets if subnet.lifecycle_state == "AVAILABLE"
        ]


@app.get("/api/oci/images")
def oci_images(compartment_id: str, user: dict[str, Any] = Depends(current_user), auth_profile_id: int | None = None) -> list[dict[str, Any]]:
    _, ctx = _auth_context(user["id"], auth_profile_id)
    with ctx:
        return ubuntu_2404_images(_oci_clients(ctx)["compute"], compartment_id)


@app.get("/api/oci/shapes")
def oci_shapes(_: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    return [{"name": shape, "flex": shape.endswith(".Flex")} for shape in DOC_APPROVED_SHAPES]


@app.post("/api/oci/instances")
def create_oci_instance(req: OciVmRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if req.shape not in DOC_APPROVED_SHAPES:
        raise HTTPException(status_code=400, detail="Shape is not approved by the initial benchmark guide")
    job_id = db.create_job(user["id"], "provision_vm", req.model_dump())
    db.audit(user["id"], "enqueued", "provision_vm", job_id, {"display_name": req.display_name, "shape": req.shape})
    return {"job_id": job_id}


@app.get("/api/oci/instances")
def list_oci_instances(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    return db.all(
        """
        SELECT id, vm_profile_id, display_name, compartment_id, availability_domain, subnet_id,
               instance_id, boot_volume_id, shape, ocpus, memory_gb, boot_volume_gb, image_id,
               assign_public_ip, public_ip, private_ip, lifecycle_state, created_at, destroyed_at
        FROM oci_compute_instances WHERE owner_user_id=? ORDER BY id DESC
        """,
        (user["id"],),
    )


@app.post("/api/oci/instances/{instance_record_id}/destroy")
def destroy_oci_instance(instance_record_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    row = db.one("SELECT id, lifecycle_state, destroyed_at FROM oci_compute_instances WHERE id=? AND owner_user_id=?", (instance_record_id, user["id"]))
    if not row:
        raise HTTPException(status_code=404, detail="OCI instance record not found")
    if row.get("destroyed_at") or row.get("lifecycle_state") in {"TERMINATING", "TERMINATED"}:
        raise HTTPException(status_code=400, detail="OCI instance record is already destroyed or terminating")
    job_id = db.create_job(user["id"], "destroy_vm", {"oci_instance_id": instance_record_id})
    db.audit(user["id"], "enqueued", "destroy_vm", job_id, {"oci_instance_id": instance_record_id})
    return {"job_id": job_id}


@app.post("/api/vm-profiles/{profile_id}/health-checks")
def enqueue_health_check(profile_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if not db.one("SELECT id FROM vm_profiles WHERE id=? AND owner_user_id=?", (profile_id, user["id"])):
        raise HTTPException(status_code=404, detail="VM profile not found")
    job_id = db.create_job(user["id"], "health_check", {"vm_profile_id": profile_id})
    db.audit(user["id"], "enqueued", "health_check", job_id, {"vm_profile_id": profile_id})
    return {"job_id": job_id}


@app.post("/api/vm-profiles/status-checks")
def quick_vm_status_checks(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT id, host, port, username, encrypted_private_key, encrypted_passphrase FROM vm_profiles WHERE owner_user_id=? ORDER BY id",
        (user["id"],),
    )
    results: list[dict[str, Any]] = []
    for row in rows:
        status = "unreachable"
        detail = ""
        try:
            profile = VmProfile(
                host=row["host"],
                port=int(row["port"]),
                username=row["username"],
                private_key=secret_box.decrypt(row["encrypted_private_key"]) or "",
                passphrase=secret_box.decrypt(row["encrypted_passphrase"]),
            )
            with SshClient(profile, timeout=5) as ssh:
                out = ssh.run_checked(HEALTH_SCRIPT, timeout=12)
            health = parse_health(out)
            status = "healthy" if health.get("ok") else "failed"
            detail = ",".join(health.get("missing_required", []))
        except Exception as exc:
            detail = redact(str(exc))[-500:]
        db.execute(
            "UPDATE vm_profiles SET last_status=?, last_checked_at=CURRENT_TIMESTAMP WHERE id=? AND owner_user_id=?",
            (status, row["id"], user["id"]),
        )
        results.append({"id": row["id"], "status": status, "detail": detail})
    return results


@app.post("/api/vllm-profiles")
def create_vllm_profile(req: VllmProfileRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    cur = db.execute(
        """
        INSERT INTO vllm_profiles(
          owner_user_id, name, model_ref, dtype, max_model_len, max_num_batched_tokens,
          max_num_seqs, cpu_kvcache_space_gib, reserved_cpu, prefix_cache_enabled,
          chunked_prefill_enabled, request_timeout_seconds, port, quantization,
          model_revision, cpu_kvcache_auto
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["id"], req.name, req.model_ref, req.dtype, req.max_model_len, req.max_num_batched_tokens,
            req.max_num_seqs, req.cpu_kvcache_space_gib or 0, req.reserved_cpu,
            int(req.prefix_cache_enabled), int(req.chunked_prefill_enabled), req.request_timeout_seconds, req.port,
            req.quantization, req.model_revision, int(req.cpu_kvcache_space_gib is None),
        ),
    )
    db.audit(user["id"], "created", "vllm_profile", int(cur.lastrowid), {"name": req.name, "model_ref": req.model_ref})
    return {"id": int(cur.lastrowid)}


@app.get("/api/vllm-profiles")
def list_vllm_profiles(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    return db.all(
        """
        SELECT id, name, model_ref, dtype, max_model_len, max_num_batched_tokens, max_num_seqs,
               CASE WHEN cpu_kvcache_auto=1 THEN NULL ELSE cpu_kvcache_space_gib END AS cpu_kvcache_space_gib,
               reserved_cpu, prefix_cache_enabled, chunked_prefill_enabled,
               request_timeout_seconds, port, quantization, model_revision
        FROM vllm_profiles WHERE owner_user_id=? ORDER BY id
        """,
        (user["id"],),
    )


@app.delete("/api/vllm-profiles/{profile_id}")
def delete_vllm_profile(profile_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _assert_owned("vllm_profiles", profile_id, user["id"])
    db.execute("DELETE FROM vllm_profiles WHERE id=? AND owner_user_id=?", (profile_id, user["id"]))
    db.audit(user["id"], "deleted", "vllm_profile", profile_id, {})
    return {"deleted": True, "id": profile_id}


@app.post("/api/runtime/start")
def start_runtime(req: StartRuntimeRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _assert_owned("vm_profiles", req.vm_profile_id, user["id"])
    _assert_owned("vllm_profiles", req.vllm_profile_id, user["id"])
    job_id = db.create_job(user["id"], "start_runtime", req.model_dump())
    return {"job_id": job_id}


@app.get("/api/runtime/processes")
def list_runtime_processes(vm_profile_id: int, user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    _assert_owned("vm_profiles", vm_profile_id, user["id"])
    profile = _vm_profile_for_user(vm_profile_id, user["id"])
    script = """
import json, os, re, subprocess
rows = []
proc = subprocess.run(["pgrep", "-af", "[v]llm serve|[v]llm-launch-[0-9]+[.]py serve"], text=True, capture_output=True)
for line in proc.stdout.splitlines():
    parts = line.split(" ", 1)
    if not parts:
        continue
    pid = parts[0]
    command = parts[1] if len(parts) > 1 else ""
    port = None
    match = re.search(r"(?:--port\\s+|--port=)(\\d+)", command)
    if match:
        port = int(match.group(1))
    log_path = f"/tmp/aig-benchmark-control/vllm-{port}.log" if port else ""
    rows.append({
        "pid": int(pid),
        "port": port,
        "command": command,
        "log_path": log_path,
        "alive": os.path.exists(f"/proc/{pid}") if os.path.exists("/proc") else True,
    })
print(json.dumps(rows))
"""
    with SshClient(profile, timeout=8) as ssh:
        out = ssh.run_checked(f"python3 - <<'PY'\n{script}\nPY", timeout=15)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not parse remote runtime process list: {redact(str(exc))}") from exc


@app.post("/api/runtime/stop")
def stop_runtime(req: StopRuntimeRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _assert_owned("vm_profiles", req.vm_profile_id, user["id"])
    if not req.pid and not req.port:
        raise HTTPException(status_code=400, detail="Provide a pid or port to stop a vLLM runtime")
    job_id = db.create_job(user["id"], "stop_runtime", req.model_dump())
    return {"job_id": job_id}


@app.post("/api/benchmarks")
def run_benchmark(req: BenchmarkRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _assert_owned("vm_profiles", req.vm_profile_id, user["id"])
    _assert_owned("vllm_profiles", req.vllm_profile_id, user["id"])
    if req.benchmark_mode not in {"concurrent", "sweep"}:
        raise HTTPException(status_code=400, detail="benchmark_mode must be concurrent or sweep")
    if req.sweep_strategy_type not in {"constant", "poisson"}:
        raise HTTPException(status_code=400, detail="sweep_strategy_type must be constant or poisson")
    concurrencies = req.concurrencies or ([req.concurrency] if req.concurrency is not None else [])
    concurrencies = sorted({int(value) for value in concurrencies if int(value) > 0})
    if not concurrencies:
        raise HTTPException(status_code=400, detail="Select at least one concurrency")
    payload = req.model_dump()
    payload["concurrencies"] = concurrencies
    payload["concurrency"] = concurrencies[0]
    job_id = db.create_job(user["id"], "run_benchmark", payload)
    return {"job_id": job_id}


@app.get("/api/jobs")
def list_jobs(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    jobs = db.all(
        "SELECT id, job_type, status, result, error_message, created_at, started_at, finished_at FROM jobs WHERE owner_user_id=? ORDER BY id DESC LIMIT 100",
        (user["id"],),
    )
    return [_decode_job(j) for j in jobs]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    job = db.one(
        "SELECT id, job_type, status, result, error_message, created_at, started_at, finished_at FROM jobs WHERE id=? AND owner_user_id=?",
        (job_id, user["id"]),
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _decode_job(job)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    job = db.one("SELECT id, status FROM jobs WHERE id=? AND owner_user_id=?", (job_id, user["id"]))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] == "running":
        raise HTTPException(status_code=409, detail="Running jobs cannot be deleted")
    db.execute("DELETE FROM jobs WHERE id=? AND owner_user_id=?", (job_id, user["id"]))
    db.audit(user["id"], "deleted", "job", job_id, {})
    return {"deleted": True, "id": job_id}


@app.get("/api/reports/{job_id}.json")
def report_json(job_id: int, user: dict[str, Any] = Depends(current_user)) -> Response:
    result = _benchmark_result(job_id, user["id"])
    return Response(json.dumps(result, indent=2), media_type="application/json")


@app.get("/api/reports/{job_id}.csv")
def report_csv_endpoint(job_id: int, user: dict[str, Any] = Depends(current_user)) -> PlainTextResponse:
    return PlainTextResponse(report_csv(_benchmark_result(job_id, user["id"])), media_type="text/csv")


@app.get("/api/reports/{job_id}.md")
def report_markdown_endpoint(job_id: int, user: dict[str, Any] = Depends(current_user)) -> PlainTextResponse:
    return PlainTextResponse(report_markdown(_benchmark_result(job_id, user["id"])), media_type="text/markdown")


@app.get("/api/reports/{job_id}.html")
def report_html_endpoint(job_id: int, user: dict[str, Any] = Depends(current_user)) -> HTMLResponse:
    return HTMLResponse(report_html(_benchmark_result(job_id, user["id"])))


def _assert_owned(table: str, entity_id: int, user_id: int) -> None:
    if table not in {"vm_profiles", "vllm_profiles"}:
        raise RuntimeError("Invalid ownership table")
    if not db.one(f"SELECT id FROM {table} WHERE id=? AND owner_user_id=?", (entity_id, user_id)):
        raise HTTPException(status_code=404, detail="Entity not found")


def _vm_profile_for_user(profile_id: int, user_id: int) -> VmProfile:
    row = db.one(
        "SELECT host, port, username, encrypted_private_key, encrypted_passphrase FROM vm_profiles WHERE id=? AND owner_user_id=?",
        (profile_id, user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="VM profile not found")
    return VmProfile(
        host=row["host"],
        port=int(row["port"]),
        username=row["username"],
        private_key=secret_box.decrypt(row["encrypted_private_key"]) or "",
        passphrase=secret_box.decrypt(row["encrypted_passphrase"]),
    )


def _save_auth_profile(user_id: int, mode: str, profile_name: str, profile_text: str | None, private_key: str | None, result: dict[str, Any]) -> None:
    region = result.get("region")
    tenancy_id = result.get("tenancy_id")
    db.execute(
        """
        INSERT INTO oci_auth_profiles(
          owner_user_id, mode, profile_name, encrypted_profile_text, encrypted_private_key,
          region, tenancy_id, last_status, last_validated_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'validated', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(owner_user_id, mode, profile_name) DO UPDATE SET
          encrypted_profile_text=excluded.encrypted_profile_text,
          encrypted_private_key=excluded.encrypted_private_key,
          region=excluded.region,
          tenancy_id=excluded.tenancy_id,
          last_status='validated',
          last_validated_at=CURRENT_TIMESTAMP,
          updated_at=CURRENT_TIMESTAMP
        """,
        (
            user_id, mode, profile_name,
            secret_box.encrypt(profile_text),
            secret_box.encrypt(private_key),
            region, tenancy_id,
        ),
    )


def _update_auth_profile_validation(profile_id: int, user_id: int, result: dict[str, Any]) -> None:
    status = "validated" if result.get("ok") else "failed"
    db.execute(
        """
        UPDATE oci_auth_profiles
        SET region=COALESCE(?, region),
            tenancy_id=COALESCE(?, tenancy_id),
            last_status=?,
            last_validated_at=CURRENT_TIMESTAMP,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND owner_user_id=?
        """,
        (result.get("region"), result.get("tenancy_id"), status, profile_id, user_id),
    )


def _validate_saved_auth_profile(row: dict[str, Any]) -> dict[str, Any]:
    if row["mode"] == "instance_principal":
        result = _validate_instance_principal()
        result["profile_id"] = row["id"]
        result["profile_name"] = row["profile_name"]
        return result
    try:
        with context_from_saved_auth(row, secret_box.decrypt) as ctx:
            import oci

            identity = oci.identity.IdentityClient(ctx.config)
            user = identity.get_user(ctx.config["user"]).data
            tenancy_id = ctx.config.get("tenancy")
            subscriptions = identity.list_region_subscriptions(tenancy_id).data if tenancy_id else []
            return {
                "ok": True,
                "validator": "oci-python-sdk",
                "mode": row["mode"],
                "profile_id": row["id"],
                "profile_name": row["profile_name"],
                "message": "Saved authentication profile validated",
                "user_name": getattr(user, "name", None),
                "region": ctx.config.get("region"),
                "tenancy_id": tenancy_id,
                "home_region": next((r.region_name for r in subscriptions if getattr(r, "is_home_region", False)), None),
                "region_subscriptions": len(subscriptions),
            }
    except Exception as exc:
        return {
            "ok": False,
            "validator": "oci-python-sdk",
            "mode": row["mode"],
            "profile_id": row["id"],
            "profile_name": row["profile_name"],
            "message": _safe_error(exc),
        }


def _auth_context(user_id: int, auth_profile_id: int | None = None):
    if auth_profile_id:
        row = db.one("SELECT * FROM oci_auth_profiles WHERE id=? AND owner_user_id=?", (auth_profile_id, user_id))
    else:
        row = db.one(
            """
            SELECT * FROM oci_auth_profiles
            WHERE owner_user_id=? AND last_status='validated'
            ORDER BY updated_at DESC, id DESC LIMIT 1
            """,
            (user_id,),
        )
    if not row:
        raise HTTPException(status_code=400, detail="Validate OCI authentication before loading OCI resources")
    return row, context_from_saved_auth(row, secret_box.decrypt)


def _oci_clients(ctx: Any) -> dict[str, Any]:
    from .oci_ops import clients

    return clients(ctx)


def _decode_job(job: dict[str, Any]) -> dict[str, Any]:
    job = dict(job)
    if job.get("result"):
        job["result"] = json.loads(job["result"])
    return job


def _benchmark_result(job_id: int, user_id: int) -> dict[str, Any]:
    job = db.one("SELECT * FROM jobs WHERE id=? AND owner_user_id=? AND job_type='run_benchmark'", (job_id, user_id))
    if not job:
        raise HTTPException(status_code=404, detail="Benchmark job not found")
    if job["status"] != "succeeded" or not job["result"]:
        raise HTTPException(status_code=409, detail="Benchmark report is available only after success")
    result = json.loads(job["result"])
    if result.get("benchmark_command_kind") == "guidellm" or result.get("guidellm_json"):
        return normalize_guidellm_result(result)
    return result


def _validate_user_profile(config_path: Path, profile_name: str) -> dict[str, Any]:
    try:
        import oci

        config = oci.config.from_file(str(config_path), profile_name)
        oci.config.validate_config(config)
        identity = oci.identity.IdentityClient(config)
        user = identity.get_user(config["user"]).data
        tenancy_id = config.get("tenancy")
        subscriptions = identity.list_region_subscriptions(tenancy_id).data if tenancy_id else []
        return {
            "ok": True,
            "validator": "oci-python-sdk",
            "mode": "user_profile",
            "message": "User profile authentication validated",
            "user_name": getattr(user, "name", None),
            "region": config.get("region"),
            "tenancy_id": tenancy_id,
            "home_region": next((r.region_name for r in subscriptions if getattr(r, "is_home_region", False)), None),
            "region_subscriptions": len(subscriptions),
        }
    except Exception as exc:
        return {
            "ok": False,
            "validator": "oci-python-sdk",
            "mode": "user_profile",
            "message": _safe_error(exc),
        }


def _validate_instance_principal() -> dict[str, Any]:
    try:
        import oci

        principal_type = "resource_principal" if os.getenv("OCI_RESOURCE_PRINCIPAL_VERSION") else "instance_principal"
        if principal_type == "instance_principal":
            metadata_req = urllib.request.Request(
                "http://169.254.169.254/opc/v2/instance/",
                headers={"Authorization": "Bearer Oracle"},
            )
            try:
                urllib.request.urlopen(metadata_req, timeout=2).read(1)
            except Exception:
                return {
                    "ok": False,
                    "validator": "oci-python-sdk",
                    "mode": "instance_principal",
                    "principal_type": principal_type,
                    "message": "OCI instance metadata service is not reachable from this container",
                }
        signer = principal_signer()
        identity = oci.identity.IdentityClient(config={}, signer=signer)
        regions = identity.list_regions().data
        tenancy_id = signer_tenancy_id(signer)
        return {
            "ok": True,
            "validator": "oci-python-sdk",
            "mode": "instance_principal",
            "principal_type": principal_type,
            "message": "OCI principal authentication validated",
            "tenancy_id": tenancy_id,
            "regions": len(regions),
        }
    except Exception as exc:
        return {
            "ok": False,
            "validator": "oci-python-sdk",
            "mode": "instance_principal",
            "message": _safe_error(exc),
        }


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    for marker in ("-----BEGIN", "private_key", "key_file"):
        text = text.replace(marker, "[REDACTED]")
    return {
        "type": exc.__class__.__name__,
        "detail": text[-1800:],
    }.__str__()
