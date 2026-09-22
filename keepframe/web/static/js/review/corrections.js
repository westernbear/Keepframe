import { postKeep } from "/static/js/api.js?v=20260921v";
import { readFileAsBase64 } from "/static/js/files.js?v=20260921v";
import { KEEP_NOTE } from "/static/js/review/workspace.js?v=20260921v";

export function attachCorrections(ws) {
  const { dom } = ws;

  function parseIntList(text) {
    return text.split(",").map((s) => parseInt(s.trim(), 10));
  }

  function reassignFrames(parts) {
    const hasReassignRange = parts.length === 2;
    if (hasReassignRange) return parts;
    return [0, ws.state.scene.frames - 1];
  }

  async function saveKeepChanges() {
    const keepSave = dom.keepSave;
    const changes = [...ws.keepPending.entries()].map(([pred, keep]) => ({ pred, keep }));
    const res = await postKeep(ws.projectId, ws.sceneId, changes, KEEP_NOTE);
    ws.keepPending.clear();
    ws.keepDirty = false;
    keepSave.disabled = true;
    keepSave.hidden = true;
    ws.refreshState(res.version.id);
  }

  async function runReassign() {
    const parts = parseIntList(document.getElementById("reassign-frames").value);
    const toId = dom.reassignTo.value;
    await ws.runCorrect("reassign", {
      from_id: document.getElementById("reassign-from").value,
      to_id: toId,
      frames: reassignFrames(parts),
      object_id: toId,
    });
  }

  async function runMask() {
    const file = document.getElementById("mask-file").files[0];
    if (!file) return;
    const b64 = await readFileAsBase64(file);
    await ws.runCorrect("mask", {
      frame: parseInt(document.getElementById("mask-frame").value, 10),
      mask_png_base64: b64,
      object_id: ws.selectedId,
    });
  }

  async function runBbox() {
    const bbox = parseIntList(document.getElementById("bbox-coords").value);
    await ws.runCorrect("bbox", {
      frame: parseInt(document.getElementById("bbox-frame").value, 10),
      bbox,
      object_id: ws.selectedId,
    });
  }

  async function runText() {
    await ws.runCorrect("text", {
      element_id: ws.selectedId,
      text: document.getElementById("text-value").value,
    });
  }

  function bindCorrections() {
    dom.keepSave.addEventListener("click", saveKeepChanges);
    document.getElementById("reassign-run").addEventListener("click", runReassign);
    document.getElementById("mask-run").addEventListener("click", runMask);
    document.getElementById("bbox-run").addEventListener("click", runBbox);
    document.getElementById("text-run").addEventListener("click", runText);
  }

  ws.bindCorrections = bindCorrections;
}
