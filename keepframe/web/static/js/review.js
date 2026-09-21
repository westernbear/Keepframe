import {
  fetchReviewState,
  fetchReviewJob,
  fetchBboxes,
  postApprove,
  postKeep,
  postEdit,
  postCorrect,
  reviewAssetUrl,
} from "/static/js/api.js?v=20260921u";
import { T, Tf } from "/static/js/i18n.js?v=20260921u";
import {
  createPreviewCache,
  createFrameTransport,
  isImageDecoded,
  seekDelayMs,
  isSeekQueuedWhilePlaying,
  frameStep,
} from "/static/js/playback.js?v=20260921u";

const JOB_POLL_INTERVAL_MS = 800;
const LOADING_PCT_START = 8;
const LOADING_PCT_STATE = 45;
const LOADING_PCT_FRAMES = 85;
const LOADING_PCT_LIST_BASE = 45;
const LOADING_PCT_LIST_SPAN = 30;
const PROGRESS_MAX = 100;
const TRACK_GUTTER = 280;
const LIST_CHUNK = 32;
const CONSTRAINT_STEP = 128;
const TIMELINE_HEIGHT_PX = 72;
const ERROR_STRIP_HEIGHT_PX = 64;
const MIN_L1_MAX = 1e-6;
const MIN_BBOX_EDGE = 2;
const KEEP_NOTE = "keep 조건 수정";

const params = new URLSearchParams(location.search);
const projectId = params.get("project");
const sceneId = params.get("scene") || "s1";
let versionId = params.get("v") || null;
let state = null;
let frame = 0;
let keepDirty = false;
const keepPending = new Map();
let errorPeaks = [];
let pollTimer = 0;
let selectedId = null;
let shownFrame = -1;
let imgTimer = 0;
let playheadEl = null;
let boxes = {};
let bboxReq = 0;
let drag = null;
let pendingIntent = null;

const orig = document.getElementById("orig");
const recon = document.getElementById("recon");
const frameNum = document.getElementById("frame-num");
const frameTotal = document.getElementById("frame-total");
const frameTime = document.getElementById("frame-time");
const playBtn = document.getElementById("play-btn");
const playIcon = document.getElementById("play-icon");
const pauseIcon = document.getElementById("pause-icon");
const trackPlayhead = document.getElementById("track-playhead");
const versionSelect = document.getElementById("version-select");
const elementList = document.getElementById("element-list");
const constraintsPanel = document.getElementById("constraints-panel");
const emptyState = document.getElementById("empty-state");
const elementCount = document.getElementById("element-count");
const keepSave = document.getElementById("keep-save");
const approveBtn = document.getElementById("review-approve");
const formsPanel = document.getElementById("forms-panel");
const jobBanner = document.getElementById("job-banner");
const timelineSvg = document.getElementById("timeline-svg");
const timelineTracks = document.getElementById("timeline-tracks");
const sceneBadge = document.getElementById("scene-badge");
const reassignTo = document.getElementById("reassign-to");
const reviewRoot = document.getElementById("review-root");
const loadingMsg = document.getElementById("review-loading-msg");
const progressBar = document.getElementById("review-progress");
const progressFill = document.getElementById("review-progress-fill");
const progressPct = document.getElementById("review-progress-pct");
const reconLoading = document.getElementById("recon-loading");
const reconError = document.getElementById("recon-error");
const origOverlay = document.getElementById("orig-overlay");
const reconOverlay = document.getElementById("recon-overlay");
const origDraw = document.getElementById("orig-draw");
const elementFilter = document.getElementById("element-filter");
const editPrompt = document.getElementById("edit-prompt");
const editFile = document.getElementById("edit-file");
const editSummary = document.getElementById("edit-summary");
const editConflicts = document.getElementById("edit-conflicts");
const editRun = document.getElementById("edit-run");
const editConfirm = document.getElementById("edit-confirm");
const editCancel = document.getElementById("edit-cancel");

const previews = createPreviewCache({
  project: projectId,
  scene: sceneId,
  version: () => versionId,
});

const transport = createFrameTransport({
  getFrame: () => frame,
  setFrameIndex: (next) => { frame = next; },
  getFps: () => state.scene.fps,
  getFrameCount: () => state.scene.frames,
  prefetch: (from, total) => previews.prefetch(from, total),
  isReady: (index) => previews.isReady(index),
  wait: (index) => previews.wait(index),
  showFrame: showNextPlaybackFrame,
  canPlay: () => Boolean(state),
  onPlayingChange: syncPlayButton,
  onFrameUnavailable: () => { reconError.hidden = false; },
});

function setProgress(pct, msg) {
  const n = Math.max(0, Math.min(PROGRESS_MAX, Math.round(pct)));
  progressFill.style.transform = `scaleX(${n / PROGRESS_MAX})`;
  progressBar.setAttribute("aria-valuenow", String(n));
  progressPct.textContent = `${n}%`;
  if (msg) loadingMsg.textContent = msg;
  reviewRoot.classList.add("is-loading");
  reviewRoot.setAttribute("aria-busy", "true");
}

function clearLoading() {
  setProgress(PROGRESS_MAX);
  reviewRoot.classList.remove("is-loading");
  reviewRoot.setAttribute("aria-busy", "false");
}

function yieldMain() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function waitImg(img) {
  if (isImageDecoded(img)) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      img.removeEventListener("load", done);
      img.removeEventListener("error", done);
      resolve();
    };
    img.addEventListener("load", done);
    img.addEventListener("error", done);
  });
}

function isFrameAlreadyOnScreen(index) {
  return shownFrame === index && isImageDecoded(orig);
}

function formatTime(f, fps) {
  const sec = f / fps;
  const mm = String(Math.floor(sec / 60)).padStart(2, "0");
  const ss = String(Math.floor(sec % 60)).padStart(2, "0");
  const ff = String(Math.floor(f % fps)).padStart(2, "0");
  return `${mm}:${ss}:${ff}`;
}

function paintPlayhead() {
  if (!state) return;
  const n = state.scene.frames;
  frameNum.textContent = String(frame);
  frameTime.textContent = formatTime(frame, state.scene.fps);
  timelineSvg.setAttribute("aria-valuenow", String(frame));
  timelineSvg.setAttribute("aria-valuemax", String(n - 1));
  drawPlayhead();
}

function applyReadyFrame(f) {
  orig.src = previews.src("orig", f);
  recon.src = previews.src("recon", f);
  shownFrame = f;
  reconLoading.hidden = true;
  reconError.hidden = true;
  loadBboxes();
  previews.prefetch(f + 1, state.scene.frames);
}

function showNextPlaybackFrame(next) {
  paintPlayhead();
  applyReadyFrame(next);
}

function syncPlayButton(playing) {
  playBtn.setAttribute("aria-label", playing ? T("review.pause") : T("review.play"));
  playBtn.classList.toggle("is-playing", playing);
  playIcon.hidden = playing;
  pauseIcon.hidden = !playing;
  playIcon.style.display = playing ? "none" : "";
  pauseIcon.style.display = playing ? "inline-block" : "none";
}

function setPlaying(on) {
  transport.setPlaying(on);
}

async function loadFrameImages() {
  imgTimer = 0;
  if (!state) return;
  const target = frame;
  if (isFrameAlreadyOnScreen(target)) return;
  previews.prefetch(target, state.scene.frames);
  if (!previews.isReady(target)) {
    reconLoading.hidden = false;
    reconError.hidden = true;
  }
  const ok = await previews.wait(target);
  if (frame !== target) return;
  if (ok) applyReadyFrame(target);
  else {
    reconLoading.hidden = true;
    reconError.hidden = false;
  }
}

function clampFrame(f) {
  return Math.max(0, Math.min(state.scene.frames - 1, f));
}

function scheduleFrameLoad(immediate) {
  if (isFrameAlreadyOnScreen(frame)) return;
  if (immediate) {
    clearTimeout(imgTimer);
    imgTimer = 0;
    loadFrameImages();
    return;
  }
  if (isSeekQueuedWhilePlaying(transport.isPlaying(), imgTimer)) return;
  clearTimeout(imgTimer);
  imgTimer = setTimeout(loadFrameImages, seekDelayMs(transport.isPlaying()));
}

function setFrame(f, immediate = false) {
  if (!state) return;
  frame = clampFrame(f);
  paintPlayhead();
  scheduleFrameLoad(immediate);
}

function mapBox(box, img, sceneW, sceneH) {
  const pane = img.parentElement.getBoundingClientRect();
  const ir = img.getBoundingClientRect();
  const scaleX = ir.width / Math.max(1, sceneW);
  const scaleY = ir.height / Math.max(1, sceneH);
  const ox = ir.left - pane.left;
  const oy = ir.top - pane.top;
  const [x0, y0, x1, y1] = box;
  return { x: ox + x0 * scaleX, y: oy + y0 * scaleY, w: (x1 - x0) * scaleX, h: (y1 - y0) * scaleY };
}

function overlayToScene(clientX, clientY, img, sceneW, sceneH) {
  const ir = img.getBoundingClientRect();
  const x = ((clientX - ir.left) / Math.max(1, ir.width)) * sceneW;
  const y = ((clientY - ir.top) / Math.max(1, ir.height)) * sceneH;
  return [x, y];
}

function paintOverlay(svg, img, box, draft = false) {
  const pane = img.parentElement;
  const pr = pane.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${pr.width} ${pr.height}`);
  svg.replaceChildren();
  if (!box || !state) return;
  const [sw, sh] = state.scene.size;
  const m = mapBox(box, img, sw, sh);
  const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  rect.setAttribute("class", draft ? "overlay-box overlay-box--draft" : "overlay-box");
  rect.setAttribute("x", String(m.x));
  rect.setAttribute("y", String(m.y));
  rect.setAttribute("width", String(Math.max(1, m.w)));
  rect.setAttribute("height", String(Math.max(1, m.h)));
  svg.appendChild(rect);
}

function drawOverlays() {
  const box = selectedId ? boxes[selectedId] : null;
  paintOverlay(origOverlay, orig, box);
  paintOverlay(reconOverlay, recon, box);
}

async function loadBboxes() {
  if (!state || !projectId) return;
  const id = ++bboxReq;
  const f = frame;
  try {
    const data = await fetchBboxes(projectId, sceneId, f, versionId);
    if (id !== bboxReq || f !== frame) return;
    boxes = data.boxes || {};
    drawOverlays();
  } catch {
    if (id === bboxReq) boxes = {};
  }
}

function drawPlayhead() {
  const n = state?.scene?.frames || 1;
  const x = ((frame + 0.5) / n) * 100;
  if (!playheadEl || !playheadEl.isConnected) {
    playheadEl = document.createElementNS("http://www.w3.org/2000/svg", "line");
    playheadEl.setAttribute("class", "playhead");
    playheadEl.setAttribute("y1", "0");
    playheadEl.setAttribute("y2", String(TIMELINE_HEIGHT_PX));
    playheadEl.setAttribute("stroke", "var(--accent)");
    playheadEl.setAttribute("stroke-width", "2");
    timelineSvg.appendChild(playheadEl);
  }
  playheadEl.setAttribute("x1", `${x}%`);
  playheadEl.setAttribute("x2", `${x}%`);
  if (trackPlayhead) {
    const body = trackPlayhead.parentElement;
    const lane = Math.max(0, (body ? body.clientWidth : 0) - TRACK_GUTTER);
    trackPlayhead.style.left = `${TRACK_GUTTER + (x / 100) * lane}px`;
    trackPlayhead.hidden = !state;
  }
}

function buildErrorStrip() {
  timelineSvg.innerHTML = "";
  playheadEl = null;
  const rec = state.report?.reconstruction;
  const bins = rec?.l1_bins || [];
  const hot = rec?.l1_hot || [];
  const max = rec?.l1_max || MIN_L1_MAX;
  errorPeaks = rec?.l1_peaks || [];
  if (bins.length) {
    const frag = document.createDocumentFragment();
    for (let b = 0; b < bins.length; b++) {
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      const height = (bins[b] / max) * ERROR_STRIP_HEIGHT_PX;
      rect.setAttribute("x", `${(b / bins.length) * 100}%`);
      rect.setAttribute("y", `${TIMELINE_HEIGHT_PX - height}`);
      rect.setAttribute("width", `${100 / bins.length}%`);
      rect.setAttribute("height", `${height}`);
      rect.setAttribute("fill", hot[b] ? "var(--error)" : "var(--muted)");
      frag.appendChild(rect);
    }
    timelineSvg.appendChild(frag);
  }
  drawPlayhead();
}

function renderTracks(from = 0) {
  const els = state.scene.elements || [];
  if (from === 0) timelineTracks.innerHTML = "";
  const end = Math.min(els.length, from + LIST_CHUNK);
  const frag = document.createDocumentFragment();
  for (let i = from; i < end; i++) {
    const el = els[i];
    const row = document.createElement("div");
    row.className = "track" + (el.id === selectedId ? " track--selected" : "");
    row.dataset.id = el.id;
    const vis = el.visible || [0, state.scene.frames - 1];
    const left = (vis[0] / state.scene.frames) * 100;
    const right = 100 - ((vis[1] + 1) / state.scene.frames) * 100;
    row.innerHTML = `<div class="track__label"><span class="mono" style="color:var(--accent)">${el.id}</span><span>${el.kind}</span></div>
      <div class="track__lane"><div class="track__bar" style="left:${left}%;right:${right}%"></div></div>`;
    row.addEventListener("click", (ev) => {
      const clickedLane = ev.target.closest(".track__lane");
      if (clickedLane) {
        const lane = row.querySelector(".track__lane");
        const r = lane.getBoundingClientRect();
        const x = (ev.clientX - r.left) / r.width;
        setFrame(Math.floor(x * state.scene.frames), true);
      }
      selectElement(el.id);
    });
    frag.appendChild(row);
  }
  timelineTracks.appendChild(frag);
}

function appendElementRow(frag, opts, el) {
  const row = document.createElement("div");
  row.className = "element-row" + (el.id === selectedId ? " element-row--selected" : "");
  row.dataset.id = el.id;
  const tex = el.canonical && el.canonical.texture;
  const thumb = tex
    ? `<img class="element-thumb" alt="" src="${reviewAssetUrl(tex, projectId, sceneId)}"/>`
    : `<span class="element-thumb"></span>`;
  row.innerHTML = `${thumb}<div style="display:flex;gap:12px;min-width:0"><span class="mono">${el.id}</span><span>${el.kind}</span></div>`;
  row.addEventListener("click", () => selectElement(el.id));
  frag.appendChild(row);
  const opt = document.createElement("option");
  opt.value = el.id;
  opt.textContent = el.id;
  opts.appendChild(opt);
}

async function renderElements() {
  const els = state.scene.elements || [];
  const hasElements = els.length > 0;
  elementCount.textContent = Tf("review.elements", { n: els.length });
  emptyState.hidden = hasElements;
  elementFilter.hidden = !hasElements;
  document.getElementById("acc-bbox").classList.toggle("accordion--open", !hasElements);
  elementList.innerHTML = "";
  constraintsPanel.innerHTML = "";
  reassignTo.innerHTML = "";
  timelineTracks.innerHTML = "";
  const n = Math.max(els.length, 1);
  for (let from = 0; from < els.length; from += LIST_CHUNK) {
    const end = Math.min(els.length, from + LIST_CHUNK);
    const frag = document.createDocumentFragment();
    const opts = document.createDocumentFragment();
    for (let i = from; i < end; i++) appendElementRow(frag, opts, els[i]);
    elementList.appendChild(frag);
    reassignTo.appendChild(opts);
    renderTracks(from);
    setProgress(LOADING_PCT_LIST_BASE + (end / n) * LOADING_PCT_LIST_SPAN, T("review.loadingList"));
    if (end < els.length) await yieldMain();
  }
  await renderKeepPanel();
}

function appendConstraintRow(frag, c) {
  const row = document.createElement("label");
  row.className = "constraint-row";
  const checked = keepPending.has(c.pred) ? keepPending.get(c.pred) : !!c.keep;
  row.innerHTML = `<input type="checkbox" data-pred="${c.pred}" ${checked ? "checked" : ""}/><span class="mono">${c.pred}</span>`;
  row.querySelector("input").addEventListener("change", (e) => {
    keepPending.set(c.pred, e.target.checked);
    keepDirty = true;
    keepSave.disabled = false;
    keepSave.hidden = false;
  });
  frag.appendChild(row);
}

async function renderKeepPanel() {
  const constraints = state.scene.constraints || [];
  if (!constraints.length) return;
  const head = document.createElement("div");
  head.style.fontWeight = "700";
  head.style.margin = "12px 0 6px";
  head.textContent = T("review.keep");
  constraintsPanel.appendChild(head);
  await renderConstraints(0);
}

async function renderConstraints(from) {
  const constraints = state.scene.constraints || [];
  const end = Math.min(constraints.length, from + CONSTRAINT_STEP);
  for (let s = from; s < end; s += LIST_CHUNK) {
    const e = Math.min(end, s + LIST_CHUNK);
    const frag = document.createDocumentFragment();
    for (let i = s; i < e; i++) appendConstraintRow(frag, constraints[i]);
    constraintsPanel.appendChild(frag);
    if (e < constraints.length) await yieldMain();
  }
  if (end < constraints.length) {
    const more = document.createElement("button");
    more.className = "btn btn--secondary";
    more.style.margin = "6px 0";
    more.textContent = Tf("review.moreConstraints", { n: constraints.length - end });
    more.addEventListener("click", () => {
      more.remove();
      renderConstraints(end);
    });
    constraintsPanel.appendChild(more);
  }
}

function selectElement(id) {
  selectedId = id;
  document.getElementById("reassign-from").value = id;
  document.getElementById("bbox-frame").value = String(frame);
  document.getElementById("mask-frame").value = String(frame);
  const el = state.scene.elements.find((e) => e.id === id);
  if (el?.canonical?.text != null) {
    document.getElementById("text-value").value = el.canonical.text;
  }
  elementList.querySelectorAll(".element-row").forEach((row) => {
    row.classList.toggle("element-row--selected", row.dataset.id === id);
  });
  timelineTracks.querySelectorAll(".track").forEach((row) => {
    row.classList.toggle("track--selected", row.dataset.id === id);
  });
  drawOverlays();
}

function applyElementFilter() {
  const q = (elementFilter.value || "").trim().toLowerCase();
  document.querySelectorAll(".element-row, .track").forEach((row) => {
    const id = (row.dataset.id || "").toLowerCase();
    const kind = (row.textContent || "").toLowerCase();
    const matchesQuery = !q || id.includes(q) || kind.includes(q);
    row.hidden = !matchesQuery;
  });
}

function goAgent() {
  const v = versionId ? `&v=${encodeURIComponent(versionId)}` : "";
  location.href = `/agent?project=${encodeURIComponent(projectId)}&scene=${encodeURIComponent(sceneId)}${v}`;
}

function isApproved(status) {
  return status === "approved";
}

function paintApprove(status) {
  if (!approveBtn) return;
  const approved = isApproved(status);
  approveBtn.hidden = false;
  approveBtn.disabled = false;
  approveBtn.dataset.i18n = approved ? "review.openAgent" : "review.approve";
  approveBtn.textContent = T(approveBtn.dataset.i18n);
}

function fillVersions() {
  versionSelect.innerHTML = "";
  const versions = state.project.versions || [];
  versions.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v.id;
    opt.textContent = `${v.id} · ${v.note || ""}`;
    if (v.id === (versionId || state.version.id)) opt.selected = true;
    versionSelect.appendChild(opt);
  });
  versionSelect.onchange = () => {
    refreshState(versionSelect.value);
  };
}

function replaceReviewUrl() {
  const url = `/review?project=${encodeURIComponent(projectId)}&scene=${encodeURIComponent(sceneId)}&v=${encodeURIComponent(versionId)}`;
  history.replaceState(null, "", url);
}

function applySceneChrome() {
  sceneBadge.textContent = `${sceneId} · ${state.scene.frames}f`;
  frameTotal.textContent = String(state.scene.frames);
  fillVersions();
  paintApprove(state.status);
}

function restoreSelection(keepSel) {
  const stillThere = keepSel && state.scene.elements.some((e) => e.id === keepSel);
  if (stillThere) selectedId = keepSel;
  else if (!state.scene.elements.length) selectedId = null;
}

async function refreshState(v) {
  const keepFrame = frame;
  const keepSel = selectedId;
  versionId = v;
  setProgress(LOADING_PCT_START, T("review.loading"));
  try {
    state = await fetchReviewState(projectId, sceneId, versionId);
  } catch (err) {
    clearLoading();
    setJobBanner(err.message || T("review.loadFailed"), true);
    return;
  }
  versionId = state.version.id;
  replaceReviewUrl();
  restoreSelection(keepSel);
  applySceneChrome();
  await renderElements();
  buildErrorStrip();
  if (selectedId) selectElement(selectedId);
  frame = Math.max(0, Math.min(keepFrame, state.scene.frames - 1));
  shownFrame = -1;
  setFrame(frame, true);
  clearLoading();
}

function selectFirstElementIfNeeded() {
  if (!selectedId && state.scene.elements.length) selectElement(state.scene.elements[0].id);
}

async function loadState() {
  setProgress(LOADING_PCT_START, T("review.loading"));
  orig.src = previews.src("orig", 0);
  const origReady = waitImg(orig);
  state = await fetchReviewState(projectId, sceneId, versionId);
  await yieldMain();
  setProgress(LOADING_PCT_STATE, T("review.loading"));
  versionId = state.version.id;
  applySceneChrome();
  reconLoading.hidden = false;
  recon.src = previews.src("recon", 0);
  await renderElements();
  buildErrorStrip();
  selectFirstElementIfNeeded();
  shownFrame = 0;
  frame = 0;
  setProgress(LOADING_PCT_FRAMES, T("review.loadingFrames"));
  await origReady;
  loadBboxes();
  clearLoading();
}

function setJobBanner(msg, isError = false) {
  jobBanner.hidden = !msg;
  jobBanner.textContent = msg || "";
  jobBanner.classList.toggle("job-banner--error", isError);
}

function setFormsDisabled(disabled) {
  formsPanel.toggleAttribute("disabled", disabled);
  keepSave.disabled = disabled || !keepDirty;
  keepSave.hidden = !keepDirty;
}

function showRunningJob(job) {
  setFormsDisabled(true);
  setJobBanner(Tf("review.running", { op: job.op || "" }));
}

function showFinishedJob(version) {
  setFormsDisabled(false);
  setJobBanner("");
  clearInterval(pollTimer);
  refreshState(version);
}

function showFailedJob(job) {
  setFormsDisabled(false);
  setJobBanner(Tf("review.retry", { err: job.error || T("review.error") }), true);
  clearInterval(pollTimer);
}

function showIdleJob() {
  setFormsDisabled(false);
  setJobBanner("");
  clearInterval(pollTimer);
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
  if (!projectId) return;
  try {
    const job = await fetchReviewJob(projectId, sceneId);
    applyJobSnapshot(job);
  } catch {
    /* ignore transient poll errors */
  }
}

async function runCorrect(op, args) {
  await postCorrect(projectId, sceneId, op, args);
  clearInterval(pollTimer);
  pollTimer = setInterval(pollJob, JOB_POLL_INTERVAL_MS);
  pollJob();
}

function seekPrevErrorPeak() {
  if (!errorPeaks.length) return;
  const prev = [...errorPeaks].reverse().find((p) => p < frame);
  setFrame(prev != null ? prev : errorPeaks[errorPeaks.length - 1]);
}

function seekNextErrorPeak() {
  if (!errorPeaks.length) return;
  const idx = errorPeaks.findIndex((p) => p >= frame);
  setFrame(idx >= 0 ? errorPeaks[idx] : errorPeaks[0]);
}

function nextErrorPeak(backward = false) {
  if (backward) seekPrevErrorPeak();
  else seekNextErrorPeak();
}

function sceneBoxFromDrag(d) {
  return [Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.max(d.x0, d.x1), Math.max(d.y0, d.y1)];
}

function isTinyBox(box) {
  return box[2] - box[0] < MIN_BBOX_EDGE || box[3] - box[1] < MIN_BBOX_EDGE;
}

function resetEditUi() {
  pendingIntent = null;
  editSummary.hidden = true;
  editSummary.textContent = "";
  editConflicts.hidden = true;
  editConflicts.innerHTML = "";
  editConfirm.hidden = true;
  editCancel.hidden = true;
}

function paintConflictChoices(conflicts) {
  conflicts.forEach((c) => {
    const wrap = document.createElement("div");
    wrap.className = "form-row";
    const lab = document.createElement("label");
    lab.textContent = c.reason || c.element;
    wrap.appendChild(lab);
    (c.choices || []).forEach((ch) => {
      const row = document.createElement("label");
      row.className = "constraint-row";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = `edit-${c.id}`;
      radio.value = ch;
      row.append(radio, document.createTextNode(T(`review.editChoice.${ch}`)));
      wrap.appendChild(row);
    });
    editConflicts.appendChild(wrap);
  });
  editConflicts.hidden = false;
}

function paintEditResult(res) {
  pendingIntent = res.intent || null;
  const summary = res.summary || "";
  editSummary.hidden = !summary;
  editSummary.textContent = summary;
  const conflicts = (res.plan && res.plan.conflicts) || [];
  const cands = (res.intent && res.intent.candidates) || [];
  editConflicts.innerHTML = "";
  if (conflicts.length) paintConflictChoices(conflicts);
  else if (cands.length) {
    editConflicts.hidden = false;
    editConflicts.textContent = T("review.editNeedElement");
  } else {
    editConflicts.hidden = true;
  }
  const needsEditConfirm = res.status === "needs_confirm" || res.status === "needs_choice";
  editConfirm.hidden = !needsEditConfirm;
  editCancel.hidden = !needsEditConfirm;
}

function editChoices() {
  const out = {};
  editConflicts.querySelectorAll("input[type=radio]:checked").forEach((el) => {
    out[el.name.replace(/^edit-/, "")] = el.value;
  });
  return out;
}

async function readEditAttachment() {
  const file = editFile.files[0];
  if (!file) return null;
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

async function editBody(confirm) {
  return {
    project: projectId,
    scene: sceneId,
    v: versionId,
    prompt: (editPrompt.value || "").trim(),
    element: selectedId,
    attachment: await readEditAttachment(),
    confirm,
    intent: pendingIntent,
    choices: editChoices(),
  };
}

function bindTransport() {
  playBtn.addEventListener("click", () => {
    setPlaying(!transport.isPlaying());
  });
  document.addEventListener("keydown", (e) => {
    if (!state || e.target.matches("input,select,textarea")) return;
    if (e.code === "Space") {
      e.preventDefault();
      playBtn.click();
    } else if (e.code === "ArrowLeft") {
      setFrame(frame - frameStep(e.shiftKey));
    } else if (e.code === "ArrowRight") {
      setFrame(frame + frameStep(e.shiftKey));
    } else if (e.key === "[") {
      seekPrevErrorPeak();
    } else if (e.key === "]") {
      seekNextErrorPeak();
    }
  });
  timelineSvg.addEventListener("click", (e) => {
    const rect = timelineSvg.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    setFrame(Math.floor(x * state.scene.frames), true);
  });
  document.getElementById("step-back").addEventListener("click", () => setFrame(frame - 1, true));
  document.getElementById("step-fwd").addEventListener("click", () => setFrame(frame + 1, true));
  document.getElementById("peak-back").addEventListener("click", () => seekPrevErrorPeak());
  document.getElementById("peak-fwd").addEventListener("click", () => seekNextErrorPeak());
  document.getElementById("recon-retry").addEventListener("click", () => setFrame(frame, true));
}

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

function bindDraw() {
  origDraw.addEventListener("pointerdown", (e) => {
    if (!state) return;
    origDraw.setPointerCapture(e.pointerId);
    const [sw, sh] = state.scene.size;
    const [x, y] = overlayToScene(e.clientX, e.clientY, orig, sw, sh);
    drag = { x0: x, y0: y, x1: x, y1: y };
  });
  origDraw.addEventListener("pointermove", (e) => {
    if (!drag || !state) return;
    const [sw, sh] = state.scene.size;
    const [x, y] = overlayToScene(e.clientX, e.clientY, orig, sw, sh);
    drag.x1 = x;
    drag.y1 = y;
    paintOverlay(origOverlay, orig, sceneBoxFromDrag(drag), true);
  });
  origDraw.addEventListener("pointerup", () => {
    if (!drag || !state) return;
    const box = sceneBoxFromDrag(drag);
    drag = null;
    if (isTinyBox(box)) {
      drawOverlays();
      return;
    }
    document.getElementById("bbox-coords").value = box.map((v) => Math.round(v)).join(",");
    document.getElementById("bbox-frame").value = String(frame);
    document.querySelectorAll(".accordion").forEach((a) => a.classList.remove("accordion--open"));
    document.getElementById("acc-bbox").classList.add("accordion--open");
    paintOverlay(origOverlay, orig, box);
  });
  orig.addEventListener("load", () => drawOverlays());
}

function bindEdit() {
  editRun.addEventListener("click", async () => {
    if (!projectId) return;
    try {
      const res = await postEdit(await editBody(false));
      paintEditResult(res);
      if (res.status === "failed") setJobBanner(res.error || T("review.editFailed"), true);
    } catch (err) {
      setJobBanner(err.message || T("review.editFailed"), true);
    }
  });
  editConfirm.addEventListener("click", async () => {
    if (!projectId || !pendingIntent) return;
    editConfirm.disabled = true;
    try {
      const res = await postEdit(await editBody(true));
      paintEditResult(res);
      if (res.status === "done" && res.version) {
        const k = res.verify ? Number(res.verify.keep_pass_rate || 0).toFixed(2) : "—";
        const t = res.verify && res.verify.temporal != null ? Number(res.verify.temporal).toFixed(2) : "—";
        setJobBanner(Tf("review.editOk", { k, t }), false);
        resetEditUi();
        await refreshState(res.version.id);
      } else if (res.status === "failed") {
        setJobBanner(res.error || T("review.editFailed"), true);
      }
    } catch (err) {
      setJobBanner(err.message || T("review.editFailed"), true);
    } finally {
      editConfirm.disabled = false;
    }
  });
  editCancel.addEventListener("click", () => resetEditUi());
}

function bindCorrections() {
  keepSave.addEventListener("click", async () => {
    const changes = [...keepPending.entries()].map(([pred, keep]) => ({ pred, keep }));
    const res = await postKeep(projectId, sceneId, changes, KEEP_NOTE);
    keepPending.clear();
    keepDirty = false;
    keepSave.disabled = true;
    keepSave.hidden = true;
    refreshState(res.version.id);
  });
  document.getElementById("reassign-run").addEventListener("click", async () => {
    const parts = document.getElementById("reassign-frames").value.split(",").map((s) => parseInt(s.trim(), 10));
    const hasRange = parts.length === 2;
    await runCorrect("reassign", {
      from_id: document.getElementById("reassign-from").value,
      to_id: reassignTo.value,
      frames: hasRange ? parts : [0, state.scene.frames - 1],
      object_id: reassignTo.value,
    });
  });
  document.getElementById("mask-run").addEventListener("click", async () => {
    const file = document.getElementById("mask-file").files[0];
    if (!file) return;
    const buf = await file.arrayBuffer();
    const b64 = btoa(String.fromCharCode(...new Uint8Array(buf)));
    await runCorrect("mask", {
      frame: parseInt(document.getElementById("mask-frame").value, 10),
      mask_png_base64: b64,
      object_id: selectedId,
    });
  });
  document.getElementById("bbox-run").addEventListener("click", async () => {
    const bbox = document.getElementById("bbox-coords").value.split(",").map((s) => parseInt(s.trim(), 10));
    await runCorrect("bbox", {
      frame: parseInt(document.getElementById("bbox-frame").value, 10),
      bbox,
      object_id: selectedId,
    });
  });
  document.getElementById("text-run").addEventListener("click", async () => {
    await runCorrect("text", {
      element_id: selectedId,
      text: document.getElementById("text-value").value,
    });
  });
}

async function handleApproveClick() {
  if (!projectId || approveBtn.disabled) return;
  if (state && isApproved(state.status)) {
    goAgent();
    return;
  }
  approveBtn.disabled = true;
  try {
    await postApprove(projectId, sceneId, versionId);
    goAgent();
  } catch (err) {
    paintApprove(state && state.status);
    setJobBanner(err.message || T("review.approveFailed"), true);
  }
}

function showEmptyReview() {
  clearLoading();
  reviewRoot.classList.add("is-empty");
  document.body.classList.add("review-empty");
}

function showDemoNote() {
  const note = document.getElementById("demo-note");
  if (note) note.hidden = false;
}

async function bootReviewWorkspace() {
  try {
    await loadState();
    pollJob();
  } catch (err) {
    clearLoading();
    setJobBanner(err.message || T("review.loadFailed"), true);
  }
}

function bindReview() {
  bindTransport();
  bindAccordions();
  elementFilter.addEventListener("input", applyElementFilter);
  bindDraw();
  bindEdit();
  bindCorrections();
  approveBtn.addEventListener("click", handleApproveClick);
  recon.addEventListener("load", () => {
    reconLoading.hidden = true;
    reconError.hidden = true;
    drawOverlays();
  });
  recon.addEventListener("error", () => {
    reconLoading.hidden = true;
    reconError.hidden = false;
  });
  window.addEventListener("keepframe:lang", () => {
    if (state) {
      renderElements();
      paintApprove(state.status);
    }
    playBtn.setAttribute("aria-label", transport.isPlaying() ? T("review.pause") : T("review.play"));
  });
  window.addEventListener("resize", () => {
    if (state) {
      drawPlayhead();
      drawOverlays();
    }
  });
}

bindReview();
playBtn.setAttribute("aria-label", T("review.play"));
if (projectId === "demo") showDemoNote();
if (!projectId) showEmptyReview();
else bootReviewWorkspace();
