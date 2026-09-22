export const JOB_POLL_INTERVAL_MS = 800;
export const LOADING_PCT_START = 8;
export const LOADING_PCT_STATE = 45;
export const LOADING_PCT_FRAMES = 85;
export const LOADING_PCT_LIST_BASE = 45;
export const LOADING_PCT_LIST_SPAN = 30;
export const PROGRESS_MAX = 100;
export const LIST_CHUNK = 32;
export const CONSTRAINT_STEP = 128;
export const TIMELINE_HEIGHT_PX = 72;
export const ERROR_STRIP_HEIGHT_PX = 64;
export const MIN_L1_MAX = 1e-6;
export const MIN_BBOX_EDGE = 2;
export const KEEP_NOTE = "keep 조건 수정";
export const DEFAULT_SCENE_ID = "s1";

export function yieldMain() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

export function trackGutterPx() {
  const label = document.querySelector(".track__label");
  if (label) return label.getBoundingClientRect().width;
  const raw = getComputedStyle(document.documentElement).getPropertyValue("--track-gutter");
  const n = parseFloat(raw);
  return Number.isFinite(n) ? n : 280;
}

function el(id) {
  return document.getElementById(id);
}

export function createReviewWorkspace() {
  const params = new URLSearchParams(location.search);
  return {
    projectId: params.get("project"),
    sceneId: params.get("scene") || DEFAULT_SCENE_ID,
    versionId: params.get("v") || null,
    state: null,
    frame: 0,
    keepDirty: false,
    keepPending: new Map(),
    errorPeaks: [],
    pollTimer: 0,
    selectedId: null,
    shownFrame: -1,
    imgTimer: 0,
    playheadEl: null,
    boxes: {},
    bboxReq: 0,
    drag: null,
    pendingIntent: null,
    previews: null,
    transport: null,
    dom: {
      orig: el("orig"),
      recon: el("recon"),
      frameNum: el("frame-num"),
      frameTotal: el("frame-total"),
      frameTime: el("frame-time"),
      playBtn: el("play-btn"),
      playIcon: el("play-icon"),
      pauseIcon: el("pause-icon"),
      trackPlayhead: el("track-playhead"),
      versionSelect: el("version-select"),
      elementList: el("element-list"),
      constraintsPanel: el("constraints-panel"),
      emptyState: el("empty-state"),
      elementCount: el("element-count"),
      keepSave: el("keep-save"),
      approveBtn: el("review-approve"),
      formsPanel: el("forms-panel"),
      jobBanner: el("job-banner"),
      timelineSvg: el("timeline-svg"),
      timelineTracks: el("timeline-tracks"),
      sceneBadge: el("scene-badge"),
      reassignTo: el("reassign-to"),
      reviewRoot: el("review-root"),
      loadingMsg: el("review-loading-msg"),
      progressBar: el("review-progress"),
      progressFill: el("review-progress-fill"),
      progressPct: el("review-progress-pct"),
      reconLoading: el("recon-loading"),
      reconError: el("recon-error"),
      origOverlay: el("orig-overlay"),
      reconOverlay: el("recon-overlay"),
      origDraw: el("orig-draw"),
      elementFilter: el("element-filter"),
      editPrompt: el("edit-prompt"),
      editFile: el("edit-file"),
      editSummary: el("edit-summary"),
      editConflicts: el("edit-conflicts"),
      editRun: el("edit-run"),
      editConfirm: el("edit-confirm"),
      editCancel: el("edit-cancel"),
    },
  };
}
