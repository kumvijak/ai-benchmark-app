/* Dedicated reporting queue and catalogue. All dynamic content uses text nodes. */
const reportSourceSelection = new Set();
let generatedReportsCache = [];
const reportArchiveLinks = new Map();
let reportRefreshBusy = false;
let reportSourceSignature = '';
let reportCatalogueSignature = '';

function reportButton(label, action) {
  const button = document.createElement('button');
  button.textContent = label;
  button.className = 'secondary';
  button.onclick = () => Promise.resolve().then(action).catch(e => toast(e.message));
  return button;
}

function renderGeneratedCatalogue() {
  const container = $('generatedCatalogue');
  container.replaceChildren();
  const query = $('generatedFilter').value.trim().toLowerCase();
  for (const report of generatedReportsCache.filter(r => `${r.id} ${r.title}`.toLowerCase().includes(query))) {
    const card = document.createElement('article'); card.className = 'generated-card';
    const heading = document.createElement('h3'); heading.textContent = `#${report.id} · ${report.title}`;
    const status = document.createElement('p');
    status.textContent = `${report.status} · ${report.stage} · ${report.progress}% · ${report.created_at} UTC`;
    card.append(heading, status);
    if (report.status === 'running' || report.status === 'queued') {
      const progress = document.createElement('progress'); progress.max = 100; progress.value = report.progress;
      progress.setAttribute('aria-label', 'Report generation progress'); card.append(progress);
    }
    if (report.summary) {
      const summary = document.createElement('p');
      summary.textContent = `${report.summary.configurations} configurations · ${report.summary.attempts} attempts · ${report.summary.points} load points · Evidence: ${report.summary.evidence_status}`;
      card.append(summary);
    }
    if (report.error_message) {
      const error = document.createElement('p'); error.textContent = report.error_message; card.append(error);
    }
    const actions = document.createElement('div'); actions.className = 'actions';
    if (report.status === 'succeeded') {
      actions.append(reportButton('Open report', async () => {
        const target = window.open('about:blank', '_blank');
        if (!target) throw new Error('Allow opening a report tab for this app');
        target.opener = null;
        try {
          await api(`/api/generated-reports/${report.id}/access`, {method:'POST',body:'{}'});
          target.location.href = `/api/generated-reports/${report.id}/files/index.html`;
        } catch(error) { target.close(); throw error; }
      }));
      for (const [label, filename] of [['Download ZIP', 'report.zip'], ['Download CSV', 'metrics.csv'], ['Download JSON', 'report.json']]) {
        actions.append(reportButton(label, async () => {
          const response = await fetch(`/api/generated-reports/${report.id}/files/${filename}`, {headers:{Authorization:`Bearer ${token}`}});
          if (!response.ok) throw new Error(await response.text());
          const url = URL.createObjectURL(await response.blob());
          const link = document.createElement('a'); link.href = url; link.download = `report-${report.id}-${filename}`;
          link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        }));
      }
    }
    if (report.status === 'succeeded') {
      const archiveStatus = document.createElement('p');
      archiveStatus.textContent = `Archive: ${report.archive_status}${report.archive_error ? ' · ' + report.archive_error : ''}`;
      card.append(archiveStatus);
      if (report.archive_status === 'ready') actions.append(reportButton('Create independent ZIP link', async () => {
        const result = await api(`/api/generated-reports/${report.id}/archive/download-link`, {method:'POST', body:'{}'});
        reportArchiveLinks.set(report.id, result);
        renderGeneratedCatalogue();
      }));
      if (reportArchiveLinks.has(report.id)) {
        const result = reportArchiveLinks.get(report.id);
        const panel = document.createElement('div');
        const link = document.createElement('a'); link.href = result.url; link.textContent = 'Download archived ZIP';
        link.rel = 'noopener noreferrer'; link.referrerPolicy = 'no-referrer';
        const input = document.createElement('input'); input.type = 'text'; input.readOnly = true; input.value = result.url;
        input.setAttribute('aria-label', 'Independent archive download URL');
        input.onclick = () => input.select();
        const note = document.createElement('p');
        note.textContent = `Save this link before shutting down the app. Works until ${new Date(result.expires_at).toLocaleString()}. Anyone with the link can download this ZIP.`;
        panel.append(link, input, reportButton('Copy link', async () => {
          try { await navigator.clipboard.writeText(result.url); toast('Link copied'); }
          catch (_) { input.focus(); input.select(); toast('Select and copy the link above'); }
        }), note);
        card.append(panel);
      }
      if (report.archive_status === 'failed') actions.append(reportButton('Retry archive', async () => {
        await api(`/api/generated-reports/${report.id}/archive/retry`, {method:'POST',body:'{}'});
        await refreshGeneratedReports();
      }));
    }
    if (report.status === 'failed') actions.append(reportButton('Retry report', async () => {
      await api(`/api/generated-reports/${report.id}/retry`, {method:'POST',body:'{}'});
      await refreshGeneratedReports();
    }));
    card.append(actions); container.append(card);
  }
  if (!container.children.length) container.textContent = 'No matching reports. Select finished attempts and queue a report.';
}

async function refreshGeneratedReports() {
  if (!token || reportRefreshBusy) return;
  reportRefreshBusy = true;
  try {
    const [sources, reports] = await Promise.all([api('/api/generated-reports/sources'), api('/api/generated-reports')]);
    const catalogueChanged = JSON.stringify(reports) !== reportCatalogueSignature;
    reportCatalogueSignature = JSON.stringify(reports);
    generatedReportsCache = reports;
    if (JSON.stringify(sources) !== reportSourceSignature) {
    reportSourceSignature = JSON.stringify(sources);
    const sourceContainer = $('generatedSources'); sourceContainer.replaceChildren();
    const available = new Set(sources.map(s => s.id));
    for (const id of reportSourceSelection) if (!available.has(id)) reportSourceSelection.delete(id);
    for (const source of sources) {
      const label = document.createElement('label');
      const input = document.createElement('input'); input.type = 'checkbox'; input.value = source.id;
      input.checked = reportSourceSelection.has(source.id);
      input.onchange = () => input.checked ? reportSourceSelection.add(source.id) : reportSourceSelection.delete(source.id);
      label.append(input, document.createTextNode(`Job ${source.id} · ${source.model || "Model unrecorded"} · ${source.precision || "Precision unverified"} · ${source.workload || "Workload unrecorded"} · ${source.status} · ${source.finished_at || source.created_at} UTC`));
      sourceContainer.append(label);
    }
    if (!sources.length) sourceContainer.textContent = 'No finished benchmark attempts yet.';
    }
    const counts = {queued:0,running:0,succeeded:0,failed:0};
    reports.forEach(r => counts[r.status]++);
    $('generatedQueueStatus').textContent = `Reporting queue — Queued: ${counts.queued} · Running: ${counts.running} · Ready: ${counts.succeeded} · Failed: ${counts.failed}`;
    if (catalogueChanged) renderGeneratedCatalogue();
  } finally { reportRefreshBusy = false; }
}

$('generateReportBtn').onclick = async () => {
  const button = $('generateReportBtn'); button.disabled = true;
  try {
    const title = $('generatedTitle').value.trim();
    if (!title || !reportSourceSelection.size) return toast('Enter a title and select finished benchmark attempts');
    if (reportSourceSelection.size > 100) return toast('Select at most 100 attempts per report');
    const result = await api('/api/generated-reports', {method:'POST',body:JSON.stringify({title,job_ids:[...reportSourceSelection]})});
    toast(`Report #${result.id}: ${result.status}`); await refreshGeneratedReports();
  } catch(e) { toast(e.message); } finally { button.disabled = false; }
};
$('selectReportSourcesBtn').onclick = () => {
  reportSourceSelection.clear();
  $('generatedSources').querySelectorAll('input').forEach((input,index) => {
    input.checked = index < 100;
    if (input.checked) reportSourceSelection.add(Number(input.value));
  });
};
$('clearReportSourcesBtn').onclick = () => {
  reportSourceSelection.clear(); $('generatedSources').querySelectorAll('input').forEach(input => input.checked = false);
};
$('generatedFilter').oninput = renderGeneratedCatalogue;
$('refreshGeneratedBtn').onclick = () => refreshGeneratedReports().catch(e => toast(e.message));
document.querySelector('[data-tab="generated"]').addEventListener('click', () => refreshGeneratedReports().catch(e => toast(e.message)));
refreshGeneratedReports().catch(e => toast(e.message));
setInterval(() => { if (token && $('tab-generated').classList.contains('active')) refreshGeneratedReports().catch(e => toast(e.message)); }, 5000);
