# AI Inference Benchmark Control

Initial containerized control plane for OCI VM provisioning, SSH-based vLLM setup, and benchmark execution on Ubuntu hosts.

## Run Locally

```bash
cd apps/benchmark-control
export APP_SECRET="$(openssl rand -base64 32)"
export APP_LOCAL_USER_EMAIL="operator@example.com"
docker compose up --build
```

Open `http://localhost:18080` unless you set a different `APP_PORT`.

## What This Version Does

- Registers and logs in multiple users.
- Starts a local UI session automatically for the configured `APP_LOCAL_USER_EMAIL`.
- Provides an Authentication tab with OCI user-profile and instance-principal validation modes through the OCI Python SDK.
- Stores validated OCI auth profiles per local user for later compartment, image, VCN, subnet, launch, and destroy operations.
- Lets the VM workflow choose which saved OCI auth profile should load compartments, networks, images, and launch requests.
- Creates guide-approved OCI VMs or stores user-scoped existing VM profiles with encrypted SSH private keys uploaded as files.
- Stores user-scoped vLLM runtime profiles.
- Runs full SSH health checks against an existing Ubuntu host, expands the root filesystem, and installs missing vLLM/GuideLLM dependencies.
- Runs a lightweight periodic SSH status check every 30 seconds from the UI so decommissioned VMs stop showing as healthy.
- Starts and stops vLLM on the selected remote host with the saved profile.
- Shows live vLLM processes discovered over SSH from the selected VM.
- Provides benchmark workload presets from the initial guide, plus a custom profile option.
- Runs GuideLLM benchmarks against the remote vLLM OpenAI-compatible endpoint using fixed concurrency or sweep mode.
- Persists job state, GuideLLM logs, GuideLLM JSON/CSV/HTML artifacts, and downloadable report formats.
- Allows stale VM profiles, vLLM profiles, and completed or queued jobs to be deleted from the UI.

## Required Remote Host Preconditions

The remote host must have:

- For new VMs: a validated OCI auth profile, selected compartment/AD/network/image values, and an SSH public key.
- For existing VMs: SSH reachable from the container host and an uploaded private key file.
- `python3`, `pip`, and `curl`; missing packages are installed during SSH validation on Ubuntu hosts.
- `vllm==0.31.0+cpu` with the vendor-appropriate backend (AMD: ZenTorch 2.13.0.1 / native ZenDNN 6.0.1; Intel: CpuPlatform) available in the managed environment for runtime start; missing, wrong-version, or non-CPU-ready vLLM is repaired into a managed SSH-user virtual environment at `~/.aig-vllm` using the release-specific CPU wheel index.
- `guidellm==0.8.0` installed as the benchmark library readiness check; missing or wrong-version GuideLLM is installed into the same managed runtime environment during SSH validation.
- An unencrypted private key for this initial build. Passphrase-protected keys are rejected explicitly rather than handled insecurely.

Benchmark execution is GuideLLM-only. The app does not include a custom benchmark runner or fallback client. If GuideLLM, vLLM, or the selected endpoint cannot run the requested scenario, the job fails with the captured GuideLLM stdout/stderr.

## No Fallback Policy

This app does not generate simulated benchmark results, does not silently switch endpoints, and does not use fallback model names, shapes, images, hosts, or credentials. All VM, model, and runtime values must come from user-created profiles or request payloads.

## VM Provisioning

The VM Connection tab supports creating a new OCI VM or using an existing VM. New VMs are limited to the shapes from the initial guide: `VM.Standard.E6.Ax.Flex`, `VM.Standard4.Ax.Flex`, `BM.Standard.E6.Ax`, and `BM.Standard4.Ax`. Defaults are 16 OCPU, 64 GB memory, 200 GB boot volume, Canonical Ubuntu 24.04, and `ubuntu`.

During validation, the worker expands the root partition/filesystem to consume the available boot disk, installs missing runtime packages, repairs the managed CPU vLLM environment when needed, and records root disk, vLLM, and GuideLLM status in the VM Setup Status table.

The VM, vLLM, and benchmark pages use explicit dropdowns for their targets. A highlighted table row is useful for inspection and deletion, but runtime and benchmark actions use the selected dropdown values.

## Runtime And Benchmarking

The vLLM tab has a VM selector, saved runtime profile table, and live process table. `Start Runtime` enqueues a runtime-start job for the selected VM/profile pair. `Stop Selected Runtime` enqueues a stop job for the selected process discovered on that VM.

The Benchmarking tab has separate VM and vLLM profile dropdowns. Workload presets populate input tokens, output tokens, requests, and recommended concurrency values. Fixed concurrency mode maps those values to a GuideLLM `concurrent` profile with `streams=[...]`. Sweep mode maps the largest selected concurrency to GuideLLM `sweep.max_concurrency` and uses a Poisson or constant sweep strategy to identify the safe operating range.

The Report tab renders successful benchmark jobs as a normalized table built from GuideLLM output. CSV, JSON, and HTML downloads are GuideLLM-generated artifacts; Markdown is a compact app rendering of the normalized GuideLLM rows for copying.

See [docs/vm-provisioning.md](docs/vm-provisioning.md) for lifecycle and cleanup details.

## Local Validation Completed

- Container image builds locally as `aig-benchmark-control:local`.
- Unit tests pass inside the image.
- The app runs locally as `aig-benchmark-control-dev`.
- API health endpoint returns `{"status":"ok"}`.
- Local UI session bootstrap works without register/login.
- Authentication validation endpoint returns explicit OCI Python SDK auth results.
- User Profile validation requires an explicit OCI config profile header and uploaded matching private key.
- Registration, login, VM profile creation, vLLM profile creation, ownership rejection, queued job processing, and failed benchmark report rejection were tested against the running container.
- Live SSH/vLLM benchmark validation is pending your Ubuntu host details and model name.

## Model precision

Version 0.2.1 adds None, INT8 and INT4 to vLLM profiles. Quantized profiles derive a checkpoint from the original model and pinned Hugging Face commit; the public served-model name stays unchanged. Leaving the revision blank resolves and records an immutable source commit before conversion. Quantized modes use BF16 for remaining floating-point layers.

INT8 uses signed 8-bit decoder attention/MLP weights, per-output-channel scales and dynamic per-token 8-bit activations (W8A8). INT4 uses packed signed 4-bit decoder weights with group size 128 and BF16 activations (W4A16). Embeddings, normalization, output heads and Qwen vision layers retain their original values. The converter currently supports Qwen/Qwen3-VL-8B-Instruct, Qwen/Qwen3-4B-Base, mistralai/Mistral-7B-v0.1 and google/gemma-7b. It validates layer coverage, storage, scales, source identity and checkpoint hashes before launch, and never falls back to an unquantized checkpoint.

Leave KV cache GiB blank for automatic CPU KV allocation. Leave Max errors blank to omit the GuideLLM error-stop constraint; request-count and duration limits remain active. Existing profiles preserve their previous numeric cache and error settings.

The CPU installer pins vLLM 0.31.0+cpu and Torch 2.13.0+cpu. CPU Triton 3.8.0+cpu is obtained from the direct wheel URL declared by vLLM rather than a separate package-index lookup, and its installed version is verified. Quantization accuracy is not measured by the performance benchmark.


## CPU vendor selection

The default CPU runtime uses vLLM 0.31.0+cpu, PyTorch 2.13.0+cpu and CPU
Triton 3.8.0+cpu on Ubuntu 24.04/Python 3.12. Hardware is detected inside the
VM; another backend is never silently substituted.

- AMD requires exposed AVX-512, ZenTorch 2.13.0.1, native ZenDNN 6.0.1 build
  metadata and native ZenCpuPlatform. The pinned PyPI wheel is installed and
  its native build metadata and binary SHA-256 hashes are recorded.
- Intel requires exposed AVX2 and native CpuPlatform. ZenTorch is not installed
  by Intel setup. Capability evidence includes AVX-512, BF16 and AMX flags.
- Other vendors or missing required instructions fail setup explicitly.

The original approved AMD E6 and Intel Standard4 shapes are available again.
E6 remains the new-VM default. Intel AMX capability is detected and recorded,
while PyTorch, oneDNN and vLLM select applicable kernels automatically. The
control app does not force vLLM's experimental `VLLM_CPU_SGL_KERNEL` path.
Operator-level AMX use still depends on the model, precision, shapes and runtime
dispatch, so confirm it separately when measuring kernel behavior.

Setup and launch share tcmalloc/LLVM OpenMP 18 preload and compile settings.
Healthy managed environments are preserved and GuideLLM uses the same package
constraints. AMD native metadata mismatches fail rather than assume a version.

Rebuild the control container, run full VM validation and restart existing
runtimes before benchmarking. A launch verifier runs inside the server process,
records detected vendor/platform/version/AMX capabilities and process identity, then
invokes the vLLM CLI. Benchmarks require matching activation evidence from a
live server and include it in job results and GuideLLM artifact labels.
BF16 remains the default; validate quantized model paths separately.

## Generated reports (0.3.0)

The **Reports** tab queues MLCommons-style offline report collections with four performance charts, configuration detail pages, side-by-side concurrency/retry tables, evidence warnings, and CSV/JSON/ZIP downloads. Finished failed attempts can be included. **Native Report** retains the existing GuideLLM exports.

Docker Compose starts a dedicated `report-worker` service using the shared data volume. Single-process local development defaults to an independent embedded reporting thread. Reports use immutable evidence snapshots and do not require live benchmark VMs. Restart existing inference runtimes after upgrading before running new benchmarks: serving-model/precision matching is now required.

See [Reports module](docs/reporting.md) for usage, queue recovery, artifact storage, API endpoints and measurement limitations. Native timing is not independently validated by this first version; reconstructed throughput is withheld.

## Kernel tracing (0.3.1)

Save a separate runtime profile with **Enable kernel tracing (diagnostic only)** checked, restart the runtime with that profile, then use **Capture Kernel Trace**. Select its `trace_runtime` job in **Queue** to download the worker traces, execution log and operator summary. Tracing defaults to off and trace-enabled profiles cannot run performance benchmarks. See [Kernel tracing](docs/kernel-tracing.md) for the capture sequence, evidence interpretation and limits.

Completed reports can be archived automatically to a private OCI Object Storage bucket with verified checksums and expiring download links that work while the app is off. See [private OCI report archive configuration](docs/reporting.md#private-oci-report-archive).
