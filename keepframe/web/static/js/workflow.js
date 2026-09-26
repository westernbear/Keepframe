import { T } from "/static/js/i18n.js?v=20260921v";

const STEPS = ["ingest", "analyze", "review"];
const PAGE_STAGE = { "/new": "ingest", "/analyze": "analyze" };
const container = document.querySelector("[data-workflow-shell]");
let shellReady = Promise.resolve();
let current = PAGE_STAGE[location.pathname] || null;
let projectName = "";
let projectStatus = "";

function completedCount() {
  if (current === "review") return ["review", "approved"].includes(projectStatus) ? 2 : 0;
  if (current === "analyze") return ["uploaded", "analyzing", "review", "approved"].includes(projectStatus) ? 1 : 0;
  return 0;
}

function render() {
  const shell = container && container.querySelector("[data-workflow]");
  if (!shell) return;
  const currentIndex = STEPS.indexOf(current);
  const completeBefore = completedCount();
  shell.setAttribute("aria-label", T("workflow.title"));
  shell.querySelectorAll("[data-workflow-step]").forEach((step, index) => {
    step.removeAttribute("aria-current");
    step.removeAttribute("data-complete");
    const isComplete = currentIndex >= 0 && index < completeBefore;
    step.dataset.state = isComplete ? "complete" : index === currentIndex ? "current" : "upcoming";
    if (index === currentIndex) step.setAttribute("aria-current", "step");
    if (isComplete) step.dataset.complete = "true";
  });
  const title = shell.querySelector("[data-workflow-project]");
  title.hidden = false;
  title.textContent = projectName || T("workflow.projectFallback");
  const status = shell.querySelector("[data-workflow-status]");
  status.textContent = current ? T(`workflow.status.${current}`) : "";
  status.setAttribute("role", "status");
}

if (container) {
  shellReady = fetch("/static/workflow.html?v=20260926v")
    .then((response) => {
      if (!response.ok) throw new Error("workflow shell unavailable");
      return response.text();
    })
    .then((html) => {
      container.innerHTML = html;
      window.addEventListener("keepframe:lang", render);
      render();
      import("/static/js/i18n.js?v=20260921v").then(({ applyI18n }) => {
        applyI18n(container);
        render();
      });
    })
    .catch(() => { container.hidden = true; });
}

export function setWorkflowStage({ stage, projectName: name, projectStatus: status } = {}) {
  if (stage && STEPS.includes(stage)) current = stage;
  if (name != null) projectName = String(name);
  if (status != null) projectStatus = String(status);
  shellReady.then(render);
}

export function setWorkflowProject(name, status = "") {
  projectName = name == null ? "" : String(name);
  projectStatus = status == null ? "" : String(status);
  shellReady.then(render);
}

setWorkflowStage({ stage: current });
