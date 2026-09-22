import { fetchAdminOrRedirect } from "/static/js/api.js?v=20260921v";
import { T } from "/static/js/i18n.js?v=20260921v";

const tbody = document.getElementById("audit-rows");
const ACTION_KEYS = {
  "업로드 검역": "admin.audit.action.quarantineUpload",
  "내보내기": "admin.audit.action.export",
  "멤버 추가": "admin.audit.action.memberAdded",
  "keep 변경": "admin.audit.action.keepChanged",
  "테넌트 정지 시도": "admin.audit.action.tenantSuspendAttempt",
  "테넌트 정지": "admin.audit.action.tenantSuspended",
  "LLM 프로바이더 변경": "admin.audit.action.llmProviderChanged",
};
function actionLabel(action) {
  if (action.startsWith("keep 해제 ")) return `${T("admin.audit.action.keepReleased")} ${action.slice("keep 해제 ".length)}`;
  if (action.startsWith("내보내기 ")) return `${T("admin.audit.action.export")} ${action.slice("내보내기 ".length)}`;
  return ACTION_KEYS[action] ? T(ACTION_KEYS[action]) : action;
}
function actionClass(action) {
  if (action.includes("정지") || action.includes("검역")) return "action--warn";
  if (action.includes("keep")) return "action--keep";
  return "";
}
function appendCell(tr, value, className = "") {
  const td = document.createElement("td");
  if (className) td.className = className;
  td.textContent = value;
  tr.appendChild(td);
}
let events = [];
function render() {
  tbody.innerHTML = "";
  events.forEach((e) => {
    const tr = document.createElement("tr");
    appendCell(tr, e.ts, "quota-muted");
    appendCell(tr, e.actor);
    appendCell(tr, actionLabel(e.action), actionClass(e.action));
    appendCell(tr, e.target, "quota-muted");
    appendCell(tr, e.detail || "-", "quota-muted");
    tbody.appendChild(tr);
  });
}
function showLoadError(err) {
  if (err.status === 401) return;
  tbody.innerHTML = "";
  const tr = document.createElement("tr");
  appendCell(tr, err.message || T("admin.loadFailed"), "admin-load-error");
  tr.firstChild.colSpan = 5;
  tbody.appendChild(tr);
}
window.addEventListener("keepframe:lang", render);
fetchAdminOrRedirect("/admin/api/audit").then((data) => { events = data.events; render(); }).catch(showLoadError);
