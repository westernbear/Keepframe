function isEditNeedsConfirm(status) {
  return status === "needs_confirm" || status === "needs_choice";
}

export { isEditNeedsConfirm };
