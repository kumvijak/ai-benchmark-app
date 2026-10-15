# AI Inference Benchmark Control

The application lives in [apps/benchmark-control](apps/benchmark-control/README.md). It controls OCI CPU hosts, manages remote vLLM runtimes over SSH, runs GuideLLM performance workloads, and exports reports.

```bash
cd apps/benchmark-control
export APP_SECRET="$(openssl rand -base64 32)"
export APP_LOCAL_USER_EMAIL="operator@example.com"
docker compose up --build -d
```

Open http://localhost:18080. Keep the same `APP_SECRET` to decrypt saved profiles on later starts. Persist it in your own protected environment rather than generating a new value every time.

- [VM provisioning workflow](apps/benchmark-control/docs/vm-provisioning.md)

[The application plan](apps/benchmark-control/plan.md) describes a broader proposed architecture. The implemented application uses plain HTML/CSS/JavaScript, FastAPI, SQLite, and one worker thread.
