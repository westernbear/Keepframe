from __future__ import annotations

import base64
import json
import re
import tempfile
import threading
from collections import OrderedDict
from email import message_from_bytes
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from keepframe.analyze.composite import composite_scene
from keepframe.analyze.device import gpu_status
from keepframe.ir.schema import FontGuess
from keepframe.ir.store import current_scene, load_project, load_scene, new_version, scene_dir
from keepframe.jobs import Job, JobSpec, JobStore
from keepframe.log import configure, get
from keepframe.review import corrections
from keepframe.web.estimate import consume_token, estimate, probe_video
from keepframe.web.liveaction import looks_live_action
from keepframe.web.workspace import create_project, list_projects, load_meta, project_dir, write_meta

log = get("keepframe.web")

CORRECTION_OPS = {"reassign", "mask", "bbox", "text"}

JOBS = JobStore()


def existing_analyze_job(project_id: str, meta: dict) -> Job | None:
    job = JOBS.for_project(project_id, "analyze")
    if job is not None:
        return job
    jid = meta.get("job_id")
    return JOBS.find(jid) if jid else None

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


PREVIEW_MAX_W = 960
PREVIEW_CACHE = 48


def _slim_report(rep: dict | None, n_frames: int) -> dict | None:
    if not rep:
        return None
    rec = rep.get("reconstruction") or {}
    per = rec.get("per_frame_l1") or rec.get("per_frame")
    if isinstance(per, dict):
        series = [float(per.get(i, per.get(str(i), 0)) or 0) for i in range(n_frames)]
    elif isinstance(per, list):
        series = [float(x) for x in per]
    else:
        series = []
    return {"reconstruction": {"per_frame_l1": series}}


def _slim_scene(scene) -> dict:
    data = scene.model_dump(by_alias=True)
    for el in data.get("elements", []):
        el.pop("tracks", None)
        el.pop("z", None)
        el.pop("raw", None)
        el.pop("fit_error", None)
        can = el.get("canonical") or {}
        el["canonical"] = {"text": can.get("text")}
    return data


def review_state_payload(project, version, scene, report, job) -> dict:
    """UI payload without per-frame tracks so the review page can parse off the main thread."""
    return {
        "project": project.model_dump(by_alias=True),
        "version": version.model_dump(),
        "scene": _slim_scene(scene),
        "report": _slim_report(report, scene.frames),
        "job": job,
    }


def _jpeg(rgb: np.ndarray, max_w: int = PREVIEW_MAX_W) -> bytes:
    rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    if w > max_w:
        nh = max(1, int(round(h * max_w / w)))
        rgb = cv2.resize(rgb, (max_w, nh), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes()


class ReviewState:
    def __init__(self, root: Path, scene_id: str):
        self.root, self.scene_id = Path(root), scene_id
        self.job = {"status": "idle", "op": None, "error": None, "version": None}
        self.lock = threading.Lock()
        self._frames: np.ndarray | None | bool = None
        self._preview: OrderedDict[tuple[str, str, int], bytes] = OrderedDict()
        self._tex: dict = {}

    def _remember(self, key: tuple[str, str, int], data: bytes) -> bytes:
        self._preview[key] = data
        self._preview.move_to_end(key)
        while len(self._preview) > PREVIEW_CACHE:
            self._preview.popitem(last=False)
        return data

    def frames(self) -> np.ndarray | None:
        if self._frames is None:
            npy = scene_dir(self.root, self.scene_id) / "stages" / "frames.npy"
            self._frames = np.load(npy, mmap_mode="r") if npy.is_file() else False
        return None if self._frames is False else self._frames

    def scene(self, version: str | None = None):
        if version:
            project = load_project(self.root)
            v = next(
                v
                for v in project.versions
                if v.id == version and v.scene_file.startswith(f"scenes/{self.scene_id}/")
            )
            return load_scene(self.root / v.scene_file), v
        return current_scene(self.root, self.scene_id)

    def orig_png(self, f: int) -> bytes:
        key = ("orig", "", f)
        hit = self._preview.get(key)
        if hit is not None:
            self._preview.move_to_end(key)
            return hit
        fr = self.frames()
        if fr is not None:
            if not 0 <= f < len(fr):
                raise IndexError("frame out of range")
            return self._remember(key, _jpeg(np.asarray(fr[f])))
        video = self.root / "source.mp4"
        cap = cv2.VideoCapture(str(video))
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, bgr = cap.read()
        cap.release()
        if not ok:
            raise IndexError("frame out of range")
        return self._remember(key, _jpeg(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)))

    def recon_png(self, f: int, version: str | None) -> bytes:
        scene, v = self.scene(version)
        key = ("recon", v.id, f)
        hit = self._preview.get(key)
        if hit is not None:
            self._preview.move_to_end(key)
            return hit
        rgb = (composite_scene(scene, scene_dir(self.root, self.scene_id), f, self._tex) * 255).round().clip(0, 255).astype(np.uint8)
        return self._remember(key, _jpeg(rgb))

    def run_correction(self, op: str, args: dict) -> None:
        with self.lock:
            if self.job["status"] == "running":
                raise RuntimeError("busy")
            self.job = {"status": "running", "op": op, "error": None, "version": None}
        log.info("correction start scene=%s op=%s", self.scene_id, op)

        def work() -> None:
            try:
                if op == "reassign":
                    v = corrections.reassign_id(
                        self.root,
                        self.scene_id,
                        tuple(args["frames"]),
                        args["from_id"],
                        args["to_id"],
                        note=args.get("note", "reassign id"),
                    )
                elif op == "mask":
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
                        t.write(base64.b64decode(args["mask_png_base64"]))
                        tmp = Path(t.name)
                    try:
                        v = corrections.set_region_mask(
                            self.root,
                            self.scene_id,
                            int(args["frame"]),
                            tmp,
                            args["object_id"],
                            note=args.get("note", "set region mask"),
                        )
                    finally:
                        tmp.unlink(missing_ok=True)
                elif op == "bbox":
                    v = corrections.add_bbox_prompt(
                        self.root,
                        self.scene_id,
                        int(args["frame"]),
                        tuple(int(x) for x in args["bbox"]),
                        args["object_id"],
                        note=args.get("note", "bbox prompt"),
                    )
                else:
                    v = corrections.edit_text(
                        self.root,
                        self.scene_id,
                        args["element_id"],
                        text=args.get("text"),
                        font=FontGuess(**args["font"]) if args.get("font") else None,
                        note=args.get("note", "edit text"),
                    )
                self._preview.clear()
                self._tex.clear()
                self.job = {"status": "done", "op": op, "error": None, "version": v.id}
                log.info("correction done scene=%s op=%s version=%s", self.scene_id, op, v.id)
            except Exception as e:
                self.job = {"status": "error", "op": op, "error": f"{type(e).__name__}: {e}", "version": None}
                log.exception("correction failed scene=%s op=%s", self.scene_id, op)

        threading.Thread(target=work, daemon=True).start()


def _read_frame_jpeg(video: Path, index: int) -> bytes | None:
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, bgr = cap.read()
    cap.release()
    if not ok:
        return None
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return buf.tobytes() if ok else None


def make_server(
    workspace: Path,
    port: int = 8765,
    host: str = "127.0.0.1",
    admin: bool = False,
    admin_svc=None,
    admin_auth=None,
) -> ThreadingHTTPServer:
    configure()
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    review_states: dict[tuple[str, str], ReviewState] = {}
    admin_routes = None
    if admin and admin_svc is not None and admin_auth is not None:
        from keepframe.admin.http import AdminRoutes

        admin_routes = AdminRoutes(admin_svc, admin_auth)

    def review_state(project_id: str, scene_id: str) -> ReviewState | None:
        if load_meta(workspace, project_id) is None:
            return None
        key = (project_id, scene_id)
        if key not in review_states:
            review_states[key] = ReviewState(project_dir(workspace, project_id), scene_id)
        return review_states[key]

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def log_error(self, fmt, *args):
            log.error("%s %s", self.address_string(), fmt % args)

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
            if admin_routes and admin_routes.handle_get(self, u):
                return
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
                ctype = {
                    ".css": "text/css",
                    ".js": "application/javascript",
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".svg": "image/svg+xml",
                    ".ico": "image/x-icon",
                    ".webp": "image/webp",
                }.get(p.suffix.lower(), "application/octet-stream")
                return self._send(200, p.read_bytes(), ctype)
            if u.path == "/api/status":
                return self._json(200, gpu_status())
            if u.path == "/api/projects":
                return self._json(200, {"projects": list_projects(workspace)})

            m = re.fullmatch(r"/api/projects/([^/]+)/filmstrip", u.path)
            if m:
                pid = m.group(1)
                meta = load_meta(workspace, pid)
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
                meta = load_meta(workspace, pid)
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

            q = parse_qs(u.query)
            pid = q.get("project", [None])[0]
            sid = q.get("scene", ["s1"])[0]
            ver = q.get("v", [None])[0]

            if u.path == "/api/state":
                if not pid:
                    return self._json(400, {"error": "project required"})
                state = review_state(pid, sid)
                if state is None:
                    return self._json(404, {"error": "not found"})
                try:
                    scene, v = state.scene(ver)
                    sd = scene_dir(state.root, state.scene_id)
                    rep = json.loads((sd / "report.json").read_text()) if (sd / "report.json").is_file() else None
                    return self._json(200, review_state_payload(load_project(state.root), v, scene, rep, state.job))
                except Exception as e:
                    log.exception("state failed project=%s scene=%s", pid, sid)
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})

            if u.path == "/api/job":
                if not pid:
                    return self._json(400, {"error": "project required"})
                state = review_state(pid, sid)
                if state is None:
                    return self._json(404, {"error": "not found"})
                return self._json(200, state.job)

            parts = u.path.strip("/").split("/")
            if parts[:2] == ["frame", "orig"] and len(parts) == 3:
                if not pid:
                    return self._json(400, {"error": "project required"})
                state = review_state(pid, sid)
                if state is None:
                    return self._json(404, {"error": "not found"})
                try:
                    return self._send(200, state.orig_png(int(parts[2])), "image/jpeg")
                except IndexError:
                    return self._json(404, {"error": "frame out of range"})
                except Exception as e:
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})

            if parts[:2] == ["frame", "recon"] and len(parts) == 3:
                if not pid:
                    return self._json(400, {"error": "project required"})
                state = review_state(pid, sid)
                if state is None:
                    return self._json(404, {"error": "not found"})
                try:
                    return self._send(200, state.recon_png(int(parts[2]), ver), "image/jpeg")
                except Exception as e:
                    log.exception("recon frame failed project=%s scene=%s", pid, sid)
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})

            if parts and parts[0] == "assets" and len(parts) >= 2:
                if not pid:
                    return self._json(400, {"error": "project required"})
                state = review_state(pid, sid)
                if state is None:
                    return self._json(404, {"error": "not found"})
                name = "/".join(parts[1:])
                if ".." in name or not name.endswith(".png"):
                    return self._json(400, {"error": "bad asset path"})
                asset = scene_dir(state.root, state.scene_id) / "assets" / name
                if not asset.is_file():
                    return self._json(404, {"error": "no such asset"})
                return self._send(200, asset.read_bytes(), "image/png")

            return self._json(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            if admin_routes and admin_routes.handle_post(self, u):
                return

            if u.path == "/api/estimate":
                body = self._read_body()
                try:
                    data = json.loads(body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "bad json"})
                if "project_id" in data:
                    meta = load_meta(workspace, data["project_id"])
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
                meta = load_meta(workspace, project_id)
                if meta is None:
                    return self._json(404, {"error": "not found"})
                token = data.get("confirm_token") or ""
                existing = existing_analyze_job(project_id, meta)
                if existing is not None and existing.status in ("queued", "running"):
                    return self._json(202, {"job": existing.to_json()})
                if existing is not None and existing.status in ("done", "error") and not token:
                    return self._json(202, {"job": existing.to_json()})
                # Refresh / re-entry must not demand a token after the first confirm.
                if meta.get("mode") == "full" and meta.get("status") != "analyzing" and not consume_token(token):
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
                write_meta(workspace, project_id, status="analyzing")
                spec = JobSpec(
                    kind="analyze",
                    args={
                        "video": str(video),
                        "start": int(start),
                        "end": int(end),
                        "out_root": str(root),
                        "workspace": str(workspace),
                        "project_id": project_id,
                    },
                )
                job = JOBS.submit(
                    "analyze",
                    spec=spec,
                    project_id=project_id,
                    scene_id="s1",
                    stage="frames",
                    eta_s=est["seconds"],
                )
                write_meta(workspace, project_id, job_id=job.id)
                log.info("analyze queued job=%s project=%s range=[%s,%s] eta_s=%s", job.id, project_id, start, end, est["seconds"])
                return self._json(202, {"job": job.to_json()})

            if u.path == "/api/keep":
                try:
                    data = json.loads(self._read_body().decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "bad json"})
                project_id = data.get("project")
                scene_id = data.get("scene", "s1")
                if not project_id:
                    return self._json(400, {"error": "project required"})
                state = review_state(project_id, scene_id)
                if state is None:
                    return self._json(404, {"error": "not found"})
                try:
                    scene, _ = state.scene()
                    wanted = {c["pred"]: bool(c["keep"]) for c in data.get("changes", [])}
                    for c in scene.constraints:
                        if c.pred in wanted:
                            c.keep = wanted[c.pred]
                    v = new_version(
                        state.root,
                        state.scene_id,
                        scene,
                        note=data.get("note", "keep 조건 수정"),
                        auto=False,
                    )
                    state._preview.clear()
                    state._tex.clear()
                    return self._json(200, {"version": json.loads(v.model_dump_json())})
                except Exception as e:
                    log.exception("keep update failed project=%s scene=%s", project_id, scene_id)
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})

            if u.path == "/api/correct":
                try:
                    data = json.loads(self._read_body().decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "bad json"})
                project_id = data.get("project")
                scene_id = data.get("scene", "s1")
                if not project_id:
                    return self._json(400, {"error": "project required"})
                state = review_state(project_id, scene_id)
                if state is None:
                    return self._json(404, {"error": "not found"})
                op = data.get("op")
                if op not in CORRECTION_OPS:
                    return self._json(400, {"error": f"unknown op {op!r}; expected one of {sorted(CORRECTION_OPS)}"})
                try:
                    state.run_correction(op, data.get("args", {}))
                except RuntimeError:
                    return self._json(409, {"error": "a correction is already running"})
                return self._json(202, {"job": state.job})

            return self._json(404, {"error": "not found"})

        def do_DELETE(self):
            u = urlparse(self.path)
            if admin_routes and admin_routes.handle_delete(self, u):
                return
            return self._json(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), H)
