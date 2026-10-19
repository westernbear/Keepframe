import { T, Tf } from "@/i18n";

const DEFAULT_FPS = 30;
const SECONDS_PER_MINUTE = 60;
const UPLOAD_LIMIT_BYTES = 512 * 1024 * 1024; // the server's upload cap
const ANALYZE_START_KEY = "keepframe.analyze-start";
const ANALYZE_ESTIMATE_KEY = "keepframe.analyze-estimate";

export interface IngestState {
  project: any;
  mode: string;
  estimateSnapshot: any;
  fullConfirmed: boolean;
  shortAcknowledged: boolean;
  totalFrames: number;
  fps: number;
}

function lastFrameIndex(totalFrames) { return Math.max(0, totalFrames - 1); }

/** `startValue`/`endValue` are the raw range inputs (`[data-start]`, `[data-end]`). */
function selectedWindow(state, startValue, endValue) {
  if (state.mode === "full") return { mode: "full", start: 0, end: lastFrameIndex(state.totalFrames) };
  const start = Math.max(0, Math.min(lastFrameIndex(state.totalFrames), Number(startValue) || 0));
  const end = Math.max(start, Math.min(lastFrameIndex(state.totalFrames), Number(endValue) || 0));
  return { mode: "range", start, end };
}

function windowFrameCount(win, totalFrames) { return win.mode === "range" ? Math.max(1, win.end - win.start + 1) : totalFrames; }

function estimatePayload(win, state): Record<string, any> {
  return { project_id: state.project.id, mode: win.mode, start: win.start, end: win.end, frames: windowFrameCount(win, state.totalFrames), fps: state.fps };
}

function startButtonState(state, est) {
  const needsConfirm = state.mode === "full" && !state.fullConfirmed;
  const needsShortAck = Boolean((est.warnings || []).length) && !state.shortAcknowledged;
  return {
    disabled: needsConfirm || needsShortAck,
    needsConfirm,
    confirmCopy: needsConfirm ? Tf("ingest.fullConfirm", { m: Math.round(est.seconds / SECONDS_PER_MINUTE), n: est.scene_count }) : "",
  };
}

/** One row per scene; every row but the last carries the editable boundary input (the next scene's first frame). */
function sceneBoundaries(est) {
  const scenes = est.scenes || [];
  const transitions = est.transitions || [];
  return scenes.map((scene, index) => {
    const row: any = { id: scene.id, range: `${scene.id} · ${scene.frames[0]}–${scene.frames[1]}` };
    if (index < scenes.length - 1) {
      row.transition = transitions[index]?.transition || "unknown";
      row.min = scene.frames[0] + 1;
      row.max = scenes[index + 1].frames[1];
      row.value = scenes[index + 1].frames[0];
      row.ariaLabel = `${scene.id}–${scenes[index + 1].id} ${T("ingest.boundaryFrame")}`;
    }
    return row;
  });
}

/** `boundaries` are the boundary input values, in order. */
function editedScenes(snapshot, boundaries, win) {
  if (!snapshot?.scenes?.length) return null;
  let first = win.start;
  return snapshot.scenes.map((scene, index) => {
    const last = index < boundaries.length ? boundaries[index] - 1 : win.end;
    const edited = { id: scene.id, frames: [first, last] };
    first = last + 1;
    return edited;
  });
}

function estimateText(est, win, state) {
  const frames = windowFrameCount(win, state.totalFrames);
  const isRange = win.mode === "range";
  return {
    seconds: Tf("ingest.aboutMin", { m: Math.round(est.seconds / SECONDS_PER_MINUTE) }),
    scenes: String(est.scene_count),
    note: est.note + Tf("ingest.perScene", { s: est.seconds_per_scene }),
    rangeMeta: Tf(isRange ? "ingest.rangeSummary" : "ingest.fullSummary", {
      sec: ((isRange ? frames : state.totalFrames) / state.fps).toFixed(1),
      frames: isRange ? frames : state.totalFrames,
      n: est.scene_count,
    }),
  };
}

function videoMeta(v, fps) {
  return `${v.width || "?"}x${v.height || "?"} · ${fps} fps · ${(v.duration_s || 0).toFixed(1)}s`;
}

function uploadTitle(fileName) { return fileName.replace(/\.[^.]+$/, ""); }

function uploadErrorText(err) {
  return err.body && err.body.code === "live_action" ? T("error.liveaction") : err.message || T("ingest.uploadFailed");
}

/** The `?query` that opens /analyze, or null while the start button's guards still hold. */
function analyzeStartQuery(state, win, referenceUi) {
  const snapshot = state.estimateSnapshot;
  if (!state.project || !snapshot || snapshot.mode !== win.mode || snapshot.start !== win.start || snapshot.end !== win.end || (state.mode === "full" && !state.fullConfirmed) || ((snapshot.warnings || []).length && !state.shortAcknowledged)) return null;
  const params = new URLSearchParams({
    job: state.project.id, token: snapshot.confirm_token, mode: snapshot.mode,
    start: String(snapshot.start), end: String(snapshot.end),
    reference: referenceUi ? "ui" : "mg",
  });
  return `?${params.toString()}`;
}

/** Hands the estimate to /analyze; the analyze page starts a job only when its search equals the saved query. */
function saveAnalyzeStart(query, snapshot) {
  sessionStorage.setItem(ANALYZE_START_KEY, query);
  sessionStorage.setItem(ANALYZE_ESTIMATE_KEY, JSON.stringify(snapshot));
}

export {
  DEFAULT_FPS,
  SECONDS_PER_MINUTE,
  UPLOAD_LIMIT_BYTES,
  ANALYZE_START_KEY,
  ANALYZE_ESTIMATE_KEY,
  lastFrameIndex,
  selectedWindow,
  windowFrameCount,
  estimatePayload,
  startButtonState,
  sceneBoundaries,
  editedScenes,
  estimateText,
  videoMeta,
  uploadTitle,
  uploadErrorText,
  analyzeStartQuery,
  saveAnalyzeStart,
};
