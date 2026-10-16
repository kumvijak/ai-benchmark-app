# Kernel tracing (0.3.1)

Kernel tracing is an opt-in diagnostic mode for vLLM CPU runtimes. It captures actual inference-worker PyTorch traces and a server log segment. It is separate from GuideLLM performance benchmarks and does not change existing saved profiles, which default to tracing disabled.

## Use from the app

1. In **vLLM Config**, select the VM and fill a new saved profile with the same model revision, precision and serving settings you want to investigate. Use a distinct name such as `qwen-int8-trace`.
2. Check **Enable kernel tracing (diagnostic only)** and save the profile.
3. Stop the current runtime if it occupies the selected port, then select the saved tracing profile and **Start Runtime**. Wait until it is ready. An existing server cannot acquire startup profiler settings without restarting.
4. Choose **Trace concurrency**, then click **Capture Kernel Trace**. The capture appears as a `trace_runtime` job in **Queue**.
5. Select the finished job, inspect its summary, and click **Download Kernel Trace ZIP**. Available partial bundles can also be downloaded from failed captures.
6. After diagnosis, stop that runtime and start a normal profile with tracing disabled before running performance benchmarks.

Tracing profiles are omitted from the Benchmarking profile dropdown. The API and worker also reject performance benchmarks against a selected tracing profile; serving-configuration matching prevents using a normal profile against a running tracing server.

## Missing runtime identity file

`/tmp/aig-benchmark-control/runtime-<port>.json` is written on the inference VM by the app's runtime launcher. VM provisioning and saving a profile with tracing enabled do not create a running vLLM server. Use **Start Runtime** with the saved tracing-enabled profile and wait until ready before **Capture Kernel Trace**.

If the file is still missing, confirm the selected VM and port. An older or externally started server, or removal of its temporary files, also requires restarting through the app before capture. Stop an existing server on that port before restarting. Never create a placeholder identity file: tracing checks the actual PID, process start time, serving configuration, and verified backend before profiling.

## What gets enabled

The launch command uses vLLM 0.31.0's `--profiler-config` with the PyTorch profiler, CPU activities, input shapes, stack recording and a per-port directory under `/tmp/aig-benchmark-control/traces-<port>`.

Diagnostic launches request `ZENDNNL_ENABLE_PROFILER=1`, `ZENDNNL_API_LOG_LEVEL=4` and vLLM debug logging. The native ZenDNN environment controls are requests to the installed library; their presence alone does not prove that library emitted execution records. Intel CPU traces are supported as well; ZenDNN settings do not establish Intel kernel behavior.

Normal launches explicitly clear the managed profiler/logging environment switches. The active runtime's recorded serving configuration includes the opt-in flag.

## Capture sequence and limits

A per-port remote file lock prevents overlapping captures from this application. The capture checks server PID/start-time identity before and after requests.

- One warmup completion occurs before profiling starts.
- `/start_profile` starts the inference-worker profiler.
- The job sends real completions using a fixed short diagnostic prompt, temperature zero and 64 output tokens by default. This is not the benchmark's synthetic 256-token input, and no throughput result is calculated.
- `/stop_profile` is attempted in a `finally` block, including after inference failures. The response is allowed up to 300 seconds for flushing. A failed stop explicitly requires a runtime restart before another capture.
- Only worker files created or changed during this capture are collected. Existing traces and startup-only log output are excluded. The server log segment starts after warmup.
- The UI sends two diagnostic requests at concurrency 1, or as many requests as the selected concurrency (up to 16). The API supports at most 16 requests and 128 output tokens per request, with requests >= concurrency.
- The ZIP transfer limit is 32 MiB, the server log segment is capped at 2 MiB, and automatic analysis is limited to 128 MiB uncompressed per trace. Oversized files remain on the VM and are reported explicitly. No trace files are deleted automatically.

The job can take several minutes while traces flush. It does not benchmark performance and should use a dedicated runtime without other traffic. External clients not using the application lock can still generate concurrent traffic or invoke profiler endpoints.

## Bundle and interpretation

The ZIP contains:

- `traces/`: raw worker `*.json` / `*.json.gz` files, including all discovered ranks/workers within collection limits.
- `server-execution.log`: the bounded log segment for the capture.
- `capture.json`: request settings, runtime identity, model profile and recorded package/backend versions.
- `summary.json`: capture outcome, completed request count, warnings and timed operator observations.
- `manifest.json`: SHA-256 hashes of the included evidence files (excluding the manifest itself).

The app retains the ZIP under `APP_ARTIFACT_DIR/traces/<owner>/<job>.zip`, with its hash/size in the job result. Binary trace data is not stored inline in SQLite. Download access checks job ownership. Source job deletion removes API access; artifact retention/garbage collection is not implemented.

The summary looks for positive-duration `zentorch::...` events during inference and excludes names indicating prepacking/reordering. It also reports observed operator families, so alternative paths can be inspected. Inclusive event durations can overlap and must not be added together as exclusive execution time.

`zentorch_operations_observed=true` means timed ZenTorch operations were found. It does **not** mean every layer uses ZenDNN or prove that the INT8 decoder matmuls did. `decoder_int8_execution` remains `unverified` until the corresponding dtype/shapes, decoder call paths and native ZenDNN execution records have been reviewed. Missing ZenTorch events do not prove that ZenDNN was unused.

Open traces in a compatible local trace viewer or Perfetto. Raw traces/logs can contain machine paths and workload information; the app does not upload them to a third party.

## API

Create a runtime profile with `kernel_tracing_enabled: true`, start it, then:

```http
POST /api/runtime/traces
Authorization: Bearer <app-session>
Content-Type: application/json

{"vm_profile_id":1,"vllm_profile_id":2,"concurrency":1,"requests":2,"max_tokens":64}
```

Poll the returned job ID through the existing jobs endpoints. Download retained evidence with authenticated `GET /api/jobs/{job_id}/trace.zip`. The download is available for succeeded or failed jobs whenever a bundle was retained.

## Validation boundary

Local tests cover launch configuration, legacy defaults, API persistence/ownership, normal-benchmark exclusion, capture sequencing, failure cleanup, gzip parsing, stale-file exclusion, lock contention, collection/analysis bounds and retained partial downloads. These tests use explicit fixtures and mocked inference responses. An actual remote vLLM 0.31.0 capture is still required to verify emitted traces and the selected native kernels on a given VM.

References: [vLLM 0.31.0 profiling](https://docs.vllm.ai/en/v0.31.0/contributing/profiling/), [AMD ZenTorch logging and profiling](https://github.com/amd/ZenDNN-pytorch-plugin#5-logging-and-debugging).
