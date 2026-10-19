import { T, Tf } from "@/i18n";
import { ANALYZE_ESTIMATE_KEY, ANALYZE_START_KEY } from "@/lib/ingest";

const ANALYZE_POLL_INTERVAL_MS = 1000;
const SECONDS_PER_MINUTE = 60;
const DEFAULT_SCENE_ID = "s1";

// tests/test_plate_stage.py checks this against the backend STAGES: same ids, same order.
const PIPELINE = ["frames", "background", "text", "regions", "tracking", "plate", "solids", "sprites", "keyframes", "semantics", "constraints", "report"];
const STEP_STAGES = {
  shots: ["frames"],
  bg: ["background"],
  text: ["text"],
  regions: ["regions", "tracking", "plate", "solids"],
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
  plate: "analyze.step.regions",
  solids: "analyze.step.regions",
  sprites: "analyze.step.sprites",
  keyframes: "analyze.step.keyframes",
  semantics: "analyze.step.predicates",
  constraints: "analyze.step.predicates",
  report: "analyze.step.report",
};

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

function stageLabel(job) {
  const key = STAGE_I18N[job.stage];
  const name = key ? T(key) : (job.stage || job.status || "—");
  return job.detail ? `${name} ${job.detail}` : name;
}

function reviewHref(project) {
  return `/review?project=${project}&scene=${DEFAULT_SCENE_ID}`;
}

function hasExplicitRange(params) {
  const analyzeStart = params.get("start");
  return params.get("mode") === "range" && analyzeStart != null && analyzeStart !== "" && params.get("end") != null;
}

function rangeLabel(params, project) {
  if (hasExplicitRange(params)) return `${params.get("start")}–${params.get("end")}`;
  if (project && project.range) return `${project.range[0]}–${project.range[1]}`;
  return null;
}

/** Reads the ingest hand-off once: a job starts only when this page was opened by ingest's start button. */
function readAnalyzeStart(search) {
  const explicitStart = sessionStorage.getItem(ANALYZE_START_KEY) === search;
  if (explicitStart) sessionStorage.removeItem(ANALYZE_START_KEY);
  const estimateSnapshot = (() => {
    try { return JSON.parse(sessionStorage.getItem(ANALYZE_ESTIMATE_KEY) || "null"); }
    catch { return null; }
  })();
  if (explicitStart) sessionStorage.removeItem(ANALYZE_ESTIMATE_KEY);
  return { explicitStart, estimateSnapshot };
}

function analyzePayload(params, estimateSnapshot) {
  const analyzeMode = params.get("mode") || "";
  const analyzeStart = params.get("start");
  const analyzeEnd = params.get("end");
  const payload: Record<string, any> = { project_id: params.get("job") || params.get("project"), confirm_token: params.get("token") || "", reference: params.get("reference") === "ui" ? "ui" : "mg" };
  if (analyzeMode) payload.mode = analyzeMode;
  if (analyzeStart != null && analyzeStart !== "") payload.start = Number(analyzeStart);
  if (analyzeEnd != null && analyzeEnd !== "") payload.end = Number(analyzeEnd);
  if (estimateSnapshot) {
    payload.scenes = estimateSnapshot.scenes || [];
    payload.transitions = estimateSnapshot.transitions || [];
    payload.boundary_digest = estimateSnapshot.boundary_digest || null;
    payload.acknowledge_short_scenes = Boolean(estimateSnapshot.acknowledge_short_scenes);
  }
  return payload;
}

export {
  ANALYZE_POLL_INTERVAL_MS,
  DEFAULT_SCENE_ID,
  PIPELINE,
  STEP_STAGES,
  STAGE_I18N,
  formatEta,
  stepState,
  stageLabel,
  reviewHref,
  hasExplicitRange,
  rangeLabel,
  readAnalyzeStart,
  analyzePayload,
};
