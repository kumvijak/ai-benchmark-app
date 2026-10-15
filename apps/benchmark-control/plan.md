# AI Inference Benchmarking App Plan

## Purpose

Build a production-grade web application for configuring, launching, benchmarking, and reporting vLLM-based inference workloads on existing virtual machines. The app starts from the workflow in `LLM_Benchmark_Initial Config_Guide.docx`: connect to a VM by SSH, configure vLLM, run workload-first benchmarks, and generate tabular reports that can be copied or downloaded.

The first build phase targets OCI CPU inference for open-weight models up to 9B parameters, with vLLM as the default serving engine and GuideLLM as the primary benchmark runner. The architecture must support multiple authenticated users, separate per-user profiles, multiple VM connections, and queued benchmark execution.

## Current Implementation Status

An initial local container build now exists in `apps/benchmark-control`.

Implemented:

- Single-container FastAPI and static UI application
- Automatic local UI session for the first operator build
- Authentication tab with OCI user-profile and instance-principal validation modes through the OCI Python SDK
- Per-user VM and vLLM profiles
- Encrypted SSH key storage using `APP_SECRET`
- SQLite-backed durable job state
- In-process queued worker loop
- Real SSH health checks using OpenSSH
- Real remote vLLM start command execution
- Real OpenAI-compatible streaming benchmark execution against the remote vLLM endpoint
- Benchmark workload preset dropdowns from the initial guide, plus a custom workload option
- Markdown, CSV, and JSON report endpoints
- Local container image `aig-benchmark-control:local`
- Local running container `aig-benchmark-control-dev` on `http://127.0.0.1:18080`

The local initial build deliberately does not simulate metrics, hardcode a model, or silently fall back to alternate endpoints. Live success-path benchmark validation is pending the Ubuntu host and model name.

## Non Goals For The First Build

- No deployment will be started without explicit user approval.
- No simulated benchmark results will be presented as real results.
- No Kubernetes or OCI resource provisioning in phase one; the app works against existing VMs supplied by users.
- No runtime expansion beyond vLLM until the vLLM baseline is validated.
- No MLPerf implementation until its scope is clarified.

## Product Workflow

### Section 1 VM Connection

Users create a VM connection profile with:

- Profile name
- VM public or private IP or DNS name
- SSH port, default `22`
- SSH username
- Private key upload or pasted key
- Optional passphrase
- Optional jump host support in a later phase

The app validates:

- Key parseability
- TCP reachability
- SSH authentication
- Remote OS details
- Python availability
- CPU and memory capacity
- Disk space
- Required tools, including `curl`, `python3`, and runtime installation paths

The UI moves to the vLLM section only after the connection profile has a current successful health check.

### Section 2 vLLM Configuration

Users create reusable vLLM configuration profiles. The first supported parameters are:

| Parameter | UI Control | Initial Default | Notes |
| --- | --- | --- | --- |
| Model ID or local model path | Text input | User supplied | Hugging Face ID or remote local path |
| `--dtype` | Dropdown | `bfloat16` | CPU baseline default |
| `--max-model-len` | Dropdown or number input | `4096` | Sweep values: `2048`, `4096`, `8192` |
| `--max-num-batched-tokens` | Number input | `2048 * world_size` | Allow explicit override |
| `--max-num-seqs` | Dropdown or number input | `8` | Sweep values: `8`, `16`, `32` |
| `VLLM_CPU_KVCACHE_SPACE` | Number input with GiB unit | `40` | Stored as environment variable |
| `VLLM_CPU_NUM_OF_RESERVED_CPU` | Number input | `2` | Reserve frontend/framework cores |
| Prefix cache | Toggle | Off | Explicit test variable |
| Chunked prefill | Toggle | Off | Enable only for selected long-prefill tests |
| Request timeout | Number input | `30` seconds | Runtime request guardrail |
| OpenAI-compatible port | Number input | `8000` | Used for health checks and GuideLLM target |

The app must persist every benchmark with the exact vLLM command, environment variables, model identity, host identity, and configuration hash.

### Section 3 Benchmarking

Users create benchmark jobs from a VM profile, vLLM profile, model, and workload profile. The default workloads are:

| Workload | Input Tokens | Output Tokens | Purpose |
| --- | ---: | ---: | --- |
| Smoke | 128 | 64 | Endpoint health |
| Chat | 256 | 128 | TTFT and light prefill |
| Q&A | 512 | 128 | Practical CPU serving |
| Text generation | 128 | 1024 | Decode-heavy behavior |
| Document summary | 4096 | 512 | Prefill-heavy behavior |
| Agent planning | 2048 | 1536 | Combined prefill and decode pressure |

Concurrency streams start from the guide:

- Smoke: synchronous, 10 requests
- Chat: `1, 2, 4, 8, 16`
- Q&A: `1, 2, 4, 8`
- Text generation: `1, 2, 4`
- Document summary: `1, 2, 4`
- Agent planning: `1, 2, 4, 8`

Safe execution limits:

- `max_requests = 64`
- `max_duration = 600` seconds
- `cooldown_duration = 5` seconds between runs
- Abort policy for repeated failures
- Per-job timeout and cancellation support

Metrics to collect:

- Success requests
- Failed requests
- Request rate and achieved RPS
- Concurrency or streams
- Prompt tokens per request
- Output tokens per request
- TTFT
- ITL
- TPOT
- End-to-end latency
- Output tokens per second
- Total tokens per second
- Duration
- Seed, prompt/data profile, and cache state
- Server-side vLLM metrics snapshot from `/metrics` when available
- Remote CPU, memory, disk, process, and log snapshots

### Section 4 Report

The report page shows:

- Job summary
- VM profile
- vLLM profile
- Workload matrix
- Per-concurrency metrics table
- Failed request details
- Raw artifact links
- Configuration hash
- Reproduction command

Report actions:

- Copy table as Markdown
- Copy table as CSV
- Download CSV
- Download JSON
- Download Markdown report

## Architecture Decision

Use a controller and worker architecture:

- Frontend: React, TypeScript, Vite
- API: FastAPI
- Database: PostgreSQL
- Queue and live status: Redis
- Workers: Celery workers for SSH, vLLM lifecycle, GuideLLM execution, telemetry collection, and report generation
- SSH library: Paramiko or AsyncSSH, with Paramiko as the conservative first choice
- Benchmark runner: GuideLLM only, targeting the vLLM OpenAI-compatible endpoint and retaining GuideLLM JSON, CSV, and HTML artifacts as report sources
- Artifacts: local filesystem in development; object storage compatible abstraction for production

FastAPI in-process background tasks are not enough for production benchmark jobs because benchmark execution is long running, stateful, failure-prone, and must survive API process restarts. The app should enqueue durable worker jobs and expose job events over polling or server-sent events. WebSockets can be added later if the deployment path supports them cleanly.

## Data Model Summary

Core entities:

- User
- UserProfile
- VmConnectionProfile
- SshHealthCheck
- ModelProfile
- VllmConfigProfile
- BenchmarkSuite
- BenchmarkJob
- BenchmarkRun
- BenchmarkMetric
- BenchmarkArtifact
- AuditEvent

Every user-owned entity must include `owner_user_id`. API queries must enforce ownership at the service layer and database query layer.

## Security Requirements

- Never log private keys, passphrases, bearer tokens, or raw secrets.
- Encrypt private keys and passphrases at rest.
- Prefer short-lived decrypted key material written to a worker-owned temp file with strict permissions, then delete it immediately after use.
- Validate and store SSH host key fingerprints.
- Keep per-user resources isolated.
- Add audit events for connection creation, connection checks, vLLM start/stop, benchmark start/cancel, report download, and failed authentication.
- Use signed download URLs or authenticated artifact routes for report downloads.
- Use role-ready authorization even if phase one has only normal users and admins.

## Implementation Phases

### Phase 0 Planning Package

Deliverables:

- `plan.md`
- `docs/product-requirements.md`
- `docs/high-level-architecture.md`
- `docs/low-level-architecture.md`
- `docs/benchmarking-methodology.md`
- `docs/security-and-operations.md`
- `docs/source-research.md`

Exit criteria:

- Plan reviewed and approved.
- Open questions resolved or accepted as assumptions.

### Phase 1 Project Scaffold

Deliverables:

- Monorepo layout
- Docker Compose for local development
- FastAPI API service
- React/Vite frontend
- PostgreSQL migrations
- Redis and Celery worker service
- Basic authentication and user model

Validation:

- API health endpoint
- Frontend loads
- Database migration test
- Worker can process a trivial queued job

### Phase 2 VM Profiles And SSH Health Checks

Deliverables:

- VM profile create/edit/delete
- Secure key handling
- SSH connection check worker
- Remote host inventory collection
- Connection status UI

Validation:

- Successful SSH check against a test VM
- Failed auth and failed network paths show clear errors
- Secrets do not appear in logs

### Phase 3 vLLM Profile And Remote Runtime Control

Deliverables:

- vLLM profile editor
- Remote preflight checks
- Start vLLM command builder
- Runtime readiness checks through `/v1/models`
- Runtime log capture
- Stop/restart support

Validation:

- Generated command matches saved configuration
- Runtime is not marked ready until OpenAI-compatible endpoint responds
- Failed startup preserves logs and error state

### Phase 4 Benchmark Queue And GuideLLM Runner

Deliverables:

- Benchmark suite builder
- Job queue
- Worker state machine
- GuideLLM execution adapter
- Remote telemetry capture
- Event stream or polling endpoint

Validation:

- Multiple benchmark jobs queue and run in order
- Job cancellation works
- Job failure is visible with logs and partial artifacts
- No browser request blocks for the full benchmark duration

### Phase 5 Reports

Deliverables:

- Results table
- Copy Markdown and CSV
- Download CSV, JSON, Markdown
- Artifact manifest
- Reproduction command

Validation:

- Report values reconcile with raw result files
- Failed requests are not hidden
- Copy/download output matches rendered UI table

### Phase 6 Production Hardening

Deliverables:

- Role-ready authorization
- Admin job view
- Retention policies
- Backup and restore notes
- Rate limits
- Operational dashboards
- OpenTelemetry hooks
- Production deployment runbook

Validation:

- Multi-user isolation tests
- Worker restart recovery tests
- Database backup and restore rehearsal
- Load test for queued job creation and status polling

## Testing Strategy

Backend:

- Unit tests for config validation, command construction, ownership filters, and metric parsing
- Integration tests for database, Redis, and worker state transitions
- SSH adapter tests with mocked and real test hosts
- Benchmark parser tests using retained GuideLLM outputs

Frontend:

- Component tests for forms and state transitions
- Playwright tests for the four-section workflow
- Accessibility checks for forms, tables, and download actions

End to end:

- Local Compose smoke test
- Test VM connection
- vLLM launch
- Smoke benchmark
- Report download validation

## Open Questions

- Which authentication provider should phase one use: local username/password, SSO, or Oracle identity integration?
- Should private SSH keys be persistently stored, or should users upload keys per session?
- What is the initial artifact retention period?
- Should phase one support private subnet VMs through bastion or only directly reachable hosts?
- Which production target should be planned first after approval: single VM Docker Compose, Kubernetes, or OCI Container Instances?
- Do we need team/shared benchmark profiles in phase one, or strictly private per-user profiles?
- Which exact first model should be used for the first live validation run?
