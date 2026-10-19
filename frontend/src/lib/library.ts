import { T, errorText } from "@/i18n";

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

function projectHref(p) {
  if (p.status === "uploaded" || p.status === "error") return `/new?project=${encodeURIComponent(p.id)}`;
  if (p.status === "analyzing") return p.job_id
    ? `/analyze?job=${encodeURIComponent(p.id)}`
    : reviewHref(p.id, p.scene || DEFAULT_SCENE_ID);
  return null;
}

function reviewHref(projectId, sceneId) {
  return `/review?project=${encodeURIComponent(projectId)}&scene=${encodeURIComponent(sceneId)}`;
}

function agentHref(projectId, sceneId, version) {
  let url = `/agent?project=${encodeURIComponent(projectId)}&scene=${encodeURIComponent(sceneId)}`;
  if (version) url += `&v=${encodeURIComponent(version)}`;
  return url;
}

function isReviewStatus(status) {
  return status === "review";
}

function isApprovedStatus(status) {
  return status === "approved";
}

function hrefForProject(p) {
  const statusHref = projectHref(p);
  if (statusHref) return statusHref;
  const scene = p.scene || DEFAULT_SCENE_ID;
  if (isReviewStatus(p.status)) return reviewHref(p.id, scene);
  if (isApprovedStatus(p.status)) return agentHref(p.id, scene, p.version);
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
  if (p.status === "error") return T("library.failedRetry"); // the row opens the range picker; the cause is in its title
  return statusLabel(p.status);
}

/** The failure cause and its code, shown on hover. */
function badgeTitle(p) {
  return p.status === "error" && p.error ? `${errorText(p.error)} (${p.error})` : undefined;
}

function badgeClass(status) {
  return BADGE_CLASS[status] || "badge--done";
}

/** Rejected uploads have no frames to show. */
function thumbUrl(p) {
  return p.status === "rejected" ? null : `/api/projects/${encodeURIComponent(p.id)}/frame/0`;
}

function visibleProjects(projects, filter, query) {
  const q = query.trim().toLowerCase();
  return projects.filter((p) => {
    const matchesReviewFilter = filter !== "review" || p.status === "review";
    if (!matchesReviewFilter) return false;
    if (!q) return true;
    const hay = `${p.title || ""} ${p.id || ""}`.toLowerCase();
    return hay.includes(q);
  });
}

export {
  DEFAULT_SCENE_ID,
  ROW_GAP_PX,
  BADGE_CLASS,
  statusLabel,
  projectHref,
  reviewHref,
  agentHref,
  isReviewStatus,
  isApprovedStatus,
  hrefForProject,
  formatVersion,
  formatConfidence,
  badgeText,
  badgeTitle,
  badgeClass,
  thumbUrl,
  visibleProjects,
};
