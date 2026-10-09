import { T } from "/static/js/i18n.js?v=20261009e";

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

export { conflictReason, editErrorText, isEditNeedsConfirm };
