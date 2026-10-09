# Container validation

Verified 9 October 2026 after user cleanup. Scope is the local control application; no OCI resources or remote models were used.

## Changes

- Replaced `benchmarking_app-api:latest` with an official Python 3.11 slim base pinned by digest.
- Installed declared application requirements, pinned OCI SDK 2.184.0, OpenSSH client, and CA certificates inside the image.
- Added dependency checking, Python compilation, a Docker health check, and a build-context ignore file.
- Assigned Compose image `aig-inference-app-v1:local`; removed the shared fixed container name and bound the local UI to loopback.
- Added four HTTP integration checks and updated startup/persistence documentation.

The model runtime and GuideLLM execute on the remote host, so those heavy packages are not installed in the control container. Existing remote setup pins and benchmark behavior were preserved.

## Results

| Check | Result |
| --- | --- |
| Docker build without cache from explicit base/dependencies | PASS |
| Application Python compilation | PASS |
| OCI SDK import and SSH executable | PASS |
| Installed dependency consistency (`pip check`) | PASS |
| Existing unit tests | 11/11 PASS |
| HTTP integration tests | 4/4 PASS |
| JavaScript syntax (`node --check`) | PASS |
| Compose configuration and health-based startup | PASS |
| Profile and session persistence after restart | PASS |
| OCI auth/provision/destroy against a tenancy | NOT_RUN |
| Remote SSH setup and vLLM lifecycle | NOT_RUN |
| Actual GuideLLM/model performance workloads | NOT_RUN |

Built platform: **linux/arm64**. The Dockerfile contains no forced architecture, but an amd64 build was not run. Final image: `aig-inference-app-v1:local`, ID `sha256:310e62100cc9a0e1d44abbdd51f818702d8386cc4d5f98af4912f45394849159`, approximately 649 MB. Machine-readable Compose results are in [container-validation.json](container-validation.json).

The HTTP checks launch a real Uvicorn process in a container with external networking disabled and use disposable SQLite data. They exercise static assets, local bootstrap, registration/login, access rejection, VM/runtime profile CRUD, user isolation, worker handling of an unsupported test job, and report access/export routes. Report rows and native artifact strings in these tests are explicitly labeled **TEST FIXTURE**, not measurements.

The separate Compose check used only project `aig-v1-clean-validation`, a temporary random application secret, loopback port 18979, and its own named volume. It checked startup and profile/session persistence through restart. Its containers, network, and volume were removed afterward. The built app image is retained; other local containers and OCI resources were untouched.

## Run locally

Follow [README.md](../README.md). Preserve `APP_SECRET` across starts so saved encrypted profiles remain readable. The default local UI is http://localhost:18080. No cloud credentials are needed to start the app; OCI and SSH operations require operator-selected credentials/resources.

Container instructions were checked against the [Dockerfile reference](https://docs.docker.com/reference/dockerfile/); SDK packaging is published on [OCI SDK PyPI](https://pypi.org/project/oci/). The results above come from the actual local build and checks, not documentation claims.
