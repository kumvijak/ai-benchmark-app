let token = localStorage.getItem("benchmarkToken") || "";
let selectedVm = null;
let selectedBenchmarkVm = null;
let selectedVllm = null;
let selectedBenchmarkJob = null;
let selectedOciInstance = null;
let selectedAuthProfile = null;
let selectedRuntimeProcess = null;
let vmProfilesCache = [];
let vllmProfilesCache = [];
let authProfilesCache = [];
let ociInstancesCache = [];
let vmDeployJobId = null;
let vmDeployPollTimer = null;
let selectedQueueJobId = null;
let knownProvisionCompletionId = Number(localStorage.getItem("knownProvisionCompletionId") || "0");
let provisionCompletionInitialized = false;
let ociPrivateKeyContent = "";
let existingVmPrivateKeyContent = "";
let newVmPrivateKeyContent = "";
let newVmPublicKeyContent = "";
let quickVmCheckRunning = false;
let authFormDirty = false;

const $ = (id) => document.getElementById(id);

const workloadProfiles = {
  Smoke: {
    input: 128,
    output: 64,
    requests: 10,
    concurrencies: [1],
    detail: "Endpoint health check. Small synchronous baseline before longer benchmark runs."
  },
  Chat: {
    input: 256,
    output: 128,
    requests: 64,
    concurrencies: [1, 2, 4, 8, 16],
    detail: "Light chat profile for TTFT and short decode behavior."
  },
  "Q&A": {
    input: 512,
    output: 128,
    requests: 64,
    concurrencies: [1, 2, 4, 8],
    detail: "Practical CPU serving profile for common question-answer workloads."
  },
  "Text generation": {
    input: 128,
    output: 1024,
    requests: 64,
    concurrencies: [1, 2, 4],
    detail: "Decode-heavy profile to expose inter-token latency and sustained generation throughput."
  },
  "Document summary": {
    input: 4096,
    output: 512,
    requests: 64,
    concurrencies: [1, 2, 4],
    detail: "Prefill-heavy summary profile to stress prompt processing and TTFT."
  },
  "Agent planning": {
    input: 2048,
    output: 1536,
    requests: 64,
    concurrencies: [1, 2, 4, 8],
    detail: "Mixed prefill and decode workload for agent planning style tasks."
  },
  Custom: {
    input: 128,
    output: 128,
    requests: 10,
    concurrencies: [1, 2, 4, 8],
    detail: "Custom workload. Edit input tokens, output tokens, requests, and concurrency."
  }
};

const api = async (path, options = {}) => {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `${res.status} ${res.statusText}`);
  }
  const contentType = res.headers.get("content-type") || "";
  return contentType.includes("application/json") ? res.json() : res.text();
};

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.style.display = "block";
  setTimeout(() => { el.style.display = "none"; }, 4500);
}

function sessionStatus(text) {
  $("sessionStatus").textContent = text;
}

function activateTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${name}`));
  localStorage.setItem("activeWorkflowTab", name);
  if (name === "vllm" && token) refreshRuntimeProcesses(false).catch(() => {});
}

async function ensureLocalSession() {
  if (token) {
    try {
      const me = await api("/api/me");
      sessionStatus(`Local session: ${me.email}`);
      await refreshAll();
      return;
    } catch {
      localStorage.removeItem("benchmarkToken");
      token = "";
    }
  }
  const data = await api("/api/local-session", { method: "POST", body: "{}" });
  token = data.token;
  localStorage.setItem("benchmarkToken", token);
  sessionStatus(`Local session: ${data.user.email}`);
  await refreshAll();
}

function rows(tableId, columns, data, onSelect, isSelected = null) {
  const table = $(tableId);
  table.innerHTML = "";
  const head = document.createElement("tr");
  for (const col of columns) {
    const th = document.createElement("th");
    th.textContent = col.label;
    head.appendChild(th);
  }
  table.appendChild(head);
  for (const item of data) {
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    if (isSelected && isSelected(item)) tr.classList.add("selected");
    tr.onclick = () => {
      table.querySelectorAll("tr.selected").forEach((row) => row.classList.remove("selected"));
      tr.classList.add("selected");
      if (onSelect) onSelect(item, tr);
    };
    for (const col of columns) {
      const td = document.createElement("td");
      const value = col.render ? col.render(item) : item[col.key];
      td.innerHTML = value == null ? "" : String(value);
      tr.appendChild(td);
    }
    table.appendChild(tr);
  }
}

async function refreshVms() {
  const data = await api("/api/vm-profiles");
  vmProfilesCache = data;
  const selectedExists = data.some((item) => item.id === selectedVm);
  if ((!selectedVm || !selectedExists) && data.length) selectedVm = (data.find((item) => item.last_status === "healthy") || data[0]).id;
  const benchmarkSelectedExists = data.some((item) => item.id === selectedBenchmarkVm);
  if ((!selectedBenchmarkVm || !benchmarkSelectedExists) && data.length) selectedBenchmarkVm = selectedVm;
  rows("vmTable", [
    { key: "id", label: "ID" },
    { key: "name", label: "Name" },
    { key: "host", label: "Host" },
    { key: "username", label: "User" },
    { key: "last_status", label: "Status", render: (x) => `<span class="status-${x.last_status}">${x.last_status}</span>` },
    { key: "last_checked_at", label: "Checked" }
  ], data, (item) => {
    selectedVm = item.id;
    selectedBenchmarkVm = item.id;
    const linked = activeOciInstances().find((oci) => oci.vm_profile_id === item.id);
    if (linked) selectedOciInstance = linked.id;
    setSelectValue("runtimeVmSelect", selectedVm);
    setSelectValue("benchmarkVmSelect", selectedBenchmarkVm);
  }, (item) => item.id === selectedVm);
  selectedVm = fillProfileSelect("runtimeVmSelect", data, selectedVm, (x) => `${x.name} | ${x.host} | ${x.last_status}`, "No VM profiles");
  selectedBenchmarkVm = fillProfileSelect("benchmarkVmSelect", data, selectedBenchmarkVm, (x) => `${x.name} | ${x.host}`, "No VM profiles");
  await refreshVmSetup();
}

async function refreshVmSetup() {
  const data = await api("/api/vm-setup-status");
  data.forEach(row => {
    const evidence = JSON.parse(row.runtime_evidence || "{}");
    row.runtime_backend = evidence.error || evidence.platform || "Not verified";
    row.torch_version = evidence.torch_version || "";
    row.zentorch_version = evidence.zentorch_version || "";
    row.zendnn_version = evidence.zendnn_version || "";
    row.cpu_vendor = evidence.cpu_vendor || "";
    row.amx_eligible = evidence.amx_eligible ? "Available" : "Unavailable";
  });
  rows("vmSetupTable", [
    { key: "vm_profile_id", label: "VM ID" },
    { key: "name", label: "Name" },
    { key: "root_expand_status", label: "Root Disk", render: (x) => `<span class="status-${x.root_expand_status}">${x.root_expand_status}</span>` },
    { key: "root_usage_after", label: "Root Usage" },
    { key: "vllm_status", label: "vLLM", render: (x) => `<span class="status-${x.vllm_status}">${x.vllm_status}</span>` },
    { key: "vllm_version", label: "vLLM Version" },
    { key: "runtime_backend", label: "Platform" },
    { key: "cpu_vendor", label: "CPU vendor" },
    { key: "amx_eligible", label: "AMX capability" },
    { key: "torch_version", label: "PyTorch" },
    { key: "zentorch_version", label: "ZenTorch" },
    { key: "zendnn_version", label: "ZenDNN" },
    { key: "benchmark_status", label: "GuideLLM", render: (x) => `<span class="status-${x.benchmark_status}">${x.benchmark_status}</span>` },
    { key: "benchmark_version", label: "GuideLLM Version" },
    { key: "last_setup_at", label: "Updated" }
  ], data, null, (item) => item.vm_profile_id === selectedVm);
}

async function refreshAuthProfiles() {
  const data = await api("/api/oci/auth-profiles");
  authProfilesCache = data;
  rows("authProfileTable", [
    { key: "id", label: "ID" },
    { key: "mode", label: "Mode" },
    { key: "profile_name", label: "Profile" },
    { key: "region", label: "Region" },
    { key: "last_status", label: "Status", render: (x) => `<span class="status-${x.last_status}">${x.last_status}</span>` },
    { key: "last_validated_at", label: "Validated" }
  ], data, (item) => {
    selectedAuthProfile = item.id;
    authFormDirty = false;
    document.querySelector(`input[name='authMode'][value='${item.mode}']`)?.click();
    $("ociProfileName").value = item.profile_name || "";
    $("ociProfileText").value = "";
    ociPrivateKeyContent = "";
    $("ociKeyFile").value = "";
    $("ociKeyHint").textContent = "Using encrypted saved key. Upload a key only when creating or replacing this profile.";
    fillProfileSelect("ociAuthProfileSelect", authProfilesCache, selectedAuthProfile, (x) => `${x.mode} / ${x.profile_name} / ${x.region || "region pending"}`, "Validate auth first");
    $("authSavedHint").textContent = `Using saved ${item.mode} profile ${item.profile_name}. Private key material remains encrypted server-side.`;
  }, (item) => item.id === selectedAuthProfile);
  selectedAuthProfile = fillProfileSelect("ociAuthProfileSelect", data, selectedAuthProfile, (x) => `${x.mode} / ${x.profile_name} / ${x.region || "region pending"}`, "Validate auth first");
  const latest = data[0];
  if (latest && latest.last_status === "validated") {
    $("authState").textContent = `Validated: ${latest.profile_name}`;
    $("authState").className = "pill ok";
    $("authSavedHint").textContent = `Latest validated profile: ${latest.mode} / ${latest.profile_name} / ${latest.region || "region not recorded"}.`;
    if (!$("authOutput").textContent || $("authOutput").textContent.startsWith("Choose an authentication mode")) {
      $("authOutput").textContent = JSON.stringify({
        ok: true,
        mode: latest.mode,
        profile_name: latest.profile_name,
        region: latest.region,
        message: "Saved authentication profile is available. Private key is encrypted server-side and is not restored into the browser."
      }, null, 2);
    }
  }
}

function modelPrecisionLabel(value) {
  return value === "int8" ? "INT8" : value === "int4" ? "INT4" : "None";
}

function syncModelPrecisionDtype() {
  const quantized = ["int8", "int4"].includes($("quantization").value);
  for (const option of $("dtype").options) {
    option.disabled = quantized && option.value !== "bfloat16";
  }
  if (quantized) $("dtype").value = "bfloat16";
}

function fillVllmForm(item) {
  $("vllmName").value = item.name;
  $("modelRef").value = item.model_ref;
  $("modelRevision").value = item.model_revision ?? "";
  $("quantization").value = item.quantization || "none";
  $("dtype").value = item.dtype;
  $("runtimePort").value = item.port;
  $("maxModelLen").value = item.max_model_len;
  $("maxBatchedTokens").value = item.max_num_batched_tokens ?? "";
  $("maxSeqs").value = item.max_num_seqs;
  $("kvCache").value = item.cpu_kvcache_space_gib ?? "";
  $("reservedCpu").value = item.reserved_cpu;
  $("prefixCache").checked = !!item.prefix_cache_enabled;
  $("chunkedPrefill").checked = !!item.chunked_prefill_enabled;
  $("requestTimeout").value = item.request_timeout_seconds;
  syncModelPrecisionDtype();
}

async function refreshVllm() {
  const data = await api("/api/vllm-profiles");
  vllmProfilesCache = data;
  if (!selectedVllm && data.length) selectedVllm = data[0].id;
  rows("vllmTable", [
    { key: "id", label: "ID" },
    { key: "name", label: "Name" },
    { key: "model_ref", label: "Model" },
    { key: "quantization", label: "Model precision", render: (x) => modelPrecisionLabel(x.quantization) },
    { key: "dtype", label: "Dtype" },
    { key: "port", label: "Port" },
    { key: "max_model_len", label: "Max Len" },
    { key: "max_num_seqs", label: "Seqs" },
    { key: "cpu_kvcache_space_gib", label: "KV GiB", render: (x) => x.cpu_kvcache_space_gib == null ? "Auto" : x.cpu_kvcache_space_gib }
  ], data, (item) => { selectedVllm = item.id; fillVllmForm(item); }, (item) => item.id === selectedVllm);
  fillProfileSelect("benchmarkVllmSelect", data, currentSelectNumber("benchmarkVllmSelect") || selectedVllm, (x) => `${x.name} | ${x.model_ref} | ${modelPrecisionLabel(x.quantization)} | port ${x.port}`, "No vLLM profiles");
}

async function refreshJobs() {
  const data = await api("/api/jobs");
  $("queueRefreshState").textContent = `Refreshed ${new Date().toLocaleTimeString()}`;
  renderQueue(data);
  const activeProvision = data.find((job) => job.job_type === "provision_vm" && ["queued", "running"].includes(job.status));
  if (activeProvision && !vmDeployJobId) {
    vmDeployJobId = activeProvision.id;
    renderVmDeployProgress(activeProvision);
    monitorVmDeployment(activeProvision.id);
  }
  await syncCompletedProvisionJobs(data);
}

async function syncCompletedProvisionJobs(data) {
  const latest = data.find((job) => job.job_type === "provision_vm" && job.status === "succeeded" && job.result && job.result.vm_profile_id);
  if (!provisionCompletionInitialized) {
    provisionCompletionInitialized = true;
    if (latest) {
      knownProvisionCompletionId = Math.max(knownProvisionCompletionId, latest.id);
      localStorage.setItem("knownProvisionCompletionId", String(knownProvisionCompletionId));
    }
    return;
  }
  if (!latest) return;
  if (latest.id <= knownProvisionCompletionId) return;
  knownProvisionCompletionId = latest.id;
  localStorage.setItem("knownProvisionCompletionId", String(knownProvisionCompletionId));
  selectedVm = latest.result.vm_profile_id;
  await Promise.all([refreshVms(), refreshOciInstances()]);
}

function jobProgressText(job) {
  const progress = job.result && job.result.progress ? job.result.progress : null;
  if (!progress) return "";
  const percent = progress.percent == null ? "" : `${progress.percent}% `;
  return `${percent}${progress.stage || ""} ${progress.message || ""}`.trim();
}

function jobSummary(job) {
  if (job.error_message) return job.error_message.slice(0, 160);
  const progress = jobProgressText(job);
  if (progress) return progress.slice(0, 160);
  if (job.result) return JSON.stringify(job.result).slice(0, 160);
  return "";
}

function renderQueue(data) {
  const counts = { queued: 0, running: 0, succeeded: 0, failed: 0 };
  for (const job of data) {
    if (counts[job.status] != null) counts[job.status] += 1;
  }
  $("queueSummary").innerHTML = `
    <span>Queued: ${counts.queued}</span>
    <span>Running: ${counts.running}</span>
    <span>Succeeded: ${counts.succeeded}</span>
    <span>Failed: ${counts.failed}</span>
  `;
  rows("queueTable", [
    { key: "id", label: "ID" },
    { key: "job_type", label: "Type" },
    { key: "status", label: "Status", render: (x) => `<span class="status-${x.status}">${x.status}</span>` },
    { key: "created_at", label: "Created" },
    { key: "started_at", label: "Started" },
    { key: "finished_at", label: "Finished" },
    { key: "result", label: "Progress / Log", render: jobSummary }
  ], data, async (item) => {
    selectedQueueJobId = item.id;
    renderQueueDetail(item);
    if (item.job_type === "run_benchmark" && item.status === "succeeded") {
      selectedBenchmarkJob = item.id;
      const result = await api(`/api/reports/${item.id}.json`);
      renderReportTable(result);
    }
  }, (item) => item.id === selectedQueueJobId);
  if (selectedQueueJobId) {
    const selected = data.find((job) => job.id === selectedQueueJobId);
    if (selected) renderQueueDetail(selected);
  }
}

function renderQueueDetail(job) {
  const lines = [
    `Job ID: ${job.id}`,
    `Type: ${job.job_type}`,
    `Status: ${job.status}`,
    `Created: ${job.created_at || ""}`,
    `Started: ${job.started_at || ""}`,
    `Finished: ${job.finished_at || ""}`
  ];
  const progress = job.result && job.result.progress ? job.result.progress : null;
  if (progress) {
    lines.push("");
    lines.push("Progress:");
    lines.push(JSON.stringify(progress, null, 2));
  }
  if (job.status === "failed") {
    lines.push("");
    lines.push("Failure log:");
    lines.push(job.error_message || "No failure log was recorded.");
  } else if (job.error_message) {
    lines.push("");
    lines.push("Log:");
    lines.push(job.error_message);
  }
  if (job.result && !progress) {
    lines.push("");
    lines.push("Result:");
    lines.push(JSON.stringify(job.result, null, 2));
  }
  $("queueDetailHint").textContent = job.status === "failed"
    ? "Failure details from the job worker are shown below."
    : "Current job details are shown below.";
  $("queueLogOutput").textContent = lines.join("\n");
}

function reportRows(result) {
  if (Array.isArray(result.normalized_rows) && result.normalized_rows.length) return result.normalized_rows;
  return Array.isArray(result.results) && result.results.length ? result.results : [result];
}

function reportColumns() {
  return [
    "workload_name", "concurrency", "requests", "success_requests", "failed_requests",
    "profile", "request_rate", "ttft_ms_p50", "ttft_ms_p95", "itl_ms_p50", "itl_ms_p95",
    "tpot_ms_p50", "e2e_ms_p50", "e2e_ms_p95", "output_tokens_per_second",
    "total_tokens_per_second", "duration_seconds"
  ];
}

function fmt(value) {
  if (value == null) return "N/C";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(3);
  return String(value);
}

function renderReportTable(result) {
  const container = $("reportOutput");
  container.classList.remove("empty");
  container.innerHTML = "";
  if (result.runtime_evidence) {
    const evidence = result.runtime_evidence;
    const summary = document.createElement("p");
    summary.textContent = `Backend: ${evidence.backend} | Platform: ${evidence.platform} | Intel AMX runtime support: ${evidence.amx_runtime_supported ? "available" : "unavailable"} | AMX selection: automatic | vLLM: ${evidence.vllm_version} | PyTorch: ${evidence.torch_version}`;
    if (evidence.zendnn_version) summary.textContent += ` | ZenTorch: ${evidence.zentorch_version} | ZenDNN: ${evidence.zendnn_version}`;
    container.appendChild(summary);
  }
  const rows = reportRows(result);
  const present = new Set(rows.flatMap((row) => Object.keys(row)));
  const table = document.createElement("table");
  const head = document.createElement("tr");
  const columns = reportColumns();
  for (const key of Array.from(present).sort()) {
    if (!columns.includes(key)) columns.push(key);
  }
  for (const col of columns) {
    const th = document.createElement("th");
    th.textContent = col;
    head.appendChild(th);
  }
  table.appendChild(head);
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const col of reportColumns()) {
      const td = document.createElement("td");
      td.textContent = fmt(row[col]);
      tr.appendChild(td);
    }
    table.appendChild(tr);
  }
  container.appendChild(table);
}

async function refreshRuntimeProcesses(showErrors = true) {
  const vmId = currentSelectNumber("runtimeVmSelect") || selectedVm;
  if (!$("runtimeProcessTable")) return;
  if (!vmId) {
    rows("runtimeProcessTable", [
      { key: "pid", label: "PID" },
      { key: "port", label: "Port" },
      { key: "alive", label: "Alive" },
      { key: "command", label: "Command" },
      { key: "log_path", label: "Log" }
    ], [], null);
    return;
  }
  try {
    const data = await api(`/api/runtime/processes?vm_profile_id=${encodeURIComponent(vmId)}`);
    if (selectedRuntimeProcess && !data.some((item) => item.pid === selectedRuntimeProcess.pid)) selectedRuntimeProcess = null;
    rows("runtimeProcessTable", [
      { key: "pid", label: "PID" },
      { key: "port", label: "Port" },
      { key: "alive", label: "Alive", render: (x) => x.alive ? "true" : "false" },
      { key: "command", label: "Command" },
      { key: "log_path", label: "Log" }
    ], data, (item) => { selectedRuntimeProcess = item; }, (item) => selectedRuntimeProcess && item.pid === selectedRuntimeProcess.pid);
  } catch (e) {
    rows("runtimeProcessTable", [
      { key: "pid", label: "PID" },
      { key: "port", label: "Port" },
      { key: "alive", label: "Alive" },
      { key: "command", label: "Command" },
      { key: "log_path", label: "Log" }
    ], [], null);
    if (showErrors) toast(e.message);
  }
}

async function refreshAll() {
  if (!token) return;
  await Promise.all([refreshAuthProfiles(), refreshVms(), refreshVllm(), refreshJobs(), refreshOciInstances()]);
}

async function refreshOciInstances() {
  const table = $("ociInstanceTable");
  if (!table) return;
  const data = await api("/api/oci/instances");
  ociInstancesCache = data;
  if (selectedOciInstance && !data.some((item) => item.id === selectedOciInstance)) {
    selectedOciInstance = null;
  }
  if (!selectedOciInstance && selectedVm) {
    const linked = activeOciInstances(data).find((item) => item.vm_profile_id === selectedVm);
    if (linked) selectedOciInstance = linked.id;
  }
  rows("ociInstanceTable", [
    { key: "id", label: "ID" },
    { key: "display_name", label: "Name" },
    { key: "shape", label: "Shape" },
    { key: "public_ip", label: "Public IP" },
    { key: "private_ip", label: "Private IP" },
    { key: "lifecycle_state", label: "State" },
    { key: "vm_profile_id", label: "SSH Profile" },
    { key: "destroyed_at", label: "Destroyed" }
  ], data, (item) => {
    selectedOciInstance = item.id;
    if (item.vm_profile_id) {
      selectedVm = item.vm_profile_id;
      selectedBenchmarkVm = item.vm_profile_id;
      setSelectValue("runtimeVmSelect", selectedVm);
      setSelectValue("benchmarkVmSelect", selectedBenchmarkVm);
    }
  }, (item) => item.id === selectedOciInstance);
}

function activeOciInstances(data = ociInstancesCache) {
  return data.filter((item) => !item.destroyed_at && !["TERMINATED", "TERMINATING"].includes(item.lifecycle_state));
}

function resolveSelectedOciInstanceForDestroy() {
  const active = activeOciInstances();
  if (selectedOciInstance && active.some((item) => item.id === selectedOciInstance)) {
    return selectedOciInstance;
  }
  const vmId = selectedVm || currentSelectNumber("runtimeVmSelect") || currentSelectNumber("benchmarkVmSelect");
  const linked = vmId ? active.find((item) => item.vm_profile_id === vmId) : null;
  if (linked) {
    selectedOciInstance = linked.id;
    return linked.id;
  }
  return null;
}

async function quickCheckVms() {
  if (quickVmCheckRunning) return;
  quickVmCheckRunning = true;
  try {
    await api("/api/vm-profiles/status-checks", { method: "POST", body: "{}" });
    await refreshVms();
  } finally {
    quickVmCheckRunning = false;
  }
}

function initWorkloads() {
  const preset = $("workloadPreset");
  preset.innerHTML = "";
  for (const name of Object.keys(workloadProfiles)) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    preset.appendChild(option);
  }
  preset.value = "Smoke";
  applyWorkloadPreset();
}

function applyWorkloadPreset() {
  const name = $("workloadPreset").value;
  const profile = workloadProfiles[name];
  $("inputTokens").value = profile.input;
  $("outputTokens").value = profile.output;
  $("maxRequests").value = profile.requests;
  $("workloadDetails").value = `${profile.detail}\nRecommended streams: ${profile.concurrencies.join(", ")}`;
  const concurrency = $("concurrency");
  concurrency.innerHTML = "";
  for (const value of profile.concurrencies) {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = String(value);
    option.selected = true;
    concurrency.appendChild(option);
  }
  if (name === "Custom") {
    for (const value of [16, 32]) {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = String(value);
      concurrency.appendChild(option);
    }
  }
}

function selectedAuthMode() {
  return document.querySelector("input[name='authMode']:checked").value;
}

function updateAuthMode() {
  const mode = selectedAuthMode();
  $("userProfileAuth").classList.toggle("hidden", mode !== "user_profile");
  document.querySelectorAll(".choice-card").forEach((card) => {
    const input = card.querySelector("input");
    card.classList.toggle("selected", input.checked);
  });
}

function initHelpTooltips() {
  document.querySelectorAll("label[data-help]").forEach((label) => {
    if (label.querySelector(":scope > .help-icon")) return;
    const icon = document.createElement("span");
    icon.className = "help-icon";
    icon.textContent = "?";
    icon.tabIndex = 0;
    icon.setAttribute("role", "button");
    icon.setAttribute("aria-label", `Help: ${label.childNodes[0]?.textContent?.trim() || "field"}`);
    const tooltip = document.createElement("span");
    tooltip.className = "help-tooltip";
    tooltip.textContent = label.dataset.help || "";
    label.append(icon, tooltip);
  });
}

function restoreAuthDraft() {
  const profileName = localStorage.getItem("ociProfileName");
  const profileText = localStorage.getItem("ociProfileText");
  const authMode = localStorage.getItem("ociAuthMode");
  if (profileName) $("ociProfileName").value = profileName;
  if (profileText) $("ociProfileText").value = profileText;
  if (authMode) {
    const input = document.querySelector(`input[name='authMode'][value='${authMode}']`);
    if (input) input.checked = true;
  }
  if (profileText) {
    $("authOutput").textContent = "Restored saved OCI profile draft. Re-upload the private key only if you need to validate again.";
  }
}

function saveAuthDraft() {
  localStorage.setItem("ociAuthMode", selectedAuthMode());
  localStorage.setItem("ociProfileName", $("ociProfileName").value || "");
  localStorage.setItem("ociProfileText", $("ociProfileText").value || "");
}

function selectedVmMode() {
  return document.querySelector("input[name='vmMode']:checked").value;
}

function updateVmMode() {
  const mode = selectedVmMode();
  $("newVmPanel").classList.toggle("hidden", mode !== "new");
  $("existingVmPanel").classList.toggle("hidden", mode !== "existing");
  document.querySelectorAll("input[name='vmMode']").forEach((input) => {
    input.closest(".choice-card").classList.toggle("selected", input.checked);
  });
}

function fillSelect(id, data, labelFn, valueFn = (x) => x.id) {
  const select = $(id);
  select.innerHTML = "";
  for (const item of data) {
    const option = document.createElement("option");
    option.value = valueFn(item);
    option.textContent = labelFn(item);
    select.appendChild(option);
  }
}

function fillProfileSelect(id, data, selectedValue, labelFn, placeholder = "Select a profile") {
  const select = $(id);
  if (!select) return null;
  select.innerHTML = "";
  if (!data.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = placeholder;
    select.appendChild(option);
    return null;
  }
  for (const item of data) {
    const option = document.createElement("option");
    option.value = String(item.id);
    option.textContent = labelFn(item);
    select.appendChild(option);
  }
  const target = selectedValue && data.some((item) => item.id === selectedValue) ? String(selectedValue) : String(data[0].id);
  select.value = target;
  return Number(target);
}

function setSelectValue(id, value) {
  const select = $(id);
  if (!select || value == null) return;
  const stringValue = String(value);
  if (Array.from(select.options).some((option) => option.value === stringValue)) {
    select.value = stringValue;
  }
}

function currentSelectNumber(id) {
  const el = $(id);
  const value = el ? Number(el.value) : 0;
  return Number.isFinite(value) && value > 0 ? value : null;
}

function authQuery() {
  const authId = currentSelectNumber("ociAuthProfileSelect") || selectedAuthProfile;
  return authId ? `auth_profile_id=${encodeURIComponent(authId)}` : "";
}

function showVmDeployProgress() {
  $("vmDeployProgress").classList.remove("hidden", "ok", "bad");
}

function setVmDeployProgress(percent, status, stage, state = "") {
  const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
  const panel = $("vmDeployProgress");
  panel.classList.remove("hidden", "ok", "bad");
  if (state) panel.classList.add(state);
  $("vmDeployBar").style.width = `${safePercent}%`;
  $("vmDeployPercent").textContent = `${safePercent}%`;
  $("vmDeployStatus").textContent = status;
  $("vmDeployStage").textContent = stage;
}

function renderVmDeployProgress(job) {
  const progress = job.result && job.result.progress ? job.result.progress : null;
  if (job.status === "queued") {
    setVmDeployProgress(2, `Deployment job ${job.id} queued`, "Waiting for the worker to start the OCI VM deployment.");
    return;
  }
  if (job.status === "running") {
    const percent = progress ? progress.percent : 10;
    const stage = progress ? `${progress.stage}: ${progress.message}` : "Worker is running the OCI VM deployment.";
    setVmDeployProgress(percent, `Deployment job ${job.id} running`, stage);
    return;
  }
  if (job.status === "succeeded") {
    const result = job.result || {};
    const ip = result.public_ip || result.private_ip || "IP recorded";
    setVmDeployProgress(100, `Deployment job ${job.id} completed`, `VM deployed and SSH profile is ready. Host: ${ip}`, "ok");
    return;
  }
  if (job.status === "failed") {
    setVmDeployProgress(100, `Deployment job ${job.id} failed`, job.error_message || "OCI VM deployment failed.", "bad");
  }
}

function stopVmDeployMonitor() {
  if (vmDeployPollTimer) clearInterval(vmDeployPollTimer);
  vmDeployPollTimer = null;
}

async function pollVmDeployment(jobId) {
  const job = await api(`/api/jobs/${jobId}`);
  renderVmDeployProgress(job);
  if (["succeeded", "failed"].includes(job.status)) {
    stopVmDeployMonitor();
    await Promise.all([refreshOciInstances(), refreshVms(), refreshJobs()]);
  }
}

function monitorVmDeployment(jobId) {
  stopVmDeployMonitor();
  pollVmDeployment(jobId).catch((e) => {
    setVmDeployProgress(100, `Deployment job ${jobId} status unavailable`, e.message, "bad");
  });
  vmDeployPollTimer = setInterval(() => {
    pollVmDeployment(jobId).catch((e) => {
      setVmDeployProgress(100, `Deployment job ${jobId} status unavailable`, e.message, "bad");
      stopVmDeployMonitor();
    });
  }, 3000);
}

async function loadOciOptions() {
  const query = authQuery();
  const [defaults, compartments, shapes] = await Promise.all([
    api("/api/oci/defaults"),
    api(`/api/oci/compartments${query ? `?${query}` : ""}`),
    api("/api/oci/shapes")
  ]);
  fillSelect("computeCompartment", compartments, (x) => `${x.name} | ${x.id}`);
  fillSelect("networkCompartment", compartments, (x) => `${x.name} | ${x.id}`);
  fillSelect("ociShape", shapes, (x) => x.name, (x) => x.name);
  $("ociShape").value = defaults.default_deployment.shape;
  $("ociOcpus").value = defaults.default_deployment.ocpus;
  $("ociMemory").value = defaults.default_deployment.memory_gb;
  $("ociBootVolume").value = defaults.default_deployment.boot_volume_gb;
  $("newVmUser").value = defaults.default_deployment.username;
  await Promise.all([loadComputeScopedOptions(), loadNetworkScopedOptions()]);
}

async function loadComputeScopedOptions() {
  const compartmentId = $("computeCompartment").value;
  if (!compartmentId) return;
  const query = authQuery();
  const suffix = query ? `&${query}` : "";
  const [ads, images] = await Promise.all([
    api(`/api/oci/availability-domains?compartment_id=${encodeURIComponent(compartmentId)}${suffix}`),
    api(`/api/oci/images?compartment_id=${encodeURIComponent(compartmentId)}${suffix}`)
  ]);
  fillSelect("availabilityDomain", ads, (x) => x.name, (x) => x.name);
  fillSelect("ociImage", images, (x) => `${x.display_name} | ${x.time_created}`, (x) => x.id);
}

async function loadNetworkScopedOptions() {
  const compartmentId = $("networkCompartment").value;
  if (!compartmentId) return;
  const query = authQuery();
  const suffix = query ? `&${query}` : "";
  const vcns = await api(`/api/oci/vcns?compartment_id=${encodeURIComponent(compartmentId)}${suffix}`);
  fillSelect("ociVcn", vcns, (x) => `${x.display_name} | ${x.cidr_block}`, (x) => x.id);
  await loadSubnets();
}

async function loadSubnets() {
  const compartmentId = $("networkCompartment").value;
  const vcnId = $("ociVcn").value;
  if (!compartmentId || !vcnId) return;
  const query = authQuery();
  const suffix = query ? `&${query}` : "";
  const subnets = await api(`/api/oci/subnets?compartment_id=${encodeURIComponent(compartmentId)}&vcn_id=${encodeURIComponent(vcnId)}${suffix}`);
  fillSelect("ociSubnet", subnets, (x) => `${x.display_name} | ${x.cidr_block} | public IP prohibited: ${x.prohibit_public_ip_on_vnic}`, (x) => x.id);
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => activateTab(tab.dataset.tab);
});

document.querySelectorAll("input[name='authMode']").forEach((input) => {
  input.onchange = () => {
    updateAuthMode();
    saveAuthDraft();
  };
});

document.querySelectorAll("input[name='vmMode']").forEach((input) => {
  input.onchange = updateVmMode;
});

$("ociKeyFile").onchange = async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  selectedAuthProfile = null;
  authFormDirty = true;
  ociPrivateKeyContent = await file.text();
  $("ociKeyHint").textContent = `Selected: ${file.name}`;
};

$("ociProfileName").oninput = () => {
  selectedAuthProfile = null;
  authFormDirty = true;
  saveAuthDraft();
};
$("ociProfileText").oninput = () => {
  selectedAuthProfile = null;
  authFormDirty = true;
  saveAuthDraft();
};

$("vmKeyFile").onchange = async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  existingVmPrivateKeyContent = await file.text();
  $("vmKeyHint").textContent = `Selected: ${file.name}`;
};

$("sshPublicKeyFile").onchange = async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  newVmPublicKeyContent = await file.text();
  $("sshPublicKeyHint").textContent = `Selected: ${file.name}`;
};

$("newVmPrivateKeyFile").onchange = async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  newVmPrivateKeyContent = await file.text();
  $("newVmPrivateKeyHint").textContent = `Selected: ${file.name}`;
};

function resetAuthActionState(button) {
  if (button) button.disabled = false;
}

function beginAuthAction(button, message) {
  button.disabled = true;
  $("authState").textContent = message;
  $("authState").className = "pill";
}

$("addAuthProfileBtn").onclick = async () => {
  const mode = selectedAuthMode();
  saveAuthDraft();
  const button = $("addAuthProfileBtn");
  if (mode === "user_profile" && !$("ociProfileName").value.trim()) {
    $("authState").textContent = "Profile name required";
    $("authState").className = "pill bad";
    $("authOutput").textContent = "Enter the OCI config profile header to save and validate this authentication profile.";
    return;
  }
  beginAuthAction(button, "Validating");
  try {
    const result = await api("/api/auth/validate", {
      method: "POST",
      body: JSON.stringify(mode === "instance_principal"
        ? { mode }
        : {
            mode,
            profile_name: $("ociProfileName").value.trim(),
            profile_text: $("ociProfileText").value,
            private_key: ociPrivateKeyContent
          })
    });
    $("authState").textContent = result.ok ? "Validated" : "Validation failed";
    $("authState").className = result.ok ? "pill ok" : "pill bad";
    $("authOutput").textContent = JSON.stringify(result, null, 2);
    if (result.ok) {
      authFormDirty = false;
      selectedAuthProfile = null;
      toast("Authentication profile saved");
      await refreshAuthProfiles();
    }
  } catch (error) {
    $("authState").textContent = "Validation failed";
    $("authState").className = "pill bad";
    $("authOutput").textContent = error.message;
    toast("Add and validation failed");
  } finally {
    resetAuthActionState(button);
  }
};

$("validateSelectedAuthBtn").onclick = async () => {
  if (!selectedAuthProfile) return toast("Select a saved authentication profile first");
  const button = $("validateSelectedAuthBtn");
  beginAuthAction(button, "Validating selected profile");
  try {
    const result = await api(`/api/oci/auth-profiles/${selectedAuthProfile}/validate`, { method: "POST", body: "{}" });
    $("authState").textContent = result.ok ? `Validated: ${result.profile_name || "selected profile"}` : "Validation failed";
    $("authState").className = result.ok ? "pill ok" : "pill bad";
    $("authOutput").textContent = JSON.stringify(result, null, 2);
    await refreshAuthProfiles();
  } catch (error) {
    $("authState").textContent = "Validation failed";
    $("authState").className = "pill bad";
    $("authOutput").textContent = error.message;
    toast("Selected profile validation failed");
  } finally {
    resetAuthActionState(button);
  }
};

$("deleteAuthProfileBtn").onclick = async () => {
  if (!selectedAuthProfile) return toast("Select a saved authentication profile first");
  await api(`/api/oci/auth-profiles/${selectedAuthProfile}`, { method: "DELETE" });
  toast(`Deleted auth profile ${selectedAuthProfile}`);
  selectedAuthProfile = null;
  authFormDirty = false;
  $("ociProfileName").value = "";
  $("ociProfileText").value = "";
  ociPrivateKeyContent = "";
  $("ociKeyFile").value = "";
  $("ociKeyHint").textContent = "No private key file selected.";
  await refreshAuthProfiles();
};

$("saveVmBtn").onclick = async () => {
  const saved = await api("/api/vm-profiles", {
    method: "POST",
    body: JSON.stringify({
      name: $("vmName").value,
      host: $("vmHost").value,
      port: Number($("vmPort").value),
      username: $("vmUser").value,
      private_key: existingVmPrivateKeyContent,
      passphrase: $("vmPassphrase").value || null
    })
  });
  selectedVm = saved.id;
  selectedBenchmarkVm = saved.id;
  toast("VM profile saved");
  await refreshVms();
};

$("healthBtn").onclick = async () => {
  if (!selectedVm) return toast("Select a VM profile first");
  const data = await api(`/api/vm-profiles/${selectedVm}/health-checks`, { method: "POST", body: "{}" });
  toast(`Health check queued as job ${data.job_id}`);
  await refreshJobs();
};

$("loadOciOptionsBtn").onclick = () => loadOciOptions().then(() => toast("OCI options loaded")).catch((e) => toast(e.message));
$("ociAuthProfileSelect").onchange = () => {
  selectedAuthProfile = currentSelectNumber("ociAuthProfileSelect");
};
$("computeCompartment").onchange = () => loadComputeScopedOptions().catch((e) => toast(e.message));
$("networkCompartment").onchange = () => loadNetworkScopedOptions().catch((e) => toast(e.message));
$("ociVcn").onchange = () => loadSubnets().catch((e) => toast(e.message));

$("createVmBtn").onclick = async () => {
  if (!$("newVmName").value.trim()) {
    setVmDeployProgress(100, "Deployment form incomplete", "Enter a VM display name before creating the VM.", "bad");
    return toast("VM display name is required");
  }
  if (!$("computeCompartment").value || !$("availabilityDomain").value || !$("ociShape").value || !$("ociImage").value || !$("ociSubnet").value) {
    setVmDeployProgress(100, "Deployment form incomplete", "Load OCI options and select compute, image, VCN, and subnet values.", "bad");
    return toast("Select all OCI deployment values");
  }
  if (!newVmPublicKeyContent.trim()) {
    setVmDeployProgress(100, "Deployment form incomplete", "Upload an SSH public key before creating the VM.", "bad");
    return toast("SSH public key is required");
  }
  showVmDeployProgress();
  setVmDeployProgress(1, "Submitting deployment", "Sending VM deployment request to the app.");
  const data = await api("/api/oci/instances", {
    method: "POST",
    body: JSON.stringify({
      display_name: $("newVmName").value,
      oci_auth_profile_id: currentSelectNumber("ociAuthProfileSelect"),
      compartment_id: $("computeCompartment").value,
      availability_domain: $("availabilityDomain").value,
      shape: $("ociShape").value,
      ocpus: Number($("ociOcpus").value),
      memory_gb: Number($("ociMemory").value),
      boot_volume_gb: Number($("ociBootVolume").value),
      image_id: $("ociImage").value,
      subnet_id: $("ociSubnet").value,
      assign_public_ip: $("assignPublicIp").value === "true",
      username: $("newVmUser").value || "ubuntu",
      ssh_public_key: newVmPublicKeyContent,
      ssh_private_key: newVmPrivateKeyContent || null
    })
  });
  vmDeployJobId = data.job_id;
  setVmDeployProgress(2, `Deployment job ${data.job_id} queued`, "Waiting for the worker to start the OCI VM deployment.");
  monitorVmDeployment(data.job_id);
  toast(`VM deployment queued as job ${data.job_id}`);
  await refreshJobs();
};

$("deleteVmBtn").onclick = async () => {
  if (!selectedVm) return toast("Select a VM profile first");
  await api(`/api/vm-profiles/${selectedVm}`, { method: "DELETE" });
  toast(`Deleted VM profile ${selectedVm}`);
  selectedVm = null;
  selectedBenchmarkVm = null;
  await Promise.all([refreshVms(), refreshOciInstances()]);
};

$("destroyVmBtn").onclick = async () => {
  if (!ociInstancesCache.length) await refreshOciInstances();
  const instanceRecordId = resolveSelectedOciInstanceForDestroy();
  if (!instanceRecordId) return toast("Select a provisioned OCI VM or its generated SSH profile first");
  const target = ociInstancesCache.find((item) => item.id === instanceRecordId);
  const data = await api(`/api/oci/instances/${instanceRecordId}/destroy`, { method: "POST", body: "{}" });
  toast(`VM destroy queued as job ${data.job_id}${target ? ` for ${target.display_name}` : ""}`);
  await Promise.all([refreshJobs(), refreshOciInstances()]);
};

$("saveVllmBtn").onclick = async () => {
  const maxBatched = $("maxBatchedTokens").value;
  const kvCache = $("kvCache").value;
  await api("/api/vllm-profiles", {
    method: "POST",
    body: JSON.stringify({
      name: $("vllmName").value,
      model_ref: $("modelRef").value,
      model_revision: $("modelRevision").value.trim() || null,
      quantization: $("quantization").value,
      dtype: $("dtype").value,
      max_model_len: Number($("maxModelLen").value),
      max_num_batched_tokens: maxBatched ? Number(maxBatched) : null,
      max_num_seqs: Number($("maxSeqs").value),
      cpu_kvcache_space_gib: kvCache ? Number(kvCache) : null,
      reserved_cpu: Number($("reservedCpu").value),
      prefix_cache_enabled: $("prefixCache").checked,
      chunked_prefill_enabled: $("chunkedPrefill").checked,
      request_timeout_seconds: Number($("requestTimeout").value),
      port: Number($("runtimePort").value)
    })
  });
  toast("vLLM profile saved");
  await refreshVllm();
};

$("deleteVllmBtn").onclick = async () => {
  if (!selectedVllm) return toast("Select a vLLM profile first");
  await api(`/api/vllm-profiles/${selectedVllm}`, { method: "DELETE" });
  toast(`Deleted vLLM profile ${selectedVllm}`);
  selectedVllm = null;
  await refreshVllm();
};

$("runtimeVmSelect").onchange = async () => {
  selectedVm = currentSelectNumber("runtimeVmSelect");
  selectedRuntimeProcess = null;
  await refreshRuntimeProcesses();
};

$("benchmarkVmSelect").onchange = () => {
  selectedBenchmarkVm = currentSelectNumber("benchmarkVmSelect");
};
$("benchmarkVllmSelect").onchange = () => {};
$("quantization").onchange = syncModelPrecisionDtype;
syncModelPrecisionDtype();
$("refreshRuntimeBtn").onclick = () => refreshRuntimeProcesses().then(() => toast("Runtime processes refreshed"));

$("startRuntimeBtn").onclick = async () => {
  const vmId = currentSelectNumber("runtimeVmSelect") || selectedVm;
  if (!vmId || !selectedVllm) return toast("Select VM and vLLM profiles first");
  const data = await api("/api/runtime/start", {
    method: "POST",
    body: JSON.stringify({ vm_profile_id: vmId, vllm_profile_id: selectedVllm, readiness_timeout_seconds: 900 })
  });
  toast(`Runtime start queued as job ${data.job_id}`);
  await refreshJobs();
  await refreshRuntimeProcesses(false);
};

$("stopRuntimeBtn").onclick = async () => {
  const vmId = currentSelectNumber("runtimeVmSelect") || selectedVm;
  if (!vmId) return toast("Select a VM first");
  if (!selectedRuntimeProcess) return toast("Select a running vLLM process first");
  const data = await api("/api/runtime/stop", {
    method: "POST",
    body: JSON.stringify({ vm_profile_id: vmId, pid: selectedRuntimeProcess.pid, port: selectedRuntimeProcess.port })
  });
  toast(`Runtime stop queued as job ${data.job_id}`);
  await refreshJobs();
  await refreshRuntimeProcesses(false);
};

$("runBenchmarkBtn").onclick = async () => {
  const vmId = currentSelectNumber("benchmarkVmSelect");
  const vllmId = currentSelectNumber("benchmarkVllmSelect");
  const concurrencies = Array.from($("concurrency").selectedOptions).map((option) => Number(option.value)).filter((value) => value > 0);
  if (!vmId || !vllmId) return toast("Select benchmark VM and vLLM profiles first");
  if (!concurrencies.length) return toast("Select at least one concurrency");
  const data = await api("/api/benchmarks", {
    method: "POST",
      body: JSON.stringify({
        vm_profile_id: vmId,
        vllm_profile_id: vllmId,
        workload_name: $("workloadPreset").value,
        input_tokens: Number($("inputTokens").value),
        output_tokens: Number($("outputTokens").value),
        concurrencies,
        max_requests: Number($("maxRequests").value),
        benchmark_mode: $("benchmarkMode").value,
        max_duration_seconds: Number($("maxDurationSeconds").value),
        max_errors: $("maxErrors").value ? Number($("maxErrors").value) : null,
        cooldown_seconds: Number($("cooldownSeconds").value),
        seed: Number($("benchmarkSeed").value),
        sweep_size: Number($("sweepSize").value),
        sweep_strategy_type: $("sweepStrategyType").value
    })
  });
  toast(`Benchmark queued as job ${data.job_id}`);
  await refreshJobs();
};

$("copyMarkdownBtn").onclick = async () => {
  if (!selectedBenchmarkJob) return toast("Select a successful benchmark job first");
  const md = await api(`/api/reports/${selectedBenchmarkJob}.md`, { headers: { Accept: "text/markdown" } });
  await navigator.clipboard.writeText(md);
  toast("Markdown copied");
};

$("deleteJobBtn").onclick = async () => {
  if (!selectedQueueJobId) return toast("Select a job first");
  await api(`/api/jobs/${selectedQueueJobId}`, { method: "DELETE" });
  toast(`Deleted job ${selectedQueueJobId}`);
  selectedQueueJobId = null;
  await refreshJobs();
};

function download(path) {
  if (!selectedBenchmarkJob) return toast("Select a successful benchmark job first");
  window.open(`/api/reports/${selectedBenchmarkJob}${path}?token=${encodeURIComponent(token)}`, "_blank");
}

$("downloadCsvBtn").onclick = () => download(".csv");
$("downloadJsonBtn").onclick = () => download(".json");
$("downloadMdBtn").onclick = () => download(".md");
$("downloadHtmlBtn").onclick = () => download(".html");
$("workloadPreset").onchange = applyWorkloadPreset;

initWorkloads();
initHelpTooltips();
restoreAuthDraft();
updateAuthMode();
const savedTab = localStorage.getItem("activeWorkflowTab") || "auth";
activateTab(document.getElementById(`tab-${savedTab}`) ? savedTab : "auth");
ensureLocalSession().catch((e) => toast(e.message));
setInterval(() => { if (token) refreshJobs().catch(() => {}); }, 5000);
setInterval(() => { if (token) quickCheckVms().catch(() => {}); }, 30000);
