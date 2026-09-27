from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
import threading
from collections import OrderedDict
from email import message_from_bytes
from email.policy import default
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from keepframe.analyze.composite import composite_scene
from keepframe.analyze.device import gpu_status
from keepframe.ir.schema import FontGuess
from keepframe.ir.store import current_scene, load_project, load_scene, new_version, scene_dir
from keepframe.ir.tracks import element_bbox
from keepframe.jobs import Job, JobSpec, JobStore
from keepframe.log import configure, get
from keepframe.review import corrections
from keepframe.web.estimate import consume_token, estimate, probe_video
from keepframe.web.liveaction import looks_live_action
from keepframe.web.demo import ensure_demo_project
from keepframe.edit.agent import edit as run_edit
from keepframe.render.native import prepare_native_job
from keepframe.render.plan import (
    PlanConflict,
    approve_render_plan,
    create_render_plan,
    load_render_plan,
    load_render_plan_state,
)
from keepframe.session import SessionAgent, SessionContext, make_llm
from keepframe.after_effects.auth import (
    AEAuthError,
    AEControllerAuthorizationError,
    AEProjectAuth,
    controller_cookie_name,
)
from keepframe.after_effects.coordinator import AECoordinator, CoordinatorConflict
from keepframe.session.provider import load_llm_settings
from keepframe.web.workspace import create_project, create_rejected_project, list_projects, load_meta, project_dir, write_meta

log = get("keepframe.web")

CORRECTION_OPS = {"reassign", "mask", "bbox", "text"}

JOBS = JobStore()
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_RENDER_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _safe_render_project(workspace: Path, project_id: str) -> Path:
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("project id is invalid")
    workspace = Path(workspace)
    root = project_dir(workspace, project_id)
    try:
        workspace_real = workspace.resolve(strict=True)
        root_real = root.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError(project_id) from exc
    if root.is_symlink() or root_real.parent != workspace_real or not root_real.is_dir():
        raise FileNotFoundError(project_id)
    try:
        meta = load_meta(workspace, project_id)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise FileNotFoundError(project_id) from exc
    if not isinstance(meta, dict) or meta.get("id") is not None and meta.get("id") != project_id:
        raise FileNotFoundError(project_id)
    return root_real


def _native_execution_job_id(plan_id: str, digest: str, execution_id: str) -> str:
    material = f"{plan_id}\0{digest}\0{execution_id}".encode("utf-8")
    return "j" + hashlib.sha256(material).hexdigest()


def _render_status(state, job: Job | None) -> str:
    if job is None:
        return "awaiting" if state.status == "awaiting_approval" else "approved"
    if job.status == "error" or job.error:
        return "failed"
    if job.status in {"queued", "running", "done"}:
        return job.status
    return "failed"


def _render_state_payload(root: Path, plan_id: str) -> dict:
    plan = load_render_plan(root, plan_id)
    state = load_render_plan_state(root, plan.id)
    if plan.backend == "after_effects":
        session = None
        try:
            session = AECoordinator.cached(root, plan.id).state()
        except CoordinatorConflict as exc:
            if "has not started" not in str(exc):
                raise
            session = None
        return {
            "plan": plan.model_dump(mode="json"),
            "state": state.model_dump(mode="json"),
            "session": session.model_dump(mode="json") if session is not None else None,
            "job": None,
            "status": session.status if session is not None else _render_status(state, None),
        }
    job_id = (
        _native_execution_job_id(plan.id, plan.digest, state.execution_id)
        if plan.backend == "native" and state.execution_id
        else None
    )
    job = JOBS.find(job_id) if job_id else None
    return {
        "plan": plan.model_dump(mode="json"),
        "state": state.model_dump(mode="json"),
        "job": job.to_json() if job is not None else None,
        "status": _render_status(state, job),
    }



def existing_analyze_job(project_id: str, meta: dict) -> Job | None:
    job = JOBS.for_project(project_id, "analyze")
    if job is not None:
        return job
    jid = meta.get("job_id")
    return JOBS.find(jid) if jid else None

STATIC = Path(__file__).parent / "static"
PAGES = {
    "/": "landing.html",
    "/library": "library.html",
    "/new": "ingest.html",
    "/analyze": "analyze.html",
    "/review": "review.html",
    "/agent": "agent.html",
}
ADMIN_OFF = {"error": "로컬판에는 이 화면이 없습니다."}


def clip_range(start: int, end: int, n_frames: int) -> tuple[int, int]:
    last = max(0, int(n_frames) - 1)
    a = max(0, min(int(start), last))
    b = max(a, min(int(end), last))
    return a, b


def resolve_analyze_window(meta: dict, data: dict, n_frames: int) -> tuple[str, int, int]:
    mode = str(data.get("mode") or meta.get("mode") or "full")
    if mode not in ("range", "full"):
        mode = "full"
    if mode != "range":
        return "full", 0, max(0, int(n_frames) - 1)
    if data.get("start") is not None and data.get("end") is not None:
        start, end = clip_range(int(data["start"]), int(data["end"]), n_frames)
    elif meta.get("range"):
        start, end = clip_range(meta["range"][0], meta["range"][1], n_frames)
    else:
        return "full", 0, max(0, int(n_frames) - 1)
    return "range", start, end


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
PREVIEW_CACHE = 256
STRIP_BINS = 200


def _slim_report(rep: dict | None, n_frames: int) -> dict | None:
    """Downsample per-frame L1 to strip bins + peak indices so the browser never sorts a long series."""
    if not rep:
        return None
    rec = rep.get("reconstruction") or {}
    per = rec.get("per_frame_l1") or rec.get("per_frame")
    if isinstance(per, dict):
        series = np.array([float(per.get(i, per.get(str(i), 0)) or 0) for i in range(n_frames)])
    elif isinstance(per, list):
        series = np.array([float(x) for x in per])
    else:
        series = np.array([])
    if series.size == 0:
        return {"reconstruction": {"l1_bins": [], "l1_peaks": [], "l1_max": 0.0, "l1_thresh": 0.0}}
    thresh = float(np.percentile(series, 80))
    max_v = float(series.max())
    bins = min(STRIP_BINS, series.size)
    edges = (np.arange(bins + 1) / bins * series.size).astype(int)
    edges[-1] = series.size
    l1_bins = []
    l1_hot = []
    for b in range(bins):
        seg = series[edges[b]:max(edges[b] + 1, edges[b + 1])]
        l1_bins.append(float(seg.max()))
        l1_hot.append(bool((seg >= thresh).any()))
    l1_peaks = np.flatnonzero(series >= thresh).astype(int).tolist()
    return {"reconstruction": {"l1_bins": l1_bins, "l1_hot": l1_hot, "l1_peaks": l1_peaks,
                               "l1_max": max_v, "l1_thresh": thresh}}


def _slim_project(project, scene_id: str) -> dict:
    return {
        "versions": [
            {"id": v.id, "note": v.note, "scene_file": v.scene_file}
            for v in project.versions
            if v.scene_file.startswith(f"scenes/{scene_id}/")
        ]
    }


def _slim_scene(scene) -> dict:
    data = scene.model_dump(by_alias=True)
    for el in data.get("elements", []):
        el.pop("tracks", None)
        el.pop("z", None)
        el.pop("raw", None)
        el.pop("fit_error", None)
        can = el.get("canonical") or {}
        tex = can.get("texture") or ""
        el["canonical"] = {"text": can.get("text"), "texture": tex.split("/")[-1] if tex else None}
    return data


def review_state_payload(project, version, scene, report, job, status=None) -> dict:
    """Slim UI payload: pre-binned error strip, scene-only version list, no per-frame tracks."""
    return {
        "project": _slim_project(project, scene.id),
        "version": version.model_dump(),
        "scene": _slim_scene(scene),
        "report": _slim_report(report, scene.frames),
        "job": job,
        "status": status,
    }


def _jpeg(rgb: np.ndarray, max_w: int = PREVIEW_MAX_W) -> bytes:
    rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    if w > max_w:
        nh = max(1, int(round(h * max_w / w)))
        rgb = cv2.resize(rgb, (max_w, nh), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes()

def _png(rgb: np.ndarray, max_w: int = PREVIEW_MAX_W) -> bytes:
    rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    if w > max_w:
        nh = max(1, int(round(h * max_w / w)))
        rgb = cv2.resize(rgb, (max_w, nh), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    return buf.tobytes() if ok else b""


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
            return self._remember(key, _png(np.asarray(fr[f])))
        video = self.root / "source.mp4"
        cap = cv2.VideoCapture(str(video))
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, bgr = cap.read()
        cap.release()
        if not ok:
            raise IndexError("frame out of range")
        return self._remember(key, _png(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)))

    def recon_png(self, f: int, version: str | None) -> bytes:
        scene, v = self.scene(version)
        key = ("recon", v.id, f)
        hit = self._preview.get(key)
        if hit is not None:
            self._preview.move_to_end(key)
            return hit
        rgb = (composite_scene(scene, scene_dir(self.root, self.scene_id), f, self._tex) * 255).round().clip(0, 255).astype(np.uint8)
        return self._remember(key, _png(rgb))

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

def _detach_project_device(root: Path, device_id: str, *, reason: str) -> bool:
    renders = root / "renders"
    if not renders.exists():
        return True
    if renders.is_symlink() or not renders.is_dir():
        raise CoordinatorConflict("render workspace is unsafe")
    settled = True
    for plan_dir in renders.iterdir():
        if (
            plan_dir.is_symlink()
            or not plan_dir.is_dir()
            or not _RENDER_PLAN_ID_RE.fullmatch(plan_dir.name)
            or not (plan_dir / "ae" / "session.json").exists()
        ):
            continue
        coordinator = AECoordinator.cached(root, plan_dir.name)
        session = coordinator.state()
        if session.device_id != device_id:
            continue
        session = coordinator.detach_device(device_id, reason=reason)
        if session.device_id == device_id and session.status not in {"done", "failed"}:
            settled = False
    return settled


def _authority(value: str) -> tuple[str, int | None] | None:
    try:
        parsed = urlparse(f"//{value}")
        port = parsed.port
    except ValueError:
        return None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.hostname.lower(), port




def make_server(
    workspace: Path,
    port: int = 8765,
    host: str = "127.0.0.1",
    admin: bool = False,
    admin_svc=None,
    admin_auth=None,
    ae_relay_url: str | None = None,
) -> ThreadingHTTPServer:
    configure()
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    review_states: dict[tuple[str, str], ReviewState] = {}
    agent_states: dict[tuple[str, str], list[dict]] = {}
    allowed_ae_hosts = {"127.0.0.1", "localhost", "::1"}
    configured_host = host.strip("[]").lower()
    if configured_host not in {"0.0.0.0", "::", ""}:
        allowed_ae_hosts.add(configured_host)

    def agent_llm():
        if admin_svc is not None:
            return make_llm(admin_svc.get_llm_settings())
        saved = load_llm_settings(workspace)
        return make_llm(saved) if saved is not None else make_llm()

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

        def _send(
            self,
            code: int,
            body: bytes,
            ctype: str,
            cache: str | None = None,
            headers: tuple[tuple[str, str], ...] = (),
        ) -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            if cache:
                self.send_header("cache-control", cache)
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(
            self,
            code: int,
            obj,
            *,
            headers: tuple[tuple[str, str], ...] = (),
        ) -> None:
            self._send(
                code,
                json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                headers=headers,
            )

        def _read_body(self) -> bytes:
            length = int(self.headers.get("content-length", 0))
            return self.rfile.read(length) if length else b""

        def _bounded_json(self, limit: int = 64 * 1024) -> dict[str, object] | None:
            if self.headers.get_content_type() != "application/json":
                self._json(415, {"error": "application/json is required"})
                return None
            length_values = self.headers.get_all("Content-Length") or []
            if len(length_values) > 1:
                self._json(400, {"error": "bad json"})
                return None
            try:
                length = int(length_values[0] if length_values else "0")
            except ValueError:
                self._json(400, {"error": "bad json"})
                return None
            if length < 0 or length > limit:
                self._json(413, {"error": "request body is too large"})
                return None
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "bad json"})
                return None
            if not isinstance(value, dict):
                self._json(400, {"error": "bad json"})
                return None
            return value

        def _same_origin(self) -> bool:
            host_values = self.headers.get_all("Host") or []
            if len(host_values) != 1:
                self._json(403, {"error": "browser origin is not allowed"})
                return False
            authority = _authority(host_values[0])
            if authority is None or authority[0] not in allowed_ae_hosts:
                self._json(403, {"error": "browser origin is not allowed"})
                return False
            server_address = self.server.server_address
            if not isinstance(server_address, tuple) or len(server_address) < 2:
                self._json(403, {"error": "browser origin is not allowed"})
                return False
            server_port = int(server_address[1])
            if authority[1] is not None and authority[1] != server_port:
                self._json(403, {"error": "browser origin is not allowed"})
                return False
            origin_values = self.headers.get_all("Origin") or []
            source: str | None = None
            if len(origin_values) == 1:
                source = origin_values[0]
            elif not origin_values and self.command == "GET":
                referer_values = self.headers.get_all("Referer") or []
                if (
                    len(referer_values) == 1
                    and self.headers.get("Sec-Fetch-Site") == "same-origin"
                ):
                    source = referer_values[0]
            if source is None:
                self._json(403, {"error": "browser origin is not allowed"})
                return False
            try:
                parsed = urlparse(source)
                source_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            except ValueError:
                self._json(403, {"error": "browser origin is not allowed"})
                return False
            is_origin = bool(origin_values)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.hostname is None
                or (
                    is_origin
                    and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment)
                )
            ):
                self._json(403, {"error": "browser origin is not allowed"})
                return False
            host_port = authority[1] or source_port
            if (
                parsed.hostname.lower() != authority[0]
                or source_port != host_port
                or host_port != server_port
            ):
                self._json(403, {"error": "browser origin is not allowed"})
                return False
            return True

        def _controller_token(self, project_id: str) -> str | None:
            values = self.headers.get_all("Cookie") or []
            if len(values) != 1:
                return None
            try:
                cookies = SimpleCookie()
                cookies.load(values[0])
                morsel = cookies.get(controller_cookie_name(project_id))
                return morsel.value if morsel is not None else None
            except (AEAuthError, AttributeError):
                return None

        def _authorize_ae_browser(
            self,
            project_id: str,
            *,
            origin_checked: bool = False,
        ) -> bool:
            if not origin_checked and not self._same_origin():
                return False
            try:
                auth = AEProjectAuth(workspace / project_id, project_id)
                authorized = auth.authenticate_controller(
                    self._controller_token(project_id)
                )
            except AEAuthError:
                authorized = False
            if not authorized:
                self._json(401, {"error": "controller authorization failed"})
                return False
            return True

        def _controller_cookie(
            self,
            project_id: str,
            token: str,
            *,
            clear: bool = False,
        ) -> str:
            value = f"{controller_cookie_name(project_id)}={token}; Path=/; HttpOnly; SameSite=Strict"
            origin = self.headers.get("Origin") or ""
            if urlparse(origin).scheme == "https":
                value += "; Secure"
            if clear:
                value += "; Max-Age=0"
            return value

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/admin", "/admin/") and not admin:
                return self._json(404, ADMIN_OFF)
            if admin_routes and admin_routes.handle_get(self, u):
                return
            if u.path == "/demo":
                row = ensure_demo_project(workspace)
                loc = f"/review?project={row['id']}&scene={row['scene']}"
                self.send_response(302)
                self.send_header("location", loc)
                self.send_header("content-length", "0")
                self.end_headers()
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
                suffix = p.suffix.lower()
                ctype = {
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".svg": "image/svg+xml",
                    ".ico": "image/x-icon",
                    ".webp": "image/webp",
                }.get(suffix, "application/octet-stream")
                cache = (
                    "no-cache, must-revalidate"
                    if suffix in {".css", ".js"}
                    else "public, max-age=604800"
                )
                return self._send(200, p.read_bytes(), ctype, cache=cache)
            if u.path == "/api/status":
                return self._json(200, gpu_status())
            if u.path == "/api/projects":
                return self._json(200, {"projects": list_projects(workspace)})

            m = re.fullmatch(r"/api/projects/([^/]+)", u.path)
            if m:
                meta = load_meta(workspace, m.group(1))
                if meta is None:
                    return self._json(404, {"error": "not found"})
                source = project_dir(workspace, m.group(1)) / "source.mp4"
                project = {**meta, "video": probe_video(source) if source.is_file() else None}
                return self._json(200, {"project": project})
            m = re.fullmatch(r"/api/projects/([^/]+)/filmstrip", u.path)
            if m:
                pid = m.group(1)
                meta = load_meta(workspace, pid)
                if meta is None or meta.get("status") == "rejected" or not (project_dir(workspace, pid) / "source.mp4").is_file():
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
                if meta is None or meta.get("status") == "rejected" or not (project_dir(workspace, pid) / "source.mp4").is_file():
                    return self._json(404, {"error": "not found"})
                video = project_dir(workspace, pid) / "source.mp4"
                jpeg = _read_frame_jpeg(video, int(frame_s)) if video.is_file() else None
                if jpeg is None:
                    sid = meta.get("scene") or "s1"
                    state = review_state(pid, sid)
                    if state is not None:
                        try:
                            jpeg = state.orig_png(int(frame_s))
                        except Exception:
                            jpeg = None
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
            artifact_match = re.fullmatch(
                r"/api/ae/artifacts/([A-Za-z0-9_-]{8,128})",
                u.path,
            )
            if artifact_match:
                artifact_id = artifact_match.group(1)
                plan_id = q.get("plan", [None])[0]
                if (
                    not isinstance(pid, str)
                    or not _PROJECT_ID_RE.fullmatch(pid)
                    or not isinstance(plan_id, str)
                    or not _RENDER_PLAN_ID_RE.fullmatch(plan_id)
                ):
                    return self._json(
                        400,
                        {"error": "project and plan are required"},
                    )
                if not self._authorize_ae_browser(pid):
                    return
                try:
                    root = _safe_render_project(workspace, pid)
                    plan = load_render_plan(root, plan_id)
                    if plan.project_id != pid or plan.backend != "after_effects":
                        return self._json(404, {"error": "not found"})
                    reservation, stream = AECoordinator.cached(
                        root,
                        plan.id,
                    ).open_artifact(artifact_id)
                except (FileNotFoundError, PlanConflict, CoordinatorConflict):
                    return self._json(404, {"error": "not found"})
                content_types = {
                    "png": "image/png",
                    "mp4": "video/mp4",
                    "zip": "application/zip",
                    "aep": "application/octet-stream",
                }
                try:
                    self.send_response(200)
                    self.send_header(
                        "content-type",
                        content_types[reservation.kind],
                    )
                    self.send_header("content-length", str(reservation.length))
                    self.send_header("cache-control", "no-store")
                    self.send_header("x-content-type-options", "nosniff")
                    self.send_header(
                        "content-disposition",
                        f'attachment; filename="keepframe-{artifact_id}.{reservation.kind}"',
                    )
                    self.end_headers()
                    while chunk := stream.read(1024 * 1024):
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                finally:
                    stream.close()
                return

            if u.path == "/api/render-state":
                plan_id = q.get("plan", [None])[0]
                if not pid or not plan_id:
                    return self._json(400, {"error": "project and plan are required"})
                if not _RENDER_PLAN_ID_RE.fullmatch(plan_id):
                    return self._json(400, {"error": "plan id is invalid"})
                try:
                    root = _safe_render_project(workspace, pid)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except FileNotFoundError:
                    return self._json(404, {"error": "not found"})
                plan_path = root / "renders" / plan_id / "plan.json"
                if not plan_path.exists() and not plan_path.is_symlink():
                    return self._json(404, {"error": "not found"})
                try:
                    plan = load_render_plan(root, plan_id)
                    if plan.project_id != pid:
                        return self._json(404, {"error": "not found"})
                    if plan.backend == "after_effects" and not self._authorize_ae_browser(pid):
                        return
                    payload = _render_state_payload(root, plan_id)
                    return self._json(200, payload)
                except (PlanConflict, CoordinatorConflict) as exc:
                    return self._json(409, {"error": str(exc)})


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
                    meta = load_meta(workspace, pid) or {}
                    return self._json(200, review_state_payload(load_project(state.root), v, scene, rep, state.job, meta.get("status")))
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

            if u.path == "/api/bboxes":
                if not pid:
                    return self._json(400, {"error": "project required"})
                state = review_state(pid, sid)
                if state is None:
                    return self._json(404, {"error": "not found"})
                try:
                    scene, _v = state.scene(ver)
                    f = int(q.get("frame", ["0"])[0])
                    f = max(0, min(scene.frames - 1, f))
                    boxes = {}
                    for el in scene.elements:
                        a, b = el.visible
                        if a <= f <= b:
                            x0, y0, x1, y1 = element_bbox(el, f)
                            boxes[el.id] = [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]
                    return self._json(200, {"size": list(scene.size), "boxes": boxes})
                except Exception as e:
                    log.exception("bboxes failed project=%s scene=%s", pid, sid)
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})

            parts = u.path.strip("/").split("/")
            if parts[:2] == ["frame", "orig"] and len(parts) == 3:
                if not pid:
                    return self._json(400, {"error": "project required"})
                state = review_state(pid, sid)
                if state is None:
                    return self._json(404, {"error": "not found"})
                try:
                    return self._send(200, state.orig_png(int(parts[2])), "image/png", cache="public, max-age=604800")
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
                    return self._send(200, state.recon_png(int(parts[2]), ver), "image/png", cache="public, max-age=604800")
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
            if u.path == "/api/ae/pairings":
                if not self._same_origin():
                    return
                if ae_relay_url is None:
                    return self._json(503, {"error": "AE relay is not configured"})
                data = self._bounded_json()
                if data is None:
                    return
                if set(data) - {"project", "capability_request"}:
                    return self._json(400, {"error": "pairing payload is invalid"})
                project_id = data.get("project")
                capability_request = data.get("capability_request", {})
                if (
                    not isinstance(project_id, str)
                    or not _PROJECT_ID_RE.fullmatch(project_id)
                    or not isinstance(capability_request, dict)
                ):
                    return self._json(400, {"error": "pairing payload is invalid"})
                try:
                    root = _safe_render_project(workspace, project_id)
                    auth = AEProjectAuth(root, project_id)
                    grant = auth.create_pairing(
                        self._controller_token(project_id),
                        capability_request,
                    )
                    active_device = auth.active_device_id()
                    if active_device is not None and not _detach_project_device(
                        root,
                        active_device,
                        reason="replacement",
                    ):
                        return self._json(
                            409,
                            {"error": "connector replacement is waiting for the active command"},
                            headers=(
                                ("Cache-Control", "no-store"),
                                (
                                    "Set-Cookie",
                                    self._controller_cookie(
                                        project_id,
                                        grant.controller_token,
                                    ),
                                ),
                            ),
                        )
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except FileNotFoundError:
                    return self._json(404, {"error": "not found"})
                except AEControllerAuthorizationError:
                    return self._json(
                        401,
                        {"error": "controller authorization failed"},
                    )
                except (AEAuthError, CoordinatorConflict) as exc:
                    return self._json(409, {"error": str(exc)})
                return self._json(
                    201,
                    {
                        "code": grant.code,
                        "expires_at": grant.expires_at,
                        "relay_url": ae_relay_url,
                    },
                    headers=(
                        ("Cache-Control", "no-store"),
                        (
                            "Set-Cookie",
                            self._controller_cookie(
                                project_id,
                                grant.controller_token,
                            ),
                        ),
                    ),
                )

            m = re.fullmatch(r"/api/render-plans/([^/]+)/approve", u.path)
            if m:
                if not self._same_origin():
                    return
                plan_id = m.group(1)
                if not _RENDER_PLAN_ID_RE.fullmatch(plan_id):
                    return self._json(400, {"error": "plan id is invalid"})
                data = self._bounded_json()
                if data is None:
                    return
                project_id = data.get("project")
                if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
                    return self._json(400, {"error": "project id is invalid"})
                try:
                    root = _safe_render_project(workspace, project_id)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except FileNotFoundError:
                    return self._json(404, {"error": "not found"})
                plan_path = root / "renders" / plan_id / "plan.json"
                if not plan_path.exists() and not plan_path.is_symlink():
                    return self._json(404, {"error": "not found"})
                try:
                    plan = load_render_plan(root, plan_id)
                except PlanConflict as exc:
                    return self._json(409, {"error": str(exc)})
                if plan.project_id != project_id:
                    return self._json(404, {"error": "not found"})
                if plan.backend == "after_effects" and not self._authorize_ae_browser(
                    project_id,
                    origin_checked=True,
                ):
                    return
                digest = data.get("digest")
                revision = data.get("revision")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    return self._json(400, {"error": "digest is invalid"})
                if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
                    return self._json(400, {"error": "revision is invalid"})
                try:
                    if plan.backend == "after_effects":
                        pending_state = load_render_plan_state(root, plan.id)
                        if pending_state.status == "approved":
                            state = approve_render_plan(
                                root,
                                plan.id,
                                digest=digest,
                                revision=revision,
                            )
                        else:
                            auth = AEProjectAuth(root, project_id)
                            with auth.authorize_controller_with_capabilities(
                                self._controller_token(project_id)
                            ) as snapshot:
                                pending_state = load_render_plan_state(root, plan.id)
                                if (
                                    pending_state.status == "awaiting_approval"
                                    and pending_state.revision == revision
                                    and digest == plan.digest
                                ):
                                    if snapshot is None:
                                        return self._json(
                                            409,
                                            {
                                                "error": (
                                                    "active published AE capabilities "
                                                    "are required"
                                                )
                                            },
                                        )
                                    manifest = snapshot.model_dump(
                                        mode="json",
                                        exclude={
                                            "capability_hash",
                                            "project_open",
                                            "timestamp",
                                        },
                                    )
                                    if (
                                        snapshot.capability_hash != plan.capability_hash
                                        or manifest != plan.capability_manifest
                                    ):
                                        return self._json(
                                            409,
                                            {
                                                "error": (
                                                    "AE capabilities changed since "
                                                    "plan creation"
                                                )
                                            },
                                        )
                                state = approve_render_plan(
                                    root,
                                    plan.id,
                                    digest=digest,
                                    revision=revision,
                                )
                    else:
                        state = approve_render_plan(
                            root,
                            plan.id,
                            digest=digest,
                            revision=revision,
                        )
                    if not state.execution_id:
                        raise PlanConflict("approved render plan has no execution id")
                    if plan.backend == "after_effects":
                        session = AECoordinator.cached(root, plan.id).start(state.execution_id)
                        return self._json(
                            202,
                            {
                                "plan": plan.model_dump(mode="json"),
                                "state": state.model_dump(mode="json"),
                                "session": session.model_dump(mode="json"),
                                "job": None,
                                "status": session.status,
                            },
                        )
                    job_id = _native_execution_job_id(plan.id, plan.digest, state.execution_id)
                    job = JOBS.find(job_id)
                    if job is None:
                        spec = prepare_native_job(root, plan.id)
                        job = JOBS.submit(
                            spec.kind,
                            spec=spec,
                            project_id=plan.project_id,
                            scene_id=plan.scene_id,
                            stage=plan.mode,
                            job_id=job_id,
                        )
                    return self._json(
                        202,
                        {
                            "plan": plan.model_dump(mode="json"),
                            "state": state.model_dump(mode="json"),
                            "job": job.to_json(),
                            "status": _render_status(state, job),
                        },
                    )
                except AEControllerAuthorizationError as exc:
                    return self._json(401, {"error": str(exc)})
                except AEAuthError as exc:
                    return self._json(409, {"error": str(exc)})
                except (PlanConflict, CoordinatorConflict) as exc:
                    return self._json(409, {"error": str(exc)})


            m = re.fullmatch(
                r"/api/ae/sessions/([A-Za-z0-9][A-Za-z0-9_.:-]{7,255})/"
                r"(stop|continue|begin-manual|sync-manual|select-checkpoint|finalize)",
                u.path,
            )
            if m:
                session_id, action = m.groups()
                if not self._same_origin():
                    return
                data = self._bounded_json()
                if data is None:
                    return
                project_id = data.get("project")
                plan_id = data.get("plan")
                revision = data.get("revision")
                if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
                    return self._json(400, {"error": "project id is invalid"})
                if not isinstance(plan_id, str) or not _RENDER_PLAN_ID_RE.fullmatch(plan_id):
                    return self._json(400, {"error": "plan id is invalid"})
                if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
                    return self._json(400, {"error": "revision is invalid"})
                if any(
                    key not in {"project", "plan", "revision", "checkpoint"}
                    for key in data
                ):
                    return self._json(400, {"error": "plan or control payload is invalid"})
                if not self._authorize_ae_browser(
                    project_id,
                    origin_checked=True,
                ):
                    return
                try:
                    root = _safe_render_project(workspace, project_id)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except FileNotFoundError:
                    return self._json(404, {"error": "not found"})
                plan_path = root / "renders" / plan_id / "plan.json"
                if not plan_path.exists() and not plan_path.is_symlink():
                    return self._json(404, {"error": "not found"})
                try:
                    plan = load_render_plan(root, plan_id)
                    if plan.project_id != project_id or plan.backend != "after_effects":
                        return self._json(404, {"error": "not found"})
                    session_path = root / "renders" / plan_id / "ae" / "session.json"
                    if not session_path.exists() and not session_path.is_symlink():
                        return self._json(404, {"error": "not found"})
                    coordinator = AECoordinator.cached(root, plan_id)
                    current = coordinator.state()
                    if current.id != session_id:
                        return self._json(404, {"error": "not found"})
                    queued_command = None
                    if action == "select-checkpoint":
                        checkpoint = data.get("checkpoint")
                        if (
                            not isinstance(checkpoint, int)
                            or isinstance(checkpoint, bool)
                            or checkpoint < 0
                        ):
                            return self._json(400, {"error": "checkpoint is invalid"})
                        current = coordinator.select_checkpoint(checkpoint, revision)
                    elif action == "sync-manual":
                        if current.revision != revision:
                            return self._json(409, {"error": "revision is stale"})
                        expected_checkpoint = max(
                            (item.index for item in current.checkpoints),
                            default=None,
                        )
                        queued_command = coordinator.enqueue_command(
                            "sync_manual",
                            {},
                            expected_state=current.status,
                            expected_checkpoint=expected_checkpoint,
                            device_id=current.device_id,
                            revision=revision,
                        )
                        current = coordinator.state()
                    else:
                        if action == "stop":
                            event = "stop"
                        elif action == "continue":
                            event = "continue"
                        elif action == "begin-manual":
                            event = "begin_manual"
                        else:
                            event = "finalize"
                        if action == "finalize" and data.get("checkpoint") is not None:
                            selected = data["checkpoint"]
                            if (
                                not isinstance(selected, int)
                                or isinstance(selected, bool)
                                or selected < 0
                            ):
                                return self._json(400, {"error": "checkpoint is invalid"})
                            current = coordinator.select_checkpoint(selected, revision)
                            revision = current.revision
                        current = coordinator.transition(event, revision)
                    render_state = load_render_plan_state(root, plan_id)
                    response = {
                        "plan": plan.model_dump(mode="json"),
                        "state": render_state.model_dump(mode="json"),
                        "session": current.model_dump(mode="json"),
                        "job": None,
                        "status": current.status,
                    }
                    if queued_command is not None:
                        response["command"] = queued_command.model_dump(mode="json")
                    return self._json(200, response)
                except (PlanConflict, CoordinatorConflict) as exc:
                    return self._json(409, {"error": str(exc)})

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
                    mode, start, end = resolve_analyze_window(meta, data, info["frames"])
                    write_meta(
                        workspace,
                        meta["id"],
                        mode=mode,
                        range=[start, end] if mode == "range" else None,
                    )
                    frames = max(1, int(end) - int(start) + 1)
                    return self._json(200, estimate(mode, frames, info["fps"], project_id=meta["id"], start=start, end=end))
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
                        create_rejected_project(workspace, title, reason)
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
                root = project_dir(workspace, project_id)
                video = root / "source.mp4"
                info = probe_video(video)
                mode, start, end = resolve_analyze_window(meta, data, info["frames"])
                # Refresh / re-entry must not demand a token after the first confirm.
                if mode == "full" and meta.get("status") != "analyzing" and not consume_token(token, project_id=project_id, mode=mode, start=start, end=end):
                    return self._json(400, {"error": "confirm required"})
                frames = max(1, int(end) - int(start) + 1)
                est = estimate(mode, frames, info["fps"], project_id=project_id, start=start, end=end)
                write_meta(
                    workspace,
                    project_id,
                    status="analyzing",
                    mode=mode,
                    range=[start, end] if mode == "range" else None,
                )
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

            if u.path == "/api/approve":
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
                    _scene, version = state.scene(data.get("v"))
                except Exception as e:
                    return self._json(400, {"error": f"{type(e).__name__}: {e}"})
                meta = write_meta(
                    workspace,
                    project_id,
                    status="approved",
                    version=version.id,
                    scene=scene_id,
                )
                log.info("approved project=%s scene=%s version=%s", project_id, scene_id, version.id)
                return self._json(200, {"project": meta, "version": version.id})

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

            if u.path == "/api/edit":
                try:
                    data = json.loads(self._read_body().decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "bad json"})
                project_id = data.get("project")
                scene_id = data.get("scene", "s1")
                prompt = (data.get("prompt") or "").strip()
                if not project_id:
                    return self._json(400, {"error": "project required"})
                if not prompt and not data.get("intent"):
                    return self._json(400, {"error": "prompt required"})
                state = review_state(project_id, scene_id)
                if state is None:
                    return self._json(404, {"error": "not found"})
                try:
                    result = run_edit(
                        state.root,
                        scene_id,
                        prompt,
                        attachment=data.get("attachment"),
                        element=data.get("element"),
                        confirm=bool(data.get("confirm")),
                        intent=data.get("intent"),
                        choices=data.get("choices"),
                        version=data.get("v"),
                    )
                except Exception as e:
                    log.exception("edit failed project=%s scene=%s", project_id, scene_id)
                    return self._json(400, {"error": f"{type(e).__name__}: {e}"})
                if result.status == "done" and result.version is not None:
                    write_meta(workspace, project_id, status="review", version=result.version.id, scene=scene_id)
                    state._preview.clear()
                    state._tex.clear()
                return self._json(200, result.to_json())

            if u.path == "/api/agent":
                if not self._same_origin():
                    return
                data = self._bounded_json()
                if data is None:
                    return
                project_id = data.get("project")
                requested_scene_id = data.get("scene", "s1")
                raw_message = data.get("message")
                message = raw_message.strip() if isinstance(raw_message, str) else ""
                if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project_id):
                    return self._json(400, {"error": "project required"})
                if not isinstance(requested_scene_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", requested_scene_id):
                    return self._json(400, {"error": "scene required"})
                if not message:
                    return self._json(400, {"error": "message required"})
                root = project_dir(workspace, project_id)
                try:
                    workspace_real = workspace.resolve()
                    root_real = root.resolve(strict=True)
                    if root.is_symlink() or root_real.parent != workspace_real or not root_real.is_dir():
                        return self._json(404, {"error": "not found"})
                except OSError:
                    return self._json(404, {"error": "not found"})
                root = root_real
                meta = load_meta(workspace, project_id)
                if meta is None:
                    return self._json(404, {"error": "not found"})
                if meta.get("id") is not None and meta.get("id") != project_id:
                    return self._json(404, {"error": "not found"})
                agent_auth = AEProjectAuth(root, project_id)
                agent_controller_token = self._controller_token(project_id)
                try:
                    controller_required = agent_auth.controller_configured()
                    controller_authorized = agent_auth.authenticate_controller(
                        agent_controller_token
                    )
                except AEAuthError:
                    controller_required = True
                    controller_authorized = False
                if controller_required and not controller_authorized:
                    return self._json(
                        401,
                        {"error": "controller authorization failed"},
                    )
                try:
                    project = load_project(root)
                    scene_id = meta.get("scene") or requested_scene_id
                    if not isinstance(scene_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scene_id):
                        raise ValueError("scene id is invalid")
                    versions = [v for v in project.versions if v.scene_file.startswith(f"scenes/{scene_id}/")]
                    if not versions:
                        raise ValueError("scene version was not found")
                    version = meta.get("version") or versions[-1].id
                    if not any(v.id == version for v in versions):
                        raise ValueError("version is not authoritative for this scene")
                except Exception as e:  # noqa: BLE001 - malformed workspace input
                    return self._json(400, {"error": f"{type(e).__name__}: {e}"})
                resolved_project_id = project_id
                resolved_scene_id = scene_id
                resolved_version = version

                def prepare_agent_render(mode: str, backend: str, direction: str | None):
                    if (
                        backend == "after_effects"
                        and not agent_auth.authenticate_controller(agent_controller_token)
                    ):
                        raise AEControllerAuthorizationError(
                            "controller authorization failed"
                        )
                    capability_hash = None
                    capability_manifest = None
                    if backend == "after_effects":
                        snapshot = agent_auth.read_capabilities()
                        if snapshot is None:
                            raise PlanConflict(
                                "active published AE capabilities are required"
                            )
                        capability_hash = snapshot.capability_hash
                        capability_manifest = snapshot.model_dump(
                            mode="json",
                            exclude={"capability_hash", "project_open", "timestamp"},
                        )
                    return create_render_plan(
                        root,
                        project_id=resolved_project_id,
                        scene_id=resolved_scene_id,
                        version_id=resolved_version,
                        backend=backend,
                        mode=mode,
                        direction=direction,
                        capability_hash=capability_hash,
                        capability_manifest=capability_manifest,
                    )

                def submit_agent_job(kind: str, args: dict, stage: str) -> dict:
                    if kind == "analyze":
                        video = root / "source.mp4"
                        info = probe_video(video)
                        mode, start, end = resolve_analyze_window(meta, args or {}, info["frames"])
                        spec = JobSpec(
                            kind="analyze",
                            args={"video": str(video), "start": int(start), "end": int(end),
                                  "out_root": str(root), "workspace": str(workspace), "project_id": resolved_project_id},
                        )
                        job = JOBS.submit("analyze", spec=spec, project_id=resolved_project_id, scene_id=resolved_scene_id, stage="frames")
                    elif kind in ("render", "export"):
                        raise ValueError("render/export plans require browser approval")
                    elif kind == "correct":
                        op = (args or {}).get("op")
                        cargs = (args or {}).get("args", {})

                        def work():
                            if op == "reassign":
                                v = corrections.reassign_id(root, resolved_scene_id, tuple(cargs["frames"]), cargs["from_id"], cargs["to_id"], note=cargs.get("note", "reassign id"))
                            elif op == "mask":
                                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
                                    t.write(base64.b64decode(cargs["mask_png_base64"]))
                                    tmp = Path(t.name)
                                try:
                                    v = corrections.set_region_mask(root, resolved_scene_id, int(cargs["frame"]), tmp, cargs["object_id"], note=cargs.get("note", "set region mask"))
                                finally:
                                    tmp.unlink(missing_ok=True)
                            elif op == "bbox":
                                v = corrections.add_bbox_prompt(root, resolved_scene_id, int(cargs["frame"]), tuple(int(x) for x in cargs["bbox"]), cargs["object_id"], note=cargs.get("note", "bbox prompt"))
                            else:
                                v = corrections.edit_text(root, resolved_scene_id, cargs["element_id"], text=cargs.get("text"),
                                                            font=FontGuess(**cargs["font"]) if cargs.get("font") else None,
                                                            note=cargs.get("note", "edit text"))
                            return {"version": v.model_dump()}

                        job = JOBS.submit("correct", fn=work, project_id=resolved_project_id, scene_id=resolved_scene_id, stage="correct")
                    else:
                        raise ValueError(f"unknown job kind {kind!r}")
                    return job.to_json()

                key = (resolved_project_id, resolved_scene_id)
                history = agent_states.setdefault(key, [])
                ctx = SessionContext(
                    root=root,
                    scene_id=resolved_scene_id,
                    version=resolved_version,
                    workspace=workspace,
                    project_id=resolved_project_id,
                    jobs=JOBS,
                    submit_job=submit_agent_job,
                    prepare_render=prepare_agent_render,
                )
                try:
                    turn = SessionAgent(agent_llm()).turn(ctx, message, history)
                except AEControllerAuthorizationError:
                    return self._json(
                        401,
                        {"error": "controller authorization failed"},
                    )
                except Exception as e:
                    log.exception("agent turn failed project=%s scene=%s", project_id, scene_id)
                    return self._json(400, {"error": f"{type(e).__name__}: {e}"})
                history.append({"role": "user", "content": message})
                history.append({"role": "assistant", "content": turn.reply})
                for res in turn.results:
                    ver = (res.get("payload") or {}).get("version")
                    if ver and ver.get("id"):
                        write_meta(workspace, project_id, status="review", version=ver["id"], scene=scene_id)
                return self._json(200, turn.to_json())

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
            match = re.fullmatch(r"/api/ae/pairings/([A-Za-z0-9_-]{1,128})", u.path)
            if match:
                project_id = match.group(1)
                if not self._authorize_ae_browser(project_id):
                    return
                token = self._controller_token(project_id)
                try:
                    root = _safe_render_project(workspace, project_id)
                    auth = AEProjectAuth(root, project_id)
                    device_id = auth.begin_unpair(token)
                    if device_id is not None and not _detach_project_device(
                        root,
                        device_id,
                        reason="unpair",
                    ):
                        return self._json(
                            409,
                            {"error": "connector unpair is waiting for the active command"},
                            headers=(("Cache-Control", "no-store"),),
                        )
                    auth.finish_unpair(token, device_id)
                except FileNotFoundError:
                    return self._json(401, {"error": "controller authorization failed"})
                except (AEAuthError, CoordinatorConflict) as exc:
                    return self._json(409, {"error": str(exc)})
                return self._json(
                    200,
                    {"unpaired": True},
                    headers=(
                        ("Cache-Control", "no-store"),
                        (
                            "Set-Cookie",
                            self._controller_cookie(project_id, "", clear=True),
                        ),
                    ),
                )
            return self._json(404, {"error": "not found"})
        def do_PUT(self):
            return self._json(404, {"error": "not found"})

        def do_PATCH(self):
            return self._json(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), H)
