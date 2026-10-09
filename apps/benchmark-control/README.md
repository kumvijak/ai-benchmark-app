# AI Inference Benchmark Control

Containerized control plane for OCI VM provisioning, SSH-based vLLM setup, and GuideLLM benchmark execution on Ubuntu CPU hosts. The UI uses plain JavaScript, the API uses FastAPI, and a single background worker processes a SQLite job queue.

The image builds from a digest-pinned official Python base and installs its requirements, OCI SDK, and OpenSSH client explicitly. It does not depend on an older locally built application image.

## Run Locally

```bash
cd apps/benchmark-control
export APP_SECRET="$(openssl rand -base64 32)"
export APP_LOCAL_USER_EMAIL="operator@example.com"
docker compose up --build
```

Open `http://localhost:18080` unless you set a different `APP_PORT`. Compose binds to workstation loopback because this operator build supports automatic local session creation.

Keep `APP_SECRET` stable across restarts: it encrypts saved credentials. Compose stores application data in a named volume at `/data`; `docker compose down` keeps it, while `docker compose down --volumes` deletes it.

To stop: `docker compose down`. To inspect startup: `docker compose logs benchmark-control`.

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
- `vllm==0.30.0+cpu` available on `PATH` for runtime start; missing, wrong-version, or non-CPU-ready vLLM is repaired into a managed SSH-user virtual environment at `~/.aig-vllm` using the release-specific CPU wheel index.
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

## Container Verification

Verified on 9 October 2026 after folder cleanup:

- Fresh Docker build with explicit dependencies; Python compilation and `pip check` pass.
- 11 existing core tests pass inside the image.
- 4 HTTP integration tests pass: health/static assets/auth, profile ownership and deletion, worker failure processing, and report routes using explicit test fixtures.
- Compose startup and persistent profiles/sessions after restart verified with an isolated disposable project.
- JavaScript syntax check passes.

Run the checks after building:

```bash
docker compose build
docker run --rm --network none --entrypoint python aig-inference-app-v1:local -m unittest discover -s app/tests -v
docker run --rm --network none --entrypoint python aig-inference-app-v1:local -m app.tests.container_smoke
```

These local checks do not provision OCI resources or run remote models. Actual OCI authentication, new-VM lifecycle, remote SSH setup, vLLM startup, and real GuideLLM workloads require the selected tenancy/host/model and remain `NOT_RUN` in this verification. Test report fixtures are not benchmark results.

See [validation results](docs/container-validation.md) and the [application mind map](docs/application-mind-map.md). The root `plan.md` is a broader target design, not the implemented stack.
