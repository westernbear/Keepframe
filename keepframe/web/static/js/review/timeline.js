import { objectColor } from "/static/js/review/colors.js?v=20261006l";
import { T } from "/static/js/i18n.js?v=20261006l";
import {
  LIST_CHUNK,
  TIMELINE_HEIGHT_PX,
  trackGutterPx,
} from "/static/js/review/workspace.js?v=20261006l";

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

  function buildRuler() {
    dom.timelineSvg.replaceChildren();
    ws.playheadEl = null;
    const n = ws.state.scene.frames;
    for (let i = 0; i <= 5; i++) {
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", `${i * 19.6 + 1}%`);
      label.setAttribute("y", "38");
      label.setAttribute("text-anchor", i === 0 ? "start" : i === 5 ? "end" : "middle");
      label.setAttribute("fill", "var(--muted)");
      label.setAttribute("font-size", "11");
      label.textContent = `${Math.round(i / 5 * (n - 1))} f`;
      dom.timelineSvg.append(label);
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
      const intervals = ws.state.analysis?.objects.find(o => o.id === item.id)?.intervals || (!ws.state.analysis ? [item.visible] : []);
      const label = document.createElement("div");
      label.className = "track__label";
      label.textContent = `${item.id} · ${T(`review.kind.${item.kind}`)}`;
      const lane = document.createElement("div");
      lane.className = "track__lane";
      for (const [a, b] of intervals) {
        const bar = document.createElement("div");
        bar.className = "track__bar";
        Object.assign(bar.style, { left: `${a / ws.state.scene.frames * 100}%`, right: `${100 - (b + 1) / ws.state.scene.frames * 100}%`, background: objectColor(item.id), borderColor: objectColor(item.id) });
        lane.append(bar);
      }
      row.append(label, lane);
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

  ws.drawPlayhead = drawPlayhead;
  ws.buildRuler = buildRuler;
  ws.renderTracks = renderTracks;
}
