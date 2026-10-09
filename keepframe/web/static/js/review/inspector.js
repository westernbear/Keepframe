import { objectColor } from "/static/js/review/colors.js?v=20261009b";
import {
  fetchReviewState,
  postApprove,
} from "/static/js/api.js?v=20261009b";
import { T, Tf } from "/static/js/i18n.js?v=20261009b";
import {
  LOADING_PCT_START,
  LOADING_PCT_LIST_BASE,
  LOADING_PCT_LIST_SPAN,
  PROGRESS_MAX,
  LIST_CHUNK,
  CONSTRAINT_STEP,
  yieldMain,
} from "/static/js/review/workspace.js?v=20261009b";

export function attachInspector(ws) {
  const { dom } = ws;

  function setProgress(pct, msg) {
    const n = Math.max(0, Math.min(PROGRESS_MAX, Math.round(pct)));
    dom.progressFill.style.transform = `scaleX(${n / PROGRESS_MAX})`;
    dom.progressBar.setAttribute("aria-valuenow", String(n));
    dom.progressPct.textContent = `${n}%`;
    if (msg) dom.loadingMsg.textContent = msg;
    dom.reviewRoot.classList.add("is-loading");
    dom.reviewRoot.setAttribute("aria-busy", "true");
  }

  function clearLoading() {
    setProgress(PROGRESS_MAX);
    dom.reviewRoot.classList.remove("is-loading");
    dom.reviewRoot.setAttribute("aria-busy", "false");
  }

  function appendElementRow(frag, opts, item) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "element-row" + (item.id === ws.selectedId ? " element-row--selected" : "");
    row.dataset.id = item.id;
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-pressed", item.id === ws.selectedId);
    const swatch = document.createElement("span");
    swatch.className = "object-swatch";
    swatch.style.background = objectColor(item.id);
    const meta = document.createElement("span");
    meta.className = "element-row__meta";
    meta.textContent = `${item.id} · ${T(`review.kind.${item.kind}`)}${item.canonical?.text ? ` · ${item.canonical.text}` : ""}`;
    row.append(swatch, meta);
    row.addEventListener("keydown", e => {
      if (["Enter", " "].includes(e.key)) { e.preventDefault(); selectElement(item.id); }
    });
    row.addEventListener("click", () => selectElement(item.id));
    frag.appendChild(row);
    const opt = document.createElement("option");
    opt.value = item.id;
    opt.textContent = item.id;
    opts.appendChild(opt);
  }

  async function renderElements() {
    const request = ws.stateReq;
    const els = ws.state.scene.elements || [];
    const hasElements = els.length > 0;
    dom.elementCount.textContent = Tf("review.elements", { n: els.length });
    dom.emptyState.hidden = hasElements;
    dom.elementFilter.hidden = !hasElements;
    document.getElementById("acc-bbox").classList.toggle("accordion--open", !hasElements);
    dom.elementList.innerHTML = "";
    dom.constraintsPanel.innerHTML = "";
    dom.reassignTo.innerHTML = "";
    dom.timelineTracks.innerHTML = "";
    const n = Math.max(els.length, 1);
    for (let from = 0; from < els.length; from += LIST_CHUNK) {
      if (request !== ws.stateReq) return;
      const end = Math.min(els.length, from + LIST_CHUNK);
      const frag = document.createDocumentFragment();
      const opts = document.createDocumentFragment();
      for (let i = from; i < end; i++) appendElementRow(frag, opts, els[i]);
      dom.elementList.appendChild(frag);
      dom.reassignTo.appendChild(opts);
      ws.renderTracks(from);
      setProgress(LOADING_PCT_LIST_BASE + (end / n) * LOADING_PCT_LIST_SPAN, T("review.loadingList"));
      if (end < els.length) await yieldMain();
    }
    await renderKeepPanel();
  }

  function appendConstraintRow(frag, c) {
    const row = document.createElement("label");
    row.className = "constraint-row";
    const checked = ws.keepPending.has(c.pred) ? ws.keepPending.get(c.pred) : !!c.keep;
    row.innerHTML = `<input type="checkbox" data-pred="${c.pred}" ${checked ? "checked" : ""}/><span class="mono">${c.pred}</span>`;
    row.querySelector("input").addEventListener("change", (e) => {
      ws.keepPending.set(c.pred, e.target.checked);
      ws.keepDirty = true;
      dom.keepSave.disabled = false;
      dom.keepSave.hidden = false;
    });
    frag.appendChild(row);
  }

  async function renderKeepPanel() {
    const bar = document.createElement("div");
    bar.className = "keep-presets";
    for (const preset of ["content_only", "motion_shape", "none"]) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn--secondary";
      btn.textContent = T(`review.keepPreset.${preset}`);
      btn.addEventListener("click", () => ws.applyKeepPreset(preset));
      bar.appendChild(btn);
    }
    dom.constraintsPanel.appendChild(bar);
    const constraints = ws.state.scene.constraints || [];
    if (!constraints.length) return;
    await renderConstraints(0);
  }

  async function renderConstraints(from) {
    const constraints = ws.state.scene.constraints || [];
    const end = Math.min(constraints.length, from + CONSTRAINT_STEP);
    for (let s = from; s < end; s += LIST_CHUNK) {
      const e = Math.min(end, s + LIST_CHUNK);
      const frag = document.createDocumentFragment();
      for (let i = s; i < e; i++) appendConstraintRow(frag, constraints[i]);
      dom.constraintsPanel.appendChild(frag);
      if (e < constraints.length) await yieldMain();
    }
    if (end < constraints.length) {
      const more = document.createElement("button");
      more.className = "btn btn--secondary btn--more-constraints";
      more.textContent = Tf("review.moreConstraints", { n: constraints.length - end });
      more.addEventListener("click", () => {
        more.remove();
        renderConstraints(end);
      });
      dom.constraintsPanel.appendChild(more);
    }
  }

  function renderFontCandidates(item) {
    const root = document.getElementById("font-candidates");
    root.replaceChildren();
    const font = item?.canonical?.font;
    if (item?.kind !== "text" || !font?.candidates?.length) return;
    const row = document.createElement("div");
    row.className = "form-row";
    const label = document.createElement("label");
    label.htmlFor = "font-candidate-family";
    label.dataset.i18n = "review.fontCandidates";
    label.textContent = T(label.dataset.i18n);
    const select = document.createElement("select");
    select.id = label.htmlFor;
    const families = font.candidates.includes(font.family_guess) ? font.candidates : [font.family_guess, ...font.candidates];
    for (const family of families) {
      const option = document.createElement("option");
      option.value = family;
      option.textContent = family;
      select.appendChild(option);
    }
    select.value = font.family_guess;
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "btn btn--secondary";
    apply.dataset.i18n = "review.applyFont";
    apply.textContent = T(apply.dataset.i18n);
    apply.addEventListener("click", () => ws.runCorrect("text", {
      element_id: item.id, font: { ...font, family_guess: select.value },
    }));
    row.append(label, select);
    root.append(row, apply);
  }

  function selectElement(id) {
    ws.selectedId = id;
    document.getElementById("reassign-from").value = id;
    document.getElementById("bbox-frame").value = String(ws.frame);
    document.getElementById("mask-frame").value = String(ws.frame);
    const item = ws.state.scene.elements.find((e) => e.id === id);
    document.getElementById("text-value").value = item?.canonical?.text || "";
    document.getElementById("text-run").disabled = item?.kind !== "text";
    document.getElementById("mask-run").disabled = item?.kind === "text";
    document.getElementById("region-mode").disabled = !item || ws.versionId !== ws.state.project.versions.at(-1)?.id;
    renderFontCandidates(item);
    dom.elementList.querySelectorAll(".element-row").forEach((row) => {
      row.classList.toggle("element-row--selected", row.dataset.id === id);
      row.setAttribute("aria-pressed", row.dataset.id === id);
    });
    dom.timelineTracks.querySelectorAll(".track").forEach((row) => {
      row.classList.toggle("track--selected", row.dataset.id === id);
    });
    renderObjectDetail();
    ws.drawOverlays();
  }

  function applyElementFilter() {
    const q = (dom.elementFilter.value || "").trim().toLowerCase();
    document.querySelectorAll(".element-row, .track").forEach((row) => {
      const id = (row.dataset.id || "").toLowerCase();
      const kind = (row.textContent || "").toLowerCase();
      const matchesQuery = !q || id.includes(q) || kind.includes(q);
      row.hidden = !matchesQuery;
    });
  }

  function goAgent() {
    const v = ws.versionId ? `&v=${encodeURIComponent(ws.versionId)}` : "";
    location.href = `/agent?project=${encodeURIComponent(ws.projectId)}&scene=${encodeURIComponent(ws.sceneId)}${v}`;
  }

  function isApproved(status) {
    return status === "approved";
  }

  function paintApprove(status) {
    if (!dom.approveBtn) return;
    const approved = isApproved(status);
    dom.approveBtn.hidden = false;
    dom.approveBtn.disabled = false;
    dom.approveBtn.dataset.i18n = approved ? "review.openAgent" : "review.approve";
    dom.approveBtn.textContent = T(dom.approveBtn.dataset.i18n);
  }

  function fillVersions() {
    dom.versionSelect.innerHTML = "";
    const versions = ws.state.project.versions || [];
    versions.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v.id;
      opt.textContent = `${v.id} · ${v.note || ""}`;
      if (v.id === (ws.versionId || ws.state.version.id)) opt.selected = true;
      dom.versionSelect.appendChild(opt);
    });
    dom.versionSelect.onchange = () => {
      refreshState(dom.versionSelect.value);
    };
  }

  function replaceReviewUrl() {
    const url = `/review?project=${encodeURIComponent(ws.projectId)}&scene=${encodeURIComponent(ws.sceneId)}&v=${encodeURIComponent(ws.versionId)}`;
    history.replaceState(null, "", url);
  }

  function applySceneChrome() {
    dom.sceneBadge.textContent = `${ws.sceneId} · ${ws.state.scene.frames}f`;
    const scenes = ws.state.project.scenes || [];
    dom.sceneSelect.replaceChildren();
    scenes.forEach((scene) => {
      const option = document.createElement("option");
      option.value = scene.id;
      option.textContent = `${scene.id} · ${scene.frames[0]}–${scene.frames[1]}`;
      option.selected = scene.id === ws.sceneId;
      dom.sceneSelect.appendChild(option);
    });
    dom.sceneSelect.hidden = scenes.length < 2;
    const current = scenes.find((scene) => scene.id === ws.sceneId);
    const links = (ws.state.project.links || []).filter((link) => link.from?.scene === ws.sceneId || link.to?.scene === ws.sceneId);
    dom.sceneMeta.textContent = current ? `${current.frames[0]}–${current.frames[1]} · 전환 ${current.transition_out?.transition || "없음"} · 링크 ${links.length}` : "";
    dom.sceneSelect.onchange = () => switchScene(dom.sceneSelect.value);
    dom.frameTotal.textContent = String(ws.state.scene.frames);
    fillVersions();
    paintApprove(ws.state.status);
  }

  async function switchScene(sceneId) {
    if (!sceneId || sceneId === ws.sceneId) return;
    ws.setPlaying(false);
    ws.sceneId = sceneId;
    ws.versionId = null;
    ws.selectedId = null;
    ws.frame = 0;
    ws.shownFrame = -1;
    await refreshState(null);
    ws.pollJob();
  }

  function restoreSelection(keepSel) {
    const stillThere = keepSel && ws.state.scene.elements.some((e) => e.id === keepSel);
    if (stillThere) ws.selectedId = keepSel;
    else ws.selectedId = null;
  }

  function renderObjectDetail() {
    dom.objectDetail.replaceChildren();
    const item = ws.state?.scene.elements.find(e => e.id === ws.selectedId);
    if (!item) return;
    const detected = ws.overlay?.objects.find(o => o.id === item.id);
    const info = ws.state.analysis?.objects.find(o => o.id === item.id);
    function line(label, value) {
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = T(label);
      dd.textContent = value;
      list.append(dt, dd);
    }
    const title = document.createElement("h2");
    title.textContent = `${item.id} · ${T("review.objectDetails")}`;
    const list = document.createElement("dl");
    line("review.objectType", T(`review.kind.${item.kind}`));
    line("review.recognizedText", detected?.ocr.map(o => o.text).join(" / ") || "—");
    if (item.kind === "text" && item.canonical?.text) line("review.savedText", item.canonical.text);
    const confidences = detected?.ocr.filter(o => Number.isFinite(o.confidence)).map(o => `${(o.confidence * 100).toFixed(1)}%`) || [];
    if (confidences.length) line("review.ocrConfidence", confidences.join(" / "));
    if (detected?.regions.length) {
      const boxes = detected.regions.map(r => r.bbox);
      const x = Math.min(...boxes.map(b => b[0])), y = Math.min(...boxes.map(b => b[1]));
      line("review.position", `${x}, ${y} px`);
      line("review.size", `${Math.max(...boxes.map(b => b[2])) - x} × ${Math.max(...boxes.map(b => b[3])) - y} px`);
    } else line("review.position", T(ws.state.analysis ? "review.notDetected" : "review.noAnalysis"));
    line("review.appearance", (info?.intervals || (!ws.state.analysis ? [item.visible] : [])).map(([a, b]) => `${a}–${b} f`).join(", ") || "—");
    dom.objectDetail.append(title, list);
    if (item.pending_asset === "3d") {
      const badge = document.createElement("span");
      badge.className = "badge badge--active";
      badge.dataset.i18n = "review.pending3d";
      badge.textContent = T(badge.dataset.i18n);
      dom.objectDetail.append(badge);
      const generate = document.createElement("button");
      generate.type = "button";
      generate.className = "btn btn--secondary";
      generate.dataset.i18n = "review.generate3d";
      generate.textContent = T(generate.dataset.i18n);
      generate.disabled = ws.versionId !== ws.state.project.versions.at(-1)?.id;
      generate.title = generate.disabled ? T("review.latestOnly") : "";
      generate.addEventListener("click", () => ws.previewReference3d(item.id));
      dom.objectDetail.append(generate);
    }
  }

  async function refreshState(v) {
    const request = ++ws.stateReq;
    const keepFrame = ws.frame, keepSel = ws.selectedId;
    ws.setPlaying(false);
    ws.invalidateFrames();
    setProgress(LOADING_PCT_START, T("review.loading"));
    try {
      const state = await fetchReviewState(ws.projectId, ws.sceneId, v);
      if (request !== ws.stateReq) return;
      ws.state = state;
      ws.versionId = state.version.id;
      replaceReviewUrl();
      restoreSelection(keepSel);
      if (!ws.selectedId) ws.selectedId = state.scene.elements[0]?.id || null;
      applySceneChrome();
      await renderElements();
      if (request !== ws.stateReq) return;
      ws.buildRuler();
      if (ws.selectedId) selectElement(ws.selectedId);
      const historical = ws.versionId !== state.project.versions.at(-1)?.id;
      dom.formsPanel.toggleAttribute("disabled", historical);
      document.getElementById("region-mode").disabled = historical || !ws.selectedId;
      document.getElementById("region-mode").title = historical ? T("review.latestOnly") : "";
      ws.updateOverlayStatus(historical && !state.analysis ? "review.noAnalysis" : "review.overlayLoading");
      await ws.setFrame(Math.max(0, Math.min(keepFrame, state.scene.frames - 1)), true);
      if (request === ws.stateReq) clearLoading();
    } catch (err) {
      if (request !== ws.stateReq) return;
      clearLoading();
      ws.setJobBanner(err.message || T("review.loadFailed"), true);
    }
  }

  async function loadState() { await refreshState(ws.versionId); }

  async function handleApproveClick() {
    if (!ws.projectId || dom.approveBtn.disabled) return;
    if (ws.state && isApproved(ws.state.status)) {
      goAgent();
      return;
    }
    dom.approveBtn.disabled = true;
    try {
      const result = await postApprove(ws.projectId, ws.sceneId, ws.versionId);
      const approvals = result.project?.approved_scenes || {};
      const next = (ws.state.project.scenes || []).find((scene) => !approvals[scene.id]);
      if (next) await switchScene(next.id);
      else goAgent();
    } catch (err) {
      paintApprove(ws.state && ws.state.status);
      ws.setJobBanner(err.message || T("review.approveFailed"), true);
    }
  }

  ws.renderObjectDetail = renderObjectDetail;
  ws.setProgress = setProgress;
  ws.clearLoading = clearLoading;
  ws.renderElements = renderElements;
  ws.selectElement = selectElement;
  ws.applyElementFilter = applyElementFilter;
  ws.goAgent = goAgent;
  ws.paintApprove = paintApprove;
  ws.refreshState = refreshState;
  ws.loadState = loadState;
  ws.handleApproveClick = handleApproveClick;
}
