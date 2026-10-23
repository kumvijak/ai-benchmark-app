# Main integration and OCI functional smoke test — 23 October 2026

## Integrated application

Application changes from upstream main `9115e46ac216ca6b9712163c003cd984eb025143` are incorporated into `vijay-dev`. The RFP-inspired navy and mint workspace, desktop sidebar and mobile navigation are preserved. Root-level repository housekeeping is excluded to honor the apps-only publishing boundary.

Included functionality: managed GuideLLM environment and isolated run directories; full-run request timing reconciliation; diagnostic kernel tracing and trace downloads; additive database migrations; report generation, Object Storage archiving, archive status/retry controls and independent download links. Report cards distinguish local downloads from archive downloads. The container now builds from the public Python 3.12 slim base instead of requiring an unpublished local base image.

## Validated configuration

The control application ran in a local Docker container, with persistent `/data`, loopback-only port binding, and the operator's DEFAULT OCI profile. The application provisioned a new Ubuntu 24.04 VM in OCI Ashburn; no existing campaign container was replaced or redeployed. The test VM's host firewall restricted incoming SSH to the observed workstation source and did not expose the model port publicly. Shared subnet security rules were unchanged.

| Setting | Value |
| --- | --- |
| Shape | VM.Standard4.Ax.Flex |
| Allocation | 2 OCPU, 16 GiB RAM, 50 GiB boot disk |
| Model | Qwen/Qwen3-0.6B |
| Precision | BF16; quantization none |
| Runtime | vLLM 0.31.0+cpu; Torch 2.13.0+cpu; CPU Triton 3.8.0+cpu |
| Benchmark client | GuideLLM 0.8.0 |
| Runtime limits | Context 512, max sequences 2, KV cache 1 GiB, reserved CPU 1 |
| Workload | 64 input tokens, 16 output tokens, seed 42 |
| Load points | Fixed concurrency 1 and 2 |
| Limits per strategy | 4 requests, 180 seconds, max errors 2 |
| Request timeout | 120 seconds |

## Results

- DEFAULT OCI authentication, VM deployment and SSH environment setup: passed.
- Model startup and real GuideLLM benchmark execution: passed.
- Both load points completed: **8 successful, 0 errored, 0 incomplete requests**.
- Generated report: one configuration, one attempt, two load points; HTML, CSV, JSON and ZIP outputs available.
- Private Object Storage upload and archive read-back: passed.
- Independent Object Storage ZIP retrieval, authenticated API ZIP download and browser local ZIP download: passed; all three byte-for-byte identical.
- ZIP integrity and every file hash in the evidence manifest: passed.
- Actual UI report-source selection, report queueing, archive-ready display, report opening and local download: passed with no JavaScript errors.
- Desktop/mobile UI: all 14 workspace screens checked, including navigation, form behavior, responsive overflow and tracing controls.
- Unit tests: 91 passed on the host and 91 passed inside a disposable instance of the built image. Container dependency consistency check passed.

Report ZIP SHA-256: `878f5c560512849fb96d48267580b1c26cd04972e1841ddb3f2f1cc83d6b9dd8`.

Tested image: `ai-benchmark-app:smoke-20261023`, image ID `sha256:09d7bda5eacb8e7d5cda67320065268d65625c386374a40112ddebd9248c12c9`.

## Qualification limits

This is a functional end-to-end smoke test on one small Intel VM. Four requests per load point do not establish production throughput, latency percentiles or comparative host performance. The generated report preserves the warning **Source model revision not pinned or not recorded**; request timing reconciliation was consistent. Model accuracy, INT8/INT4 conversion, E6 hosts, sweeps and actual diagnostic kernel execution were not exercised in this smoke run. Tracing received unit and UI verification only; AMX capability and automatic dispatch metadata are not proof of executed AMX instructions.

The private archived report and local container data are retained for review. OCI verified both the test VM and its boot volume reached TERMINATED after the test. Operational identifiers, credentials and bearer download URLs are deliberately excluded from this committed test record.
