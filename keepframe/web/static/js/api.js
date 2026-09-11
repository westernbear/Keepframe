async function api(path, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const res = await fetch(path, {
    headers: isForm ? { ...(opts.headers || {}) } : { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json().catch(() => ({})) : null;
  if (!res.ok) {
    const err = new Error((data && data.error) || res.statusText);
    err.status = res.status;
    err.body = data;
    throw err;
  }
  return data;
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

async function fetchFilmstrip(projectId, n = 8) {
  return api(`/api/projects/${projectId}/filmstrip?n=${n}`);
}

async function fetchReviewState(project, scene, v) {
  let url = `/api/state?project=${encodeURIComponent(project)}&scene=${encodeURIComponent(scene)}`;
  if (v) url += `&v=${encodeURIComponent(v)}`;
  return api(url);
}

async function fetchReviewJob(project, scene) {
  return api(`/api/job?project=${encodeURIComponent(project)}&scene=${encodeURIComponent(scene)}`);
}

async function fetchBboxes(project, scene, frame, v) {
  let url = `/api/bboxes?project=${encodeURIComponent(project)}&scene=${encodeURIComponent(scene)}&frame=${encodeURIComponent(String(frame))}`;
  if (v) url += `&v=${encodeURIComponent(v)}`;
  return api(url);
}

async function postKeep(project, scene, changes, note) {
  return api("/api/keep", {
    method: "POST",
    body: JSON.stringify({ project, scene, changes, note }),
  });
}

async function postCorrect(project, scene, op, args) {
  return api("/api/correct", {
    method: "POST",
    body: JSON.stringify({ project, scene, op, args }),
  });
}

function reviewFrameUrl(kind, frame, project, scene, v) {
  let url = `/frame/${kind}/${frame}?project=${encodeURIComponent(project)}&scene=${encodeURIComponent(scene)}`;
  if (v && kind === "recon") url += `&v=${encodeURIComponent(v)}`;
  return url;
}

function reviewAssetUrl(name, project, scene) {
  return `/assets/${encodeURIComponent(name)}?project=${encodeURIComponent(project)}&scene=${encodeURIComponent(scene)}`;
}

export {
  api,
  uploadProject,
  fetchEstimate,
  fetchFilmstrip,
  fetchReviewState,
  fetchReviewJob,
  fetchBboxes,
  postKeep,
  postCorrect,
  reviewFrameUrl,
  reviewAssetUrl,
};
