# OCI deployment — 2026-10-16

Current endpoint (2026-10-19): **http://158.101.111.62:8080** — timing correction and report pipeline 1.2.0, HTTP port 8080, original automatic local session, and OCI resource principals enabled. Earlier deployments below are retained as history.

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
