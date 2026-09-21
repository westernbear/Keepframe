import { fetchAdminOrRedirect } from "/static/js/api.js?v=20260921u";

const RETRY_CAP = 4;
const BADGE_CLASS = {
  "실행": "badge--active",
  "실패": "badge--error",
  "대기": "badge--done",
};

let jobs = [];
let selected = null;
let policyNote = "";

const tbody = document.getElementById("job-rows");

function isQuarantined(j) {
  return j.status === "검역";
}

function isFailed(j) {
  return j.status === "실패";
}

function retriesExhausted(j) {
  return (j.retries ?? 0) >= RETRY_CAP;
}

function retryLabel(j) {
  if (isQuarantined(j)) return "—";
  const n = j.retries ?? 0;
  if (retriesExhausted(j)) return `${n}/${RETRY_CAP} 한도 소진`;
  return `${n}/${RETRY_CAP}`;
}

function badgeClass(status) {
  return BADGE_CLASS[status] || "";
}

function renderQuarantineRow(j) {
  return `<td class="mono" style="color:var(--muted)">${j.id}</td><td>${j.tenant}</td><td class="mono" style="font-size:var(--fs-micro);color:var(--muted)">${j.kind}</td><td>${j.target}</td><td colspan="4"><span class="badge">검역으로 이동</span></td>`;
}

function renderActiveRow(j) {
  const exhausted = retriesExhausted(j);
  return `<td class="mono" style="color:var(--muted)">${j.id}</td><td>${j.tenant}</td><td class="mono" style="font-size:var(--fs-micro);color:var(--muted)">${j.kind}</td><td>${j.target}</td><td><span class="badge ${badgeClass(j.status)}">${j.status}</span></td><td class="mono" style="font-size:var(--fs-micro)">${j.gpu || "—"}</td><td style="color:var(--muted)">—</td><td style="color:${exhausted ? "var(--error)" : "var(--muted)"}">${retryLabel(j)}</td>`;
}

function renderTable() {
  tbody.innerHTML = "";
  jobs.forEach((j) => {
    const tr = document.createElement("tr");
    if (selected && selected.id === j.id) tr.className = "row-selected";
    if (isQuarantined(j)) tr.style.opacity = "0.6";
    tr.innerHTML = isQuarantined(j) ? renderQuarantineRow(j) : renderActiveRow(j);
    tr.addEventListener("click", () => selectJob(j));
    tbody.appendChild(tr);
  });
}

function selectJob(j) {
  selected = j;
  document.getElementById("drawer-id").textContent = j.id;
  document.getElementById("drawer-target").textContent = j.target;
  const fail = isFailed(j);
  document.getElementById("drawer-error").textContent = fail ? (j.error || "작업 실패") : "—";
  const retriesEl = document.getElementById("drawer-retries");
  retriesEl.textContent = fail ? retryLabel(j) : "—";
  retriesEl.style.color = retriesExhausted(j) ? "var(--error)" : "var(--ink)";
  document.getElementById("drawer-policy").textContent = fail ? `"${policyNote}"` : "";
  renderTable();
}

Promise.all([
  fetchAdminOrRedirect("/admin/api/jobs"),
  fetchAdminOrRedirect("/admin/api/policy"),
]).then(([jData, pol]) => {
  jobs = jData.jobs;
  policyNote = pol.note;
  const failed = jobs.find(isFailed) || jobs[0];
  if (failed) selectJob(failed);
  else renderTable();
}).catch(() => {});
