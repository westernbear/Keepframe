from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from email import message_from_bytes
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2

from refstudio.analyze.pipeline import analyze
from refstudio.web.estimate import consume_token, estimate, probe_video
from refstudio.web.jobs import JobStore
from refstudio.web.liveaction import looks_live_action
from refstudio.web.workspace import create_project, list_projects, project_dir

JOBS = JobStore()

STATIC = Path(__file__).parent / "static"
PAGES = {
    "/": "library.html",
    "/new": "ingest.html",
    "/analyze": "analyze.html",
    "/review": "review.html",
}
ADMIN_OFF = {"error": "로컬판에는 이 화면이 없습니다."}


def _parse_multipart(body: bytes, content_type: str) -> dict[str, str | tuple[str, bytes]]:
    msg = message_from_bytes(
        b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body,
        policy=default,
    )
    out: dict[str, str | tuple[str, bytes]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if filename:
            out[name] = (filename, payload or b"")
        else:
            out[name] = (payload or b"").decode("utf-8", errors="replace")
    return out


def _load_meta(workspace: Path, project_id: str) -> dict | None:
    meta = project_dir(workspace, project_id) / "meta.json"
    if not meta.is_file():
        return None
    return json.loads(meta.read_text(encoding="utf-8"))


def _write_meta(workspace: Path, project_id: str, **updates) -> dict:
    root = project_dir(workspace, project_id)
    meta_path = root / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(updates)
    meta["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return meta


def _read_frame_jpeg(video: Path, index: int) -> bytes | None:
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, bgr = cap.read()
    cap.release()
    if not ok:
        return None
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return buf.tobytes() if ok else None


def make_server(workspace: Path, port: int = 8765, host: str = "127.0.0.1", admin: bool = False) -> ThreadingHTTPServer:
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _read_body(self) -> bytes:
            length = int(self.headers.get("content-length", 0))
            return self.rfile.read(length) if length else b""

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/admin", "/admin/") and not admin:
                return self._json(404, ADMIN_OFF)
            if u.path in PAGES:
                p = STATIC / PAGES[u.path]
                if not p.exists():
                    return self._json(404, {"error": f"{PAGES[u.path]} missing"})
                return self._send(200, p.read_bytes(), "text/html; charset=utf-8")
            if u.path.startswith("/static/"):
                rel = u.path[len("/static/") :]
                if ".." in rel:
                    return self._json(400, {"error": "bad path"})
                p = STATIC / rel
                if not p.is_file():
                    return self._json(404, {"error": "not found"})
                ctype = (
                    "text/css"
                    if p.suffix == ".css"
                    else "application/javascript"
                    if p.suffix == ".js"
                    else "application/octet-stream"
                )
                return self._send(200, p.read_bytes(), ctype)
            if u.path == "/api/projects":
                return self._json(200, {"projects": list_projects(workspace)})

            m = re.fullmatch(r"/api/projects/([^/]+)/filmstrip", u.path)
            if m:
                pid = m.group(1)
                meta = _load_meta(workspace, pid)
                if meta is None:
                    return self._json(404, {"error": "not found"})
                n = int(parse_qs(u.query).get("n", ["8"])[0])
                video = project_dir(workspace, pid) / "source.mp4"
                info = probe_video(video)
                total = max(1, info["frames"])
                n = max(1, min(n, total))
                if n == 1:
                    indices = [0]
                else:
                    indices = [int(round(i * (total - 1) / (n - 1))) for i in range(n)]
                urls = [f"/api/projects/{pid}/frame/{i}" for i in indices]
                return self._json(200, {"frames": urls})

            m = re.fullmatch(r"/api/projects/([^/]+)/frame/(\d+)", u.path)
            if m:
                pid, frame_s = m.group(1), m.group(2)
                meta = _load_meta(workspace, pid)
                if meta is None:
                    return self._json(404, {"error": "not found"})
                video = project_dir(workspace, pid) / "source.mp4"
                jpeg = _read_frame_jpeg(video, int(frame_s))
                if jpeg is None:
                    return self._json(404, {"error": "frame not found"})
                return self._send(200, jpeg, "image/jpeg")

            m = re.fullmatch(r"/api/jobs/([^/]+)", u.path)
            if m:
                jid = m.group(1)
                try:
                    return self._json(200, JOBS.get(jid).to_json())
                except KeyError:
                    return self._json(404, {"error": "not found"})

            return self._json(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)

            if u.path == "/api/estimate":
                body = self._read_body()
                try:
                    data = json.loads(body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "bad json"})
                if "project_id" in data:
                    meta = _load_meta(workspace, data["project_id"])
                    if meta is None:
                        return self._json(404, {"error": "not found"})
                    video = project_dir(workspace, meta["id"]) / "source.mp4"
                    info = probe_video(video)
                    mode = meta.get("mode", "full")
                    if mode == "range" and meta.get("range"):
                        start, end = meta["range"]
                        frames = max(1, int(end) - int(start) + 1)
                    else:
                        frames = info["frames"]
                    return self._json(200, estimate(mode, frames, info["fps"]))
                mode = data.get("mode", "full")
                frames = int(data.get("frames", 0))
                fps = float(data.get("fps", 30))
                return self._json(200, estimate(mode, frames, fps))

            if u.path == "/api/projects":
                ctype = self.headers.get("content-type", "")
                if "multipart/form-data" not in ctype:
                    body = self._read_body()
                    try:
                        json.loads(body.decode("utf-8") or "{}")
                    except json.JSONDecodeError:
                        return self._json(400, {"error": "bad json"})
                    return self._json(400, {"error": "video required"})

                fields = _parse_multipart(self._read_body(), ctype)
                title = str(fields.get("title", "Untitled"))
                mode = str(fields.get("mode", "full"))
                video_field = fields.get("video")
                if not isinstance(video_field, tuple):
                    return self._json(400, {"error": "video required"})
                _, video_bytes = video_field
                if not video_bytes:
                    return self._json(400, {"error": "video required"})

                start = int(str(fields.get("start", "0")))
                end = int(str(fields.get("end", "0")))
                range_ = (start, end) if mode == "range" else None

                tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
                try:
                    tmp.write(video_bytes)
                    tmp.close()
                    tmp_path = Path(tmp.name)
                    reason = looks_live_action(tmp_path)
                    if reason:
                        tmp_path.unlink(missing_ok=True)
                        return self._json(422, {"error": reason, "code": "live_action"})
                    row = create_project(workspace, title, tmp_path, mode, range_)
                finally:
                    Path(tmp.name).unlink(missing_ok=True)
                video_path = project_dir(workspace, row["id"]) / "source.mp4"
                row = {**row, "video": probe_video(video_path)}
                return self._json(201, {"project": row})

            if u.path == "/api/analyze":
                body = self._read_body()
                try:
                    data = json.loads(body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "bad json"})
                project_id = data.get("project_id")
                if not project_id:
                    return self._json(400, {"error": "project_id required"})
                meta = _load_meta(workspace, project_id)
                if meta is None:
                    return self._json(404, {"error": "not found"})
                token = data.get("confirm_token") or ""
                if meta.get("mode") == "full" and not consume_token(token):
                    return self._json(400, {"error": "confirm required"})
                root = project_dir(workspace, project_id)
                video = root / "source.mp4"
                info = probe_video(video)
                if meta.get("range"):
                    start, end = meta["range"]
                else:
                    start, end = 0, max(0, info["frames"] - 1)
                frames = max(1, int(end) - int(start) + 1)
                est = estimate(meta.get("mode", "full"), frames, info["fps"])
                _write_meta(workspace, project_id, status="analyzing", job_id=None)

                def run_analyze() -> dict:
                    try:
                        analyze(video, int(start), int(end), root)
                        _write_meta(workspace, project_id, status="review", job_id=None)
                        return {"project_id": project_id}
                    except Exception as e:
                        _write_meta(
                            workspace,
                            project_id,
                            status="error",
                            error=f"{type(e).__name__}: {e}",
                        )
                        raise

                job = JOBS.submit(
                    "analyze",
                    run_analyze,
                    project_id=project_id,
                    scene_id="s1",
                    stage="pipeline",
                    eta_s=est["seconds"],
                )
                _write_meta(workspace, project_id, job_id=job.id)
                return self._json(202, {"job": job.to_json()})

            return self._json(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), H)
