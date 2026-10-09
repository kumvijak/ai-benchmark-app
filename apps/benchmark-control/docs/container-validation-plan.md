# Container validation plan

9 October 2026, after workspace cleanup.

1. Keep the existing application workflow and remote vLLM/GuideLLM versions unchanged.
2. Replace the dependency on an unrelated local base image with an official digest-pinned Python base. Install application requirements, OCI SDK, certificates, and SSH client explicitly.
3. Build an isolated local image; run compilation, dependency checks, and existing unit tests in it.
4. Start an isolated Compose project, verify health/static UI, local login, ownership, profile CRUD, queue behavior, and report exports using disposable local data. Verify persistence after restart.
5. Remove only validation containers and volumes; leave other local containers and all OCI resources untouched.
6. Save the updated application mind map, verification results, and a persistent memory correction.

Architecture: browser HTML/CSS/JavaScript → FastAPI → SQLite profiles/jobs → one worker thread → OCI SDK / SSH → remote Ubuntu vLLM and GuideLLM. `/data` holds persistent SQLite state. OCI deployment, SSH host preparation, and real model benchmarks need an authorized remote environment and are outside this local container validation.
