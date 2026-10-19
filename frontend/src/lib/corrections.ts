// Request bodies of the review corrections and the edit form (legacy review/corrections.js, review/edit-form.js).
import { isEditNeedsConfirm } from "@/lib/edit-status";

const KEEP_PRESETS = ["content_only", "motion_shape", "none"];

function parseIntList(text) {
  return text.split(",").map((s) => parseInt(s.trim(), 10));
}

/** Two numbers are a frame range; anything else reassigns the whole scene. */
function reassignFrames(parts, frames) {
  const hasReassignRange = parts.length === 2;
  if (hasReassignRange) return parts;
  return [0, frames - 1];
}

/** A pending keep toggle wins over the saved value. */
function constraintChecked(c, keepPending) {
  return keepPending.has(c.pred) ? keepPending.get(c.pred) : !!c.keep;
}

function keepChanges(keepPending) {
  return [...keepPending.entries()].map(([pred, keep]) => ({ pred, keep }));
}

function reassignArgs(fromId, toId, framesText, frames) {
  return { from_id: fromId, to_id: toId, frames: reassignFrames(parseIntList(framesText), frames), object_id: toId };
}

function maskArgs(frameText, b64, selectedId) {
  return { frame: parseInt(frameText, 10), mask_png_base64: b64, object_id: selectedId };
}

function bboxArgs(coordsText, frameText, selectedId) {
  return { frame: parseInt(frameText, 10), bbox: parseIntList(coordsText), object_id: selectedId };
}

function textArgs(selectedId, text) {
  return { element_id: selectedId, text };
}

/** What the edit form shows for a /api/edit response. */
function editResultView(res) {
  const conflicts = (res.plan && res.plan.conflicts) || [];
  const cands = (res.intent && res.intent.candidates) || [];
  return {
    pendingIntent: res.intent || null,
    summary: res.summary || "",
    conflicts,
    needElement: !conflicts.length && cands.length > 0,
    needsConfirm: isEditNeedsConfirm(res.status),
  };
}

/** Checked conflict radios (`name="edit-<conflict id>"`) → {conflict id: choice}. */
function editChoices(checked) {
  const out = {};
  for (const node of checked) out[node.name.replace(/^edit-/, "")] = node.value;
  return out;
}

function editBody({ projectId, sceneId, versionId, prompt, selectedId, attachment, confirm, pendingIntent, choices }) {
  return {
    project: projectId,
    scene: sceneId,
    v: versionId,
    prompt: (prompt || "").trim(),
    element: selectedId,
    attachment,
    confirm,
    intent: pendingIntent,
    choices,
  };
}

function formatVerifyPair(res) {
  const k = res.verify ? Number(res.verify.keep_pass_rate || 0).toFixed(2) : "—";
  const t = res.verify && res.verify.temporal != null ? Number(res.verify.temporal).toFixed(2) : "—";
  return { k, t };
}

/** The intent behind "Generate 3D": previewed first, submitted only on confirm. */
function reference3dIntent(id) {
  return { targets: [{ element: id, property: "model", value: "reference" }] };
}

export {
  KEEP_PRESETS,
  parseIntList,
  reassignFrames,
  constraintChecked,
  keepChanges,
  reassignArgs,
  maskArgs,
  bboxArgs,
  textArgs,
  editResultView,
  editChoices,
  editBody,
  formatVerifyPair,
  reference3dIntent,
};
