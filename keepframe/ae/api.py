"""Browser and paired-extension routes on Keepframe's existing HTTP server."""

import errno
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote

from .devices import Devices
from .jobs import Jobs
from .spec import comp_spec, prepare_footage, spec_asset_paths, spec_json, spec_level
from .verify import sample_frames, verify
from ..log import get
from ..ir.store import load_project, load_scene, scene_dir
from ..web.bodies import LengthError, content_length


EXTENSION_PROTOCOL_MAJOR = 1
UPLOAD_IDLE_TIMEOUT = 60
STATIC = Path(__file__).resolve().parent / "static"
_BROWSER = {"/api/ae/codes", "/api/ae/devices", "/api/ae/send", "/api/ae/state",
            "/api/ae/verify", "/api/ae/verify-image"}
_VERIFY_IMAGE = re.compile(r"f[0-9]{4,6}_(ae|kf|diff)\.png")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_LEVEL = re.compile(r"[1-9][0-9]{0,2}")
_UPDATE = {"error": "update the Keepframe extension", "download": "/ae/keepframe.zxp"}
_OUTDATED = "update the Keepframe extension: this scene uses video, gradients or text styles it cannot draw"
_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
log = get("keepframe.ae")


def _error(handler, status, message):
    handler._json(status, {"error": message})


def _length(handler, cap, *, required=False):
    try:
        return content_length(handler.headers, cap, required=required)
    except LengthError as exc:
        _error(handler, exc.status, str(exc))
        return None


def _bad_constant(value):
    raise ValueError("bad json")


def _read_json(handler, cap=1024 * 1024):
    length = _length(handler, cap)
    if length is None:
        return None
    try:
        body = handler.rfile.read(length)
        value = json.loads(body, parse_constant=_bad_constant)
        if len(body) != length or not isinstance(value, dict):
            raise ValueError
    except (ValueError, RecursionError, OSError):
        raise ValueError("bad json") from None
    return value


def _verification_error(exc):
    if isinstance(exc, FileNotFoundError):
        return f"file not found: {Path(exc.filename).name}" if exc.filename else "file not found"
    if isinstance(exc, ValueError):
        return str(exc)
    log.exception("After Effects verification failed")
    return f"verification failed: {type(exc).__name__}"


class AERoutes:
    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.devices = Devices(workspace)
        self.jobs = Jobs(workspace)
        self._progress = {}
        self._progress_lock = threading.Lock()
        self._upload_lock = threading.Lock()
        self._uploaded = {}
        self._uploading = {}
        # ponytail: one renderer at a time; use a bounded worker pool if throughput matters.
        self._verify_lock = threading.Lock()
        self._verify_state_lock = threading.Lock()
        self._verifying = {}
        self._verify_errors = {}
        self._stop = threading.Event()
        self._sweeper = threading.Thread(target=self._sweep, name="keepframe-ae-sweeper", daemon=True)
        self._sweeper.start()

    def _sweep(self):
        while not self._stop.wait(10):
            try:
                with self._progress_lock:
                    failed = self.jobs.sweep({row["id"]: row["last_seen"] for row in self.devices.list()
                                              if row["last_seen"] is not None})
                    for job in failed:
                        self._progress.pop(job.id, None)
                        with self._upload_lock:
                            self._uploaded.pop(job.id, None)
                            self._uploading.pop(job.id, None)
            except (OSError, ValueError):
                log.exception("After Effects job sweep failed")

    def close(self) -> None:
        with self._verify_state_lock:
            self._stop.set()
        self._sweeper.join()

    def _extension(self, handler, pair=False):
        version = handler.headers.get("X-Keepframe-Extension", "")
        match = _SEMVER.fullmatch(version)
        if match is None or match[1] != str(EXTENSION_PROTOCOL_MAJOR):
            handler._json(426, _UPDATE)
            return None
        if pair:
            return ""
        auth = handler.headers.get("Authorization", "")
        device = self.devices.authenticate(auth[7:]) if auth.startswith("Bearer ") else None
        if device is None:
            _error(handler, 401, "device is not paired; pair again from the Keepframe web page")
            return None
        self.devices.seen(device.id)
        return device.id

    def _handle(self, handler, u, dispatch):
        if not u.path.startswith("/api/ae/") and u.path != "/ae/keepframe.zxp":
            return False
        try:
            try:
                device = None
                if u.path in _BROWSER or u.path.startswith("/api/ae/devices/"):
                    # Read-only browser GETs use the Host guard; responses have no CORS headers.
                    if handler.command == "GET" and u.path in {"/api/ae/devices", "/api/ae/state", "/api/ae/verify-image"}:
                        if not handler._host_allowed():
                            return True
                    elif not handler._same_origin():
                        return True
                elif u.path != "/ae/keepframe.zxp":
                    device = self._extension(handler, pair=u.path == "/api/ae/pair")
                    if device is None:
                        return True
                dispatch(handler, u, device)
            except FileNotFoundError as exc:
                reason = f"file not found: {Path(exc.filename).name}" if exc.filename else str(exc) or "not found"
                log.warning("After Effects %s %s: %s", handler.command, u.path, reason)
                _error(handler, 404, reason)
            except ValueError as exc:
                log.warning("After Effects %s %s: %s", handler.command, u.path, exc)
                _error(handler, 400, str(exc))
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                raise
            except Exception:
                log.exception("After Effects request failed")
                _error(handler, 500, "After Effects request failed")
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            log.info("client closed the connection during %s %s", handler.command, u.path)
        return True

    def handle_get(self, handler, u) -> bool:
        return self._handle(handler, u, self._get)

    def handle_post(self, handler, u) -> bool:
        return self._handle(handler, u, self._post)

    def handle_put(self, handler, u) -> bool:
        return self._handle(handler, u, self._put)

    def handle_delete(self, handler, u) -> bool:
        return self._handle(handler, u, self._delete)

    def _job(self, job_id, device):
        job = self.jobs.get(job_id)
        if job is None or job.device != device:
            raise FileNotFoundError
        return job

    @staticmethod
    def _panel_level(handler):
        """What the panel can draw (spec.SPEC_LEVEL); panels from before Task 14 send no level."""
        level = handler.headers.get("X-Keepframe-Spec-Level", "")
        return int(level) if _LEVEL.fullmatch(level) else 1

    def _outdated(self, handler, job):
        """Fail the job with a readable reason, and tell the panel to update (it stops and offers the download)."""
        with self._progress_lock:
            try:
                self.jobs.finish(job.id, False, error=_OUTDATED)
            except ValueError:   # no longer running
                pass
            self._progress.pop(job.id, None)
        handler._json(426, _UPDATE)

    def _scene(self, project_id, scene_id, version_id=None):
        identities = (project_id, scene_id) if version_id is None else (project_id, scene_id, version_id)
        if any(not isinstance(value, str) or not _ID.fullmatch(value) for value in identities):
            raise FileNotFoundError
        root = self.workspace / project_id
        if not root.is_dir() or root.is_symlink() or root.resolve().parent != self.workspace.resolve():
            raise FileNotFoundError
        project = load_project(root)
        directory = scene_dir(root, scene_id)
        if not any(ref.id == scene_id for ref in project.scenes) or not directory.resolve().is_relative_to(root.resolve()):
            raise FileNotFoundError
        versions = [v for v in project.versions if v.scene_file.startswith(f"scenes/{scene_id}/")]
        version = (versions[-1] if versions else None) if version_id is None else next(
            (v for v in versions if v.id == version_id), None)
        if version is None or not _ID.fullmatch(version.id):
            raise FileNotFoundError
        path = root / version.scene_file
        if not path.resolve().is_relative_to(directory.resolve()):
            raise FileNotFoundError
        scene = load_scene(path)
        return scene, directory, version.id

    def _stream(self, handler, path, *, headers=(), content_type=None):
        with path.open("rb") as stream:
            handler.send_response(200)
            handler.send_header("content-type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            handler.send_header("content-length", str(path.stat().st_size))
            for name, value in headers:
                handler.send_header(name, value)
            handler.end_headers()
            shutil.copyfileobj(stream, handler.wfile, length=1024 * 1024)

    def _verify_directory(self, project, scene, version):
        directory = self.workspace / project / "ae" / scene / version / "verify"
        if not directory.resolve().is_relative_to((self.workspace / project).resolve()):
            raise FileNotFoundError
        return directory

    def _verify_state(self, project, scene):
        job = self.jobs.latest(project, scene, "render_frames")
        if job is None:
            return None
        state = {"job": job.id, "version": job.version}
        if job.state in {"queued", "running"}:
            return {**state, "state": "rendering"}
        if job.state == "failed":
            return {**state, "state": "failed", "error": job.error}
        with self._verify_state_lock:
            if job.id in self._verifying:
                return {**state, "state": "verifying"}
            if job.id in self._verify_errors:
                return {**state, "state": "failed", "error": self._verify_errors[job.id]}
            path = self._verify_directory(project, scene, job.version) / "verify.json"
            if path.is_file():
                if path.resolve().parent != path.parent.resolve():
                    raise FileNotFoundError
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(report, dict):
                        raise ValueError("invalid saved report")
                except (OSError, ValueError):
                    message = "the saved report could not be read — verify again"
                    log.warning("After Effects %s", message)
                    return {**state, "state": "failed", "error": message}
                if report.get("job") == job.id:
                    return {**report, **state, "state": "failed" if "error" in report else "done"}
        return {**state, "state": "interrupted"}

    def _start_verification(self, job, ae_frames):
        snapshot = None
        try:
            # Uploads replace the shared version files; retain this job's bytes before freeing the device.
            snapshot = tempfile.TemporaryDirectory(
                dir=self._verify_directory(job.project, job.scene, job.version).parent, prefix=".verify-frames-")
            frames = {f: Path(snapshot.name) / path.name for f, path in ae_frames.items()}
            for frame, path in ae_frames.items():
                shutil.copyfile(path, frames[frame])
            with self._verify_state_lock:
                self._verifying[job.id] = job.version
            threading.Thread(target=self._run_verification, args=(job, frames, snapshot),
                             name=f"keepframe-ae-verify-{job.id}", daemon=True).start()
        except Exception as exc:
            with self._verify_state_lock:
                self._verifying.pop(job.id, None)
                self._verify_errors[job.id] = _verification_error(exc)
            if snapshot is not None:
                snapshot.cleanup()

    def _run_verification(self, job, ae_frames, snapshot):
        report = {}
        log.info("After Effects verification start project=%s scene=%s version=%s job=%s",
                 job.project, job.scene, job.version, job.id)
        try:
            with snapshot, self._verify_lock:
                if self._stop.is_set():
                    return
                if self.jobs.latest(job.project, job.scene, "render_frames", version=job.version).id != job.id:
                    return
                # Stage images too: a verifier finishing after close() must not publish any files.
                with tempfile.TemporaryDirectory(prefix="keepframe-ae-report-") as temporary:
                    staging = Path(temporary)
                    directory = None
                    try:
                        directory = self._verify_directory(job.project, job.scene, job.version)
                        with self._verify_state_lock:
                            if self._stop.is_set():
                                return
                            directory.mkdir(parents=True, exist_ok=True)
                            for path in directory.iterdir():
                                if path.name == "verify.json" or re.fullmatch(r"f.*_(ae|kf|diff)\.png", path.name):
                                    path.unlink()
                        scene, scene_dir, version = self._scene(job.project, job.scene, job.version)
                        spec = comp_spec(scene, scene_dir, project=job.project, scene_id=job.scene,
                                         version=version, fonts=self.devices.fonts(job.device), derive=False)
                        elements = {el.id: el for el in scene.elements}
                        masked = {}
                        for layer in spec["layers"]:
                            eid = layer["id"].removeprefix("kf:")
                            if layer["kind"] != "text" or eid.endswith("~text") or eid not in elements:
                                continue
                            font = layer["source"]["font"]
                            if font["substituted"] and elements[eid].canonical.font is not None:
                                masked[eid] = {"name": layer["name"], "font": font["family"],
                                               "requested": elements[eid].canonical.font.family_guess}
                        report = verify(scene, scene_dir, ae_frames, staging, masked=masked)
                    except Exception as exc:
                        report = {"error": _verification_error(exc)}
                    report.update(job=job.id, version=job.version, finished=time.time())
                    with self._verify_state_lock:
                        if self._stop.is_set():
                            return
                        if self.jobs.latest(job.project, job.scene, "render_frames", version=job.version).id != job.id:
                            return
                        try:
                            if directory is None:
                                raise FileNotFoundError
                            directory.mkdir(parents=True, exist_ok=True)
                            if "error" not in report:
                                for path in staging.iterdir():
                                    if _VERIFY_IMAGE.fullmatch(path.name):
                                        shutil.copyfile(path, directory / path.name)
                            fd, path = tempfile.mkstemp(dir=directory, prefix=".verify-", suffix=".tmp")
                            try:
                                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                                    json.dump(report, stream, allow_nan=False)
                                os.replace(path, directory / "verify.json")
                            finally:
                                Path(path).unlink(missing_ok=True)
                        except Exception as exc:
                            self._verify_errors[job.id] = _verification_error(exc)
        except Exception as exc:
            with self._verify_state_lock:
                if not self._stop.is_set():
                    self._verify_errors[job.id] = _verification_error(exc)
        finally:
            with self._verify_state_lock:
                self._verifying.pop(job.id, None)
            log.info("After Effects verification finish project=%s scene=%s version=%s mean=%s passed=%s",
                     job.project, job.scene, job.version, report.get("mean"), report.get("passed"))

    def _get(self, handler, u, device):
        query = parse_qs(u.query)
        if u.path == "/ae/keepframe.zxp":
            path = STATIC / "keepframe.zxp"
            if not path.is_file():
                _error(handler, 404, "extension not built; run scripts/build_zxp.sh")
            else:
                self._stream(handler, path, content_type="application/zip", headers=(
                    ("content-disposition", 'attachment; filename="keepframe.zxp"'),))
            return
        if u.path == "/api/ae/devices":
            handler._json(200, {"devices": self.devices.list()})
            return
        if u.path == "/api/ae/state":
            project, scene = query.get("project", [None])[0], query.get("scene", [None])[0]
            self._scene(project, scene)
            with self._progress_lock:
                state = self.jobs.state(project, scene)
                state["progress"] = {job["id"]: self._progress[job["id"]] for job in state["jobs"]
                                     if job["state"] == "running" and job["id"] in self._progress}
                state["verify"] = self._verify_state(project, scene)
            handler._json(200, {**state, "devices": self.devices.list()})
            return
        if u.path == "/api/ae/verify-image":
            project, scene = query.get("project", [None])[0], query.get("scene", [None])[0]
            version, name = query.get("version", [None])[0], query.get("name", [""])[0]
            if version is None or _VERIFY_IMAGE.fullmatch(name) is None:
                raise FileNotFoundError
            self._scene(project, scene, version)
            directory = self._verify_directory(project, scene, version)
            path = directory / name
            if path.resolve().parent != directory.resolve():
                raise FileNotFoundError
            self._stream(handler, path, content_type="image/png")
            return
        if u.path == "/api/ae/next":
            wait = float(query.get("wait", [25])[0])
            if not math.isfinite(wait) or not 0 <= wait <= 30:
                raise ValueError("invalid wait")
            saved = handler.headers.get("X-Keepframe-Project-Saved", "0")
            if saved not in {"0", "1"}:
                raise ValueError("invalid project_saved")
            name = handler.headers.get("X-Keepframe-Project")
            self.devices.touch(device, {"project_name": unquote(name) if name is not None else None,
                                        "project_saved": saved == "1"})
            with self._progress_lock:
                for abandoned in self.jobs.abandon(device, (
                        "the Keepframe panel lost this job (it asked for new work); send again")):
                    self._progress.pop(abandoned.id, None)
                    with self._upload_lock:
                        self._uploaded.pop(abandoned.id, None)
                        self._uploading.pop(abandoned.id, None)
            job = self.jobs.next(device, wait)
            if job is None:
                handler._send(204, b"", "application/json")
            else:
                handler._json(200, {"job": job.to_dict()})
            return
        match = re.fullmatch(r"/api/ae/jobs/([^/]+)/(spec|assets/(.+))", u.path)
        if match:
            job = self._job(match[1], device)
            if match[2] == "spec" and job.kind != "sync":
                raise FileNotFoundError
            scene, directory, version = self._scene(job.project, job.scene, job.version)
            level = self._panel_level(handler)
            # Footage is derived in the background (started when the job was sent), never inside this request:
            # the panel asks again on 202. An older panel cannot draw footage at all.
            if match[2] == "spec" and prepare_footage(scene, directory):
                if level < 2:
                    self._outdated(handler, job)
                else:
                    handler._json(202, {"preparing": True})
                return
            spec = comp_spec(scene, directory, project=job.project, scene_id=job.scene,
                             version=version, fonts=self.devices.fonts(device), derive=False)
            if match[2] == "spec":
                if spec_level(spec) > level:
                    self._outdated(handler, job)
                else:
                    handler._send(200, spec_json(spec).encode("utf-8"), "application/json")
            else:
                name = unquote(match[3])
                if ".." in name or "/" in name or "\\" in name or Path(name).is_absolute():
                    raise FileNotFoundError
                asset = next((asset for asset in spec["assets"] if asset["name"] == name), None)
                if asset is None:
                    raise FileNotFoundError
                self._stream(handler, spec_asset_paths(scene, directory, derive=False)[name],
                             content_type=mimetypes.guess_type(name)[0], headers=(("X-Keepframe-Sha256", asset["sha256"]),))
            return
        _error(handler, 404, "not found")

    def _post(self, handler, u, device):
        if u.path == "/api/ae/codes":
            code, expires_at = self.devices.create_code()
            handler._json(200, {"code": code, "expires_at": expires_at})
            return
        match = re.fullmatch(r"/api/ae/jobs/([^/]+)/(result|progress)", u.path)
        if match:
            job = self._job(match[1], device)
            invalid = None
            try:
                data = _read_json(handler)
            except ValueError as exc:
                if match[2] != "result":
                    raise
                data, invalid = {}, str(exc)
            if data is None:
                return
            with self._progress_lock:
                if self._job(job.id, device).state != "running":
                    _error(handler, 409, "job must be running")
                    return
                if match[2] == "progress":
                    stage, done, total = data.get("stage"), data.get("done"), data.get("total")
                    if (not isinstance(stage, str) or not stage or len(stage) > 256
                            or type(done) is not int or type(total) is not int or not 0 <= done <= total):
                        raise ValueError("invalid progress")
                    self._progress[job.id] = {"stage": stage, "done": done, "total": total}
                    handler._send(204, b"", "application/json")
                else:
                    try:
                        if invalid is not None:
                            raise ValueError(invalid)
                        error = data.get("error")
                        if data.get("line") is not None:
                            if not isinstance(error, str) or type(data["line"]) is not int:
                                raise ValueError("invalid error or line")
                            error = f'{error} (line {data["line"]})'[:2000]
                        ok, result = data.get("ok"), data.get("result")
                        ae_frames = {}
                        if job.kind == "render_frames" and ok is True:
                            if not isinstance(result, dict):
                                raise ValueError("invalid result: expected a JSON dictionary")
                            frames = result.get("frames")
                            if not isinstance(frames, list) or any(type(f) is not int for f in frames):
                                raise ValueError("invalid frames: expected a list of integers")
                            expected = set(job.params.get("frames", []))
                            with self._upload_lock:
                                uploaded = self._uploaded.get(job.id, set()).copy()
                            missing = (expected ^ set(frames)) | {
                                f for f in expected if f"frame_{f:04d}.png" not in uploaded}
                            if missing:
                                ok, error = False, ("frames missing from AE: " + ", ".join(map(str, sorted(missing))))[:2000]
                            else:
                                directory = self.workspace / job.project / "ae" / job.scene / job.version
                                ae_frames = {f: directory / f"frame_{f:04d}.png" for f in expected}
                        job = self.jobs.finish(job.id, ok, result, error)
                        if job.kind == "render_frames" and job.state == "done":
                            self._start_verification(job, ae_frames)
                    except ValueError as exc:
                        if str(exc) == "job must be running":
                            _error(handler, 409, str(exc))
                            return
                        invalid = str(exc)
                        job = self.jobs.finish(job.id, False, error=(
                            "invalid result from the extension: " + invalid)[:2000])
                    self._progress.pop(job.id, None)
                    with self._upload_lock:
                        self._uploaded.pop(job.id, None)
                        self._uploading.pop(job.id, None)
                    if invalid is not None:
                        _error(handler, 400, invalid)
                    else:
                        handler._json(200, {"job": job.to_dict()})
            return
        if u.path not in {"/api/ae/pair", "/api/ae/info", "/api/ae/send", "/api/ae/verify"}:
            _error(handler, 404, "not found")
            return
        data = _read_json(handler, 8 * 1024 * 1024)
        if data is None:
            return
        if u.path == "/api/ae/pair":
            previous = data.get("previous_device_id")
            if previous is not None and (not isinstance(previous, str) or not re.fullmatch(r"d_[0-9a-f]{12}", previous)):
                raise ValueError("invalid previous_device_id")
            paired = self.devices.pair(data.get("code"), data.get("info"))
            if paired is None:
                _error(handler, 401, "pairing code is invalid or expired")
            else:
                if previous is not None:
                    self._revoke(previous)
                handler._json(200, {"device_id": paired[0], "token": paired[1], "server_name": socket.gethostname()})
        elif u.path == "/api/ae/info":
            self.devices.update_info(device, data.get("info"))
            handler._send(204, b"", "application/json")
        else:
            scene, directory, version = self._scene(data.get("project"), data.get("scene"), data.get("version"))
            devices = self.devices.list()
            if data.get("device") is not None:
                device = next((row["id"] for row in devices if row["id"] == data["device"]), None)
                if device is None:
                    raise FileNotFoundError
            else:
                connected = [row for row in devices if row["connected"]]
                if not connected:
                    _error(handler, 409, "no connected After Effects")
                    return
                device = max(connected, key=lambda row: row["last_seen"])["id"]
            kind, params = "sync", {"force": bool(data.get("force"))}
            if u.path == "/api/ae/verify":
                kind, params = "render_frames", {"frames": sample_frames(scene.frames),
                                                 "tag": f"keepframe:{data['project']}/{data['scene']}"}
            job = self.jobs.enqueue(device, kind, data["project"], data["scene"], version, params=params)
            if kind == "sync":
                prepare_footage(scene, directory)   # AE footage for the scene's videos, in the background
            handler._json(202, {"job": job.to_dict()})

    def _put(self, handler, u, device):
        match = re.fullmatch(r"/api/ae/jobs/([^/]+)/files/(.+)", u.path)
        if match is None:
            raise FileNotFoundError
        job, name = self._job(match[1], device), unquote(match[2])
        allowed = {"render_frames": (r"frame_[0-9]{4,6}\.png", 25 * 1024 * 1024),
                   "render_final": (r"final\.mp4", 4 * 1024**3), "package": (r"project\.zip", 4 * 1024**3)}
        if job.kind not in allowed or not re.fullmatch(allowed[job.kind][0], name):
            raise FileNotFoundError
        if job.state != "running":
            _error(handler, 409, "job must be running")
            return
        length = _length(handler, allowed[job.kind][1], required=True)
        if length is None:
            return
        self._scene(job.project, job.scene, job.version)
        directory = self.workspace / job.project / "ae" / job.scene / job.version
        if not directory.resolve().is_relative_to((self.workspace / job.project).resolve()):
            raise FileNotFoundError
        with self._upload_lock:
            if self._job(job.id, device).state != "running":
                _error(handler, 409, "job must be running")
                return
            uploaded = self._uploaded.get(job.id, set())
            uploading = self._uploading.get(job.id, set())
            if name in uploading:
                _error(handler, 409, "file is already uploading")
                return
            if job.kind == "render_frames" and name not in uploaded and len(uploaded | uploading) >= 16:
                _error(handler, 413, "at most 16 frames per job")
                return
            self._uploading.setdefault(job.id, set()).add(name)
        temporary = None
        status = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, path = tempfile.mkstemp(dir=directory, prefix=f".{name}.part-")
            temporary = Path(path)
            with os.fdopen(fd, "wb") as stream:
                previous_timeout = handler.connection.gettimeout()
                try:
                    handler.connection.settimeout(UPLOAD_IDLE_TIMEOUT)
                    remaining = length
                    while remaining:
                        try:
                            # read1 returns arriving bytes even when a slow upload never fills a chunk.
                            chunk = handler.rfile.read1(min(remaining, 1024 * 1024))
                        except OSError as exc:
                            raise ValueError("upload failed or incomplete") from exc
                        if not chunk:
                            raise ValueError("incomplete upload")
                        self.devices.seen(device)
                        stream.write(chunk)
                        remaining -= len(chunk)
                finally:
                    handler.connection.settimeout(previous_timeout)
            with self._progress_lock, self._upload_lock:
                if self._job(job.id, device).state != "running":
                    raise ValueError("job must be running")
                os.replace(temporary, directory / name)
                self._uploaded.setdefault(job.id, set()).add(name)
        except Exception as exc:
            log.error("After Effects upload failed: %s", type(exc).__name__)
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                log.error("After Effects upload cleanup failed: %s", type(cleanup_error).__name__)
                exc = cleanup_error
            status = 400 if isinstance(exc, ValueError) else 500
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                status = 507
            reason = (exc.strerror or type(exc).__name__) if isinstance(exc, OSError) else (
                str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            message = f"upload failed: {reason}"
        finally:
            with self._upload_lock:
                uploading = self._uploading.get(job.id)
                if uploading is not None:
                    uploading.discard(name)
                    if not uploading:
                        self._uploading.pop(job.id, None)
        if status is not None:
            _error(handler, status, message)
            return
        handler._send(204, b"", "application/json")

    def _revoke(self, device):
        if not self.devices.revoke(device):
            return False
        with self._progress_lock, self._upload_lock:
            for job in self.jobs.abandon(device, "After Effects was disconnected from this server", queued=True):
                self._progress.pop(job.id, None)
                self._uploaded.pop(job.id, None)
                self._uploading.pop(job.id, None)
        return True

    def _delete(self, handler, u, device):
        match = re.fullmatch(r"/api/ae/devices/([^/]+)", u.path)
        if match and self._revoke(match[1]):
            handler._send(204, b"", "application/json")
        else:
            _error(handler, 404, "not found")
