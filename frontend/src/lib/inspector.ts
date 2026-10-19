// Decisions of the review inspector (legacy review/inspector.js): element list, font picker, object detail,
// scene chrome, approve.
import { T, Tf } from "@/i18n";
import { LOADING_PCT_LIST_BASE, LOADING_PCT_LIST_SPAN, PROGRESS_MAX } from "@/lib/review-geom";

function clampProgress(pct) {
  return Math.max(0, Math.min(PROGRESS_MAX, Math.round(pct)));
}

/** Loading progress after `end` of `count` element rows are listed. */
function listProgress(end, count) {
  return LOADING_PCT_LIST_BASE + (end / Math.max(count, 1)) * LOADING_PCT_LIST_SPAN;
}

function isLatestVersion(versionId, versions) {
  return versionId === versions.at(-1)?.id;
}

function elementLabel(item) {
  return `${item.id} · ${T(`review.kind.${item.kind}`)}${item.canonical?.text ? ` · ${item.canonical.text}` : ""}`;
}

/** Element filter: matches the id or the row's text (id · kind · text). */
function matchesElementFilter(query, id, text) {
  const q = (query || "").trim().toLowerCase();
  return !q || (id || "").toLowerCase().includes(q) || (text || "").toLowerCase().includes(q);
}

/** The keep-alive selection after a reload, else the first element. */
function nextSelection(keepSel, elements) {
  const stillThere = keepSel && elements.some((e) => e.id === keepSel);
  return (stillThere ? keepSel : null) || elements[0]?.id || null;
}

/** Correction-form state for the selected element. */
function selectionForm(item, versionId, versions) {
  return {
    text: item?.canonical?.text || "",
    textRunDisabled: item?.kind !== "text",
    maskRunDisabled: item?.kind === "text",
    regionModeDisabled: !item || !isLatestVersion(versionId, versions),
  };
}

/** Font families to offer for a text element (the current family always first-class), or null for no picker. */
function fontPicker(item) {
  const font = item?.canonical?.font;
  if (item?.kind !== "text" || !font?.candidates?.length) return null;
  const families = font.candidates.includes(font.family_guess) ? font.candidates : [font.family_guess, ...font.candidates];
  return { font, families, value: font.family_guess };
}

/** The "text" correction for the picked family; copies the shown font so the loaded scene stays untouched. */
function fontCorrection(id, font, family) {
  return { element_id: id, font: { ...font, family_guess: family } };
}

/** Rows of the object-detail panel as [label key, value], plus the 3D-generation button state (null when the
 *  element is no 3D candidate). */
function objectDetail(item, detected, info, analysis, versionId, versions) {
  const lines: [string, string][] = [];
  const line = (label, value) => lines.push([label, value]);
  line("review.objectType", T(`review.kind.${item.kind}`));
  line("review.recognizedText", detected?.ocr.map((o) => o.text).join(" / ") || "—");
  if (item.kind === "text" && item.canonical?.text) line("review.savedText", item.canonical.text);
  const confidences = detected?.ocr.filter((o) => Number.isFinite(o.confidence)).map((o) => `${(o.confidence * 100).toFixed(1)}%`) || [];
  if (confidences.length) line("review.ocrConfidence", confidences.join(" / "));
  if (detected?.regions.length) {
    const boxes = detected.regions.map((r) => r.bbox);
    const x = Math.min(...boxes.map((b) => b[0])), y = Math.min(...boxes.map((b) => b[1]));
    line("review.position", `${x}, ${y} px`);
    line("review.size", `${Math.max(...boxes.map((b) => b[2])) - x} × ${Math.max(...boxes.map((b) => b[3])) - y} px`);
  } else line("review.position", T(analysis ? "review.notDetected" : "review.noAnalysis"));
  line("review.appearance", (info?.intervals || (!analysis ? [item.visible] : [])).map(([a, b]) => `${a}–${b} f`).join(", ") || "—");
  let pending3d: { disabled: boolean; title: string } | null = null;
  if (item.pending_asset === "3d") {
    const disabled = !isLatestVersion(versionId, versions);
    pending3d = { disabled, title: disabled ? T("review.latestOnly") : "" };
  }
  return { title: `${item.id} · ${T("review.objectDetails")}`, lines, pending3d };
}

function sceneBadge(sceneId, frames) {
  return `${sceneId} · ${frames}f`;
}

function sceneOptionLabel(scene) {
  return `${scene.id} · ${scene.frames[0]}–${scene.frames[1]}`;
}

function sceneMeta(scenes, links, sceneId) {
  const current = scenes.find((scene) => scene.id === sceneId);
  if (!current) return "";
  const count = (links || []).filter((link) => link.from?.scene === sceneId || link.to?.scene === sceneId).length;
  return Tf("review.sceneMeta", {
    range: `${current.frames[0]}–${current.frames[1]}`,
    transition: current.transition_out?.transition || T("review.transitionNone"),
    links: count,
  });
}

function versionLabel(v) {
  return `${v.id} · ${v.note || ""}`;
}

function isApproved(status) {
  return status === "approved";
}

function approveLabelKey(status) {
  return isApproved(status) ? "review.openAgent" : "review.approve";
}

/** The first scene still waiting for approval, after an approve. */
function nextUnapprovedScene(scenes, approvals) {
  return (scenes || []).find((scene) => !approvals[scene.id]);
}

function agentUrl(projectId, sceneId, versionId) {
  const v = versionId ? `&v=${encodeURIComponent(versionId)}` : "";
  return `/agent?project=${encodeURIComponent(projectId)}&scene=${encodeURIComponent(sceneId)}${v}`;
}

function reviewUrl(projectId, sceneId, versionId) {
  return `/review?project=${encodeURIComponent(projectId)}&scene=${encodeURIComponent(sceneId)}&v=${encodeURIComponent(versionId)}`;
}

export {
  clampProgress,
  listProgress,
  isLatestVersion,
  elementLabel,
  matchesElementFilter,
  nextSelection,
  selectionForm,
  fontPicker,
  fontCorrection,
  objectDetail,
  sceneBadge,
  sceneOptionLabel,
  sceneMeta,
  versionLabel,
  isApproved,
  approveLabelKey,
  nextUnapprovedScene,
  agentUrl,
  reviewUrl,
};
