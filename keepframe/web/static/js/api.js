const ADMIN_LOGIN_PATH = "/admin/login";

function jsonHeaders(extra) {
  return { "Content-Type": "application/json", ...(extra || {}) };
}

function withQuery(url, params) {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value == null || value === "") return;
    q.set(key, String(value));
  });
  const encoded = q.toString();
  return encoded ? `${url}?${encoded}` : url;
}

async function readJsonOrNull(res) {
  const contentType = res.headers.get("content-type") || "";
  const isJson = contentType.includes("json");
  if (!isJson) return null;
  return res.json().catch(() => ({}));
}

async function api(path, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const res = await fetch(path, {
    headers: isForm ? { ...(opts.headers || {}) } : jsonHeaders(opts.headers),
    ...opts,
  });
  const data = await readJsonOrNull(res);
  if (!res.ok) {
    const err = new Error((data && data.error) || res.statusText);
    err.status = res.status;
    err.body = data;
    throw err;
  }
  return data;
}

async function fetchAdminOrRedirect(path, opts = {}) {
  try {
    return await api(path, opts);
  } catch (err) {
    const isUnauthorized = err.status === 401;
    if (isUnauthorized) location.assign(ADMIN_LOGIN_PATH);
    throw err;
  }
}

async function fetchProjects() {
  return api("/api/projects");
}

async function fetchProject(projectId) {
  return api(`/api/projects/${encodeURIComponent(projectId)}`);
}

async function fetchStatus() {
  return api("/api/status");
}

async function fetchJob(jobId) {
  return api(`/api/jobs/${jobId}`);
}

async function postAnalyze(body) {
  return api("/api/analyze", { method: "POST", body: JSON.stringify(body) });
}

async function postAdminLogin({ email, password }) {
  return api("/admin/api/login", { method: "POST", body: JSON.stringify({ email, password }) });
}

async function uploadProject({ title, video, mode, start, end }) {
  const fd = new FormData();
  fd.append("title", title);
  fd.append("video", video);
  fd.append("mode", mode);
  fd.append("start", String(start));
  fd.append("end", String(end));
  return api("/api/projects", { method: "POST", body: fd });
}

async function fetchEstimate(body) {
  return api("/api/estimate", { method: "POST", body: JSON.stringify(body) });
}

const DEFAULT_FILMSTRIP_COUNT = 8;

async function fetchFilmstrip(projectId, n = DEFAULT_FILMSTRIP_COUNT) {
  return api(`/api/projects/${projectId}/filmstrip?n=${n}`);
}

async function fetchReviewState(project, scene, v) {
  return api(withQuery("/api/state", { project, scene, v }));
}

async function fetchReviewJob(project, scene) {
  return api(withQuery("/api/job", { project, scene }));
}

async function fetchBboxes(project, scene, frame, v) {
  return api(withQuery("/api/bboxes", { project, scene, frame, v }));
}

async function postApprove(project, scene, v) {
  const body = { project, scene };
  if (v) body.v = v;
  return api("/api/approve", { method: "POST", body: JSON.stringify(body) });
}

async function postKeep(project, scene, changes, note) {
  return api("/api/keep", {
    method: "POST",
    body: JSON.stringify({ project, scene, changes, note }),
  });
}

async function postEdit(body) {
  return api("/api/edit", { method: "POST", body: JSON.stringify(body) });
}

async function postAgent(body) {
  return api("/api/agent", { method: "POST", body: JSON.stringify(body) });
}

async function fetchAgentHistory(project, scene, before, limit = 50) {
  return api(withQuery("/api/agent", { project, scene, before, limit }));
}

async function fetchRenderPlans(project, scene, version) {
  return api(withQuery("/api/render-plans", { project, scene, version }));
}

async function fetchRenderState(project, plan) {
  return api(withQuery("/api/render-state", { project, plan }));
}

async function fetchAeStatus(project) {
  return api(withQuery("/api/ae/status", { project }));
}

async function postAePairing(project, capabilityRequest = {}) {
  return api("/api/ae/pairings", {
    method: "POST",
    body: JSON.stringify({
      project,
      capability_request: capabilityRequest,
    }),
  });
}

async function postRenderPlan(body) {
  return api("/api/render-plans", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

async function approveRenderPlan(planId, body) {
  return api(`/api/render-plans/${encodeURIComponent(planId)}/approve`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

async function postAeControl(sessionId, action, body) {
  return api(
    `/api/ae/sessions/${encodeURIComponent(sessionId)}/${encodeURIComponent(action)}`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

function aeArtifactUrl(artifactId, project, plan) {
  return withQuery(`/api/ae/artifacts/${encodeURIComponent(artifactId)}`, {
    project,
    plan,
  });
}
function nativeArtifactUrl(planId, kind, project) {
  return withQuery(
    `/api/native/artifacts/${encodeURIComponent(planId)}/${encodeURIComponent(kind)}`,
    { project },
  );
}
function lottieArtifactUrl(planId, project) {
  return withQuery(`/api/lottie/artifacts/${encodeURIComponent(planId)}/animation`, { project });
}

async function postCorrect(project, scene, op, args) {
  return api("/api/correct", {
    method: "POST",
    body: JSON.stringify({ project, scene, op, args }),
  });
}

function reviewFrameUrl(kind, frame, project, scene, v) {
  const version = kind === "recon" ? v : null;
  return withQuery(`/frame/${kind}/${frame}`, { project, scene, v: version });
}

function reviewAssetUrl(name, project, scene) {
  return `/assets/${encodeURIComponent(name)}?project=${encodeURIComponent(project)}&scene=${encodeURIComponent(scene)}`;
}

export {
  api,
  fetchAdminOrRedirect,
  fetchProject,
  fetchProjects,
  fetchStatus,
  fetchJob,
  postAnalyze,
  postAdminLogin,
  uploadProject,
  fetchEstimate,
  fetchFilmstrip,
  fetchReviewState,
  fetchReviewJob,
  fetchBboxes,
  postApprove,
  postKeep,
  postEdit,
  postAgent,
  fetchAgentHistory,
  postCorrect,
  fetchRenderPlans,
  fetchRenderState,
  fetchAeStatus,
  postAePairing,
  postRenderPlan,
  approveRenderPlan,
  postAeControl,
  aeArtifactUrl,
  nativeArtifactUrl,
  lottieArtifactUrl,
  reviewFrameUrl,
  reviewAssetUrl,
  DEFAULT_FILMSTRIP_COUNT,
};
