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
  postCorrect,
  reviewFrameUrl,
  reviewAssetUrl,
  DEFAULT_FILMSTRIP_COUNT,
};
