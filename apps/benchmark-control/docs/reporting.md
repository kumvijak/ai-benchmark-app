# Reports module

Version 0.3.0 adds a dedicated reporting queue and offline report bundles based on the MLCommons Endpoints phase2 presentation. These are GuideLLM reports, not official MLPerf submissions or certification.

## Generate and view

1. Finish one or more benchmark attempts; succeeded and failed jobs are eligible.
2. Open **Reports**, enter a title, and select up to 100 attempts.
3. Click **Queue Report**. Progress advances through validation, calculation, rendering and packaging.
4. Open the completed catalogue, filter configurations, and select a detail page. Toggle attempts to inspect overlapping chart points. Download HTML and evidence together using **Download ZIP**, or download CSV/JSON separately.

The existing per-job GuideLLM exports remain under **Native Report**.

Each detail page groups an identical model/revision, precision, host, runtime, software and workload configuration. Different request/duration/error constraints remain separate. Concurrency points and retries remain individual columns; attempts are never averaged together.

## Data and measurement boundary

Inputs are snapshotted when the report is queued. Source job deletion or later profile changes do not change an existing report. The worker uses retained data only: it never connects to OCI, SSH or an inference endpoint.

New benchmark runs record the serving profile, observed hardware, runtime backend/versions and actual GuideLLM version. The running server must confirm a matching serving configuration before a new benchmark starts. **Restart existing runtimes after upgrading** to obtain this evidence. Historical results can still be reported, with explicit identity/revision warnings; current saved profiles are not used as historical facts.

- Four charts show native system output TPS vs aggregate TPS/concurrency, native system TPS vs concurrency, P95 TTFT vs concurrency, and aggregate TPS/concurrency vs concurrency.
- Aggregate TPS/concurrency is a disclosed diagnostic ratio, not MLPerf Endpoints' percentile-based per-user metric.
- TTFT/TPOT retain native milliseconds. Native query latency in seconds is converted to milliseconds.
- Native throughput is labelled unvalidated. Version 1 of the report pipeline does not independently validate retained request timing or reconstruct throughput, so reconstructed rates are withheld. Every report currently carries an evidence warning.
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

From `apps/benchmark-control` in an environment with the application dependencies and `httpx`:

```sh
python -m unittest discover -s app/tests -p 'test_*.py'
```

Tests cover grouping/retries, missing and failed measurements, latency conversion, ownership, path traversal, snapshot survival after deletion, checksums, ZIP integrity, concurrent claims, lease expiry, stale-worker fencing, escaping and serving-profile mismatch. Browser verification uses clearly labelled disposable fixtures; it does not establish benchmark validity or perform a cloud run.
