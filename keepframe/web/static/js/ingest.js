import { uploadProject, fetchProject, fetchEstimate, fetchFilmstrip, fetchStatus, DEFAULT_FILMSTRIP_COUNT } from "/static/js/api.js?v=20261010a";
import { T, Tf } from "/static/js/i18n.js?v=20261010a";
import { setWorkflowStage } from "/static/js/workflow.js?v=20261010a";

const DEFAULT_FPS = 30;
const SECONDS_PER_MINUTE = 60;
const state = { project: null, mode: "range", estimateSnapshot: null, fullConfirmed: false, shortAcknowledged: false, totalFrames: 0, fps: DEFAULT_FPS };
let estimateRequest = 0;
const qs = (sel) => document.querySelector(sel);
const dropzone = qs("[data-dropzone]");
const fileInput = qs("[data-file-input]");
const uploadCard = qs("[data-upload-card]");
const editor = qs("[data-editor]");
const filmstrip = qs("[data-filmstrip]");
const errorEl = qs("[data-error]");
const startBtn = qs("[data-start-btn]");
const fullConfirm = qs("[data-full-confirm]");
const confirmCopy = qs("[data-confirm-copy]");
const confirmBtn = qs("[data-confirm-btn]");
const boundaryPanel = qs("[data-scene-boundaries]");
const boundaryList = qs("[data-scene-boundary-list]");
const shortConfirm = qs("[data-short-confirm]");
const shortAck = qs("[data-short-ack]");

function showError(msg) {
  errorEl.hidden = !msg;
  errorEl.textContent = msg;
}

function lastFrameIndex() { return Math.max(0, state.totalFrames - 1); }
function selectedWindow() {
  if (state.mode === "full") return { mode: "full", start: 0, end: lastFrameIndex() };
  const start = Math.max(0, Math.min(lastFrameIndex(), Number(qs("[data-start]").value) || 0));
  const end = Math.max(start, Math.min(lastFrameIndex(), Number(qs("[data-end]").value) || 0));
  return { mode: "range", start, end };
}
function windowFrameCount(win) { return win.mode === "range" ? Math.max(1, win.end - win.start + 1) : state.totalFrames; }
function estimatePayload(win) {
  return { project_id: state.project.id, mode: win.mode, start: win.start, end: win.end, frames: windowFrameCount(win), fps: state.fps };
}

function setMode(mode) {
  state.mode = mode;
  state.fullConfirmed = false;
  state.shortAcknowledged = false;
  state.estimateSnapshot = null;
  startBtn.disabled = true;
  fullConfirm.hidden = true;
  document.querySelectorAll("[data-mode]").forEach((btn) => btn.classList.toggle("segmented__btn--active", btn.dataset.mode === mode));
  qs("[data-range-inputs]").hidden = mode === "full";
  refreshEstimate();
}

async function loadFilmstrip(projectId, result = null) {
  const { frames } = result || await fetchFilmstrip(projectId, DEFAULT_FILMSTRIP_COUNT);
  filmstrip.innerHTML = "";
  frames.forEach((url, i) => {
    const div = document.createElement("div");
    div.className = "filmstrip__frame" + (i === 0 ? " filmstrip__frame--active" : "");
    const img = document.createElement("img");
    img.src = url;
    img.alt = "";
    img.className = "cover-img";
    div.appendChild(img);
    filmstrip.appendChild(div);
  });
}

function updateStartButton(est) {
  const needsConfirm = state.mode === "full" && !state.fullConfirmed;
  const needsShortAck = Boolean((est.warnings || []).length) && !state.shortAcknowledged;
  startBtn.disabled = needsConfirm || needsShortAck;
  fullConfirm.hidden = !needsConfirm;
  if (needsConfirm) confirmCopy.textContent = Tf("ingest.fullConfirm", {
    m: Math.round(est.seconds / SECONDS_PER_MINUTE), n: est.scene_count,
  });
}

function renderSceneBoundaries(est) {
  const scenes = est.scenes || [];
  const transitions = est.transitions || [];
  boundaryPanel.hidden = scenes.length < 2;
  boundaryList.replaceChildren();
  scenes.forEach((scene, index) => {
    const row = document.createElement("div");
    row.className = "scene-boundaries__row";
    const range = document.createElement("span");
    range.textContent = `${scene.id} · ${scene.frames[0]}–${scene.frames[1]}`;
    row.appendChild(range);
    if (index < scenes.length - 1) {
      const transition = transitions[index]?.transition || "unknown";
      const label = document.createElement("label");
      label.textContent = `${transition} · ${T("ingest.boundaryFrame")} `;
      const input = document.createElement("input");
      input.className = "input";
      input.type = "number";
      input.min = String(scene.frames[0] + 1);
      input.max = String(scenes[index + 1].frames[1]);
      input.value = String(scenes[index + 1].frames[0]);
      input.dataset.boundaryIndex = String(index);
      input.setAttribute("aria-label", `${scene.id}–${scenes[index + 1].id} ${T("ingest.boundaryFrame")}`);
      label.appendChild(input);
      row.appendChild(label);
    }
    boundaryList.appendChild(row);
  });
  const hasWarnings = Boolean((est.warnings || []).length);
  shortConfirm.hidden = !hasWarnings;
  shortAck.checked = hasWarnings && state.shortAcknowledged;
}

function editedScenes() {
  const snapshot = state.estimateSnapshot;
  if (!snapshot?.scenes?.length) return null;
  const boundaries = [...boundaryList.querySelectorAll("[data-boundary-index]")].map((input) => Number(input.value));
  const win = selectedWindow();
  let first = win.start;
  return snapshot.scenes.map((scene, index) => {
    const last = index < boundaries.length ? boundaries[index] - 1 : win.end;
    const edited = { id: scene.id, frames: [first, last] };
    first = last + 1;
    return edited;
  });
}

async function refreshEstimate(scenes = null) {
  if (!state.project) return;
  const request = ++estimateRequest;
  const win = selectedWindow();
  const payload = estimatePayload(win);
  if (scenes) payload.scenes = scenes;
  const est = await fetchEstimate(payload);
  if (request !== estimateRequest) return;
  state.estimateSnapshot = { ...payload, ...est };
  state.estimateSnapshot.acknowledge_short_scenes = state.shortAcknowledged;
  const frames = windowFrameCount(win);
  const isRange = win.mode === "range";
  qs("[data-est-seconds]").textContent = Tf("ingest.aboutMin", { m: Math.round(est.seconds / SECONDS_PER_MINUTE) });
  qs("[data-est-scenes]").textContent = String(est.scene_count);
  qs("[data-est-note]").hidden = false;
  qs("[data-est-note]").textContent = est.note + Tf("ingest.perScene", { s: est.seconds_per_scene });
  qs("[data-range-meta]").textContent = Tf(isRange ? "ingest.rangeSummary" : "ingest.fullSummary", {
    sec: ((isRange ? frames : state.totalFrames) / state.fps).toFixed(1),
    frames: isRange ? frames : state.totalFrames,
    n: est.scene_count,
  });
  renderSceneBoundaries(state.estimateSnapshot);
  updateStartButton(est);
}

function resetFileUi() {
  dropzone.hidden = false;
  uploadCard.hidden = true;
  editor.hidden = true;
  startBtn.disabled = true;
}

async function showProject(project, filmstripData = null) {
  state.project = project;
  setWorkflowStage({ stage: "ingest", projectName: project.title || project.id, projectStatus: project.status || "" });
  const v = project.video || {};
  state.fps = v.fps || DEFAULT_FPS;
  state.totalFrames = v.frames || 1;
  qs("[data-video-name]").textContent = project.title || project.id;
  qs("[data-video-meta]").textContent = `${v.width || "?"}x${v.height || "?"} · ${state.fps} fps · ${(v.duration_s || 0).toFixed(1)}s`;
  await loadFilmstrip(project.id, filmstripData);
  const range = project.range || [0, lastFrameIndex()];
  qs("[data-start]").value = String(range[0]);
  qs("[data-end]").value = String(range[1]);
  state.mode = project.mode || "range";
  qs("[data-reference-ui]").checked = project.reference === "ui";
  state.shortAcknowledged = false;
  uploadCard.hidden = false;
  editor.hidden = false;
  dropzone.hidden = true;
  document.querySelectorAll("[data-mode]").forEach((btn) => btn.classList.toggle("segmented__btn--active", btn.dataset.mode === state.mode));
  qs("[data-range-inputs]").hidden = state.mode === "full";
  await refreshEstimate();
}

async function handleFile(file) {
  showError("");
  dropzone.hidden = true;
  uploadCard.hidden = false;
  qs("[data-video-name]").textContent = file.name;
  qs("[data-video-meta]").textContent = T("ingest.uploading");
  editor.hidden = true;
  startBtn.disabled = true;
  try {
    const { project } = await uploadProject({ title: file.name.replace(/\.[^.]+$/, ""), video: file, mode: "full", start: 0, end: 0 });
    await showProject(project);
  } catch (err) {
    resetFileUi();
    showError(err.body && err.body.code === "live_action" ? T("error.liveaction") : err.message || T("ingest.uploadFailed"));
  }
}

function bindFilePicker() {
  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("dragover", (e) => e.preventDefault());
  dropzone.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer?.files[0]) handleFile(e.dataTransfer.files[0]); });
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) handleFile(fileInput.files[0]); });
  qs("[data-remove-video]").addEventListener("click", () => {
    state.project = null;
    state.estimateSnapshot = null;
    fileInput.value = "";
    resetFileUi();
    showError("");
  });
}

function bindWindowControls() {
  document.querySelectorAll("[data-mode]").forEach((btn) => btn.addEventListener("click", () => setMode(btn.dataset.mode)));
  [qs("[data-start]"), qs("[data-end]")].forEach((input) => input.addEventListener("change", () => {
    state.fullConfirmed = false;
    state.shortAcknowledged = false;
    state.estimateSnapshot = null;
    startBtn.disabled = true;
    refreshEstimate();
  }));
  confirmBtn.addEventListener("click", () => {
    if (!state.estimateSnapshot || state.estimateSnapshot.mode !== "full") return;
    state.fullConfirmed = true;
    fullConfirm.hidden = true;
    updateStartButton(state.estimateSnapshot);
  });
  boundaryList.addEventListener("change", async (event) => {
    if (!event.target.matches("[data-boundary-index]")) return;
    const scenes = editedScenes();
    state.fullConfirmed = false;
    state.shortAcknowledged = false;
    state.estimateSnapshot = null;
    startBtn.disabled = true;
    try { await refreshEstimate(scenes); }
    catch (err) { showError(err.message || T("ingest.uploadFailed")); }
  });
  shortAck.addEventListener("change", () => {
    state.shortAcknowledged = shortAck.checked;
    if (state.estimateSnapshot) {
      state.estimateSnapshot.acknowledge_short_scenes = state.shortAcknowledged;
      updateStartButton(state.estimateSnapshot);
    }
  });
}

function bindIngest() {
  bindFilePicker();
  bindWindowControls();
  startBtn.addEventListener("click", startAnalysis);
}

async function refreshGpu() {
  const el = qs("[data-gpu]");
  if (!el) return;
  try {
    const s = await fetchStatus();
    el.textContent = s.cuda ? (s.name || "ON") : "OFF";
  } catch {
    el.textContent = "OFF";
  }
}

function startAnalysis() {
  const win = selectedWindow();
  const snapshot = state.estimateSnapshot;
  if (!state.project || !snapshot || snapshot.mode !== win.mode || snapshot.start !== win.start || snapshot.end !== win.end || (state.mode === "full" && !state.fullConfirmed) || ((snapshot.warnings || []).length && !state.shortAcknowledged)) return;
  const params = new URLSearchParams({
    job: state.project.id, token: snapshot.confirm_token, mode: snapshot.mode,
    start: String(snapshot.start), end: String(snapshot.end),
    reference: qs("[data-reference-ui]").checked ? "ui" : "mg",
  });
  const query = params.toString();
  sessionStorage.setItem("keepframe.analyze-start", `?${query}`);
  sessionStorage.setItem("keepframe.analyze-estimate", JSON.stringify(snapshot));
  location.href = `/analyze?${query}`;
}

bindIngest();
refreshGpu();
const existingProject = new URLSearchParams(location.search).get("project");
if (existingProject) {
  fetchProject(existingProject).then(({ project }) => {
    if (!project || project.status !== "uploaded" || !project.video) throw new Error(T("ingest.uploadFailed"));
    return showProject(project);
  }).catch((err) => showError(err.message || T("ingest.uploadFailed")));
}
window.addEventListener("keepframe:lang", () => { if (state.project) refreshEstimate(); });
