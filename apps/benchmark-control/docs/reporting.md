# Reports module

Version 0.3.0 adds a dedicated reporting queue and offline report bundles based on the MLCommons Endpoints phase2 presentation. These are GuideLLM reports, not official MLPerf submissions or certification.

## Generate and view

1. Finish one or more benchmark attempts; succeeded and failed jobs are eligible.
2. Open **Reports**, enter a title, and select up to 100 attempts.
3. Click **Queue Report**. Progress advances through validation, calculation, rendering and packaging.
4. Open the completed catalogue, filter configurations, and select a detail page. Toggle attempts to inspect overlapping chart points. Download HTML and evidence together using **Download ZIP**, or download CSV/JSON separately.

The existing per-job GuideLLM exports remain under **Native Report**.

Each detail page groups an identical model/revision, precision, host, runtime, software and workload configuration. Different request/duration/error constraints remain separate. Concurrency points and retries remain individual columns; attempts are never averaged together.

## Catalogue and detail presentation (pipeline 1.1.0)

Pipeline 1.2.1 uses **Concurrency 1**, **Concurrency 2**, etc. as run-data column
headings. A single-attempt table shows the job identifier once above the table;
column headings contain no job or point notation. For retries, headings add only
**Attempt 1**, **Attempt 2**, etc., with the job mapping above the table. Missing
concurrency is labelled **Concurrency not recorded**. Queue a new report after
upgrading to obtain these headings; existing bundles remain immutable.

The catalogue follows the supplied CPU benchmark HTML reference: a shared header,
summary counts, a responsive two-column card grid, and filters for model, host,
precision, test profile, completion and run date. Text search also matches saved
software/configuration fields. Filters combine; Reset filters restores all cards,
and a live count and empty state explain the current selection.

Each card represents one attempt, including failures without measurements. Cards
link to an anchored attempt summary on the matching configuration's detail page.
Retries share configuration details but keep separate cards, chart points and
run-data columns. Attempt completion uses all retained points: any failed point
means failed; otherwise any incomplete point means incomplete. The summary counts
refer to workload attempts, not individual concurrency points. Request counts sum
retained load points only when every contributing count is known; missing data
is never displayed as zero.

Precision labels retain the recorded distinction between BF16, FP16 and weight
quantization. Quantized runs also display their floating-point dtype in the detail
page. Missing quantization evidence remains unverified. Status badges say
**COMPLETE**, **FAILED**, or **INCOMPLETE**; completion is separate from evidence
validity and does not imply MLPerf qualification. Evidence findings identify missing
records or detected inconsistencies.

Detail pages include report facts, four performance charts with grid labels and
keyboard-accessible point readouts, attempt toggles and summaries, model/workload/
hardware/software configuration, a horizontally scrollable run-data table,
evidence findings, download links and measurement definitions. The reference's
historical measurements, campaign-specific claims and reconstructed throughput
are not imported into newly generated reports.

Pipeline 1.1.0 changes presentation without changing the metric methodology or
bundle data schema. Queue a report after upgrading to obtain the new layout;
existing generated bundles stay immutable. The version participates in report
identity, so selecting the same sources and title produces a fresh report after
this upgrade. Offline ZIPs remain self-contained with no external fonts or scripts.

## Full-run timing correction (pipeline 1.2.0)

The runner explicitly sets GuideLLM profile `warmup` and `cooldown` to `null`
for both concurrent and sweep modes. Request, duration, error and over-saturation
constraints remain in force. The duration is a safety cap, not the denominator
for a request-limited run that finishes earlier. This avoids GuideLLM 0.8.0's
future measurement boundary when a duration cooldown is combined with an early
request-count stop. A duration cooldown of **zero** is also unsafe: it still
installs a boundary at the duration cap. Null disables the phase entirely.

The UI's old cooldown control was removed: it incorrectly described a pause
between strategies, while GuideLLM actually used it as a measurement exclusion.
The legacy API field remains accepted for compatibility but is not applied.
Its default is now zero. Results record `timing_policy` with the effective null
phases, legacy requested value and safety-cap role; snapshots retain that policy.
No GuideLLM installation or previously generated native artifacts are patched.

The report's primary throughput is **full-run successful output tokens/s**:
actual successful output tokens divided by latest request end minus earliest
request start **across all outcomes**. This includes fill, drain and scheduling
gaps. It is not steady-state capacity, per-request speed, latency-qualified
goodput or an official MLPerf metric. Warmup/cooldown-filtered native metrics
have a different scope and are preserved rather than silently replaced.

Reconstruction requires all retained status counts and their total to reconcile,
unique request IDs, finite ordered timestamps within scheduler execution, matching
scheduler request bounds, and actual successful output token counts that match
native count/sum summaries. Sampled or missing records withhold the rate. No
configured output-token target is substituted for generated tokens. Native
measurement boundaries are checked independently against scheduler execution,
positive duration and reported duration; negative cooldown is a specific finding.
Native timing inconsistencies do not prevent recovering a separately reconciled
full-run rate. Native latency distributions remain labelled as native.

`report.json` retains native fields, adds per-point timing checks/findings, full-run
boundaries/duration/token total/TPS, and exposes the rate through the existing
`reconstructed_system_tps` field. CSV adds timing statuses, full-run duration,
token total and TPS/concurrency; old native fields keep their original semantics.
Throughput charts use only reconciled full-run rates; native values remain in the
run table for audit. The former blanket warning is replaced by specific findings.

Queue a new report after upgrading to recover eligible historical attempts with
the new method. Existing bundles remain immutable. No new inference run is
needed if historical records are complete. Incomplete records require a rerun to
obtain a full-run rate. Future native exports use the corrected runner policy.

## Data and measurement boundary

Inputs are snapshotted when the report is queued. Source job deletion or later profile changes do not change an existing report. The worker uses retained data only: it never connects to OCI, SSH or an inference endpoint.

New benchmark runs record the serving profile, observed hardware, runtime backend/versions and actual GuideLLM version. The running server must confirm a matching serving configuration before a new benchmark starts. **Restart existing runtimes after upgrading** to obtain this evidence. Historical results can still be reported, with explicit identity/revision warnings; current saved profiles are not used as historical facts.

- Four charts show full-run successful output TPS vs full-run TPS/concurrency, full-run output TPS vs concurrency, native P95 TTFT vs concurrency, and full-run TPS/concurrency vs concurrency.
- Aggregate TPS/concurrency is a disclosed diagnostic ratio, not MLPerf Endpoints' percentile-based per-user metric.
- TTFT/TPOT retain native milliseconds. Native query latency in seconds is converted to milliseconds.
- Pipeline 1.2.0 reconstructs full-run throughput only from reconciled retained request/token records. Native throughput and timing remain visible for audit; native timing inconsistencies receive explicit findings. Missing or sampled records withhold reconstructed throughput.
- Completion requires a succeeded source job, the configured request target, reconciled request counts, and no errors/incomplete requests. It does not qualify accuracy, joint latency SLOs or production readiness.
- Failed attempts with native partial measurements retain those measurements. Earlier failures without retained artifacts produce a failure-evidence page with no invented metrics.
- Missing values remain **Not measured**. AMX capability does not establish kernel execution.

Bundles contain `index.html`, configuration detail HTML pages, `metrics.csv`, `report.json`, a `manifest.json` of file hashes, and sanitized structural snapshots in `evidence/job-N.json`. Native JSON is retained inside each snapshot; original executable HTML and free-text logs are not republished. Known credential fields, key/token patterns, prompt text and response text are removed from snapshots. The source hash identifies the sanitized snapshot, not the original unsanitized export. The manifest excludes itself and the ZIP to avoid recursive checksums.

Charts use inline SVG and scripts; there are no external chart dependencies. Extract the ZIP before opening `index.html`. Report routes enforce ownership, constrain artifact paths and sandbox generated HTML. A path-scoped HttpOnly cookie permits navigation within a report after an authenticated access request; the UI does not put the session token into report URLs.

## Queue and storage

The durable `report_jobs` table is separate from execution `jobs`, preventing either worker from consuming or resetting the other's jobs. SQLite migrations are additive.

Compose runs `report-worker` as a separate service with the same `/data` volume and no OCI/SSH credentials or app secret. The API uses `APP_REPORT_WORKER_MODE=external`. For a single local Uvicorn process, the default `embedded` mode starts an independent reporting thread. Use external mode when running multiple API processes.

To run the standalone worker:

```sh
APP_DB_PATH=/path/to/app.db APP_ARTIFACT_DIR=/path/to/artifacts python -m app.report_worker
```

Both processes must point at the same database and artifact directory. A single local filesystem is required; this SQLite design is not a distributed/NFS queue.

Claims use a SQLite write transaction. Workers renew 60-second leases every 10 seconds. Expired leases retry up to three claimed attempts; explicit failures can be retried through the UI. Each attempt writes into its own directory. The published artifact pointer is updated only by the current lease holder after generation completes, so stale workers cannot overwrite a published report.

Output lives at `APP_ARTIFACT_DIR/reports/<owner>/<report>/<attempt>/`. Identical source snapshots, title and pipeline version reuse a report ID. Failed attempts can leave unpublished directories for diagnosis; automatic artifact retention/deletion is not implemented. Changing methodology or rendering requires a pipeline version bump to produce a fresh immutable report.

## API

- `GET /api/generated-reports/sources`: owned finished benchmark candidates.
- `POST /api/generated-reports`: `{ "title": "Campaign review", "job_ids": [1, 2] }`.
- `GET /api/generated-reports`: catalogue and dedicated queue state.
- `POST /api/generated-reports/{id}/retry`: requeue a failed generation.
- `POST /api/generated-reports/{id}/access`: establish path-scoped report navigation access.
- `GET /api/generated-reports/{id}/files/{filename}`: owned HTML, CSV, JSON, evidence or ZIP.

## Validation

The remote benchmark wrapper and GuideLLM CLI run from the same managed environment,
`~/.aig-vllm`. GuideLLM metadata is checked before any benchmark requests; missing
installation returns an actionable VM Setup error. The result records the actual
`benchmark_python`, GuideLLM version, CLI exit status, stdout/stderr, and native
artifacts even when the CLI fails. Each remote attempt uses a unique directory
under `/tmp/aig-benchmark-control/guidellm-<job-id>-<unique-suffix>/` so replacing
the control app and reusing a job ID cannot overwrite an earlier attempt.

An older wrapper used system Python for version lookup after running the managed
CLI. `PackageNotFoundError: guidellm` in that wrapper is a reporting/environment
failure and does not establish the native benchmark's outcome. Existing native
files may remain in the older `/tmp/aig-benchmark-control/guidellm-<job-id>/`
directory on the inference VM; inspect its JSON/log before interpreting the run.

From `apps/benchmark-control` in an environment with the application dependencies and `httpx`:

```sh
python -m unittest discover -s app/tests -p 'test_*.py'
```

Tests cover grouping/retries, missing and failed measurements, latency conversion, ownership, path traversal, snapshot survival after deletion, checksums, ZIP integrity, concurrent claims, lease expiry, stale-worker fencing, escaping and serving-profile mismatch. Browser verification uses clearly labelled disposable fixtures; it does not establish benchmark validity or perform a cloud run.

## Private OCI report archive

Completed generated reports can be automatically copied to OCI Object Storage.
Local generation and cloud archival have separate status: a cloud failure never
removes a local report. With archiving configured, the worker also backfills
existing completed reports whose local bundles remain available.

Set these variables on **both the API and report worker** before deployment:

```sh
APP_REPORT_ARCHIVE_BUCKET=benchmark
APP_REPORT_ARCHIVE_REGION=us-ashburn-1
APP_REPORT_ARCHIVE_NAMESPACE=idhpsi4njgc8
APP_REPORT_ARCHIVE_AUTH=principal
APP_REPORT_ARCHIVE_LINK_HOURS=168
```

An empty bucket setting disables archiving. The bucket must already exist and
have `NoPublicAccess`. `principal` uses OCI resource principals on Container
Instances and instance principals on Compute. Enable resource principals for
**both containers**. For local development, set `APP_REPORT_ARCHIVE_AUTH=config`
and provide the standard OCI SDK config/key files to each process; an optional
`OCI_CLI_PROFILE` selects the profile. Compose does not mount workstation
credentials automatically.

Grant the container dynamic group bucket inspection, object creation/overwrite
and reads, plus permission to create and manage pre-authenticated requests for
this bucket. Example policy statements (replace `REPORT_CONTAINER_GROUP` with
the deployment's dynamic group; scope to the app compartment):

```text
Allow dynamic-group REPORT_CONTAINER_GROUP to read buckets in compartment sun-app-cmp where target.bucket.name = 'benchmark'
Allow dynamic-group REPORT_CONTAINER_GROUP to manage objects in compartment sun-app-cmp where all {target.bucket.name = 'benchmark', any {request.permission = 'OBJECT_CREATE', request.permission = 'OBJECT_OVERWRITE', request.permission = 'OBJECT_READ'}}
Allow dynamic-group REPORT_CONTAINER_GROUP to manage buckets in compartment sun-app-cmp where all {target.bucket.name = 'benchmark', request.permission = 'PAR_MANAGE'}
```

Supplying the namespace avoids a namespace discovery call. Bucket setup and IAM
are deployment operations, not actions performed automatically by the app.

The worker verifies local manifest hashes and ZIP contents, uploads `report.zip`,
`metrics.csv`, `report.json` and `manifest.json` with transport SHA-256 validation,
then reads every uploaded object's actual bytes back and compares SHA-256 hashes.
Only then does archive status become `ready`. Objects use an owner/report/unique
artifact-token prefix, avoiding collisions after database replacement. All HTML
and evidence are inside the ZIP. SQLite and other app settings are not archived.

Archival uses independent renewable leases with stale-worker fencing. Failures
retry after 30 seconds, up to three claims; **Retry archive** resets a failed
archive. Local files must remain available until verification succeeds. There
is no automatic deletion of local bundles or bucket objects. Versioned buckets
retain overwritten versions according to their configured lifecycle policies.

On a ready report, **Create independent ZIP link** creates a read-only OCI
pre-authenticated request for that single ZIP. Save or copy the returned link
before shutting down the app. The link contacts Object Storage directly and
works until expiry even if the app is deleted. New links and renewals require a
running app with OCI permission; an existing URL cannot be extended. The default
lifetime is seven days (configurable from 1 to 8760 hours). Anyone holding the URL
can download the ZIP, so treat it as a bearer credential. Revoke it from OCI
Object Storage's pre-authenticated requests list if necessary. URLs are returned
only to the report owner with `Cache-Control: private, no-store`; they are not
stored in the report catalogue or logs.

Additional owner-authenticated API endpoints:

- `POST /api/generated-reports/{id}/archive/retry`
- `POST /api/generated-reports/{id}/archive/download-link`: `{url, expires_at}`

For rollout verification, generate a disposable report, wait for archive `ready`,
create a link, and fetch that URL directly without app cookies or Authorization.
Compare the ZIP's SHA-256 to the worker's verified archive metadata and extract it
to open `index.html`. The bucket must remain private. Provision a replacement
only after preserving the current instance's ephemeral app data; archive storage
alone does not restore the app's catalogue or settings.

OCI references: [Object Storage IAM permissions](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/objectstoragepolicyreference.htm), [pre-authenticated requests](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usingpreauthenticatedrequests.htm).

Setup completed on 2026-10-19: private Standard bucket `benchmark` was created in
`us-ashburn-1`, namespace `idhpsi4njgc8`, in the existing `sun-app-cmp` compartment,
with object versioning enabled. The existing completed report #1 was copied and
all four objects were read back with matching SHA-256 checksums. Its independent
ZIP link was fetched without app credentials and matched the verified ZIP hash.
Per the requested rollout decision, the running app has not been redeployed;
automatic archival for future reports requires deploying this code with the
configuration and worker permissions described above.
