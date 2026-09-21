import { fetchAdminOrRedirect } from "/static/js/api.js?v=20260921u";

const FILE_ICON = '<svg class="icon" style="color:var(--muted)" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/></svg>';
const TENANT_LABEL = {
  org_solo: "Solo",
  org_northwind: "Northwind",
  org_hanbit: "Hanbit",
};

let items = [];
let selected = null;
const rowsEl = document.getElementById("quarantine-rows");

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function tenantLabel(id) {
  return TENANT_LABEL[id] || id;
}

function renderRows() {
  rowsEl.innerHTML = "";
  items.forEach((item) => {
    const row = document.createElement("div");
    const isSelected = selected && selected.id === item.id;
    row.className = "quarantine-row" + (isSelected ? " quarantine-row--selected" : "");
    row.innerHTML = `
<div style="display:flex;align-items:center;gap:8px;min-width:0">${FILE_ICON}<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${item.filename}</span></div>
<div style="color:var(--muted)">${tenantLabel(item.tenant_id)}</div>
<div class="mono" style="font-size:var(--fs-micro);color:var(--muted)">${fmtTime(item.rejected_at)}</div>
<div style="font-size:var(--fs-micro);color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${item.reason}</div>`;
    row.addEventListener("click", () => selectItem(item));
    rowsEl.appendChild(row);
  });
}

function selectItem(item) {
  selected = item;
  document.getElementById("detail-filename").textContent = item.filename;
  document.getElementById("detail-tenant").textContent = tenantLabel(item.tenant_id);
  document.getElementById("detail-time").textContent = fmtTime(item.rejected_at);
  document.getElementById("detail-reason").textContent = item.reason;
  renderRows();
}

Promise.all([
  fetchAdminOrRedirect("/admin/api/quarantine"),
  fetchAdminOrRedirect("/admin/api/policy"),
]).then(([qData, pol]) => {
  items = qData.items;
  document.getElementById("quarantine-count").textContent = `${items.length} 항목`;
  document.getElementById("detail-policy").textContent = pol.note;
  if (items.length) selectItem(items[0]);
  else renderRows();
}).catch(() => {});
