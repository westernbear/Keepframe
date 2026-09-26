import { fetchAdminOrRedirect } from "/static/js/api.js?v=20260921v";
import { T } from "/static/js/i18n.js?v=20260921v";

let retryCap = 0;
let jobs = [];
let selected = null;
const tbody = document.getElementById("job-rows");

const STATUS_KEYS = { queued: "admin.status.queued", running: "admin.status.running", done: "admin.status.done", failed: "admin.status.failed", error: "admin.status.failed", "대기": "admin.status.queued", "실행": "admin.status.running", "완료": "admin.status.done", "실패": "admin.status.failed" };
function canonicalStatus(status) {
  if (status === "대기") return "queued";
  if (status === "실행") return "running";
  if (status === "완료") return "done";
  if (status === "실패" || status === "error") return "failed";
  return status;
}
function statusLabel(status) { return STATUS_KEYS[status] ? T(STATUS_KEYS[status]) : status; }
function isQuarantined(j) { return j.status === "검역" || j.status === "quarantine"; }
function isFailed(j) { return ["failed", "error", "실패"].includes(canonicalStatus(j.status)); }
function retriesExhausted(j) { return (j.retries ?? 0) >= retryCap; }
function retryLabel(j) {
  if (isQuarantined(j)) return T("common.none");
  const n = j.retries ?? 0;
  return retriesExhausted(j) ? `${n}/${retryCap} ${T("admin.queue.retryExhausted")}` : `${n}/${retryCap}`;
}
function badgeClass(status) {
  return { running: "badge--active", failed: "badge--error", error: "badge--error", queued: "badge--done", "실행": "badge--active", "실패": "badge--error", "대기": "badge--done" }[status] || "";
}
function appendCell(tr, value, className = "", colspan = 1) {
  const td = document.createElement("td");
  if (className) td.className = className;
  if (colspan > 1) td.colSpan = colspan;
  td.textContent = value;
  tr.appendChild(td);
}
function renderRow(j) {
  const tr = document.createElement("tr");
  tr.tabIndex = 0;
  tr.setAttribute("aria-label", `${j.id} ${j.target}`);
  if (selected && selected.id === j.id) tr.className = "row-selected";
  if (isQuarantined(j)) tr.classList.add("row-dimmed");
  appendCell(tr, j.id, "mono tenant-id");
  appendCell(tr, j.tenant);
  appendCell(tr, j.kind, "mono tenant-id");
  appendCell(tr, j.target);
  if (isQuarantined(j)) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = T("admin.queue.movedToQuarantine");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.appendChild(badge);
    tr.appendChild(td);
  } else {
    const badge = document.createElement("span");
    badge.className = `badge ${badgeClass(j.status)}`;
    badge.textContent = statusLabel(j.status);
    const status = document.createElement("td");
    status.appendChild(badge);
    tr.appendChild(status);
    appendCell(tr, j.gpu || T("common.none"), "mono tenant-id");
    appendCell(tr, T("common.none"), "quota-muted");
    appendCell(tr, retryLabel(j), retriesExhausted(j) ? "retry--exhausted" : "quota-muted");
  }
  tr.addEventListener("click", () => selectJob(j));
  tr.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectJob(j); } });
  return tr;
}
function renderTable() {
  const counts = jobs.reduce((out, job) => { out[canonicalStatus(job.status)] = (out[canonicalStatus(job.status)] || 0) + 1; return out; }, {});
  document.getElementById("queue-queued").textContent = counts.queued || 0;
  document.getElementById("queue-running").textContent = counts.running || 0;
  document.getElementById("queue-failed").textContent = counts.failed || 0;
  document.getElementById("queue-gpu").textContent = jobs.filter((j) => j.gpu).length;
  tbody.innerHTML = "";
  jobs.forEach((j) => tbody.appendChild(renderRow(j)));
}
function selectJob(j) {
  selected = j;
  document.getElementById("drawer-id").textContent = j.id;
  document.getElementById("drawer-target").textContent = j.target;
  const fail = isFailed(j);
  document.getElementById("drawer-error").textContent = fail ? (j.error || T("admin.queue.failedDefault")) : "—";
  const retriesEl = document.getElementById("drawer-retries");
  retriesEl.textContent = fail ? retryLabel(j) : "—";
  retriesEl.classList.toggle("retry--exhausted", fail && retriesExhausted(j));
  document.getElementById("drawer-policy").textContent = fail ? `"${T("admin.policy")}"` : "";
  renderTable();
}
function showLoadError(err) {
  if (err.status === 401) return;
  tbody.innerHTML = "";
  const tr = document.createElement("tr");
  appendCell(tr, err.message || T("admin.loadFailed"), "admin-load-error", 8);
  tbody.appendChild(tr);
}
function repaint() { if (selected) selectJob(selected); else renderTable(); }
window.addEventListener("keepframe:lang", repaint);
Promise.all([fetchAdminOrRedirect("/admin/api/jobs"), fetchAdminOrRedirect("/admin/api/policy")])
  .then(([jData, pol]) => {
    jobs = jData.jobs;
    retryCap = pol.retry_cap;
    const failed = jobs.find(isFailed) || jobs[0];
    if (failed) selectJob(failed); else renderTable();
  }).catch(showLoadError);
