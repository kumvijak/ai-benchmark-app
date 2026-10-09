# AI Inference Benchmark Control

The application lives in [apps/benchmark-control](apps/benchmark-control/README.md). It controls OCI CPU hosts, manages remote vLLM runtimes over SSH, runs GuideLLM performance workloads, and exports reports.

```bash
cd apps/benchmark-control
export APP_SECRET="$(openssl rand -base64 32)"
export APP_LOCAL_USER_EMAIL="operator@example.com"
docker compose up --build -d
```

Open http://localhost:18080. Keep the same `APP_SECRET` to decrypt saved profiles on later starts. Persist it in your own protected environment rather than generating a new value every time.

- [Application mind map](apps/benchmark-control/docs/application-mind-map.md)
- [Container validation](apps/benchmark-control/docs/container-validation.md)
- [VM provisioning workflow](apps/benchmark-control/docs/vm-provisioning.md)

`plan.md` describes a broader proposed architecture. The implemented application uses plain HTML/CSS/JavaScript, FastAPI, SQLite, and one worker thread. `LLM_Benchmark_Initial Config_Guide.docx` is the original workload/configuration reference.
