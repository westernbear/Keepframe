from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
import threading
import time
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
from keepframe.analyze.constraints import KEEP_PRESETS, apply_keep_preset
from keepframe.analyze.device import gpu_status
from keepframe.analyze.shots import boundary_digest as make_boundary_digest, scene_layout, validate_scenes
from keepframe.analyze.video import read_frames
from keepframe.ir.schema import FontGuess
from keepframe.ir.store import approve_scene, current_scene, load_project, load_scene, new_version, scene_dir
from keepframe.ir.tracks import element_bbox
from keepframe.jobs import Job, JobSpec, JobStore
from keepframe.log import configure, get
from keepframe.review import corrections
from keepframe.web.estimate import consume_token, estimate, probe_video
from keepframe.web.liveaction import looks_live_action
from keepframe.web.demo import ensure_demo_project
from keepframe.edit.agent import edit as run_edit
from keepframe.render.native import prepare_native_job
from keepframe.render.lottie import prepare_lottie_job
from keepframe.render.plan import (
    PlanConflict,
    approve_render_plan,
    create_render_plan,
    load_render_plan,
    load_render_plan_state,
)
from keepframe.session import (
    SessionAgent,
    SessionContext,
    UIContextError,
    append_turn,
    llm_history,
    make_llm,
    scene_lock as agent_scene_lock,
    session_page,
    validate_ui_context,
)
from keepframe.session.store import MAX_HISTORY_BYTES
from keepframe.after_effects.workflow import AEWorkflowService
from keepframe.after_effects.auth import (
    AEAuthError,
    AEControllerAuthorizationError,
    AEProjectAuth,
    controller_cookie_name,
)
from keepframe.after_effects.coordinator import AECoordinator, CoordinatorConflict
from keepframe.session.provider import load_llm_settings
from keepframe.after_effects.planning import AERenderDraft, prepare_ae_render_plan
from keepframe.web.workspace import (
    create_project,
    create_rejected_project,
    list_projects,
    load_meta,
    project_dir,
    write_meta,
)

log = get("keepframe.web")

CORRECTION_OPS = {"reassign", "mask", "bbox", "text"}

JOBS = JobStore()
_FINAL_PLAN_LOCK = threading.RLock()
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

_NATIVE_ARTIFACTS = {
    "mp4": ("render.mp4", "MP4", "video/mp4"),
    "zip": ("project.zip", "Project ZIP", "application/zip"),
}


def _native_artifact_path(root: Path, plan, job: Job | None, kind: str) -> Path:
    if (
        job is None
        or job.status != "done"
        or not isinstance(job.result, dict)
        or kind not in _NATIVE_ARTIFACTS
        or kind == "zip" and plan.mode != "final"
    ):
        raise FileNotFoundError(kind)
    filename = _NATIVE_ARTIFACTS[kind][0]
    output = root / "renders" / plan.id / "native" / "output"
    artifact = output / filename
    reported = job.result.get(kind)
    try:
        if (
            not isinstance(reported, str)
            or Path(reported).resolve(strict=True) != artifact
            or artifact.resolve(strict=True) != artifact
            or not artifact.is_file()
        ):
            raise FileNotFoundError(kind)
    except OSError as exc:
        raise FileNotFoundError(kind) from exc
    return artifact


def _native_artifacts(root: Path, plan, job: Job | None) -> list[dict[str, str]]:
    artifacts = []
    for kind, (_, label, _) in _NATIVE_ARTIFACTS.items():
        try:
            _native_artifact_path(root, plan, job, kind)
        except FileNotFoundError:
            continue
        artifacts.append({"kind": kind, "label": label})
    return artifacts


def _lottie_artifact_path(root: Path, plan, job: Job | None) -> Path:
    if job is None or job.status != "done" or not isinstance(job.result, dict):
        raise FileNotFoundError("animation")
    artifact = root / "renders" / plan.id / "lottie" / "animation.json"
    reported = job.result.get("animation")
    try:
        if not isinstance(reported, str) or Path(reported).resolve(strict=True) != artifact or not artifact.is_file():
            raise FileNotFoundError("animation")
    except OSError as exc:
        raise FileNotFoundError("animation") from exc
    return artifact


def _local_artifacts(root: Path, plan, job: Job | None) -> list[dict[str, str]]:
    if plan.backend == "lottie":
        try:
            _lottie_artifact_path(root, plan, job)
        except FileNotFoundError:
            return []
        return [{"kind": "animation", "label": "Lottie JSON"}]
    return _native_artifacts(root, plan, job)


def _ae_execution_plan(root: Path, plan):
    if plan.mode != "final":
        return plan
    if plan.predecessor_id is None or plan.predecessor_digest is None:
        raise PlanConflict("final AE plan is missing its preview predecessor")
    predecessor = load_render_plan(root, plan.predecessor_id)
    if (
        predecessor.digest != plan.predecessor_digest
        or predecessor.project_id != plan.project_id
        or predecessor.backend != "after_effects"
        or predecessor.mode != "preview"
    ):
        raise PlanConflict("final AE plan predecessor is invalid")
    return predecessor


def _render_state_payload(root: Path, plan) -> dict:
    state = load_render_plan_state(root, plan.id)
    if plan.backend == "after_effects":
        execution_plan = _ae_execution_plan(root, plan)
        session = None
        try:
            session = AECoordinator.cached(root, execution_plan.id).state()
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
        if state.execution_id
        else None
    )
    job = JOBS.find(job_id) if job_id else None
    return {
        "plan": plan.model_dump(mode="json"),
        "state": state.model_dump(mode="json"),
        "job": job.to_json() if job is not None else None,
        "status": _render_status(state, job),
        "artifacts": _local_artifacts(root, plan, job),
    }


def _equivalent_ae_successor(
    candidate,
    predecessor,
    *,
    checkpoint: int,
    checkpoint_digest: str,
) -> bool:
    return (
        candidate.backend == "after_effects"
        and candidate.mode == "final"
        and candidate.project_id == predecessor.project_id
        and candidate.scene_id == predecessor.scene_id
        and candidate.version_id == predecessor.version_id
        and candidate.direction == predecessor.direction
        and candidate.scene_sha256 == predecessor.scene_sha256
        and candidate.assets == predecessor.assets
        and candidate.locked_targets == predecessor.locked_targets
        and candidate.permitted_operations == predecessor.permitted_operations
        and candidate.effect_schemas == predecessor.effect_schemas
        and candidate.capability_hash == predecessor.capability_hash
        and candidate.capability_manifest == predecessor.capability_manifest
        and candidate.substitutions == predecessor.substitutions
        and candidate.substitutions_acknowledged
        == predecessor.substitutions_acknowledged
        and candidate.predecessor_id == predecessor.id
        and candidate.predecessor_digest == predecessor.digest
        and candidate.predecessor_checkpoint == checkpoint
        and candidate.predecessor_checkpoint_digest == checkpoint_digest
    )


def prepare_ae_final_successor(
    root: Path,
    *,
    project_id: str,
    scene_id: str,
    version_id: str,
    predecessor_id: str,
    predecessor_digest: str,
    predecessor_checkpoint: int,
    predecessor_checkpoint_digest: str | None = None,
    direction: str | None = None,
    capabilities,
):
    """Create or reuse one final AE plan for a selected preview checkpoint."""
    if (
        not isinstance(predecessor_checkpoint, int)
        or isinstance(predecessor_checkpoint, bool)
        or predecessor_checkpoint < 0
    ):
        raise PlanConflict("predecessor checkpoint is invalid")
    root = Path(root)
    with _FINAL_PLAN_LOCK:
        predecessor = load_render_plan(root, predecessor_id)
        if (
            predecessor.project_id != project_id
            or predecessor.scene_id != scene_id
            or predecessor.version_id != version_id
            or predecessor.backend != "after_effects"
            or predecessor.mode != "preview"
            or predecessor.digest != predecessor_digest
        ):
            raise PlanConflict("predecessor does not match the authoritative AE preview")
        if direction is not None and direction != predecessor.direction:
            raise PlanConflict("predecessor direction changed")
        meta = load_meta(root.parent, project_id)
        if (
            not isinstance(meta, dict)
            or meta.get("status") != "approved"
            or meta.get("version") != version_id
        ):
            raise PlanConflict("final render requires the approved version")
        try:
            predecessor_coordinator = AECoordinator.cached(root, predecessor.id)
            predecessor_session = predecessor_coordinator.state()
        except CoordinatorConflict as exc:
            raise PlanConflict("predecessor coordinator is unavailable") from exc
        if not predecessor_session.baseline_complete:
            raise PlanConflict("predecessor baseline is incomplete")
        if predecessor_session.selected_checkpoint != predecessor_checkpoint:
            raise PlanConflict("predecessor checkpoint is not selected")
        checkpoint = next(
            (
                item
                for item in predecessor_session.checkpoints
                if item.index == predecessor_checkpoint
            ),
            None,
        )
        if checkpoint is None or not checkpoint.passed or not checkpoint.lineage_valid:
            raise PlanConflict("predecessor checkpoint did not pass current lineage verification")
        context_digest = checkpoint.context_digest
        if not isinstance(context_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", context_digest):
            raise PlanConflict("predecessor checkpoint context is unavailable")
        if (
            predecessor_checkpoint_digest is not None
            and predecessor_checkpoint_digest != context_digest
        ):
            raise PlanConflict("predecessor checkpoint context digest does not match")
        if capabilities is None:
            raise PlanConflict("active published AE capabilities are required")
        try:
            from keepframe.after_effects.models import AECapabilities

            if not isinstance(capabilities, AECapabilities):
                capabilities = AECapabilities.model_validate(capabilities)
        except (TypeError, ValueError) as exc:
            raise PlanConflict("AE capabilities are invalid") from exc
        if (
            capabilities.capability_hash != predecessor.capability_hash
            or capabilities.model_dump(
                mode="json",
                exclude={"capability_hash", "project_open", "timestamp"},
            )
            != predecessor.capability_manifest
        ):
            raise PlanConflict("AE capabilities changed since predecessor preview")

        renders = root / "renders"
        if renders.is_dir() and not renders.is_symlink():
            for candidate_dir in renders.iterdir():
                if (
                    candidate_dir.is_symlink()
                    or not candidate_dir.is_dir()
                    or not _RENDER_PLAN_ID_RE.fullmatch(candidate_dir.name)
                ):
                    continue
                try:
                    candidate = load_render_plan(root, candidate_dir.name)
                except PlanConflict:
                    continue
                if _equivalent_ae_successor(
                    candidate,
                    predecessor,
                    checkpoint=predecessor_checkpoint,
                    checkpoint_digest=context_digest,
                ):
                    return candidate

        return create_render_plan(
            root,
            project_id=project_id,
            scene_id=scene_id,
            version_id=version_id,
            backend="after_effects",
            mode="final",
            direction=predecessor.direction,
            locked_targets=predecessor.locked_targets,
            permitted_operations=predecessor.permitted_operations,
            effect_schemas=predecessor.effect_schemas,
            capability_hash=predecessor.capability_hash,
            capability_manifest=predecessor.capability_manifest,
            substitutions=predecessor.substitutions,
            substitutions_acknowledged=predecessor.substitutions_acknowledged,
            predecessor_id=predecessor.id,
            predecessor_digest=predecessor.digest,
            predecessor_checkpoint=predecessor_checkpoint,
            predecessor_checkpoint_digest=context_digest,
            _predecessor_plan=predecessor,
        )




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
        "scenes": [scene.model_dump(mode="json") for scene in project.scenes],
        "links": project.links,
        "approved_scenes": project.approved_scenes,
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
        el.pop("confidence", None)
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


from ..review.overlay import frame_overlay, read_manifest


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

    def orig_png(self, f: int, version: str | None = None) -> bytes:
        scene, v = self.scene(version)
        if not 0 <= f < scene.frames:
            raise IndexError("frame out of range")
        key = ("orig", v.id, f)
        hit = self._preview.get(key)
        if hit is not None:
            self._preview.move_to_end(key)
            return hit
        if v.analysis_file:
            manifest = read_manifest(self.root, v.analysis_file)
            fr = np.load(scene_dir(self.root, self.scene_id) / manifest["frames_file"], mmap_mode="r")
        else:
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
        if scene.ui is not None or any(element.kind == "3d" for element in scene.elements):
            from keepframe.compose.composer import compose
            from keepframe.render.renderer import render

            with self.lock, tempfile.TemporaryDirectory(prefix="keepframe-preview-") as temp:
                temp_path = Path(temp)
                html_path = compose(scene, scene_dir(self.root, self.scene_id), temp_path / "composition.html")
                result = render(html_path, scene, temp_path / "render", frames=[f], probe=False)
                data = (result.frames_dir / "f_00000.png").read_bytes()
            return self._remember(key, data)
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
                self._frames = None
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
    workflow: AEWorkflowService | None = None,
) -> ThreadingHTTPServer:
    configure()
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    review_states: dict[tuple[str, str], ReviewState] = {}
    allowed_ae_hosts = {"127.0.0.1", "localhost", "::1"}
    configured_host = host.strip("[]").lower()
    if configured_host not in {"0.0.0.0", "::", ""}:
        allowed_ae_hosts.add(configured_host)

    def agent_llm():
        if admin_svc is not None:
            return make_llm(admin_svc.get_llm_settings())
        saved = load_llm_settings(workspace)
        return make_llm(saved) if saved is not None else make_llm()
    ae_workflow = workflow if workflow is not None else AEWorkflowService(
        workspace,
        agent_llm,
    )

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
                self._json(413 if length > 64 * 1024 else 400, {"error": "request body is too large" if length > 64 * 1024 else "bad json"})
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
            if u.path == "/api/agent":
                if not isinstance(pid, str) or not _PROJECT_ID_RE.fullmatch(pid):
                    return self._json(400, {"error": "project is required"})
                if not isinstance(sid, str) or not _PROJECT_ID_RE.fullmatch(sid):
                    return self._json(400, {"error": "scene is required"})
                try:
                    root = _safe_render_project(workspace, pid)
                    project = load_project(root)
                    if ver:
                        version = next(item for item in project.versions if item.id == ver and item.scene_file.startswith(f"scenes/{sid}/"))
                        scene = load_scene(root / version.scene_file)
                    else:
                        scene, version = current_scene(root, sid)
                    limit = min(100, max(1, int(q.get("limit", ["50"])[0])))
                    transcript = session_page(root, sid, before=q.get("before", [None])[0], limit=limit)
                except FileNotFoundError:
                    return self._json(404, {"error": "not found"})
                except (ValueError, IndexError, StopIteration):
                    return self._json(400, {"error": "invalid agent history request"})
                return self._json(
                    200,
                    {
                        **transcript,
                        "project": _slim_project(project, sid),
                        "scene": _slim_scene(scene),
                        "version": version.model_dump(mode="json"),
                    },
                )
            if u.path == "/api/ae/status":
                if not isinstance(pid, str) or not _PROJECT_ID_RE.fullmatch(pid):
                    return self._json(400, {"error": "project is required"})
                try:
                    root = _safe_render_project(workspace, pid)
                    auth = AEProjectAuth(root, pid)
                    device_id = auth.active_device_id()
                except FileNotFoundError:
                    return self._json(404, {"error": "not found"})
                if device_id is None:
                    return self._json(
                        200,
                        {
                            "relay_configured": ae_relay_url is not None,
                            "paired": False,
                            "device_id": None,
                            "capability_hash": None,
                            "ae_version": None,
                            "ae_ready": False,
                            "project_open": False,
                        },
                    )
                if not self._authorize_ae_browser(pid):
                    return
                snapshot = auth.read_capabilities(device_id)
                return self._json(
                    200,
                    {
                        "relay_configured": ae_relay_url is not None,
                        "paired": True,
                        "device_id": device_id,
                        "capability_hash": (
                            snapshot.capability_hash if snapshot is not None else None
                        ),
                        "ae_version": snapshot.version if snapshot is not None else None,
                        "ae_ready": snapshot.ready if snapshot is not None else False,
                        "project_open": (
                            snapshot.project_open if snapshot is not None else False
                        ),
                    },
                )

            if u.path == "/api/render-plans":
                version_id = q.get("version", [ver])[0]
                if (
                    not isinstance(pid, str)
                    or not _PROJECT_ID_RE.fullmatch(pid)
                    or not isinstance(sid, str)
                    or not _PROJECT_ID_RE.fullmatch(sid)
                    or not isinstance(version_id, str)
                    or not _PROJECT_ID_RE.fullmatch(version_id)
                ):
                    return self._json(
                        400,
                        {"error": "project, scene, and version are required"},
                    )
                try:
                    root = _safe_render_project(workspace, pid)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except FileNotFoundError:
                    return self._json(404, {"error": "not found"})
                ae_authorized = False
                try:
                    with AEProjectAuth(root, pid).authorize_controller_with_capabilities(
                        self._controller_token(pid)
                    ):
                        ae_authorized = True
                except (AEAuthError, FileNotFoundError):
                    ae_authorized = False
                records: list[tuple[int, dict]] = []
                renders = root / "renders"
                if renders.is_dir() and not renders.is_symlink():
                    for index, plan_dir in enumerate(renders.iterdir()):
                        if index >= 1024:
                            break
                        if (
                            plan_dir.is_symlink()
                            or not plan_dir.is_dir()
                            or not _RENDER_PLAN_ID_RE.fullmatch(plan_dir.name)
                        ):
                            continue
                        try:
                            plan = load_render_plan(root, plan_dir.name)
                            if (
                                plan.project_id != pid
                                or plan.scene_id != sid
                                or plan.version_id != version_id
                                or (plan.backend == "after_effects" and not ae_authorized)
                            ):
                                continue
                            payload = _render_state_payload(root, plan)
                            modified = (plan_dir / "plan.json").stat().st_mtime_ns
                        except (
                            FileNotFoundError,
                            OSError,
                            PlanConflict,
                            CoordinatorConflict,
                        ):
                            continue
                        records.append((modified, payload))
                records.sort(key=lambda item: (item[0], item[1]["plan"]["id"]))
                return self._json(200, {"plans": [item[1] for item in records]})

            native_artifact_match = re.fullmatch(
                r"/api/native/artifacts/([A-Za-z0-9_-]{8,128})/(mp4|zip)",
                u.path,
            )
            if native_artifact_match:
                plan_id, kind = native_artifact_match.groups()
                if not isinstance(pid, str) or not _PROJECT_ID_RE.fullmatch(pid):
                    return self._json(400, {"error": "project is required"})
                try:
                    root = _safe_render_project(workspace, pid)
                    plan = load_render_plan(root, plan_id)
                    state = load_render_plan_state(root, plan_id)
                    if (
                        plan.project_id != pid
                        or plan.backend != "native"
                        or state.execution_id is None
                    ):
                        raise FileNotFoundError(kind)
                    job = JOBS.find(
                        _native_execution_job_id(
                            plan.id,
                            plan.digest,
                            state.execution_id,
                        )
                    )
                    artifact = _native_artifact_path(root, plan, job, kind)
                    stream = artifact.open("rb")
                    stream.seek(0, 2)
                    length = stream.tell()
                    stream.seek(0)
                except (FileNotFoundError, OSError, PlanConflict):
                    return self._json(404, {"error": "not found"})
                try:
                    self.send_response(200)
                    self.send_header("content-type", _NATIVE_ARTIFACTS[kind][2])
                    self.send_header("content-length", str(length))
                    self.send_header("cache-control", "no-store")
                    self.send_header("x-content-type-options", "nosniff")
                    self.send_header(
                        "content-disposition",
                        f'attachment; filename="keepframe-native.{kind}"',
                    )
                    self.end_headers()
                    while chunk := stream.read(1024 * 1024):
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                finally:
                    stream.close()
                return

            lottie_artifact_match = re.fullmatch(
                r"/api/lottie/artifacts/([A-Za-z0-9_-]{8,128})/animation",
                u.path,
            )
            if lottie_artifact_match:
                plan_id = lottie_artifact_match.group(1)
                if not isinstance(pid, str) or not _PROJECT_ID_RE.fullmatch(pid):
                    return self._json(400, {"error": "project is required"})
                try:
                    root = _safe_render_project(workspace, pid)
                    plan = load_render_plan(root, plan_id)
                    state = load_render_plan_state(root, plan_id)
                    if plan.project_id != pid or plan.backend != "lottie" or state.execution_id is None:
                        raise FileNotFoundError("animation")
                    job = JOBS.find(_native_execution_job_id(plan.id, plan.digest, state.execution_id))
                    artifact = _lottie_artifact_path(root, plan, job)
                    stream = artifact.open("rb")
                    stream.seek(0, 2)
                    length = stream.tell()
                    stream.seek(0)
                except (FileNotFoundError, OSError, PlanConflict):
                    return self._json(404, {"error": "not found"})
                try:
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(length))
                    self.send_header("cache-control", "no-store")
                    self.send_header("x-content-type-options", "nosniff")
                    self.send_header("content-disposition", 'attachment; filename="animation.json"')
                    self.end_headers()
                    while chunk := stream.read(1024 * 1024):
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                finally:
                    stream.close()
                return

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
                    execution_plan = _ae_execution_plan(root, plan)
                    reservation, stream = AECoordinator.cached(
                        root,
                        execution_plan.id,
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
                    payload = _render_state_payload(root, plan)
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
                    project = load_project(state.root)
                    status = meta.get("status")
                    approved = project.approved_scenes.get(scene.id)
                    if approved == v.id:
                        status = "approved"
                    elif status == "approved" and (project.approved_scenes or meta.get("version") != v.id):
                        status = "review"
                    payload = review_state_payload(project, v, scene, rep, state.job, status)
                    payload["analysis"] = read_manifest(state.root, v.analysis_file) if v.analysis_file else None
                    return self._json(200, payload)
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

            if u.path == "/api/analysis-overlay":
                if not pid:
                    return self._json(400, {"error": "project required"})
                state = review_state(pid, sid)
                if state is None:
                    return self._json(404, {"error": "not found"})
                try:
                    scene, version = state.scene(ver)
                    frame = int(q.get("frame", ["0"])[0])
                    return self._json(200, frame_overlay(state.root, scene, version, frame))
                except ValueError:
                    return self._json(400, {"error": "invalid frame"})
                except (StopIteration, IndexError, FileNotFoundError):
                    return self._json(404, {"error": "analysis or frame not found"})

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
                    return self._send(200, state.orig_png(int(parts[2]), ver), "image/png", cache="public, max-age=604800")
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

            if u.path == "/api/render-plans":
                if not self._same_origin():
                    return
                data = self._bounded_json()
                if data is None:
                    return
                allowed = {
                    "project",
                    "scene",
                    "version",
                    "backend",
                    "mode",
                    "direction",
                    "predecessor_id",
                    "predecessor_digest",
                    "predecessor_checkpoint",
                    "predecessor_checkpoint_digest",
                    "substitutions",
                    "substitutions_acknowledged",
                    "compatibility_issues",
                }
                if set(data) - allowed:
                    return self._json(400, {"error": "render plan payload is invalid"})
                project_id = data.get("project")
                backend = data.get("backend")
                mode = data.get("mode")
                if (
                    not isinstance(project_id, str)
                    or not _PROJECT_ID_RE.fullmatch(project_id)
                    or backend not in {"native", "after_effects", "lottie"}
                    or mode not in {"preview", "final"}
                ):
                    return self._json(400, {"error": "render plan payload is invalid"})
                try:
                    root = _safe_render_project(workspace, project_id)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except FileNotFoundError:
                    return self._json(404, {"error": "not found"})
                meta = load_meta(workspace, project_id) or {}
                scene_id = data.get("scene", meta.get("scene"))
                version_id = data.get("version", meta.get("version"))
                if (
                    not isinstance(scene_id, str)
                    or not _PROJECT_ID_RE.fullmatch(scene_id)
                    or not isinstance(version_id, str)
                    or not _PROJECT_ID_RE.fullmatch(version_id)
                ):
                    return self._json(400, {"error": "scene and version are required"})
                direction = data.get("direction")
                if direction is not None and not isinstance(direction, str):
                    return self._json(400, {"error": "direction must be a string or None"})
                predecessor_id = data.get("predecessor_id")
                predecessor_digest = data.get("predecessor_digest")
                predecessor_checkpoint = data.get("predecessor_checkpoint")
                predecessor_checkpoint_digest = data.get("predecessor_checkpoint_digest")
                if backend == "after_effects" and not self._authorize_ae_browser(
                    project_id,
                    origin_checked=True,
                ):
                    return
                try:
                    before = {
                        item.name
                        for item in (root / "renders").iterdir()
                        if item.is_dir() and not item.is_symlink()
                    } if (root / "renders").is_dir() and not (root / "renders").is_symlink() else set()
                    if backend == "after_effects":
                        auth = AEProjectAuth(root, project_id)
                        snapshot = auth.read_capabilities()
                        if mode == "final":
                            if (
                                not isinstance(predecessor_id, str)
                                or not isinstance(predecessor_digest, str)
                                or not isinstance(predecessor_checkpoint, int)
                                or isinstance(predecessor_checkpoint, bool)
                            ):
                                raise PlanConflict(
                                    "final AE plans require a preview predecessor checkpoint"
                                )
                            with _FINAL_PLAN_LOCK:
                                before = {
                                    item.name
                                    for item in (root / "renders").iterdir()
                                    if item.is_dir() and not item.is_symlink()
                                } if (root / "renders").is_dir() and not (root / "renders").is_symlink() else set()
                                plan = prepare_ae_final_successor(
                                    root,
                                    project_id=project_id,
                                    scene_id=scene_id,
                                    version_id=version_id,
                                    predecessor_id=predecessor_id,
                                    predecessor_digest=predecessor_digest,
                                    predecessor_checkpoint=predecessor_checkpoint,
                                    predecessor_checkpoint_digest=predecessor_checkpoint_digest,
                                    direction=direction,
                                    capabilities=snapshot,
                                )
                        else:
                            candidate = prepare_ae_render_plan(
                                root,
                                project_id=project_id,
                                scene_id=scene_id,
                                version_id=version_id,
                                mode=mode,
                                direction=direction,
                                capabilities=snapshot,
                                client=agent_llm(),
                                substitutions=data.get("substitutions"),
                                substitutions_acknowledged=data.get(
                                    "substitutions_acknowledged",
                                    False,
                                ),
                                compatibility_issues=data.get("compatibility_issues"),
                                predecessor_id=predecessor_id,
                                predecessor_digest=predecessor_digest,
                                predecessor_checkpoint=predecessor_checkpoint,
                                predecessor_checkpoint_digest=predecessor_checkpoint_digest,
                            )
                            if isinstance(candidate, AERenderDraft):
                                return self._json(
                                    200,
                                    {
                                        "draft": candidate.model_dump(mode="json"),
                                        "status": "awaiting_substitution_acknowledgement",
                                    },
                                )
                            plan = candidate
                    else:
                        plan = create_render_plan(
                            root,
                            project_id=project_id,
                            scene_id=scene_id,
                            version_id=version_id,
                            backend=backend,
                            mode=mode,
                            direction=direction,
                            predecessor_id=predecessor_id,
                            predecessor_digest=predecessor_digest,
                            predecessor_checkpoint=predecessor_checkpoint,
                            predecessor_checkpoint_digest=predecessor_checkpoint_digest,
                        )
                    state = load_render_plan_state(root, plan.id)
                    return self._json(
                        200 if plan.id in before else 201,
                        {
                            "plan": plan.model_dump(mode="json"),
                            "state": state.model_dump(mode="json"),
                        },
                    )
                except (PlanConflict, CoordinatorConflict) as exc:
                    return self._json(409, {"error": str(exc)})
                except (TypeError, ValueError) as exc:
                    return self._json(400, {"error": str(exc)})


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
                        if pending_state.status != "approved":
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
                    if not state.execution_id:
                        raise PlanConflict("approved render plan has no execution id")
                    if plan.backend == "after_effects":
                        if plan.mode == "final":
                            if not plan.predecessor_id:
                                raise PlanConflict("final AE plan has no preview predecessor")
                            session = AECoordinator.cached(
                                root,
                                plan.predecessor_id,
                            ).state()
                        else:
                            session = AECoordinator.cached(root, plan.id).start(
                                state.execution_id
                            )
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
                        spec = prepare_lottie_job(root, plan.id) if plan.backend == "lottie" else prepare_native_job(root, plan.id)
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
                            "artifacts": _local_artifacts(root, plan, job),
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
                allowed_keys = {"project", "plan", "revision", "checkpoint"}
                if action == "finalize":
                    allowed_keys.update({"final_plan_id", "execution_id"})
                if any(key not in allowed_keys for key in data):
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
                        queued_command = ae_workflow.manual_sync(coordinator)
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
                        selected = None
                        if action == "finalize" and data.get("checkpoint") is not None:
                            selected = data["checkpoint"]
                            if (
                                not isinstance(selected, int)
                                or isinstance(selected, bool)
                                or selected < 0
                            ):
                                return self._json(400, {"error": "checkpoint is invalid"})
                        if action == "finalize":
                            final_plan_id = data.get("final_plan_id")
                            execution_id = data.get("execution_id")
                            if (
                                not isinstance(final_plan_id, str)
                                or not _RENDER_PLAN_ID_RE.fullmatch(final_plan_id)
                            ):
                                return self._json(400, {"error": "final plan id is invalid"})
                            if (
                                not isinstance(execution_id, str)
                                or not re.fullmatch(r"ex-[0-9a-f]{32}", execution_id)
                            ):
                                return self._json(400, {"error": "execution id is invalid"})
                            transition_data = {
                                "final_plan_id": final_plan_id,
                                "execution_id": execution_id,
                            }
                            if selected is not None:
                                transition_data["selected_checkpoint"] = selected
                            current = coordinator.transition(
                                event,
                                revision,
                                **transition_data,
                            )
                        else:
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
                    started = time.perf_counter()
                    try:
                        if data.get("scenes") is not None:
                            scenes = validate_scenes(data["scenes"], start, end)
                            transitions = data.get("transitions") or [
                                {"from": left["id"], "to": right["id"], "frame": right["frames"][0], "transition": "unknown"}
                                for left, right in zip(scenes, scenes[1:])
                            ]
                            warnings = [
                                {"code": "short_scene", "scene": scene["id"], "frames": scene["frames"][1] - scene["frames"][0] + 1, "requires_ack": True}
                                for scene in scenes
                                if scene["frames"][1] - scene["frames"][0] + 1 < 6
                            ]
                        elif mode == "range":
                            scenes = [{"id": "s1", "frames": [start, end]}]
                            transitions, warnings = [], []
                        else:
                            sampled, _ = read_frames(video, start, end)
                            scenes, transitions, warnings = scene_layout(sampled, global_start=start)
                    except ValueError as exc:
                        return self._json(400, {"error": str(exc)})
                    digest = make_boundary_digest(scenes)
                    return self._json(
                        200,
                        estimate(
                            mode,
                            frames,
                            info["fps"],
                            project_id=meta["id"],
                            start=start,
                            end=end,
                            scenes=scenes,
                            transitions=transitions,
                            warnings=warnings,
                            boundary_digest=digest,
                            local_analysis_seconds=time.perf_counter() - started,
                        ),
                    )
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
                if meta.get("status") != "analyzing" and not token:
                    return self._json(400, {"error": "confirm required"})
                root = project_dir(workspace, project_id)
                video = root / "source.mp4"
                info = probe_video(video)
                mode, start, end = resolve_analyze_window(meta, data, info["frames"])
                try:
                    requested_scenes = validate_scenes(
                        data.get("scenes") or [{"id": "s1", "frames": [start, end]}],
                        start,
                        end,
                    )
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                digest = make_boundary_digest(requested_scenes)
                supplied_digest = data.get("boundary_digest")
                if supplied_digest is None:
                    return self._json(400, {"error": "boundary digest required"})
                if (
                    not isinstance(supplied_digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", supplied_digest)
                    or supplied_digest != digest
                ):
                    return self._json(400, {"error": "boundary digest mismatch"})
                short = [scene for scene in requested_scenes if scene["frames"][1] - scene["frames"][0] + 1 < 6]
                if short and not data.get("acknowledge_short_scenes"):
                    return self._json(400, {"error": "short_scene_ack_required", "scenes": [scene["id"] for scene in short]})
                # Refresh / re-entry must not demand a token after the first confirm.
                if meta.get("status") != "analyzing" and not consume_token(
                    token,
                    project_id=project_id,
                    mode=mode,
                    start=start,
                    end=end,
                    boundary_digest=supplied_digest,
                ):
                    return self._json(400, {"error": "confirm required"})
                frames = max(1, int(end) - int(start) + 1)
                est = estimate(mode, frames, info["fps"], project_id=project_id, start=start, end=end)
                reference = "ui" if data.get("reference") == "ui" else "mg"
                write_meta(
                    workspace,
                    project_id,
                    status="analyzing",
                    mode=mode,
                    range=[start, end] if mode == "range" else None,
                    reference=reference,
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
                        "mode": mode,
                        "scenes": requested_scenes,
                        "transitions": data.get("transitions") or [],
                        "options": {"ui": reference == "ui"},
                    },
                )
                job = JOBS.submit(
                    "analyze",
                    spec=spec,
                    project_id=project_id,
                    scene_id=requested_scenes[0]["id"],
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
                project = approve_scene(state.root, scene_id, version.id)
                fully_approved = all(ref.id in project.approved_scenes for ref in project.scenes)
                meta = write_meta(
                    workspace,
                    project_id,
                    status="approved" if fully_approved else "review",
                    version=version.id,
                    scene=scene_id,
                    approved_scenes=project.approved_scenes,
                )
                log.info("approved project=%s scene=%s version=%s", project_id, scene_id, version.id)
                return self._json(200, {"project": meta, "version": version.id})

            if u.path == "/api/keep":
                try:
                    data = json.loads(self._read_body().decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "bad json"})
                preset = data.get("preset")
                if preset is not None and (not isinstance(preset, str) or preset not in KEEP_PRESETS):
                    return self._json(400, {"error": "unknown preset"})
                project_id = data.get("project")
                scene_id = data.get("scene", "s1")
                if not project_id:
                    return self._json(400, {"error": "project required"})
                state = review_state(project_id, scene_id)
                if state is None:
                    return self._json(404, {"error": "not found"})
                try:
                    scene, _ = state.scene()
                    if preset:
                        scene.constraints = apply_keep_preset(scene.constraints, preset)
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
                data = self._bounded_json(2 * 1024 * 1024)
                if data is None:
                    return
                if int(self.headers.get("Content-Length", "0")) > 64 * 1024 and "ui_context" not in data:
                    return self._json(413, {"error": "request body is too large"})
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
                if len(message.encode("utf-8")) > MAX_HISTORY_BYTES:
                    return self._json(413, {"error": "message is too large"})
                try:
                    ui_context = validate_ui_context(data.get("ui_context"))
                except UIContextError as exc:
                    return self._json(413 if "too large" in str(exc) else 400, {"error": str(exc)})
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
                    scene_id = requested_scene_id
                    if not isinstance(scene_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scene_id):
                        raise ValueError("scene id is invalid")
                    versions = [v for v in project.versions if v.scene_file.startswith(f"scenes/{scene_id}/")]
                    if not versions:
                        raise ValueError("scene version was not found")
                    requested_version = data.get("v")
                    version = requested_version or (meta.get("version") if meta.get("scene") == scene_id else None) or versions[-1].id
                    if not any(v.id == version for v in versions):
                        raise ValueError("version is not authoritative for this scene")
                except Exception as e:  # noqa: BLE001 - malformed workspace input
                    return self._json(400, {"error": f"{type(e).__name__}: {e}"})
                resolved_project_id = project_id
                resolved_scene_id = scene_id
                resolved_version = version
                client = agent_llm()

                def _latest_ae_preview(capability_hash: str, direction: str | None):
                    renders = root / "renders"
                    candidates = []
                    if not renders.is_dir() or renders.is_symlink():
                        return None
                    for plan_dir in renders.iterdir():
                        if (
                            plan_dir.is_symlink()
                            or not plan_dir.is_dir()
                            or not _RENDER_PLAN_ID_RE.fullmatch(plan_dir.name)
                        ):
                            continue
                        try:
                            candidate = load_render_plan(root, plan_dir.name)
                            if (
                                candidate.backend != "after_effects"
                                or candidate.mode != "preview"
                                or candidate.scene_id != resolved_scene_id
                                or candidate.version_id != resolved_version
                                or candidate.capability_hash != capability_hash
                                or (direction is not None and candidate.direction != direction)
                            ):
                                continue
                            session = AECoordinator.cached(root, candidate.id).state()
                        except (PlanConflict, CoordinatorConflict):
                            continue
                        checkpoint = next(
                            (
                                item
                                for item in session.checkpoints
                                if item.index == session.selected_checkpoint
                            ),
                            None,
                        )
                        if (
                            session.baseline_complete
                            and checkpoint is not None
                            and checkpoint.passed
                            and checkpoint.lineage_valid
                            and checkpoint.context_digest is not None
                        ):
                            candidates.append((session.updated_at, candidate, session))
                    return max(candidates, key=lambda item: item[0]) if candidates else None

                def prepare_agent_render(mode: str, backend: str, direction: str | None):
                    if (
                        backend == "after_effects"
                        and not agent_auth.authenticate_controller(agent_controller_token)
                    ):
                        raise AEControllerAuthorizationError(
                            "controller authorization failed"
                        )
                    if backend == "after_effects":
                        snapshot = agent_auth.read_capabilities()
                        if snapshot is None:
                            raise PlanConflict(
                                "active published AE capabilities are required"
                            )
                        if mode == "final":
                            preview = _latest_ae_preview(snapshot.capability_hash, direction)
                            if preview is None:
                                raise PlanConflict(
                                    "final AE plans require a selected preview checkpoint"
                                )
                            _, predecessor, session = preview
                            return prepare_ae_final_successor(
                                root,
                                project_id=resolved_project_id,
                                scene_id=resolved_scene_id,
                                version_id=resolved_version,
                                predecessor_id=predecessor.id,
                                predecessor_digest=predecessor.digest,
                                predecessor_checkpoint=session.selected_checkpoint,
                                direction=direction,
                                capabilities=snapshot,
                            )
                        return prepare_ae_render_plan(
                            root,
                            project_id=resolved_project_id,
                            scene_id=resolved_scene_id,
                            version_id=resolved_version,
                            mode=mode,
                            direction=direction,
                            capabilities=snapshot,
                            client=client,
                        )
                    return create_render_plan(
                        root,
                        project_id=resolved_project_id,
                        scene_id=resolved_scene_id,
                        version_id=resolved_version,
                        backend=backend,
                        mode=mode,
                        direction=direction,
                        capability_hash=None,
                        capability_manifest=None,
                    )

                def submit_agent_job(kind: str, args: dict, stage: str) -> dict:
                    if kind == "analyze":
                        video = root / "source.mp4"
                        info = probe_video(video)
                        mode, start, end = resolve_analyze_window(meta, args or {}, info["frames"])
                        spec = JobSpec(
                            kind="analyze",
                            args={"video": str(video), "start": int(start), "end": int(end), "mode": mode,
                                  "options": {"ui": meta.get("reference") == "ui"},
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
                    with agent_scene_lock(root, resolved_scene_id):
                        history = llm_history(root, resolved_scene_id)
                        agent = SessionAgent(client)
                        turn = (
                            agent.turn(ctx, message, history, ui_context=ui_context)
                            if ui_context is not None
                            else agent.turn(ctx, message, history)
                        )
                        append_turn(root, resolved_scene_id, message, turn)
                except AEControllerAuthorizationError:
                    return self._json(
                        401,
                        {"error": "controller authorization failed"},
                    )
                except Exception as e:
                    log.exception("agent turn failed project=%s scene=%s", project_id, scene_id)
                    return self._json(400, {"error": f"{type(e).__name__}: {e}"})
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
                args = data.get("args", {})
                if args.get("version") and args["version"] != state.scene()[1].id:
                    return self._json(409, {"error": "Select the latest version to make corrections."})
                try:
                    state.run_correction(op, args)
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

    server = ThreadingHTTPServer((host, port), H)
    server.ae_workflow = ae_workflow
    return server
