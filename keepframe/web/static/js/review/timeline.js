import {
  LIST_CHUNK,
  TIMELINE_HEIGHT_PX,
  ERROR_STRIP_HEIGHT_PX,
  MIN_L1_MAX,
  trackGutterPx,
} from "/static/js/review/workspace.js?v=20260921v";

export function attachTimeline(ws) {
  const { dom } = ws;

  function drawPlayhead() {
    const n = ws.state?.scene?.frames || 1;
    const x = ((ws.frame + 0.5) / n) * 100;
    if (!ws.playheadEl || !ws.playheadEl.isConnected) {
      ws.playheadEl = document.createElementNS("http://www.w3.org/2000/svg", "line");
      ws.playheadEl.setAttribute("class", "playhead");
      ws.playheadEl.setAttribute("y1", "0");
      ws.playheadEl.setAttribute("y2", String(TIMELINE_HEIGHT_PX));
      ws.playheadEl.setAttribute("stroke", "var(--accent)");
      ws.playheadEl.setAttribute("stroke-width", "2");
      dom.timelineSvg.appendChild(ws.playheadEl);
    }
    ws.playheadEl.setAttribute("x1", `${x}%`);
    ws.playheadEl.setAttribute("x2", `${x}%`);
    if (dom.trackPlayhead) {
      const body = dom.trackPlayhead.parentElement;
      const gutter = trackGutterPx();
      const lane = Math.max(0, (body ? body.clientWidth : 0) - gutter);
      dom.trackPlayhead.style.left = `${gutter + (x / 100) * lane}px`;
      dom.trackPlayhead.hidden = !ws.state;
    }
  }

  function buildErrorStrip() {
    dom.timelineSvg.innerHTML = "";
    ws.playheadEl = null;
    const rec = ws.state.report?.reconstruction;
    const bins = rec?.l1_bins || [];
    const hot = rec?.l1_hot || [];
    const max = rec?.l1_max || MIN_L1_MAX;
    ws.errorPeaks = rec?.l1_peaks || [];
    if (bins.length) {
      const frag = document.createDocumentFragment();
      for (let b = 0; b < bins.length; b++) {
        const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        const height = (bins[b] / max) * ERROR_STRIP_HEIGHT_PX;
        rect.setAttribute("x", `${(b / bins.length) * 100}%`);
        rect.setAttribute("y", `${TIMELINE_HEIGHT_PX - height}`);
        rect.setAttribute("width", `${100 / bins.length}%`);
        rect.setAttribute("height", `${height}`);
        rect.setAttribute("fill", hot[b] ? "var(--error)" : "var(--muted)");
        frag.appendChild(rect);
      }
      dom.timelineSvg.appendChild(frag);
    }
    drawPlayhead();
  }

  function renderTracks(from = 0) {
    const els = ws.state.scene.elements || [];
    if (from === 0) dom.timelineTracks.innerHTML = "";
    const end = Math.min(els.length, from + LIST_CHUNK);
    const frag = document.createDocumentFragment();
    for (let i = from; i < end; i++) {
      const item = els[i];
      const row = document.createElement("div");
      row.className = "track" + (item.id === ws.selectedId ? " track--selected" : "");
      row.dataset.id = item.id;
      const vis = item.visible || [0, ws.state.scene.frames - 1];
      const left = (vis[0] / ws.state.scene.frames) * 100;
      const right = 100 - ((vis[1] + 1) / ws.state.scene.frames) * 100;
      row.innerHTML = `<div class="track__label"><span class="mono track__id">${item.id}</span><span>${item.kind}</span></div>
      <div class="track__lane"><div class="track__bar" style="left:${left}%;right:${right}%"></div></div>`;
      row.addEventListener("click", (ev) => {
        const clickedLane = ev.target.closest(".track__lane");
        if (clickedLane) {
          const lane = row.querySelector(".track__lane");
          const r = lane.getBoundingClientRect();
          const x = (ev.clientX - r.left) / r.width;
          ws.setFrame(Math.floor(x * ws.state.scene.frames), true);
        }
        ws.selectElement(item.id);
      });
      frag.appendChild(row);
    }
    dom.timelineTracks.appendChild(frag);
  }

  function seekPrevErrorPeak() {
    if (!ws.errorPeaks.length) return;
    const prev = [...ws.errorPeaks].reverse().find((p) => p < ws.frame);
    ws.setFrame(prev != null ? prev : ws.errorPeaks[ws.errorPeaks.length - 1]);
  }

  function seekNextErrorPeak() {
    if (!ws.errorPeaks.length) return;
    const idx = ws.errorPeaks.findIndex((p) => p >= ws.frame);
    ws.setFrame(idx >= 0 ? ws.errorPeaks[idx] : ws.errorPeaks[0]);
  }

  function nextErrorPeak(backward = false) {
    if (backward) seekPrevErrorPeak();
    else seekNextErrorPeak();
  }

  ws.drawPlayhead = drawPlayhead;
  ws.buildErrorStrip = buildErrorStrip;
  ws.renderTracks = renderTracks;
  ws.seekPrevErrorPeak = seekPrevErrorPeak;
  ws.seekNextErrorPeak = seekNextErrorPeak;
  ws.nextErrorPeak = nextErrorPeak;
}
