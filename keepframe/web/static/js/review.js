import { fetchProjects } from "/static/js/api.js?v=20261006e";
import { T } from "/static/js/i18n.js?v=20261006e";
import { setWorkflowStage, setWorkflowProject } from "/static/js/workflow.js?v=20261006e";
import { createReviewWorkspace } from "/static/js/review/workspace.js?v=20261006e";
import { attachPlayback } from "/static/js/review/playback-ui.js?v=20261006e";
import { attachTimeline } from "/static/js/review/timeline.js?v=20261006e";
import { attachInspector } from "/static/js/review/inspector.js?v=20261006e";
import { attachEditForm } from "/static/js/review/edit-form.js?v=20261006e";
import { attachCorrections } from "/static/js/review/corrections.js?v=20261006e";
import { attachJob } from "/static/js/review/job.js?v=20261006e";

const ws = createReviewWorkspace();
attachJob(ws);
attachPlayback(ws);
attachTimeline(ws);
attachInspector(ws);
attachEditForm(ws);
attachCorrections(ws);

function bindAccordions() {
  document.querySelectorAll(".accordion__head").forEach((head) => {
    head.addEventListener("click", () => {
      const acc = head.parentElement;
      const open = acc.classList.contains("accordion--open");
      document.querySelectorAll(".accordion").forEach((a) => a.classList.remove("accordion--open"));
      if (!open) acc.classList.add("accordion--open");
    });
  });
}

function showEmptyReview() {
  setWorkflowStage({ stage: null, projectName: "", projectStatus: "" });
  ws.clearLoading();
  ws.dom.reviewRoot.classList.add("is-empty");
  document.body.classList.add("review-empty");
}

function showDemoNote() {
  const note = document.getElementById("demo-note");
  if (note) note.hidden = false;
}

async function bootReviewWorkspace() {
  try {
    await ws.loadState();
    setWorkflowStage({ stage: "review", projectName: ws.projectId, projectStatus: ws.state.status });
    fetchProjects().then(({ projects }) => {
      const project = (projects || []).find((row) => row.id === ws.projectId);
      if (project && project.title) setWorkflowProject(project.title, ws.state.status);
    }).catch(() => {});
    ws.pollJob();
  } catch (err) {
    setWorkflowStage({ stage: null, projectName: "", projectStatus: "" });
    ws.clearLoading();
    ws.setJobBanner(err.message || T("review.loadFailed"), true);
  }
}

function bindReview() {
  ws.bindTransport();
  bindAccordions();
  ws.dom.elementFilter.addEventListener("input", ws.applyElementFilter);
  ws.bindDraw();
  ws.bindEdit();
  ws.bindCorrections();
  ws.dom.approveBtn.addEventListener("click", ws.handleApproveClick);
  window.addEventListener("keepframe:lang", () => {
    if (ws.state) {
      ws.renderElements().then(() => ws.clearLoading());
      ws.renderObjectDetail();
      ws.drawOverlays();
      ws.updateOverlayStatus();
      ws.paintApprove(ws.state.status);
    }
    ws.dom.playBtn.setAttribute("aria-label", ws.transport.isPlaying() ? T("review.pause") : T("review.play"));
  });
  window.addEventListener("resize", () => {
    if (ws.state) {
      ws.drawPlayhead();
      ws.drawOverlays();
    }
  });
}

bindReview();
ws.dom.playBtn.setAttribute("aria-label", T("review.play"));
if (ws.projectId === "demo") showDemoNote();
if (!ws.projectId) showEmptyReview();
else bootReviewWorkspace();
