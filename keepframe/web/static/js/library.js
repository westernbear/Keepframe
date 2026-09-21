import { fetchProjects } from "/static/js/api.js?v=20260921u";
import { T, Tf, applyI18n } from "/static/js/i18n.js?v=20260921u";

const DEFAULT_SCENE_ID = "s1";
const ROW_GAP_PX = 8;

const BADGE_CLASS = {
  uploaded: "badge--done",
  analyzing: "badge--active",
  review: "badge--active",
  approved: "badge--done",
  error: "badge--error",
  rejected: "badge--error",
};

function statusLabel(status) {
  const key = `status.${status}`;
  const label = T(key);
  const isMissingKey = label === key;
  return isMissingKey ? status : label;
}

function isAnalyzeQueue(status) {
  return status === "analyzing" || status === "uploaded";
}

function analyzeJobHref(projectId) {
  return `/analyze?job=${encodeURIComponent(projectId)}`;
}

function reviewHref(projectId, sceneId) {
  return `/review?project=${encodeURIComponent(projectId)}&scene=${encodeURIComponent(sceneId)}`;
}

function agentHref(projectId, sceneId, version) {
  let url = `/agent?project=${encodeURIComponent(projectId)}&scene=${encodeURIComponent(sceneId)}`;
  if (version) url += `&v=${encodeURIComponent(version)}`;
  return url;
}

function hrefForProject(p) {
  if (isAnalyzeQueue(p.status)) return analyzeJobHref(p.id);
  const scene = p.scene || DEFAULT_SCENE_ID;
  if (p.status === "review") return reviewHref(p.id, scene);
  if (p.status === "approved") return agentHref(p.id, scene, p.version);
  return null;
}

function formatVersion(v) {
  if (v == null || v === "") return null;
  const s = String(v);
  return s.startsWith("v") ? s : `v${s}`;
}

function formatConfidence(c) {
  if (c == null) return null;
  return typeof c === "number" ? c.toFixed(2) : String(c);
}

function badgeText(p) {
  if (p.status === "rejected") return p.reason || p.error || statusLabel("rejected");
  if (p.status === "error" && p.error) return p.error;
  return statusLabel(p.status);
}

function appendThumb(thumb, p) {
  const isRejected = p.status === "rejected";
  if (isRejected) return;
  const img = document.createElement("img");
  img.src = `/api/projects/${encodeURIComponent(p.id)}/frame/0`;
  img.alt = "";
  img.style.width = "100%";
  img.style.height = "100%";
  img.style.objectFit = "cover";
  img.onerror = () => { img.remove(); };
  thumb.appendChild(img);
}

function renderProject(p, index) {
  const row = document.createElement("div");
  row.className = "row";
  if (index > 0) row.style.marginTop = `${ROW_GAP_PX}px`;

  const thumb = document.createElement("div");
  thumb.className = "row__thumb";
  appendThumb(thumb, p);

  const body = document.createElement("div");
  body.className = "row__body";

  const title = document.createElement("div");
  title.className = "row__title";
  title.textContent = p.title || p.id;

  const meta = document.createElement("div");
  meta.className = "row__meta";
  const ver = formatVersion(p.version);
  if (ver) {
    const span = document.createElement("span");
    span.textContent = ver;
    meta.appendChild(span);
  }
  const conf = formatConfidence(p.confidence);
  if (conf != null) {
    const span = document.createElement("span");
    span.textContent = Tf("library.confidence", { c: conf });
    meta.appendChild(span);
  }

  const badge = document.createElement("span");
  badge.className = `badge ${BADGE_CLASS[p.status] || "badge--done"}`;
  badge.textContent = badgeText(p);

  body.append(title, meta, badge);
  row.append(thumb, body);

  const href = hrefForProject(p);
  if (href) {
    row.style.cursor = "pointer";
    row.addEventListener("click", () => { location.href = href; });
  }

  return row;
}

let cached = [];
let filter = "all";
let query = "";

function visibleProjects(projects) {
  const q = query.trim().toLowerCase();
  return projects.filter((p) => {
    const matchesReviewFilter = filter !== "review" || p.status === "review";
    if (!matchesReviewFilter) return false;
    if (!q) return true;
    const hay = `${p.title || ""} ${p.id || ""}`.toLowerCase();
    return hay.includes(q);
  });
}

function paint(projects) {
  const list = document.getElementById("project-list");
  const empty = document.getElementById("library-empty");
  cached = projects;
  const shown = visibleProjects(projects);
  list.innerHTML = "";
  if (!shown.length) {
    empty.hidden = false;
    applyI18n(empty);
    return;
  }
  empty.hidden = true;
  shown.forEach((p, i) => list.appendChild(renderProject(p, i)));
}

function bindFilters() {
  document.querySelector("[data-search]").addEventListener("input", (e) => {
    query = e.target.value || "";
    paint(cached);
  });
  document.querySelectorAll("[data-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      filter = btn.dataset.filter;
      document.querySelectorAll("[data-filter]").forEach((el) => {
        el.classList.toggle("chip--active", el === btn);
      });
      paint(cached);
    });
  });
}

async function bootLibrary() {
  const empty = document.getElementById("library-empty");
  try {
    const { projects } = await fetchProjects();
    paint(projects);
  } catch (err) {
    paint([]);
    const msg = empty.querySelector("[data-i18n='library.empty']");
    if (msg) msg.textContent = err.message || T("library.loadFailed");
  }
}

bindFilters();
bootLibrary();
window.addEventListener("keepframe:lang", () => paint(cached));
