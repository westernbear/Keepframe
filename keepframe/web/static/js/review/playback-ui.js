import { fetchAnalysisOverlay } from "/static/js/api.js?v=20261009a";
import { T } from "/static/js/i18n.js?v=20261009a";
import { createPreviewCache, createFrameTransport, seekDelayMs, frameStep } from "/static/js/playback.js?v=20261009a";
import { MIN_BBOX_EDGE } from "/static/js/review/workspace.js?v=20261009a";
import { objectColor } from "/static/js/review/colors.js?v=20261009a";

const NS = "http://www.w3.org/2000/svg";
export function attachPlayback(ws) {
  const { dom } = ws;
  let frameRequest = 0;
  let overlayRequest = 0;
  let overlayController;
  let statusKey = "review.overlayLoading";

  function updateOverlayStatus(key = statusKey) {
    statusKey = key;
    dom.overlayStatus.textContent = T(ws.overlayEnabled ? key : "review.layerHidden");
  }
  function clearOverlay() {
    ++overlayRequest;
    overlayController?.abort();
    ws.overlay = null;
    dom.origOverlay.replaceChildren();
    delete dom.origOverlay.dataset.frame;
    delete dom.origOverlay.dataset.version;
    ws.renderObjectDetail?.();
  }
  function invalidateFrames() {
    ++frameRequest;
    clearTimeout(ws.imgTimer);
    clearOverlay();
    ws.shownFrame = -1;
    ws.drag = null;
    ws.setRegionMode(false);
  }
  function paintPlayhead() {
    if (!ws.state) return;
    const fps = ws.state.scene.fps;
    const sec = Math.floor(ws.frame / fps);
    dom.frameNum.textContent = String(ws.frame);
    dom.frameTime.textContent = [Math.floor(sec / 60), sec % 60, Math.floor(ws.frame % fps)].map(v => String(v).padStart(2, "0")).join(":");
    dom.timelineSvg.setAttribute("aria-valuenow", ws.frame);
    dom.timelineSvg.setAttribute("aria-valuemax", ws.state.scene.frames - 1);
    ws.drawPlayhead();
  }
  function frameMatches(f, v, request) {
    return request === frameRequest && ws.frame === f && ws.versionId === v;
  }
  async function loadOverlay(f, v) {
    clearOverlay();
    if (!ws.overlayEnabled) return updateOverlayStatus();
    if (!ws.state.analysis) return updateOverlayStatus("review.noAnalysis");
    const request = overlayRequest;
    overlayController = new AbortController();
    const controller = overlayController;
    const timeout = setTimeout(() => controller.abort(), 5000);
    updateOverlayStatus("review.overlayLoading");
    try {
      const data = await fetchAnalysisOverlay(ws.projectId, ws.sceneId, f, v, controller.signal);
      if (request !== overlayRequest || ws.frame !== f || ws.shownFrame !== f || ws.versionId !== v) return;
      if (data.frame !== f || data.version !== v || data.scene !== ws.sceneId || data.snapshot !== ws.state.analysis.snapshot) throw new Error("overlay mismatch");
      ws.overlay = data;
      updateOverlayStatus(!data.available ? "review.noAnalysis" : data.objects.length ? "review.selectRegion" : "review.noDetections");
      drawOverlays();
      ws.renderObjectDetail();
    } catch {
      if (request !== overlayRequest) return;
      ws.overlay = null;
      drawOverlays();
      ws.renderObjectDetail();
      updateOverlayStatus("review.overlayFailed");
    } finally { clearTimeout(timeout); }
  }
  function applyReadyFrame(f) {
    clearOverlay();
    const img = ws.previews.image("orig", f);
    img.id = "orig";
    img.alt = T("review.orig");
    img.dataset.frame = f;
    img.dataset.version = ws.versionId;
    if (dom.orig !== img) dom.orig.replaceWith(img);
    dom.orig = img;
    ws.shownFrame = f;
    dom.frameLoading.hidden = true;
    dom.frameError.hidden = true;
    loadOverlay(f, ws.versionId);
    ws.previews.prefetch(f + 1, ws.state.scene.frames);
  }
  async function loadFrameImages() {
    ws.imgTimer = 0;
    if (!ws.state) return;
    const f = ws.frame, v = ws.versionId, request = ++frameRequest;
    ws.previews.prefetch(f, ws.state.scene.frames);
    dom.frameLoading.hidden = ws.previews.isReady(f);
    dom.frameError.hidden = true;
    const ok = await ws.previews.wait(f);
    if (!frameMatches(f, v, request)) return;
    if (ok) applyReadyFrame(f);
    else {
      dom.frameLoading.hidden = true;
      dom.frameError.hidden = false;
      clearOverlay();
    }
  }
  function setFrame(f, immediate = false) {
    if (!ws.state) return;
    setPlaying(false);
    ++frameRequest;
    clearOverlay();
    ws.drag = null;
    ws.frame = Math.max(0, Math.min(ws.state.scene.frames - 1, Math.round(f)));
    paintPlayhead();
    clearTimeout(ws.imgTimer);
    if (immediate) return loadFrameImages();
    ws.imgTimer = setTimeout(loadFrameImages, seekDelayMs(false));
  }
  function syncPlayButton(playing) {
    dom.playBtn.setAttribute("aria-label", playing ? T("review.pause") : T("review.play"));
    dom.playBtn.classList.toggle("is-playing", playing);
    dom.playIcon.hidden = playing;
    dom.pauseIcon.hidden = !playing;
  }
  function setPlaying(on) {
    if (on) { clearTimeout(ws.imgTimer); ++frameRequest; setRegionMode(false); }
    ws.transport.setPlaying(on);
  }
  function imageRect() {
    const rect = dom.orig.getBoundingClientRect();
    const [sw, sh] = ws.state.scene.size;
    const scale = Math.min(rect.width / sw, rect.height / sh);
    return { left: rect.left + (rect.width - sw * scale) / 2, top: rect.top + (rect.height - sh * scale) / 2, width: sw * scale, height: sh * scale };
  }
  function svgNode(tag, attrs) {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  }
  function positionOverlay() {
    const r = imageRect(), pane = dom.orig.parentElement.getBoundingClientRect();
    Object.assign(dom.origOverlay.style, { left: `${r.left - pane.left}px`, top: `${r.top - pane.top}px`, width: `${r.width}px`, height: `${r.height}px` });
    dom.origOverlay.setAttribute("viewBox", `0 0 ${ws.state.scene.size.join(" ")}`);
    return r;
  }
  function drawOverlays() {
    dom.origOverlay.replaceChildren();
    if (!ws.state || ws.shownFrame !== ws.frame) return;
    const r = positionOverlay();
    if (ws.overlayEnabled && ws.overlay?.version === ws.versionId && ws.overlay.frame === ws.shownFrame) {
      dom.origOverlay.dataset.frame = ws.overlay.frame;
      dom.origOverlay.dataset.version = ws.overlay.version;
      const placedLabels = [];
      for (const obj of ws.overlay.objects) {
        const group = svgNode("g", { "data-object-id": obj.id, role: "button", tabindex: ws.editRegion ? -1 : 0, "aria-label": `${obj.id} ${T(`review.kind.${obj.kind}`)} ${obj.ocr.map(o => o.text).join(" ")}`, "aria-pressed": obj.id === ws.selectedId });
        for (const region of obj.regions) {
          const d = region.rings.map(ring => ring.points.map(([x, y], i) => `${i ? "L" : "M"}${x},${y}`).join(" ") + " Z").join(" ");
          group.append(svgNode("path", { d, fill: objectColor(obj.id), "fill-opacity": ws.overlayOpacity, "fill-rule": "evenodd", stroke: objectColor(obj.id), "stroke-width": obj.id === ws.selectedId ? 3 : 1, "vector-effect": "non-scaling-stroke", class: "analysis-region" }));
        }
        const [x, y] = obj.regions[0].bbox;
        const fontSize = Math.max(10, 12 * ws.state.scene.size[0] / Math.max(1, r.width));
        const label = `${obj.id} · ${T(`review.kind.${obj.kind}`)}${obj.ocr.length ? ` · ${obj.ocr.map(o => o.text).join(" ")}` : ""}`;
        const maxChars = Math.max(12, Math.floor(ws.state.scene.size[0] / (fontSize * 0.7)));
        const caption = label.length > maxChars ? `${label.slice(0, maxChars - 1)}…` : label;
        const labelX = Math.max(2, Math.min(x, ws.state.scene.size[0] - caption.length * fontSize * 0.7));
        let labelY = Math.max(fontSize, y - fontSize / 3);
        const labelWidth = caption.length * fontSize * 0.7;
        for (let attempt = 0; attempt < placedLabels.length; attempt++) {
          if (!placedLabels.some(p => Math.abs(p.y - labelY) < fontSize * 1.2 && labelX < p.x + p.width && labelX + labelWidth > p.x)) break;
          labelY = labelY > fontSize * 2.4 ? labelY - fontSize * 1.3 : labelY + fontSize * 1.3;
        }
        placedLabels.push({ x: labelX, y: labelY, width: labelWidth });
        const text = svgNode("text", { x: labelX, y: labelY, fill: objectColor(obj.id), "font-size": fontSize, class: "analysis-label" });
        text.textContent = caption;
        group.append(text);
        const select = () => { if (!ws.editRegion) ws.selectElement(obj.id); };
        group.addEventListener("click", select);
        group.addEventListener("keydown", e => { if (["Enter", " "].includes(e.key)) { e.preventDefault(); e.stopPropagation(); select(); } });
        dom.origOverlay.append(group);
      }
    }
    if (ws.drag) {
      const [x, y, x1, y1] = dragBox();
      dom.origOverlay.append(svgNode("rect", { x, y, width: x1 - x, height: y1 - y, class: "overlay-box overlay-box--draft", "vector-effect": "non-scaling-stroke" }));
    }
  }
  function setRegionMode(on) {
    ws.editRegion = Boolean(on && ws.selectedId && ws.state && ws.state.version.id === ws.state.project.versions.at(-1)?.id);
    if (ws.editRegion) setPlaying(false);
    ws.drag = null;
    dom.origDraw.hidden = !ws.editRegion;
    document.getElementById("region-mode").setAttribute("aria-pressed", ws.editRegion);
    if (ws.editRegion) updateOverlayStatus("review.drawHint");
    else updateOverlayStatus(ws.overlay?.objects.length ? "review.selectRegion" : ws.state?.analysis ? "review.noDetections" : "review.noAnalysis");
    drawOverlays();
  }
  function scenePoint(e) {
    const r = imageRect(), [w, h] = ws.state.scene.size;
    return [Math.round(Math.max(0, Math.min(w, (e.clientX - r.left) / r.width * w))), Math.round(Math.max(0, Math.min(h, (e.clientY - r.top) / r.height * h)))];
  }
  function dragBox() {
    const d = ws.drag;
    return [Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.max(d.x0, d.x1), Math.max(d.y0, d.y1)];
  }
  function bindDraw() {
    dom.origDraw.addEventListener("pointerdown", e => {
      if (!ws.editRegion || ws.shownFrame !== ws.frame || e.button !== 0) return;
      e.preventDefault();
      dom.origDraw.setPointerCapture(e.pointerId);
      const [x, y] = scenePoint(e);
      ws.drag = { x0: x, y0: y, x1: x, y1: y };
    });
    dom.origDraw.addEventListener("pointermove", e => {
      if (!ws.drag) return;
      [ws.drag.x1, ws.drag.y1] = scenePoint(e);
      drawOverlays();
    });
    dom.origDraw.addEventListener("pointerup", e => {
      if (!ws.drag) return;
      [ws.drag.x1, ws.drag.y1] = scenePoint(e);
      const box = dragBox();
      ws.drag = null;
      if (box[2] - box[0] >= MIN_BBOX_EDGE && box[3] - box[1] >= MIN_BBOX_EDGE) {
        document.getElementById("bbox-coords").value = box.join(",");
        document.getElementById("bbox-frame").value = ws.shownFrame;
        document.querySelectorAll(".accordion").forEach(a => a.classList.remove("accordion--open"));
        document.getElementById("acc-bbox").classList.add("accordion--open");
      }
      drawOverlays();
    });
    dom.origDraw.addEventListener("pointercancel", () => { ws.drag = null; drawOverlays(); });
    document.getElementById("region-mode").addEventListener("click", () => setRegionMode(!ws.editRegion));
    document.getElementById("overlay-toggle").addEventListener("change", e => {
      ws.overlayEnabled = e.target.checked;
      if (ws.overlayEnabled && ws.shownFrame === ws.frame) loadOverlay(ws.frame, ws.versionId);
      else { clearOverlay(); updateOverlayStatus(); }
    });
    document.getElementById("overlay-opacity").addEventListener("input", e => {
      ws.overlayOpacity = Number(e.target.value) / 100;
      document.getElementById("opacity-value").value = `${e.target.value}%`;
      drawOverlays();
    });
    new ResizeObserver(() => { if (ws.state) drawOverlays(); }).observe(dom.orig.parentElement);
  }
  function bindTransport() {
    dom.playBtn.addEventListener("click", () => setPlaying(!ws.transport.isPlaying()));
    document.addEventListener("keydown", e => {
      if (!ws.state || e.target.matches("input,select,textarea,button,[role=button]")) return;
      if (e.code === "Space") { e.preventDefault(); dom.playBtn.click(); }
      if (["ArrowLeft", "ArrowRight"].includes(e.code)) { e.preventDefault(); setFrame(ws.frame + (e.code === "ArrowLeft" ? -1 : 1) * frameStep(e.shiftKey)); }
      if (e.code === "Escape") setRegionMode(false);
    });
    dom.timelineSvg.addEventListener("click", e => {
      if (!ws.state) return;
      const r = dom.timelineSvg.getBoundingClientRect();
      setFrame(Math.floor((e.clientX - r.left) / r.width * ws.state.scene.frames), true);
    });
    document.getElementById("step-back").addEventListener("click", () => setFrame(ws.frame - 1, true));
    document.getElementById("step-fwd").addEventListener("click", () => setFrame(ws.frame + 1, true));
    document.getElementById("frame-retry").addEventListener("click", () => { ws.previews.clear(); setFrame(ws.frame, true); });
  }
  ws.previews = createPreviewCache({ project: ws.projectId, scene: () => ws.sceneId, version: () => ws.versionId, kinds: ["orig"] });
  ws.transport = createFrameTransport({
    getFrame: () => ws.frame, setFrameIndex: next => { ws.frame = next; },
    getFps: () => ws.state.scene.fps, getFrameCount: () => ws.state.scene.frames,
    prefetch: (from, total) => ws.previews.prefetch(from, total), isReady: index => ws.previews.isReady(index), wait: index => ws.previews.wait(index),
    showFrame: next => { paintPlayhead(); applyReadyFrame(next); }, canPlay: () => Boolean(ws.state), onPlayingChange: syncPlayButton,
    onFrameUnavailable: () => { clearOverlay(); dom.frameError.hidden = false; },
  });
  Object.assign(ws, { paintPlayhead, applyReadyFrame, syncPlayButton, setPlaying, setFrame, drawOverlays, bindTransport, bindDraw, invalidateFrames, setRegionMode, updateOverlayStatus });
}
