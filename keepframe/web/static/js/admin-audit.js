import { fetchAdminOrRedirect } from "/static/js/api.js?v=20260921u";

const tbody = document.getElementById("audit-rows");

function actionStyle(action) {
  const isWarnAction = action.includes("정지") || action.includes("검역");
  if (isWarnAction) return "color:var(--error);font-weight:600";
  const isKeepAction = action.includes("keep");
  if (isKeepAction) return "color:var(--secondary)";
  return "";
}

fetchAdminOrRedirect("/admin/api/audit").then((data) => {
  tbody.innerHTML = "";
  data.events.forEach((e) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
<td style="color:var(--muted)">${e.ts}</td>
<td>${e.actor}</td>
<td style="${actionStyle(e.action)}">${e.action}</td>
<td style="color:var(--muted)">${e.target}</td>
<td style="color:var(--muted)">${e.detail || "-"}</td>`;
    tbody.appendChild(tr);
  });
}).catch(() => {});
