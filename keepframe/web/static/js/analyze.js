import { fetchProjects, fetchFilmstrip, fetchJob, postAnalyze, DEFAULT_FILMSTRIP_COUNT } from "/static/js/api.js?v=20260921v";
import { T, Tf } from "/static/js/i18n.js?v=20260921v";

const ANALYZE_POLL_INTERVAL_MS = 1000;
const SECONDS_PER_MINUTE = 60;
const DEFAULT_SCENE_ID = "s1";

const params = new URLSearchParams(location.search);
const projectId = params.get("job") || params.get("project");
const token = params.get("token") || "";
const analyzeMode = params.get("mode") || "";
const analyzeStart = params.get("start");
const analyzeEnd = params.get("end");
const statusEl = document.querySelector("[data-status]");
const stageEl = document.querySelector("[data-stage]");
const etaEl = document.querySelector("[data-eta]");

const PIPELINE = ["frames", "background", "text", "regions", "tracking", "sprites", "keyframes", "semantics", "constraints", "report"];
const STEP_STAGES = {
  shots: ["frames"],
  bg: ["background"],
  text: ["text"],
  regions: ["regions", "tracking"],
  sprites: ["sprites"],
  keyframes: ["keyframes"],
  predicates: ["semantics", "constraints"],
  report: ["report"],
};
const STAGE_I18N = {
  frames: "analyze.step.shots",
  background: "analyze.step.bg",
  text: "analyze.step.text",
  regions: "analyze.step.regions",
  tracking: "analyze.step.regions",
  sprites: "analyze.step.sprites",
  keyframes: "analyze.step.keyframes",
  semantics: "analyze.step.predicates",
  constraints: "analyze.step.predicates",
  report: "analyze.step.report",
};
const CHECK = '<svg class="icon" style="width:14px;height:14px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>';

let pollTimer = 0;

function showError(msg) {
  statusEl.hidden = !msg;
  statusEl.textContent = msg || "";
}

function formatEta(seconds) {
  if (seconds == null) return "—";
  const mins = Math.max(1, Math.round(seconds / SECONDS_PER_MINUTE));
  return Tf("analyze.etaLeft", { m: mins });
}

function stepState(stepKey, currentStage) {
  const stages = STEP_STAGES[stepKey];
  const cur = PIPELINE.indexOf(currentStage);
  if (cur < 0) return "waiting";
  const first = PIPELINE.indexOf(stages[0]);
  const last = PIPELINE.indexOf(stages[stages.length - 1]);
  if (cur > last) return "done";
  if (cur >= first) return "active";
  return "waiting";
}

function updateSteps(stage) {
  document.querySelectorAll("[data-step]").forEach((li) => {
    const state = stepState(li.dataset.step, stage);
    li.classList.remove("is-active", "is-done", "is-waiting");
    li.classList.add(`is-${state}`);
    const dot = li.querySelector(".step-dot");
    if (!dot) return;
    const isActive = state === "active";
    const isDone = state === "done";
    dot.classList.toggle("step-dot--live", isActive);
    dot.innerHTML = isDone ? CHECK : "";
  });
}

function stageLabel(job) {
  const key = STAGE_I18N[job.stage];
  const name = key ? T(key) : (job.stage || job.status || "—");
  return job.detail ? `${name} ${job.detail}` : name;
}

function goToReview(project) {
  location.href = `/review?project=${project}&scene=${DEFAULT_SCENE_ID}`;
}

function updateJob(job) {
  updateSteps(job.stage);
  stageEl.textContent = stageLabel(job);
  etaEl.textContent = formatEta(job.eta_s);
  if (job.status === "done") {
    updateSteps("report");
    goToReview(job.project_id);
    return;
  }
  if (job.status === "error") {
    showError(job.error || T("analyze.failed"));
    const strip = document.querySelector("[data-filmstrip]");
    if (strip) strip.innerHTML = "";
  }
}

async function showFilmstrip(id) {
  const { frames } = await fetchFilmstrip(id, DEFAULT_FILMSTRIP_COUNT);
  const strip = document.querySelector("[data-filmstrip]");
  if (!strip || !frames) return;
  strip.innerHTML = "";
  const mid = Math.floor(frames.length / 2);
  frames.forEach((url, i) => {
    const div = document.createElement("div");
    const isMid = i === mid;
    div.className = "filmstrip__frame" + (isMid ? " filmstrip__frame--active" : "");
    const img = document.createElement("img");
    img.src = url;
    img.alt = "";
    div.appendChild(img);
    strip.appendChild(div);
  });
}

async function poll(jobId) {
  try {
    const job = await fetchJob(jobId);
    updateJob(job);
  } catch (err) {
    showError(err.message || T("analyze.pollFailed"));
  }
}

function startPolling(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => poll(jobId), ANALYZE_POLL_INTERVAL_MS);
}

function hasExplicitRange() {
  return analyzeMode === "range" && analyzeStart != null && analyzeStart !== "" && analyzeEnd != null;
}

function paintRangeLabel(project) {
  const rangeEl = document.querySelector("[data-range-label]");
  if (!rangeEl) return;
  if (hasExplicitRange()) {
    rangeEl.textContent = `${analyzeStart}–${analyzeEnd}`;
    return;
  }
  if (project && project.range) {
    rangeEl.textContent = `${project.range[0]}–${project.range[1]}`;
  }
}

function analyzePayload() {
  const payload = { project_id: projectId, confirm_token: token };
  if (analyzeMode) payload.mode = analyzeMode;
  if (analyzeStart != null && analyzeStart !== "") payload.start = Number(analyzeStart);
  if (analyzeEnd != null && analyzeEnd !== "") payload.end = Number(analyzeEnd);
  return payload;
}

async function loadProjectChrome() {
  const { projects } = await fetchProjects();
  const p = (projects || []).find((x) => x.id === projectId);
  if (p && p.title) {
    document.querySelectorAll("[data-clip-name]").forEach((el) => { el.textContent = p.title; });
  }
  paintRangeLabel(p);
}

async function loadOptionalChrome() {
  try {
    await loadProjectChrome();
  } catch (err) {
    showError(err.message || T("analyze.chromeFailed"));
  }
}

async function loadOptionalFilmstrip() {
  try {
    await showFilmstrip(projectId);
  } catch (err) {
    showError(err.message || T("analyze.filmstripFailed"));
  }
}

async function startAnalyzeJob() {
  const { job } = await postAnalyze(analyzePayload());
  updateJob(job);
  startPolling(job.id);
}

async function bootAnalyze() {
  if (!projectId) {
    showError(T("analyze.noProject"));
    return;
  }
  await loadOptionalChrome();
  await loadOptionalFilmstrip();
  try {
    await startAnalyzeJob();
  } catch (err) {
    showError(err.message || T("analyze.startFailed"));
  }
}

bootAnalyze();
