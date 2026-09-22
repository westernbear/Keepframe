import {
  fetchReviewState,
  postApprove,
  reviewAssetUrl,
} from "/static/js/api.js?v=20260921v";
import { T, Tf } from "/static/js/i18n.js?v=20260921v";
import {
  LOADING_PCT_START,
  LOADING_PCT_STATE,
  LOADING_PCT_FRAMES,
  LOADING_PCT_LIST_BASE,
  LOADING_PCT_LIST_SPAN,
  PROGRESS_MAX,
  LIST_CHUNK,
  CONSTRAINT_STEP,
  yieldMain,
} from "/static/js/review/workspace.js?v=20260921v";

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
    const row = document.createElement("div");
    row.className = "element-row" + (item.id === ws.selectedId ? " element-row--selected" : "");
    row.dataset.id = item.id;
    const tex = item.canonical && item.canonical.texture;
    const thumb = tex
      ? `<img class="element-thumb" alt="" src="${reviewAssetUrl(tex, ws.projectId, ws.sceneId)}"/>`
      : `<span class="element-thumb"></span>`;
    row.innerHTML = `${thumb}<div class="element-row__meta"><span class="mono">${item.id}</span><span>${item.kind}</span></div>`;
    row.addEventListener("click", () => selectElement(item.id));
    frag.appendChild(row);
    const opt = document.createElement("option");
    opt.value = item.id;
    opt.textContent = item.id;
    opts.appendChild(opt);
  }

  async function renderElements() {
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
    const constraints = ws.state.scene.constraints || [];
    if (!constraints.length) return;
    const head = document.createElement("div");
    head.className = "keep-head";
    head.textContent = T("review.keep");
    dom.constraintsPanel.appendChild(head);
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

  function selectElement(id) {
    ws.selectedId = id;
    document.getElementById("reassign-from").value = id;
    document.getElementById("bbox-frame").value = String(ws.frame);
    document.getElementById("mask-frame").value = String(ws.frame);
    const item = ws.state.scene.elements.find((e) => e.id === id);
    if (item?.canonical?.text != null) {
      document.getElementById("text-value").value = item.canonical.text;
    }
    dom.elementList.querySelectorAll(".element-row").forEach((row) => {
      row.classList.toggle("element-row--selected", row.dataset.id === id);
    });
    dom.timelineTracks.querySelectorAll(".track").forEach((row) => {
      row.classList.toggle("track--selected", row.dataset.id === id);
    });
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
    dom.frameTotal.textContent = String(ws.state.scene.frames);
    fillVersions();
    paintApprove(ws.state.status);
  }

  function restoreSelection(keepSel) {
    const stillThere = keepSel && ws.state.scene.elements.some((e) => e.id === keepSel);
    if (stillThere) ws.selectedId = keepSel;
    else if (!ws.state.scene.elements.length) ws.selectedId = null;
  }

  async function refreshState(v) {
    const keepFrame = ws.frame;
    const keepSel = ws.selectedId;
    ws.versionId = v;
    setProgress(LOADING_PCT_START, T("review.loading"));
    try {
      ws.state = await fetchReviewState(ws.projectId, ws.sceneId, ws.versionId);
    } catch (err) {
      clearLoading();
      ws.setJobBanner(err.message || T("review.loadFailed"), true);
      return;
    }
    ws.versionId = ws.state.version.id;
    replaceReviewUrl();
    restoreSelection(keepSel);
    applySceneChrome();
    await renderElements();
    ws.buildErrorStrip();
    if (ws.selectedId) selectElement(ws.selectedId);
    ws.frame = Math.max(0, Math.min(keepFrame, ws.state.scene.frames - 1));
    ws.shownFrame = -1;
    ws.setFrame(ws.frame, true);
    clearLoading();
  }

  function selectFirstElementIfNeeded() {
    if (!ws.selectedId && ws.state.scene.elements.length) selectElement(ws.state.scene.elements[0].id);
  }

  async function loadState() {
    setProgress(LOADING_PCT_START, T("review.loading"));
    dom.orig.src = ws.previews.src("orig", 0);
    const origReady = ws.waitImg(dom.orig);
    ws.state = await fetchReviewState(ws.projectId, ws.sceneId, ws.versionId);
    await yieldMain();
    setProgress(LOADING_PCT_STATE, T("review.loading"));
    ws.versionId = ws.state.version.id;
    applySceneChrome();
    dom.reconLoading.hidden = false;
    dom.recon.src = ws.previews.src("recon", 0);
    await renderElements();
    ws.buildErrorStrip();
    selectFirstElementIfNeeded();
    ws.shownFrame = 0;
    ws.frame = 0;
    setProgress(LOADING_PCT_FRAMES, T("review.loadingFrames"));
    await origReady;
    ws.loadBboxes();
    clearLoading();
  }

  async function handleApproveClick() {
    if (!ws.projectId || dom.approveBtn.disabled) return;
    if (ws.state && isApproved(ws.state.status)) {
      goAgent();
      return;
    }
    dom.approveBtn.disabled = true;
    try {
      await postApprove(ws.projectId, ws.sceneId, ws.versionId);
      goAgent();
    } catch (err) {
      paintApprove(ws.state && ws.state.status);
      ws.setJobBanner(err.message || T("review.approveFailed"), true);
    }
  }

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
