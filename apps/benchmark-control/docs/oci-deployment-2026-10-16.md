# OCI deployment — 2026-10-16

Current endpoint (2026-10-21): **http://132.145.149.11:8080** — report pipeline 1.2.1 with automatic verified Object Storage archives and independent ZIP links. HTTP port 8080 and original automatic local sessions retained by request. Earlier deployments below are retained as history.

- Endpoint: https://129.213.153.136
- Region: us-ashburn-1
- Compartment: sun-app-cmp
- Instance: aig-benchmark-control-20261016-v031-private (name retained after adding public IP)
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliahl4h7mvy5jwecvmu4ispmccjyzog3t7idgqdhu33vsqq`
- Shape: CI.Standard.A1.Flex, 1 OCPU, 4 GB memory
- VCN: OKE-VCN-01
- Subnet: k8sapipublic
- Private IP: 10.3.200.238
- Public IP: 129.213.153.136 (ephemeral)
- App version: 0.3.1
- App image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:97b04c87e2f98769acbdf0621f04f7df4b9e4e2b63a8040ced008d7d4701908d`
- Proxy image: `docker.io/library/nginx@sha256:970364228b1e4f932abc22d321932816782cca8a20b85a77552a7cfe9acfb3b5`

## Services and access

The instance runs benchmark-api, report-worker, and access-proxy. The API listens
only on loopback port 8081; Nginx exposes HTTPS on port 443 with Basic authentication.
The public endpoint was explicitly requested. The shared subnet security list was
left unchanged; authentication protects access from all source IPs.

Username: `benchmark`. The generated password is stored only on the deployment
workstation in `/private/tmp/aig-oci-login.txt` (mode 0600). Do not commit that file.
The TLS certificate is self-signed and valid for 90 days, so browsers show a warning.

The existing default VCN internet-gateway route table was assigned to this
instance's VNIC only. Shared subnet route tables were not changed.

## Storage and validation

The API and dedicated report worker share an ephemeral `/data` volume for SQLite
and report artifacts. Deleting the container instance loses this data; export
needed results before deletion. No existing profiles, credentials, or results were
migrated into this deployment.

All three containers were ACTIVE and running. The public endpoint returned 401
without credentials; authenticated `/api/health` returned status `ok`, version
`0.3.1`. The OpenAPI version and Reports UI were also checked. The deployment image
passed all 63 unit tests before upload to the private OCIR repository.

## HTTP deployment

A replacement public HTTP instance was requested after the HTTPS deployment.
The HTTPS instance above remains intact; its database and artifacts were not
migrated or deleted. This HTTP instance has a fresh ephemeral `/data` volume.

- Endpoint: http://129.213.156.29
- Instance: aig-benchmark-control-20261016-http
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliaat2zmtzirrx2mw5gcnhk2wzyw6ak57jyy4thbjrf455q`
- Public IP: 129.213.156.29
- Private IP: 10.3.200.124
- Image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:939fbd1541dd5b7517677e3ac51af7328e3f1aec76305248ea35555ede81f797`
- Same region, compartment, subnet, and shape as above.

The Nginx proxy listens on port 80 and forwards to loopback port 8081. It does
not use Basic authentication. The API runs `uvicorn app.gateway:create_app
--factory` with a password hash in `APP_GATEWAY_PASSWORD_HASH` and the existing
required app environment settings. The operator enters the existing password
from `/private/tmp/aig-oci-login.txt` into a login page; no username is required.

The gateway uses a signed, encrypted HttpOnly SameSite=Strict session cookie
with a 12-hour lifetime and a key derived separately from APP_SECRET. This
leaves the app's Bearer Authorization header available for API authentication.
Anonymous API requests return 401 without a WWW-Authenticate challenge, so
background polling does not open browser credential dialogs. Cookies use Secure
when the gateway runs behind HTTPS; they omit Secure for this requested HTTP
deployment. HTTP traffic, including passwords and sessions, is unencrypted.

The image passed all 68 unit tests, including cookie expiry/tampering, mixed
cookie/Bearer requests, HTTP/HTTPS cookie flags, and rejected login attempts.

## HTTP without the added login

The user explicitly requested removal of the added authentication module and
restoration of the previous app behavior. The optional gateway source and its
tests were removed, and the original tested v0.3.1 image was deployed directly.

- Endpoint: http://150.136.237.226
- Instance: aig-benchmark-control-20261016-http-open
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliasuip4fxc7kbhw5zenr2v5iplnvjsdvm6munnlmz5nevq`
- Public IP: 150.136.237.226
- Private IP: 10.3.200.242
- Image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:97b04c87e2f98769acbdf0621f04f7df4b9e4e2b63a8040ced008d7d4701908d`
- VCN: OKE-VCN-01; subnet: k8sapipublic; region: us-ashburn-1.
- Shape: CI.Standard.A1.Flex, 1 OCPU, 4 GB memory.

Two containers run: benchmark-api and report-worker. The API listens directly
on HTTP port 80 using `uvicorn app.main:app`; no added proxy or gateway login
is present. The original app's automatic `/api/local-session` and API Bearer
sessions remain unchanged. With the current shared security list, anyone able
to reach the public IP can access the app; HTTP traffic is unencrypted.

The previous HTTPS and cookie-login HTTP instances remain intact to preserve
their data. No database or artifacts were migrated into this fresh instance.
The shared ephemeral `/data` volume is lost if this instance is deleted.

## Restored port 8080 and OCI principal

- Endpoint: http://129.213.33.227:8080
- Instance: aig-benchmark-control-20261016-8080-principal
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwlia27c72yukvs7dlqimkchgxuo4o5leluxnohghprdmpucq`
- Public IP: 129.213.33.227; private IP: 10.3.200.177
- Image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:97b04c87e2f98769acbdf0621f04f7df4b9e4e2b63a8040ced008d7d4701908d`
- Same VCN/subnet, region, compartment, and 1-OCPU/4-GB A1 shape as above.

The API listens on HTTP port 8080 without an added login. Resource principals
are enabled on the API container; the offline report worker leaves them disabled.
The initial validation failure occurred because the deployment disabled resource
principals, causing the existing code to fall back to unreachable VM metadata.
No application code or IAM policy changes were needed to fix this.

The existing `benchmark-dg` matching rule includes container instances in
sun-app-cmp, and its policy grants `manage all-resources in tenancy`. After being
informed of these permissions and the public unauthenticated endpoint, the user
explicitly requested principal enablement now and login protection in the future.
No new permissions were granted and existing IAM policies were left unchanged.

Live verification passed: HTTP health, automatic local-session creation, OCI
validation (`ok: true`, `principal_type: resource_principal`, 44 regions), and
read-only listing of 13 accessible compartments. The validated principal profile
was saved as profile ID 1. Prior profiles/artifacts were not migrated into this
fresh instance; earlier user-facing instances remain intact.

Ephemeral storage is lost on stopping or restarting the instance as well as on
deletion. See [Oracle's stop documentation](https://docs.oracle.com/en-us/iaas/Content/container-instances/stopping-a-container-instance.htm).

## Trace-generator fix deployment

- Endpoint: http://158.101.111.178:8080
- Instance: aig-benchmark-control-20261016-trace-fixed
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliaeotem54fbvsvl4y5g3esydiv74yk235du7zxzncdsj3q`
- Public IP: 158.101.111.178; private IP: 10.3.200.230
- Image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:b49bb3fed6d0e515f69656746e8704e48832d569286e36a8ffcbe12acfa165d6`
- OCIR tag: `trace-script-fix-20261016`
- API resource principal enabled; dedicated report worker principal disabled.
- Same VCN/subnet, region, compartment, and 1-OCPU/4-GB A1 shape as above.

The previous trace packager searched its own source for a main-guard string;
that search matched its own string literal and truncated the generated Python
payload. The fix uses the parsed generator function's position to exclude it,
and compiles the payload before shipping. A regression test executes both
encoding layers in a real interpreter, reaching capture validation rather than
only compiling the outer wrapper. All 66 tests passed locally and in the
Python 3.12 deployment image. The missing-runtime-identity recovery message is
also included.

Live checks passed for HTTP access, automatic local sessions, resource-principal
validation, and compartment listing. Saved vLLM profile settings were copied via
the API; the VM SSH key cannot be exported through the app and must be re-added
by the user under VM Connection. The prior instance and all of its data remain
intact at http://129.213.33.227:8080. No running inference VM was restarted or
reprovisioned, and no real kernel trace was claimed as verified by these checks.

## Managed benchmark environment fix deployment

- Endpoint: http://150.136.100.214:8080
- Instance: aig-benchmark-control-20261016-benchmark-fixed
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwlial4ufk6ttl422drexb42m326lljh5xfyzyzaszgexqc5q`
- Public IP: 150.136.100.214; private IP: 10.3.200.169
- Image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:9097ea81cc4162161936b449c72a7fc5d7f2dae2d84162b3067cff2c460119d5`
- OCIR tag: `benchmark-env-fix-20261016`
- API resource principal enabled; offline report worker principal disabled.
- Same network, region, compartment, 1-OCPU/4-GB A1 shape, and no added login.

The remote benchmark wrapper now runs under `~/.aig-vllm/bin/python`, selects
the CLI from the same environment, and checks package metadata before running.
The result retains the actual CLI status/output/artifacts on native failure and
records its Python interpreter. Unique per-attempt remote directories prevent
reused app job IDs from overwriting earlier VM artifacts. The image also includes
the trace-generator fix and missing-runtime recovery message. All 70 tests passed
locally and inside the Python 3.12 image, using an explicit isolated CLI fixture
for runner integration tests rather than fabricated performance measurements.

Live checks passed for HTTP health, automatic sessions, principal validation, and
listing 13 accessible compartments. The saved vLLM settings were copied from
http://158.101.111.178:8080. The user must re-add the existing VM SSH connection;
private keys cannot be exported through the API. Earlier app data and inference
VM files were left intact. A new real benchmark requires that SSH connection;
no new inference benchmark was run as part of deployment validation.

## Report presentation 1.1.0 deployment

- Endpoint: http://150.136.10.218:8080
- Instance: aig-benchmark-control-20261016-reports-v11
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliano2dgowucugbonv5hilfqtifyj436sjp2pl7i73unmwa`
- Public IP: 150.136.10.218; private IP: 10.3.200.248
- Image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:dde78fe78142fd7c09967e98dfc52b80b4e7aa3c9971d324374b2ac46328c8b9`
- OCIR tag: `report-layout-20261016`
- Region: us-ashburn-1; VCN: OKE-VCN-01; subnet: k8sapipublic.
- Shape: CI.Standard.A1.Flex, 1 OCPU, 4 GB memory.
- API resource principal enabled; dedicated report worker principal disabled.
- Public HTTP on port 8080 with the original automatic local session behavior.

The new image includes the reference-based catalogue, attempt cards and summary
counts, combined search and six filters, responsive detail pages, accessible
chart readouts, attempt summaries and retained run-data/evidence tables. Report
pipeline 1.1.0 creates fresh report identities while keeping the existing metric
methodology and data schema. The trace and managed benchmark environment fixes
remain included. All 75 tests passed both locally and in the deployment image.

Both OCI containers are ACTIVE and running the pinned image above. Live checks
passed for HTTP health, automatic sessions, resource-principal validation and
read-only listing of 13 accessible compartments. No IAM or shared subnet policy
changes were made. The replacement VNIC uses the prior app's route override.

Saved vLLM configuration profiles are copied through the app API. SSH credentials
cannot be exported through that API and must be re-added in VM Connection. This
instance has a fresh ephemeral database/artifact volume; existing benchmark jobs
and generated report bundles remain on the prior app at
http://150.136.100.214:8080. That app and the inference VM were not restarted,
stopped or deleted. No inference benchmark or synthetic benchmark evidence was
added to the production app as part of deployment verification.

## Timing correction deployment — 2026-10-19

- Endpoint: http://158.101.111.62:8080
- Instance: aig-benchmark-control-20261019-timing-fixed
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliag2oipdrd7mhrehmgxvfasysptrtqw52txz2mb26gkesa`
- Public IP: 158.101.111.62; private IP: 10.3.200.103.
- Image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:21256b3f7ed0f7cc1107a33aeaaae4c5dcbd780850a048e5f583f28719145baf`
- Private OCIR tag: `timing-fix-20261019`.
- Region: us-ashburn-1; VCN: OKE-VCN-01; subnet: k8sapipublic.
- Shape: CI.Standard.A1.Flex, 1 OCPU, 4 GB memory.
- API resource principal enabled; offline report worker principal disabled.
- Direct public HTTP port 8080; original automatic local session behavior.

The runner uses null warmup/cooldown phases for full-run measurement, preserving
request/duration/error guards. Legacy cooldown inputs remain accepted but are not
applied. The misleading UI cooldown control was removed. Report pipeline 1.2.0
uses reconciled retained request timestamps and actual successful output tokens
for full-run throughput. Native measurements are retained for audit with specific
consistency findings. Run columns lead with concurrency and retain job identity.

All 84 tests passed locally and inside the ARM64 deployment image. Both containers
are ACTIVE and running the pinned image. Live checks passed for public HTTP,
automatic sessions, resource-principal validation, 13 accessible compartments,
report API access, the corrected zero legacy cooldown default and updated UI.
The VNIC inherits the existing subnet/VCN public default route. No shared route,
security list or IAM policies were changed. No inference benchmark was launched
and no inference VM was restarted as part of deployment verification.

The previously recorded report-layout instance and VNIC returned OCI 404
NotAuthorizedOrNotFound during this deployment, so a live profile/data migration
from that instance was unavailable. It was not stopped, restarted or deleted by
this deployment. The retained job 2 serving configuration from 2026-10-16 was
restored and verified as profile 1, named `qwen-cpu-4b-INT8 (restored job 2)`.
This is a historical configuration snapshot, not a claim about later edits.
SSH credentials were not copied; add the VM connection through the app.

The new instance has a fresh shared ephemeral database/artifact volume. Existing
benchmark jobs and report bundles were not imported. Retained local job 2 evidence
and the regenerated offline report remain on the workstation; availability of
prior OCI data cannot be established from the stale deployment record. Stop,
restart or deletion of the new instance loses its ephemeral data.

## Concurrency headings and existing-VM deployment — 2026-10-19

- Endpoint: http://129.213.39.17:8080
- Instance: aig-benchmark-control-20261019-concurrency-headings
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliaibsjamyor7exo3nff3w5e57r27ahs7omshtt627ppaia`
- Public IP: 129.213.39.17; private IP: 10.3.200.153.
- Image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:d81d8a118f5e8380a56d5f2750eb157d63bef3a21e4f06f94598533c1233eb68`
- Private OCIR tag: `concurrency-headings-20261019`.
- Source commit: `200f71e`; report pipeline: 1.2.1.
- Same us-ashburn-1 region, OKE-VCN-01 VCN, k8sapipublic subnet,
  CI.Standard.A1.Flex shape with 1 OCPU and 4 GB memory.
- API resource principal enabled; offline report worker principal disabled.
- Direct HTTP port 8080 with the existing automatic local-session behavior.

The image includes concurrency-only run column headings for single-attempt
reports, with the job shown once above the table. Retry columns use attempt labels
and an attempt/job mapping above the table. Existing timing corrections remain.
All 84 tests passed in the deployment image. Both OCI containers are ACTIVE and
running the pinned digest. Public health, corrected timing defaults, report API
access, OCI principal validation and compartment listing passed verification.

The existing inference VM is `ubuntu@129.213.46.180`, not the older VM address
from the October 16 deployment history. VM profile 1 is `aig-bench-a6x01-ssh`.
The user-supplied SSH key was verified against this VM and configured through the
new app API without logging the private key. SSH from the new app was verified
through read-only runtime discovery: vLLM PID 10941, port 8000, alive. A direct
read-only SSH check confirmed HTTP health and model listing; the existing server
is Qwen/Qwen3-4B-Base INT8 with the ZenDNN backend.

Both vLLM profiles were copied and verified from the prior app: profile 1
`qwen-cpu-4b-INT8 (restored job 2)` and profile 2 `qwen-cpu-4b-bf16`. The INT8
configuration matches the currently running runtime. The VM was not provisioned,
restarted or run through VM Setup; no benchmark was launched for verification.
The previous app at http://158.101.111.62:8080 remains intact. New app storage is
fresh and ephemeral: prior benchmark jobs and report bundles were not imported.
No shared network routes, security lists or IAM policies were changed.

## Report archiving deployment — 2026-10-21

- Endpoint: http://132.145.149.11:8080
- Instance: aig-benchmark-control-20261021-report-archive
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliaq5dodiajudtvfnidxbqf26llmqu7ujyuyx3uyi2hwqmq`
- Public IP: 132.145.149.11; private IP: 10.3.200.205.
- Image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:4754e3a2ef6fea94fc3c3ed9b21285f3ed1180459f074fcac11dedb3045655c8`
- Private OCIR tag: `report-archive-20261021`.
- Source commit: `7a199dd`; app version: 0.3.1; report pipeline: 1.2.1.
- Same Ashburn region, app compartment, subnet and CI.Standard.A1.Flex shape
  with 1 OCPU and 4 GB memory.
- Both API and report-worker containers have resource principals enabled.
- Archive: private, versioned `benchmark` bucket, namespace `idhpsi4njgc8`,
  region `us-ashburn-1`; default independent link expiry is 168 hours.

The user explicitly requested deploying the current code with report archiving
only. Security changes and the GuideLLM 0.8.1 upgrade are not included. The
existing HTTP and automatic local-session access behavior is unchanged.
Existing IAM policies, shared networking rules and app instances were not changed.
The prior recorded instance returned OCI 404 NotAuthorizedOrNotFound; this does
not establish whether it was deleted or is inaccessible to the current principal.

All 91 tests passed inside the ARM64 deployment image. Both containers are ACTIVE.
Live checks passed for public health, UI assets, archive API route availability,
owner-authenticated report listing, rejection of unauthenticated report listing,
and API resource-principal authentication. No inference benchmark was launched.

The report worker's startup validation generated an explicitly labelled empty
report bundle with no measurements. Using its actual resource principal, it
uploaded and read back all four archive objects, checked SHA-256, created a
single-ZIP read-only pre-authenticated request, and downloaded that ZIP without
app credentials, again checking SHA-256. Validation artifacts remain under
`reports/deployment-validation/20261021/` in the bucket. A marker in the shared
volume prevents repeating the check on ordinary worker restarts.

This instance starts with a fresh shared ephemeral database/artifact volume;
VM connections, runtime profiles, old jobs and the old report catalogue were not
migrated. Existing report archives in Object Storage are retained. Only verified
archived reports survive loss of the instance's ephemeral data; app settings and
unarchived files still require separate backup. As in the previous deployment,
instance stop/restart/deletion can discard ephemeral app data.

Deployment state is recorded locally in
`/private/tmp/aig-oci-report-archive-deployment-state.json`. The app encryption
secret is retained separately in a mode-0600 workstation file and is not committed
or printed. Keep this secret protected for any later encrypted-profile migration.

## Four parallel-test app instances — 2026-10-22

Four additional independent OCI Container Instances were provisioned in Ashburn
for parallel testing. The user explicitly approved the existing public HTTP,
unauthenticated local-session behavior and OCI resource-principal access on all
four. The previously recorded app instance was not changed or terminated.

| App | Endpoint | Container Instance OCID |
|---|---|---|
| 1 | http://129.213.37.93:8080 | `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliaf6u5xpxwqy4bflkram57df4o7bj5t3r2klg65ntcbc4q` |
| 2 | http://150.136.96.161:8080 | `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliakylsvuhvpftr6cgsb2jc4y5zgwobo5in2fz5dnb7k4bq` |
| 3 | http://129.213.109.99:8080 | `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliaseuxjpg475idcqlcmbx77haz67ean53cz34irfeps2iq` |
| 4 | http://129.213.36.223:8080 | `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwlia6ihkurm6jsowf7stfdzuorxezqovknfewwdczrztioda` |

Each instance has 1 OCPU and 4 GB memory and runs an API container plus a
report-worker container. The four instances therefore add 4 OCPUs and 16 GB
memory in total. Each has a separate shared ephemeral database/artifact volume,
independent app secret and queue, and a fresh configuration with no migrated
VM profiles or jobs. The shared private `benchmark` Object Storage bucket uses
unique artifact-token paths to keep archived report bundles distinct.

All four run the previously tested image:
`us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:4754e3a2ef6fea94fc3c3ed9b21285f3ed1180459f074fcac11dedb3045655c8`.
No application code, GuideLLM version, IAM policy or shared network rule was changed.

All eight containers are ACTIVE. Each app passed health, UI asset, archive API,
authenticated report-listing and unauthenticated report-listing rejection checks.
The API checks passed from a temporary client-only diagnostic container within
OCI using both the public and private health endpoints. App 3 also passed the
workstation checks. Public connections to apps 1, 2 and 4 timed out from the
workstation VPN, despite passing from OCI; no shared network rule was changed
to address this client-path limitation. The diagnostic exposed no listening
service or OCI resource principal and was deleted after collecting results.
Each API confirmed resource-principal authentication. Each report worker used its
actual resource principal to upload a clearly labelled empty validation bundle,
read back and verify all four files, and verify an independent ZIP download's
SHA-256. These checks generated no benchmark measurements and launched no
inference benchmark. Validation objects are under
`reports/deployment-validation/20261022-parallel/`.

Instance state is saved in workstation files
`/private/tmp/aig-oci-parallel-20261022-{1..4}-state.json`. Independent app secrets
are stored separately with mode 0600 and were not printed or committed.
Only verified Object Storage archives survive loss of ephemeral instance data;
app settings and unarchived files still require separate backup.

For independent performance measurements, use separate inference VMs or isolated
inference runtimes. Separate control apps do not isolate contention when tests
share the same inference server.

## SMT selection and HF-token GUI deployment — 2026-10-23

- Endpoint: http://150.136.125.105:8080
- Instance: `aig-benchmark-control-20261023-smt-hf`
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliaonj2whhz2lh3b2epsds6kvtczz3ygwr3kqgzjt2nsnla`
- Public IP: `150.136.125.105`; private IP: `10.3.200.117`.
- Immutable image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:5d575f4cd5b8c0ef9153d2ad723430deeb407224faa70ef815bbd06188c30452`
- Private tag: `smt-hf-20261023`; app version 0.3.1, report pipeline 1.2.1.
- Ashburn, existing app compartment and subnet; `CI.Standard.A1.Flex`,
  1 OCPU and 4 GiB RAM. API and report-worker containers are both ACTIVE.

The image contains the current workspace application source, including explicit
SMT selection for newly provisioned benchmark instances and encrypted HF-token
GUI support. It was built by copying the application into the prior immutable
report-archive image; package requirements are unchanged. GuideLLM remains 0.8.0.
All 104 tests passed inside the ARM64 deployment image before publication.

The existing public HTTP and automatic local-session behavior and resource-principal
permissions are retained. Existing app instances, IAM policies and shared network
rules were not modified. No inference VM or benchmark was launched during checks.
SMT changes were verified with simulated OCI responses in tests; no live SMT-off
benchmark VM was provisioned for deployment verification.

Public endpoint checks passed for health, the SMT/HF controls, SMT defaults,
empty instance/report catalogues and rejection of report listing without a session.
The colocated startup verification also passed resource-principal authentication.
The report worker uploaded an explicitly labelled empty validation report to the
private `benchmark` bucket, verified all four archive objects, created a read-only
expiring ZIP download link and verified the independent download SHA-256.
Validation objects are under `reports/deployment-validation/20261023-smt-hf/`.

This app has fresh ephemeral database/artifact storage. Previous VM connections,
runtime profiles, jobs and report catalogues were not migrated. Verified Object
Storage archives persist independently; app settings and unarchived files still
need separate backup. State is retained locally at
`/private/tmp/aig-oci-smt-hf-20261023-state.json`; the independent app secret is in
a separate mode-0600 workstation file and was not printed or committed.

## Current workspace deployment — 2026-10-26

- Endpoint: http://158.101.111.13:8080
- Instance: `aig-benchmark-control-20261026-b6dd372`
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwliao247yotdvosu3gv5jayapxxcmlwxyctblywuvr74hf5q`
- Public IP: `158.101.111.13`; private IP: `10.3.200.5`.
- Immutable image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:87df168087976919526032a43965b23159532256b2bea05d24a27eea5a4853a1`
- Private tag: `workspace-b6dd372-20261026`; app 0.3.3, report pipeline 1.3.0.
- Source: commit `b6dd372a87d3f749665cd0ee4d429b865157ddd7` plus the local merge-conflict fixes described below.
- Ashburn, existing compartment/subnet, `CI.Standard.A1.Flex`, 1 OCPU and 4 GiB RAM.
- API and separate report-worker containers are ACTIVE and use the pinned digest.

Today's committed source contained unresolved merge markers in `app/main.py`
and README.md. Deployment preparation resolved them while retaining both CPU
memory utilization and encrypted HF-token storage/listing. A regression test
verifies these settings can be saved together without exposing the token.
These fixes remain uncommitted in the workspace. All 110 tests passed inside
the ARM64 image. Both browser JavaScript assets passed syntax checks. The image
was built from the repository Dockerfile using `python:3.12-slim`, not a previous
application image. Test-only httpx was installed only in the disposable test
container, not in the published image.

Public health returned app version 0.3.3. Public checks passed for the SMT,
HF-token and CPU memory controls, SMT defaults, empty runtime/instance/report
catalogues and rejection of report listing without a session. Colocated startup
checks also validated OCI resource-principal authentication. The report worker
uploaded an explicitly labelled empty validation report to the private benchmark
bucket, verified ZIP/CSV/JSON/manifest objects, and verified an independent ZIP
pre-authenticated download SHA-256. Validation objects are under
`reports/deployment-validation/20261026-b6dd372/`.

Existing public HTTP/local-session access and resource-principal permissions
are retained. No existing app, shared network rule or IAM policy was changed.
No inference VM, runtime setup or benchmark was launched during verification.
The new app starts with fresh ephemeral data; previous VM connections, profiles,
jobs and report catalogues were not migrated. Verified Object Storage archives
persist independently; app settings and unarchived files require separate backup.

Local deployment state: `/private/tmp/aig-20261026-state.json`. Source hashes and
merge-fix patch are retained separately in `/private/tmp/aig-20261026-source.json`
and `/private/tmp/aig-20261026-merge-fixes.patch`. The app secret is stored in a
separate mode-0600 workstation file and was not printed or committed.

## Separate parallel app with concurrency through 64 — 2026-10-26

- Endpoint: http://129.213.41.35:8080
- Instance: `aig-benchmark-control-20261026-parallel64`
- Instance OCID: `ocid1.computecontainerinstance.oc1.iad.anuwcljsrmrvwlia2yozmwvtxdnx2bgzzu5gw7dhnxzdxhgc6rvllf6bejaa`
- Public IP: `129.213.41.35`; private IP: `10.3.200.221`.
- Immutable image: `us-ashburn-1.ocir.io/idhpsi4njgc8/aig-benchmark-control/private-app@sha256:2d0cb048c2c0ed457f1cd33ca86ab0c86619bb56aa29c68786e1be41f59112c4`
- Private tag: `parallel-concurrency64-20261026`; app 0.3.3, report pipeline 1.3.0.
- Source: current workspace, including the earlier merge fixes and new shared
  concurrency choices/defaults. All seven workload profiles offer 1, 2, 4, 8,
  16, 32 and 64, with 1, 2, 4, 8 and 16 selected by default.
- Ashburn, existing compartment/subnet; `CI.Standard.A1.Flex`, 1 OCPU and
  4 GiB RAM. Both API and report-worker containers are ACTIVE.

The image was built from the repository Dockerfile; all 110 tests passed inside
it. Public checks verified health, concurrency options/defaults, SMT/HF/CPU memory
controls, empty profile/instance/report catalogues, and rejection of report access
without a session. Startup checks validated OCI resource-principal authentication
and private report archiving: all four objects were read back and verified, and
the independent ZIP pre-authenticated download matched its SHA-256. Empty validation
artifacts are under `reports/deployment-validation/20261026-parallel64/`.

This is a new app instance with its own ephemeral database, queue, encryption
secret and artifact storage. No profiles, jobs or report catalogues were migrated.
The existing public HTTP/local-session and OCI permission settings are retained.
No existing instance, shared network rule or IAM policy was modified. Read-only
checks confirmed the existing app at http://158.101.111.13:8080 is healthy and
both of its containers remain ACTIVE on their original image digest. No inference
VM, runtime or benchmark was started or stopped during this deployment.

Local state: `/private/tmp/aig-parallel64-20261026-state.json`. Source hashes are
in `/private/tmp/aig-parallel64-source.json`. The separate app secret is retained
in a mode-0600 workstation file and was not printed or committed. Only verified
Object Storage reports survive loss of ephemeral storage; settings and unarchived
files need separate backup. Separate control apps still require separate inference
VMs/runtimes to avoid contention during parallel performance measurements.
