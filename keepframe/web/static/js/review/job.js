import { fetchReviewJob, postCorrect } from "/static/js/api.js?v=20260921v";
import { T, Tf } from "/static/js/i18n.js?v=20260921v";
import { JOB_POLL_INTERVAL_MS } from "/static/js/review/workspace.js?v=20260921v";

export function attachJob(ws) {
  const { dom } = ws;

  function setJobBanner(msg, isError = false) {
    dom.jobBanner.hidden = !msg;
    dom.jobBanner.textContent = msg || "";
    dom.jobBanner.classList.toggle("job-banner--error", isError);
  }

  function setFormsDisabled(disabled) {
    const keepSave = dom.keepSave;
    dom.formsPanel.toggleAttribute("disabled", disabled);
    keepSave.disabled = disabled || !ws.keepDirty;
    keepSave.hidden = !ws.keepDirty;
  }

  function showRunningJob(job) {
    setFormsDisabled(true);
    setJobBanner(Tf("review.running", { op: job.op || "" }));
  }

  function showFinishedJob(version) {
    setFormsDisabled(false);
    setJobBanner("");
    clearInterval(ws.pollTimer);
    ws.refreshState(version);
  }

  function showFailedJob(job) {
    setFormsDisabled(false);
    setJobBanner(Tf("review.retry", { err: job.error || T("review.error") }), true);
    clearInterval(ws.pollTimer);
  }

  function showIdleJob() {
    setFormsDisabled(false);
    setJobBanner("");
    clearInterval(ws.pollTimer);
  }

  function applyJobSnapshot(job) {
    if (job.status === "running") {
      showRunningJob(job);
      return;
    }
    if (job.status === "done" && job.version) {
      showFinishedJob(job.version);
      return;
    }
    if (job.status === "error") {
      showFailedJob(job);
      return;
    }
    showIdleJob();
  }

  async function pollJob() {
    if (!ws.projectId) return;
    try {
      const job = await fetchReviewJob(ws.projectId, ws.sceneId);
      applyJobSnapshot(job);
    } catch {
      /* ignore transient poll errors */
    }
  }

  async function runCorrect(op, args) {
    await postCorrect(ws.projectId, ws.sceneId, op, args);
    clearInterval(ws.pollTimer);
    ws.pollTimer = setInterval(pollJob, JOB_POLL_INTERVAL_MS);
    pollJob();
  }

  ws.setJobBanner = setJobBanner;
  ws.setFormsDisabled = setFormsDisabled;
  ws.applyJobSnapshot = applyJobSnapshot;
  ws.pollJob = pollJob;
  ws.runCorrect = runCorrect;
}
