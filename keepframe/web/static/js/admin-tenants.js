import { fetchAdminOrRedirect } from "/static/js/api.js?v=20260921u";

const STATUS = { ok: "정상", quota: "할당량 초과", suspended: "정지" };
const ROLE = { admin: "운영자", billing: "결제", review_lead: "검수 리드", maker: "제작" };
const FILTER_KEYS = ["ok", "quota", "suspended"];
const QUOTA_FULL_PCT = 100;

let tenants = [];
let selected = null;
let policyNote = "";
let filter = "ok";

const rowsEl = document.getElementById("tenant-rows");
const roleBox = document.getElementById("role-box");

function pct(t) {
  if (!t.analyze_quota_min) return 0;
  return Math.min(QUOTA_FULL_PCT, Math.round((t.analyze_min / t.analyze_quota_min) * QUOTA_FULL_PCT));
}

function isOverQuota(t) {
  return t.status === "quota";
}

function renderRows() {
  rowsEl.innerHTML = "";
  const list = tenants.filter((t) => t.status === filter);
  list.forEach((t) => {
    const row = document.createElement("div");
    const isSelected = selected && selected.id === t.id;
    row.className = "admin-table-row" + (isSelected ? " admin-table-row--selected" : "");
    const over = isOverQuota(t);
    row.innerHTML = `
<div>
<div style="font-weight:500;font-size:var(--fs-body-sm)">${t.name}</div>
<div class="mono" style="font-size:var(--fs-micro);color:var(--muted)">${t.id}</div>
</div>
<div>${t.members}</div>
<div>
<div style="font-size:var(--fs-micro);margin-bottom:4px${over ? ";color:var(--error)" : ""}">${t.analyze_min}분 <span style="color:var(--muted)">/ ${t.analyze_quota_min}분</span>${t.render_min ? ` · 렌더 ${t.render_min}분` : ""}</div>
<div class="progress-bar"><div class="progress-bar__fill" style="width:${pct(t)}%${over ? ";background:var(--error)" : ""}"></div></div>
</div>
<div style="text-align:right"><span class="status-pill${over || t.status === "suspended" ? " status-pill--warn" : ""}">${t.status === "ok" ? '<span class="status-dot"></span>' : ""}${STATUS[t.status] || t.status}</span></div>`;
    row.addEventListener("click", () => selectTenant(t));
    rowsEl.appendChild(row);
  });
}

async function loadMembers(t) {
  const data = await fetchAdminOrRedirect(`/admin/api/tenants/${encodeURIComponent(t.id)}`);
  const counts = {};
  data.members.forEach((m) => { counts[m.role] = (counts[m.role] || 0) + 1; });
  roleBox.innerHTML = Object.entries(counts).map(([role, n]) =>
    `<div class="role-row"><span style="color:var(--muted)">${ROLE[role] || role}</span><span>${n}</span></div>`
  ).join("") || '<div class="role-row"><span style="color:var(--muted)">—</span><span>0</span></div>';
}

function selectTenant(t) {
  selected = t;
  document.getElementById("inspector-name").textContent = t.name;
  document.getElementById("inspector-meta").textContent = `${t.id} · ${t.created}`;
  document.getElementById("policy-note").textContent = policyNote;
  document.getElementById("btn-suspend").disabled = t.status === "suspended";
  renderRows();
  loadMembers(t).catch(() => { roleBox.innerHTML = ""; });
}

document.querySelectorAll(".segmented__btn").forEach((btn, i) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".segmented__btn").forEach((b) => b.classList.remove("segmented__btn--active"));
    btn.classList.add("segmented__btn--active");
    filter = FILTER_KEYS[i];
    renderRows();
  });
});

document.getElementById("btn-members").addEventListener("click", () => {
  if (selected) loadMembers(selected);
});

document.getElementById("btn-suspend").addEventListener("click", async () => {
  const alreadySuspended = !selected || selected.status === "suspended";
  if (alreadySuspended) return;
  if (!confirm(`${selected.name} 테넌트를 정지할까요?`)) return;
  const data = await fetchAdminOrRedirect(`/admin/api/tenants/${encodeURIComponent(selected.id)}/suspend`, { method: "POST", body: "{}" });
  const idx = tenants.findIndex((t) => t.id === selected.id);
  if (idx >= 0) tenants[idx] = data.tenant;
  selectTenant(data.tenant);
});

Promise.all([
  fetchAdminOrRedirect("/admin/api/tenants"),
  fetchAdminOrRedirect("/admin/api/policy"),
]).then(([tData, pol]) => {
  tenants = tData.tenants;
  policyNote = pol.note;
  if (tenants.length) selectTenant(tenants[0]);
  else renderRows();
}).catch(() => {});
