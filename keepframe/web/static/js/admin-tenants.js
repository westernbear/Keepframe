import { fetchAdminOrRedirect } from "/static/js/api.js?v=20260921v";
import { T, Tf } from "/static/js/i18n.js?v=20260921v";

const STATUS_KEYS = { ok: "admin.status.ok", quota: "admin.status.quota", suspended: "admin.status.suspended" };
const ROLE_KEYS = { admin: "admin.role.admin", billing: "admin.role.billing", review_lead: "admin.role.reviewLead", maker: "admin.role.maker" };
const QUOTA_FULL_PCT = 100;

let tenants = [];
let selected = null;
let filter = "ok";
const members = new Map();

const rowsEl = document.getElementById("tenant-rows");
const roleBox = document.getElementById("role-box");

function statusLabel(status) { return STATUS_KEYS[status] ? T(STATUS_KEYS[status]) : status; }
function roleLabel(role) { return ROLE_KEYS[role] ? T(ROLE_KEYS[role]) : role; }
function pct(t) {
  if (!t.analyze_quota_min) return 0;
  return Math.min(QUOTA_FULL_PCT, Math.round((t.analyze_min / t.analyze_quota_min) * QUOTA_FULL_PCT));
}
function isOverQuota(t) { return t.status === "quota"; }
function isSuspended(t) { return t.status === "suspended"; }

function createQuotaCell(t) {
  const over = isOverQuota(t);
  const wrap = document.createElement("div");
  const meta = document.createElement("div");
  meta.className = "quota-meta" + (over ? " quota-meta--over" : "");
  const used = document.createTextNode(Tf("admin.usage.minutes", { n: t.analyze_min }) + " ");
  const cap = document.createElement("span");
  cap.className = "quota-muted";
  cap.textContent = `/ ${Tf("admin.usage.minutes", { n: t.analyze_quota_min })}`;
  meta.append(used, cap);
  if (t.render_min) meta.append(` · ${Tf("admin.usage.render", { n: t.render_min })}`);
  const bar = document.createElement("div");
  bar.className = "progress-bar";
  const fill = document.createElement("div");
  fill.className = "progress-bar__fill" + (over ? " progress-bar__fill--error" : "");
  fill.style.width = `${pct(t)}%`;
  bar.appendChild(fill);
  wrap.append(meta, bar);
  return wrap;
}

function createStatusCell(t) {
  const cell = document.createElement("div");
  cell.className = "status-cell";
  const pill = document.createElement("span");
  const warn = isOverQuota(t) || isSuspended(t);
  pill.className = "status-pill" + (warn ? " status-pill--warn" : "");
  if (t.status === "ok") {
    const dot = document.createElement("span");
    dot.className = "status-dot";
    pill.appendChild(dot);
  }
  pill.append(statusLabel(t.status));
  cell.appendChild(pill);
  return cell;
}

function createTenantRow(t) {
  const row = document.createElement("div");
  const isSelected = selected && selected.id === t.id;
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.setAttribute("aria-label", t.name);
  row.className = "admin-table-row" + (isSelected ? " admin-table-row--selected" : "");
  const nameCell = document.createElement("div");
  const name = document.createElement("div");
  name.className = "tenant-name";
  name.textContent = t.name;
  const id = document.createElement("div");
  id.className = "mono tenant-id";
  id.textContent = t.id;
  nameCell.append(name, id);
  const memberCount = document.createElement("div");
  memberCount.textContent = String(t.members);
  row.append(nameCell, memberCount, createQuotaCell(t), createStatusCell(t));
  row.addEventListener("click", () => selectTenant(t));
  row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectTenant(t); } });
  return row;
}

function renderRows() {
  rowsEl.innerHTML = "";
  tenants.filter((t) => t.status === filter).forEach((t) => rowsEl.appendChild(createTenantRow(t)));
}

function renderMembers(t) {
  const list = members.get(t.id) || [];
  const counts = {};
  list.forEach((m) => { counts[m.role] = (counts[m.role] || 0) + 1; });
  roleBox.innerHTML = "";
  Object.entries(counts).forEach(([role, n]) => {
    const row = document.createElement("div");
    row.className = "role-row";
    const label = document.createElement("span");
    label.className = "quota-muted";
    label.textContent = roleLabel(role);
    const count = document.createElement("span");
    count.textContent = String(n);
    row.append(label, count);
    roleBox.appendChild(row);
  });
  if (!list.length) roleBox.innerHTML = `<div class="role-row"><span class="quota-muted">${T("admin.members.zero")}</span><span>0</span></div>`;
}

async function loadMembers(t) {
  if (!members.has(t.id)) {
    const data = await fetchAdminOrRedirect(`/admin/api/tenants/${encodeURIComponent(t.id)}`);
    members.set(t.id, data.members);
  }
  if (selected && selected.id === t.id) renderMembers(t);
}

function selectTenant(t) {
  selected = t;
  document.getElementById("inspector-name").textContent = t.name;
  document.getElementById("inspector-meta").textContent = `${t.id} · ${t.created}`;
  document.getElementById("policy-note").textContent = T("admin.policy");
  document.getElementById("btn-suspend").disabled = isSuspended(t);
  renderRows();
  renderMembers(t);
  loadMembers(t).catch(() => { roleBox.innerHTML = ""; });
}

function bindFilters() {
  document.querySelectorAll("[data-filter]").forEach((btn) => btn.addEventListener("click", () => {
    document.querySelectorAll("[data-filter]").forEach((b) => b.classList.remove("segmented__btn--active"));
    btn.classList.add("segmented__btn--active");
    filter = btn.dataset.filter;
    renderRows();
  }));
}

function repaint() {
  if (selected) selectTenant(selected);
  else renderRows();
}
function showLoadError(err) {
  if (err.status === 401) return;
  rowsEl.textContent = err.message || T("admin.loadFailed");
  rowsEl.classList.add("admin-load-error");
}

document.getElementById("btn-members").addEventListener("click", () => { if (selected) loadMembers(selected); });
document.getElementById("btn-suspend").addEventListener("click", async () => {
  if (!selected || isSuspended(selected)) return;
  if (!confirm(Tf("admin.tenant.confirmSuspend", { name: selected.name }))) return;
  const data = await fetchAdminOrRedirect(`/admin/api/tenants/${encodeURIComponent(selected.id)}/suspend`, { method: "POST", body: "{}" });
  const idx = tenants.findIndex((t) => t.id === selected.id);
  if (idx >= 0) tenants[idx] = data.tenant;
  selectTenant(data.tenant);
});
window.addEventListener("keepframe:lang", repaint);

bindFilters();
Promise.all([fetchAdminOrRedirect("/admin/api/tenants"), fetchAdminOrRedirect("/admin/api/policy")])
  .then(([tData]) => {
    tenants = tData.tenants;
    if (tenants.length) selectTenant(tenants[0]);
    else renderRows();
  }).catch(showLoadError);
