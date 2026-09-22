import { postEdit } from "/static/js/api.js?v=20260921v";
import { T, Tf } from "/static/js/i18n.js?v=20260921v";
import { readFileAsDataUrl } from "/static/js/files.js?v=20260921v";
import { isEditNeedsConfirm } from "/static/js/edit-status.js?v=20260921v";

export function attachEditForm(ws) {
  const { dom } = ws;

  function resetEditUi() {
    ws.pendingIntent = null;
    dom.editSummary.hidden = true;
    dom.editSummary.textContent = "";
    dom.editConflicts.hidden = true;
    dom.editConflicts.innerHTML = "";
    dom.editConfirm.hidden = true;
    dom.editCancel.hidden = true;
  }

  function paintConflictChoices(conflicts) {
    conflicts.forEach((c) => {
      const wrap = document.createElement("div");
      wrap.className = "form-row";
      const lab = document.createElement("label");
      lab.textContent = c.reason || c.element;
      wrap.appendChild(lab);
      (c.choices || []).forEach((ch) => {
        const row = document.createElement("label");
        row.className = "constraint-row";
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = `edit-${c.id}`;
        radio.value = ch;
        row.append(radio, document.createTextNode(T(`review.editChoice.${ch}`)));
        wrap.appendChild(row);
      });
      dom.editConflicts.appendChild(wrap);
    });
    dom.editConflicts.hidden = false;
  }

  function paintNeedElement() {
    dom.editConflicts.hidden = false;
    dom.editConflicts.textContent = T("review.editNeedElement");
  }

  function paintEditResult(res) {
    ws.pendingIntent = res.intent || null;
    const summary = res.summary || "";
    dom.editSummary.hidden = !summary;
    dom.editSummary.textContent = summary;
    const conflicts = (res.plan && res.plan.conflicts) || [];
    const cands = (res.intent && res.intent.candidates) || [];
    dom.editConflicts.innerHTML = "";
    if (conflicts.length) paintConflictChoices(conflicts);
    else if (cands.length) paintNeedElement();
    else dom.editConflicts.hidden = true;
    const needsEditConfirm = isEditNeedsConfirm(res.status);
    dom.editConfirm.hidden = !needsEditConfirm;
    dom.editCancel.hidden = !needsEditConfirm;
  }

  function editChoices() {
    const out = {};
    dom.editConflicts.querySelectorAll("input[type=radio]:checked").forEach((node) => {
      out[node.name.replace(/^edit-/, "")] = node.value;
    });
    return out;
  }

  async function readEditAttachment() {
    return readFileAsDataUrl(dom.editFile.files[0]);
  }

  async function editBody(confirm) {
    return {
      project: ws.projectId,
      scene: ws.sceneId,
      v: ws.versionId,
      prompt: (dom.editPrompt.value || "").trim(),
      element: ws.selectedId,
      attachment: await readEditAttachment(),
      confirm,
      intent: ws.pendingIntent,
      choices: editChoices(),
    };
  }

  function formatVerifyPair(res) {
    const k = res.verify ? Number(res.verify.keep_pass_rate || 0).toFixed(2) : "—";
    const t = res.verify && res.verify.temporal != null ? Number(res.verify.temporal).toFixed(2) : "—";
    return { k, t };
  }

  async function runEditPreview() {
    if (!ws.projectId) return;
    try {
      const res = await postEdit(await editBody(false));
      paintEditResult(res);
      if (res.status === "failed") ws.setJobBanner(res.error || T("review.editFailed"), true);
    } catch (err) {
      ws.setJobBanner(err.message || T("review.editFailed"), true);
    }
  }

  async function runEditConfirm() {
    if (!ws.projectId || !ws.pendingIntent) return;
    dom.editConfirm.disabled = true;
    try {
      const res = await postEdit(await editBody(true));
      paintEditResult(res);
      if (res.status === "done" && res.version) {
        ws.setJobBanner(Tf("review.editOk", formatVerifyPair(res)), false);
        resetEditUi();
        await ws.refreshState(res.version.id);
        return;
      }
      if (res.status === "failed") ws.setJobBanner(res.error || T("review.editFailed"), true);
    } catch (err) {
      ws.setJobBanner(err.message || T("review.editFailed"), true);
    } finally {
      dom.editConfirm.disabled = false;
    }
  }

  function bindEdit() {
    dom.editRun.addEventListener("click", runEditPreview);
    dom.editConfirm.addEventListener("click", runEditConfirm);
    dom.editCancel.addEventListener("click", () => resetEditUi());
  }

  ws.resetEditUi = resetEditUi;
  ws.paintEditResult = paintEditResult;
  ws.bindEdit = bindEdit;
}
