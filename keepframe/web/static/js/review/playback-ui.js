import { fetchBboxes } from "/static/js/api.js?v=20260921v";
import { T } from "/static/js/i18n.js?v=20260921v";
import {
  createPreviewCache,
  createFrameTransport,
  isImageDecoded,
  seekDelayMs,
  isSeekQueuedWhilePlaying,
  frameStep,
} from "/static/js/playback.js?v=20260921v";
import { MIN_BBOX_EDGE } from "/static/js/review/workspace.js?v=20260921v";

export function attachPlayback(ws) {
  const { dom } = ws;

  function waitImg(img) {
    if (isImageDecoded(img)) return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => {
        img.removeEventListener("load", done);
        img.removeEventListener("error", done);
        resolve();
      };
      img.addEventListener("load", done);
      img.addEventListener("error", done);
    });
  }

  function isFrameAlreadyOnScreen(index) {
    return ws.shownFrame === index && isImageDecoded(dom.orig);
  }

  function formatTime(f, fps) {
    const sec = f / fps;
    const mm = String(Math.floor(sec / 60)).padStart(2, "0");
    const ss = String(Math.floor(sec % 60)).padStart(2, "0");
    const ff = String(Math.floor(f % fps)).padStart(2, "0");
    return `${mm}:${ss}:${ff}`;
  }

  function paintPlayhead() {
    if (!ws.state) return;
    const n = ws.state.scene.frames;
    dom.frameNum.textContent = String(ws.frame);
    dom.frameTime.textContent = formatTime(ws.frame, ws.state.scene.fps);
    dom.timelineSvg.setAttribute("aria-valuenow", String(ws.frame));
    dom.timelineSvg.setAttribute("aria-valuemax", String(n - 1));
    ws.drawPlayhead();
  }

  function applyReadyFrame(f) {
    dom.orig.src = ws.previews.src("orig", f);
    dom.recon.src = ws.previews.src("recon", f);
    ws.shownFrame = f;
    dom.reconLoading.hidden = true;
    dom.reconError.hidden = true;
    loadBboxes();
    ws.previews.prefetch(f + 1, ws.state.scene.frames);
  }

  function showNextPlaybackFrame(next) {
    paintPlayhead();
    applyReadyFrame(next);
  }

  function syncPlayButton(playing) {
    dom.playBtn.setAttribute("aria-label", playing ? T("review.pause") : T("review.play"));
    dom.playBtn.classList.toggle("is-playing", playing);
    dom.playIcon.hidden = playing;
    dom.pauseIcon.hidden = !playing;
  }

  function setPlaying(on) {
    ws.transport.setPlaying(on);
  }

  async function loadFrameImages() {
    ws.imgTimer = 0;
    if (!ws.state) return;
    const target = ws.frame;
    if (isFrameAlreadyOnScreen(target)) return;
    ws.previews.prefetch(target, ws.state.scene.frames);
    if (!ws.previews.isReady(target)) {
      dom.reconLoading.hidden = false;
      dom.reconError.hidden = true;
    }
    const ok = await ws.previews.wait(target);
    if (ws.frame !== target) return;
    if (ok) applyReadyFrame(target);
    else {
      dom.reconLoading.hidden = true;
      dom.reconError.hidden = false;
    }
  }

  function clampFrame(f) {
    return Math.max(0, Math.min(ws.state.scene.frames - 1, f));
  }

  function scheduleFrameLoad(immediate) {
    if (isFrameAlreadyOnScreen(ws.frame)) return;
    if (immediate) {
      clearTimeout(ws.imgTimer);
      ws.imgTimer = 0;
      loadFrameImages();
      return;
    }
    if (isSeekQueuedWhilePlaying(ws.transport.isPlaying(), ws.imgTimer)) return;
    clearTimeout(ws.imgTimer);
    ws.imgTimer = setTimeout(loadFrameImages, seekDelayMs(ws.transport.isPlaying()));
  }

  function setFrame(f, immediate = false) {
    if (!ws.state) return;
    ws.frame = clampFrame(f);
    paintPlayhead();
    scheduleFrameLoad(immediate);
  }

  function mapBox(box, img, sceneW, sceneH) {
    const pane = img.parentElement.getBoundingClientRect();
    const ir = img.getBoundingClientRect();
    const scaleX = ir.width / Math.max(1, sceneW);
    const scaleY = ir.height / Math.max(1, sceneH);
    const ox = ir.left - pane.left;
    const oy = ir.top - pane.top;
    const [x0, y0, x1, y1] = box;
    return { x: ox + x0 * scaleX, y: oy + y0 * scaleY, w: (x1 - x0) * scaleX, h: (y1 - y0) * scaleY };
  }

  function overlayToScene(clientX, clientY, img, sceneW, sceneH) {
    const ir = img.getBoundingClientRect();
    const x = ((clientX - ir.left) / Math.max(1, ir.width)) * sceneW;
    const y = ((clientY - ir.top) / Math.max(1, ir.height)) * sceneH;
    return [x, y];
  }

  function paintOverlay(svg, img, box, draft = false) {
    const pane = img.parentElement;
    const pr = pane.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${pr.width} ${pr.height}`);
    svg.replaceChildren();
    if (!box || !ws.state) return;
    const [sw, sh] = ws.state.scene.size;
    const m = mapBox(box, img, sw, sh);
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("class", draft ? "overlay-box overlay-box--draft" : "overlay-box");
    rect.setAttribute("x", String(m.x));
    rect.setAttribute("y", String(m.y));
    rect.setAttribute("width", String(Math.max(1, m.w)));
    rect.setAttribute("height", String(Math.max(1, m.h)));
    svg.appendChild(rect);
  }

  function drawOverlays() {
    const box = ws.selectedId ? ws.boxes[ws.selectedId] : null;
    paintOverlay(dom.origOverlay, dom.orig, box);
    paintOverlay(dom.reconOverlay, dom.recon, box);
  }

  async function loadBboxes() {
    if (!ws.state || !ws.projectId) return;
    const id = ++ws.bboxReq;
    const f = ws.frame;
    try {
      const data = await fetchBboxes(ws.projectId, ws.sceneId, f, ws.versionId);
      if (id !== ws.bboxReq || f !== ws.frame) return;
      ws.boxes = data.boxes || {};
      drawOverlays();
    } catch {
      if (id === ws.bboxReq) ws.boxes = {};
    }
  }

  function sceneBoxFromDrag(d) {
    return [Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.max(d.x0, d.x1), Math.max(d.y0, d.y1)];
  }

  function isTinyBox(box) {
    return box[2] - box[0] < MIN_BBOX_EDGE || box[3] - box[1] < MIN_BBOX_EDGE;
  }

  function isTypingInField(target) {
    return target.matches("input,select,textarea");
  }

  function handleTransportKey(e) {
    if (!ws.state || isTypingInField(e.target)) return;
    if (e.code === "Space") {
      e.preventDefault();
      dom.playBtn.click();
      return;
    }
    if (e.code === "ArrowLeft") {
      setFrame(ws.frame - frameStep(e.shiftKey));
      return;
    }
    if (e.code === "ArrowRight") {
      setFrame(ws.frame + frameStep(e.shiftKey));
      return;
    }
    if (e.key === "[") {
      ws.seekPrevErrorPeak();
      return;
    }
    if (e.key === "]") ws.seekNextErrorPeak();
  }

  function bindTransport() {
    dom.playBtn.addEventListener("click", () => {
      setPlaying(!ws.transport.isPlaying());
    });
    document.addEventListener("keydown", handleTransportKey);
    dom.timelineSvg.addEventListener("click", (e) => {
      const rect = dom.timelineSvg.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width;
      setFrame(Math.floor(x * ws.state.scene.frames), true);
    });
    document.getElementById("step-back").addEventListener("click", () => setFrame(ws.frame - 1, true));
    document.getElementById("step-fwd").addEventListener("click", () => setFrame(ws.frame + 1, true));
    document.getElementById("peak-back").addEventListener("click", () => ws.seekPrevErrorPeak());
    document.getElementById("peak-fwd").addEventListener("click", () => ws.seekNextErrorPeak());
    document.getElementById("recon-retry").addEventListener("click", () => setFrame(ws.frame, true));
  }

  function openBboxAccordion(box) {
    document.getElementById("bbox-coords").value = box.map((v) => Math.round(v)).join(",");
    document.getElementById("bbox-frame").value = String(ws.frame);
    document.querySelectorAll(".accordion").forEach((a) => a.classList.remove("accordion--open"));
    document.getElementById("acc-bbox").classList.add("accordion--open");
    paintOverlay(dom.origOverlay, dom.orig, box);
  }

  function bindDraw() {
    dom.origDraw.addEventListener("pointerdown", (e) => {
      if (!ws.state) return;
      dom.origDraw.setPointerCapture(e.pointerId);
      const [sw, sh] = ws.state.scene.size;
      const [x, y] = overlayToScene(e.clientX, e.clientY, dom.orig, sw, sh);
      ws.drag = { x0: x, y0: y, x1: x, y1: y };
    });
    dom.origDraw.addEventListener("pointermove", (e) => {
      if (!ws.drag || !ws.state) return;
      const [sw, sh] = ws.state.scene.size;
      const [x, y] = overlayToScene(e.clientX, e.clientY, dom.orig, sw, sh);
      ws.drag.x1 = x;
      ws.drag.y1 = y;
      paintOverlay(dom.origOverlay, dom.orig, sceneBoxFromDrag(ws.drag), true);
    });
    dom.origDraw.addEventListener("pointerup", () => {
      if (!ws.drag || !ws.state) return;
      const box = sceneBoxFromDrag(ws.drag);
      ws.drag = null;
      if (isTinyBox(box)) {
        drawOverlays();
        return;
      }
      openBboxAccordion(box);
    });
    dom.orig.addEventListener("load", () => drawOverlays());
  }

  ws.previews = createPreviewCache({
    project: ws.projectId,
    scene: ws.sceneId,
    version: () => ws.versionId,
  });
  ws.transport = createFrameTransport({
    getFrame: () => ws.frame,
    setFrameIndex: (next) => { ws.frame = next; },
    getFps: () => ws.state.scene.fps,
    getFrameCount: () => ws.state.scene.frames,
    prefetch: (from, total) => ws.previews.prefetch(from, total),
    isReady: (index) => ws.previews.isReady(index),
    wait: (index) => ws.previews.wait(index),
    showFrame: showNextPlaybackFrame,
    canPlay: () => Boolean(ws.state),
    onPlayingChange: syncPlayButton,
    onFrameUnavailable: () => { dom.reconError.hidden = false; },
  });

  ws.waitImg = waitImg;
  ws.paintPlayhead = paintPlayhead;
  ws.applyReadyFrame = applyReadyFrame;
  ws.syncPlayButton = syncPlayButton;
  ws.setPlaying = setPlaying;
  ws.setFrame = setFrame;
  ws.mapBox = mapBox;
  ws.drawOverlays = drawOverlays;
  ws.loadBboxes = loadBboxes;
  ws.bindTransport = bindTransport;
  ws.bindDraw = bindDraw;
}
