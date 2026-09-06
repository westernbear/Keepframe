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

export { api, uploadProject, fetchEstimate, fetchFilmstrip };
