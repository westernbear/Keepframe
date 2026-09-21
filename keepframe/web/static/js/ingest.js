import { uploadProject, fetchEstimate, fetchFilmstrip, fetchStatus, DEFAULT_FILMSTRIP_COUNT } from "/static/js/api.js?v=20260921u";
import { T, Tf } from "/static/js/i18n.js?v=20260921u";

const DEFAULT_FPS = 30;
const SECONDS_PER_MINUTE = 60;

const state = {
  project: null,
  mode: "range",
  confirmToken: null,
  fullConfirmed: false,
  totalFrames: 0,
  fps: DEFAULT_FPS,
};

function qs(sel) {
  return document.querySelector(sel);
}

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

function showError(msg) {
  errorEl.hidden = !msg;
  errorEl.textContent = msg || "";
}

function needsFullVideoConfirm() {
  return state.mode === "full" && !state.fullConfirmed;
}

function lastFrameIndex() {
  return Math.max(0, state.totalFrames - 1);
}

function selectedWindow() {
  const start = parseInt(qs("[data-start]").value, 10);
  const end = parseInt(qs("[data-end]").value, 10);
  const last = lastFrameIndex();
  const s = Number.isFinite(start) ? Math.max(0, start) : 0;
  const e = Number.isFinite(end) ? Math.max(s, end) : last;
  return { mode: state.mode, start: s, end: Math.min(e, last) };
}

function windowFrameCount(win) {
  if (win.mode === "range") return Math.max(1, win.end - win.start + 1);
  return state.totalFrames;
}

function estimatePayload(win) {
  return {
    project_id: state.project.id,
    mode: win.mode,
    start: win.start,
    end: win.end,
    frames: windowFrameCount(win),
    fps: state.fps,
  };
}

function setMode(mode) {
  state.mode = mode;
  state.fullConfirmed = false;
  fullConfirm.hidden = true;
  document.querySelectorAll("[data-mode]").forEach((btn) => {
    btn.classList.toggle("segmented__btn--active", btn.dataset.mode === mode);
  });
  qs("[data-range-inputs]").hidden = mode === "full";
  refreshEstimate();
}

async function loadFilmstrip(projectId) {
  const { frames } = await fetchFilmstrip(projectId, DEFAULT_FILMSTRIP_COUNT);
  filmstrip.innerHTML = "";
  frames.forEach((url, i) => {
    const div = document.createElement("div");
    const isFirst = i === 0;
    div.className = "filmstrip__frame" + (isFirst ? " filmstrip__frame--active" : "");
    const img = document.createElement("img");
    img.src = url;
    img.alt = "";
    img.style.width = "100%";
    img.style.height = "100%";
    img.style.objectFit = "cover";
    div.appendChild(img);
    filmstrip.appendChild(div);
  });
}

function updateStartButton(est) {
  if (needsFullVideoConfirm()) {
    startBtn.disabled = true;
    fullConfirm.hidden = false;
    confirmCopy.textContent = Tf("ingest.fullConfirm", {
      m: Math.round(est.seconds / SECONDS_PER_MINUTE),
      n: est.scene_count,
    });
    return;
  }
  fullConfirm.hidden = state.mode !== "full" || state.fullConfirmed;
  startBtn.disabled = false;
}

async function refreshEstimate() {
  if (!state.project) return;
  const win = selectedWindow();
  const frames = windowFrameCount(win);
  const est = await fetchEstimate(estimatePayload(win));
  state.confirmToken = est.confirm_token;
  const mins = Math.round(est.seconds / SECONDS_PER_MINUTE);
  qs("[data-est-seconds]").textContent = Tf("ingest.aboutMin", { m: mins });
  qs("[data-est-scenes]").textContent = String(est.scene_count);
  qs("[data-est-note]").hidden = false;
  qs("[data-est-note]").textContent = est.note + Tf("ingest.perScene", { s: est.seconds_per_scene });
  const isRange = state.mode === "range";
  qs("[data-range-meta]").textContent = Tf(isRange ? "ingest.rangeSummary" : "ingest.fullSummary", {
    sec: ((isRange ? frames : state.totalFrames) / state.fps).toFixed(1),
    frames: isRange ? frames : state.totalFrames,
    n: est.scene_count,
  });
  updateStartButton(est);
}

function isLiveActionError(err) {
  return Boolean(err.body && err.body.code === "live_action");
}

function resetFileUi() {
  dropzone.hidden = false;
  uploadCard.hidden = true;
  editor.hidden = true;
  startBtn.disabled = true;
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
    const { project } = await uploadProject({
      title: file.name.replace(/\.[^.]+$/, ""),
      video: file,
      mode: "full",
      start: 0,
      end: 0,
    });
    state.project = project;
    const v = project.video || {};
    state.fps = v.fps || DEFAULT_FPS;
    state.totalFrames = v.frames || 1;
    qs("[data-video-meta]").textContent =
      `${v.width || "?"}x${v.height || "?"} · ${state.fps} fps · ${(v.duration_s || 0).toFixed(1)}s`;
    await loadFilmstrip(project.id);
    qs("[data-end]").value = String(lastFrameIndex());
    qs("[data-start]").value = "0";
    editor.hidden = false;
    setMode("range");
  } catch (err) {
    resetFileUi();
    if (isLiveActionError(err)) showError(T("error.liveaction"));
    else showError(err.message || T("ingest.uploadFailed"));
  }
}

function bindIngest() {
  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("dragover", (e) => { e.preventDefault(); });
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    const file = e.dataTransfer && e.dataTransfer.files[0];
    if (file) handleFile(file);
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) handleFile(fileInput.files[0]);
  });
  qs("[data-remove-video]").addEventListener("click", () => {
    state.project = null;
    fileInput.value = "";
    resetFileUi();
    showError("");
  });
  document.querySelectorAll("[data-mode]").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });
  qs("[data-start]").addEventListener("change", refreshEstimate);
  qs("[data-end]").addEventListener("change", refreshEstimate);
  confirmBtn.addEventListener("click", () => {
    state.fullConfirmed = true;
    fullConfirm.hidden = true;
    startBtn.disabled = false;
  });
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

async function startAnalysis() {
  if (!state.project) return;
  try {
    const win = selectedWindow();
    const est = await fetchEstimate(estimatePayload(win));
    state.confirmToken = est.confirm_token;
    if (needsFullVideoConfirm()) {
      updateStartButton(est);
      return;
    }
    const q = new URLSearchParams({
      job: state.project.id,
      token: est.confirm_token,
      mode: win.mode,
      start: String(win.start),
      end: String(win.end),
    });
    location.href = `/analyze?${q}`;
  } catch (err) {
    showError(err.message);
  }
}

bindIngest();
refreshGpu();
window.addEventListener("keepframe:lang", () => {
  if (state.project) refreshEstimate();
});
