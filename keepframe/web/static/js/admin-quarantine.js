import { fetchAdminOrRedirect } from "/static/js/api.js?v=20260921v";
import { T, Tf } from "/static/js/i18n.js?v=20260921v";

const FILE_ICON = '<svg class="icon quota-muted" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/></svg>';
const TENANT_LABEL = { org_solo: "Solo", org_northwind: "Northwind", org_hanbit: "Hanbit" };
let items = [];
let selected = null;
const rowsEl = document.getElementById("quarantine-rows");
function tenantLabel(id) { return TENANT_LABEL[id] || id; }
function renderRows() {
  rowsEl.innerHTML = "";
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "quarantine-row" + (selected && selected.id === item.id ? " quarantine-row--selected" : "");
    const name = document.createElement("div");
    name.className = "quarantine-row__file";
    name.innerHTML = FILE_ICON;
    const filename = document.createElement("span");
    filename.className = "quarantine-row__name";
    filename.textContent = item.filename;
    name.appendChild(filename);
    const tenant = document.createElement("div");
    tenant.className = "quota-muted";
    tenant.textContent = tenantLabel(item.tenant_id);
    const time = document.createElement("div");
    time.className = "mono tenant-id";
    time.textContent = item.rejected_at;
    const reason = document.createElement("div");
    reason.className = "quarantine-row__reason";
    reason.textContent = item.reason;
    row.append(name, tenant, time, reason);
    row.addEventListener("click", () => selectItem(item));
    rowsEl.appendChild(row);
  });
}
function selectItem(item) {
  selected = item;
  document.getElementById("detail-filename").textContent = item.filename;
  document.getElementById("detail-tenant").textContent = tenantLabel(item.tenant_id);
  document.getElementById("detail-time").textContent = item.rejected_at;
  document.getElementById("detail-reason").textContent = item.reason;
  renderRows();
}
function repaint() {
  document.getElementById("quarantine-count").textContent = Tf("admin.quarantine.count", { n: items.length });
  document.getElementById("detail-policy").textContent = T("admin.policy");
  if (selected) selectItem(selected); else renderRows();
}
function showLoadError(err) {
  if (err.status === 401) return;
  rowsEl.textContent = err.message || T("admin.loadFailed");
  rowsEl.classList.add("admin-load-error");
}
window.addEventListener("keepframe:lang", repaint);
Promise.all([fetchAdminOrRedirect("/admin/api/quarantine"), fetchAdminOrRedirect("/admin/api/policy")])
  .then(([qData]) => {
    items = qData.items;
    repaint();
    if (items.length) selectItem(items[0]);
  }).catch(showLoadError);
