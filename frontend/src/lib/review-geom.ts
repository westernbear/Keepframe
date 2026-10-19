// Review workspace constants and the timeline / player geometry (legacy review/workspace.js, timeline.js,
// playback-ui.js).
const JOB_POLL_INTERVAL_MS = 800;
const LOADING_PCT_START = 8;
const LOADING_PCT_STATE = 45;
const LOADING_PCT_FRAMES = 85;
const LOADING_PCT_LIST_BASE = 45;
const LOADING_PCT_LIST_SPAN = 30;
const PROGRESS_MAX = 100;
const LIST_CHUNK = 32;
const CONSTRAINT_STEP = 128;
const TIMELINE_HEIGHT_PX = 72;
const MIN_BBOX_EDGE = 2;
const KEEP_NOTE = "keep 조건 수정"; // the version note stored server-side, not UI copy
const DEFAULT_SCENE_ID = "s1";
const TRACK_GUTTER_FALLBACK_PX = 280;

/** Playhead position in % of the timeline: the centre of the frame's slot. */
function playheadPct(frame, frames) {
  const n = frames || 1;
  return ((frame + 0.5) / n) * 100;
}

/** Track-lane playhead `left` in px: the lane starts after the label gutter. */
function trackPlayheadLeft(pct, bodyWidth, gutter) {
  const lane = Math.max(0, bodyWidth - gutter);
  return gutter + (pct / 100) * lane;
}

function rulerLabels(n) {
  return Array.from({ length: 6 }, (_, i) => ({
    x: `${i * 19.6 + 1}%`,
    anchor: i === 0 ? "start" : i === 5 ? "end" : "middle",
    text: `${Math.round(i / 5 * (n - 1))} f`,
  }));
}

/** Frame intervals of a track: the analysis object's, else (no analysis) the element's visible span. */
function trackIntervals(item, analysis) {
  return analysis?.objects.find((o) => o.id === item.id)?.intervals || (!analysis ? [item.visible] : []);
}

function trackBar([a, b], frames) {
  return { left: `${a / frames * 100}%`, right: `${100 - (b + 1) / frames * 100}%` };
}

/** Frame under a click on the timeline ruler or a track lane. */
function frameAtX(clientX, rect, frames) {
  return Math.floor((clientX - rect.left) / rect.width * frames);
}

function clampFrame(f, frames) {
  return Math.max(0, Math.min(frames - 1, Math.round(f)));
}

/** mm:ss:ff */
function timecode(frame, fps) {
  const sec = Math.floor(frame / fps);
  return [Math.floor(sec / 60), sec % 60, Math.floor(frame % fps)].map((v) => String(v).padStart(2, "0")).join(":");
}

/** The letterboxed image inside an element rect (object-fit: contain). */
function imageRect(rect, [sw, sh]) {
  const scale = Math.min(rect.width / sw, rect.height / sh);
  return { left: rect.left + (rect.width - sw * scale) / 2, top: rect.top + (rect.height - sh * scale) / 2, width: sw * scale, height: sh * scale };
}

/** A pointer position in scene pixels, clamped to the scene. */
function scenePoint(clientX, clientY, r, [w, h]) {
  return [Math.round(Math.max(0, Math.min(w, (clientX - r.left) / r.width * w))), Math.round(Math.max(0, Math.min(h, (clientY - r.top) / r.height * h)))];
}

function dragBox(d) {
  return [Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.max(d.x0, d.x1), Math.max(d.y0, d.y1)];
}

function isBoxBigEnough(box) {
  return box[2] - box[0] >= MIN_BBOX_EDGE && box[3] - box[1] >= MIN_BBOX_EDGE;
}

function regionPath(rings) {
  return rings.map((ring) => ring.points.map(([x, y], i) => `${i ? "L" : "M"}${x},${y}`).join(" ") + " Z").join(" ");
}

/** Places one overlay caption in scene px, moving it off captions already in `placed` (which it joins). */
function placeLabel(label, [x, y], sceneWidth, rectWidth, placed) {
  const fontSize = Math.max(10, 12 * sceneWidth / Math.max(1, rectWidth));
  const maxChars = Math.max(12, Math.floor(sceneWidth / (fontSize * 0.7)));
  const caption = label.length > maxChars ? `${label.slice(0, maxChars - 1)}…` : label;
  const labelX = Math.max(2, Math.min(x, sceneWidth - caption.length * fontSize * 0.7));
  let labelY = Math.max(fontSize, y - fontSize / 3);
  const labelWidth = caption.length * fontSize * 0.7;
  for (let attempt = 0; attempt < placed.length; attempt++) {
    if (!placed.some((p) => Math.abs(p.y - labelY) < fontSize * 1.2 && labelX < p.x + p.width && labelX + labelWidth > p.x)) break;
    labelY = labelY > fontSize * 2.4 ? labelY - fontSize * 1.3 : labelY + fontSize * 1.3;
  }
  placed.push({ x: labelX, y: labelY, width: labelWidth });
  return { x: labelX, y: labelY, fontSize, caption };
}

/** Overlay status once an overlay response arrived. */
function overlayStatusKey(data) {
  return !data.available ? "review.noAnalysis" : data.objects.length ? "review.selectRegion" : "review.noDetections";
}

/** Overlay status when leaving region-draw mode. */
function idleOverlayStatusKey(overlay, analysis) {
  return overlay?.objects.length ? "review.selectRegion" : analysis ? "review.noDetections" : "review.noAnalysis";
}

export {
  JOB_POLL_INTERVAL_MS,
  LOADING_PCT_START,
  LOADING_PCT_STATE,
  LOADING_PCT_FRAMES,
  LOADING_PCT_LIST_BASE,
  LOADING_PCT_LIST_SPAN,
  PROGRESS_MAX,
  LIST_CHUNK,
  CONSTRAINT_STEP,
  TIMELINE_HEIGHT_PX,
  MIN_BBOX_EDGE,
  KEEP_NOTE,
  DEFAULT_SCENE_ID,
  TRACK_GUTTER_FALLBACK_PX,
  playheadPct,
  trackPlayheadLeft,
  rulerLabels,
  trackIntervals,
  trackBar,
  frameAtX,
  clampFrame,
  timecode,
  imageRect,
  scenePoint,
  dragBox,
  isBoxBigEnough,
  regionPath,
  placeLabel,
  overlayStatusKey,
  idleOverlayStatusKey,
};
