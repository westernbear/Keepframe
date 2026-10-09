import { T, Tf } from "/static/js/i18n.js?v=20261009f";

const EDIT_ERRORS = { tint_unavailable: "agent.tintUnavailable", tint_failed: "agent.tintFailed" };

function isEditNeedsConfirm(status) {
  return status === "needs_confirm" || status === "needs_choice";
}

// Edit errors arrive as codes; known ones are shown in the page's language, anything else as sent.
function editErrorText(code) {
  return EDIT_ERRORS[code] ? T(EDIT_ERRORS[code]) : code;
}

// A conflict the page knows by id is explained in the page's language; others keep the server's reason.
function conflictReason(conflict) {
  const key = `review.editConflict.${conflict.id}`;
  const text = T(key);
  return text !== key ? text : (conflict.reason || conflict.element);
}

const BACKGROUND_CONFLICTS = ["background_kind", "background_video"];

// The page's own words for a background colour edit: before the choice it asks how (a picture, gradient or video
// background), afterwards it says what was done (tinted / replaced / cancelled). null for any other edit.
function backgroundSummary(res, choices) {
  const targets = (res && res.intent && res.intent.targets) || [];
  if (targets.length !== 1 || targets[0].property !== "background") return null;
  const colour = targets[0].value || "";
  const conflict = ((res.plan && res.plan.conflicts) || []).find((c) => BACKGROUND_CONFLICTS.includes(c.id));
  if (res.status === "cancelled") return T("agent.cancelled");
  if (res.status === "done") {
    const mode = conflict ? (choices || {})[conflict.id] || (choices || {}).background : "replace";
    return Tf(mode === "tint" ? "agent.bgTinted" : "agent.bgReplaced", { colour });
  }
  return conflict ? Tf("review.editBgChoose", { colour }) : null;
}

export { backgroundSummary, conflictReason, editErrorText, isEditNeedsConfirm };
