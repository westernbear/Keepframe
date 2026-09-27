from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..ir.store import load_project, load_scene

RenderBackend = Literal["native", "after_effects"]
RenderMode = Literal["preview", "final"]
RenderPlanStatus = Literal["awaiting_approval", "approved", "running", "paused", "done", "failed"]


class PlanConflict(RuntimeError):
    """Raised when an immutable plan or its approval state no longer matches."""



_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()

class PlanAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str
    project_path: str
    sha256: str
    length: int
    media_kind: str = "application/octet-stream"
    role: Literal["texture", "raw", "substitution"] = "texture"

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}(?:\.[a-z0-9]+)?", value):
            raise ValueError("asset id must be content-addressed")
        return value

    @field_validator("project_path")
    @classmethod
    def _project_path(cls, value: str) -> str:
        if (
            not value
            or "\\" in value
            or value.startswith("/")
            or re.match(r"^[A-Za-z]:", value)
            or any(part in ("", ".", "..") for part in value.split("/"))
        ):
            raise ValueError("asset project_path must be normalized and relative")
        return value

    @field_validator("media_kind", "role")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("asset fields must not be empty")
        return value

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("asset sha256 must be a lowercase SHA-256 digest")
        return value

    @field_validator("length")
    @classmethod
    def _length(cls, value: int) -> int:
        if value < 0:
            raise ValueError("asset length must be non-negative")
        return value


# Names used by callers that prefer a render-specific name.
RenderAsset = PlanAsset


class RenderPlan(BaseModel):
    """The immutable, approval-bound description of one render request."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str
    project_id: str
    scene_id: str
    version_id: str
    backend: RenderBackend
    mode: RenderMode
    direction: str | None = None
    scene_sha256: str
    assets: tuple[PlanAsset, ...] = ()
    locked_targets: tuple[str, ...] = ()
    permitted_operations: tuple[dict[str, Any], ...] = ()
    effect_schemas: tuple[dict[str, Any], ...] = ()
    capability_hash: str | None = None
    capability_manifest: dict[str, Any] | None = None
    substitutions: tuple[dict[str, Any], ...] = ()
    substitutions_acknowledged: bool = False
    artifact_contract: dict[str, Any] = Field(default_factory=dict)
    predecessor_id: str | None = None
    predecessor_digest: str | None = None
    digest: str

    @field_validator("id", "project_id", "scene_id", "version_id")
    @classmethod
    def _ids_nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("plan identifiers must not be empty")
        return value

    @field_validator("direction")
    @classmethod
    def _direction_size(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 4096:
            raise ValueError("direction is too long")
        return value

    @field_validator("scene_sha256", "capability_hash", "predecessor_digest")
    @classmethod
    def _optional_sha256(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("digest fields must be lowercase SHA-256 digests")
        return value

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("digest must be a lowercase SHA-256 digest")
        return value

    @field_validator("locked_targets")
    @classmethod
    def _targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("locked target identifiers must not be empty")
        return value

    @model_validator(mode="after")
    def _capability_binding(self) -> "RenderPlan":
        if self.backend != "after_effects":
            return self
        if self.capability_hash is None or self.capability_manifest is None:
            raise ValueError(
                "after_effects plans require a capability hash and manifest"
            )
        try:
            manifest_hash = hashlib.sha256(
                _canonical_payload(self.capability_manifest)
            ).hexdigest()
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("capability manifest is invalid") from exc
        if manifest_hash != self.capability_hash:
            raise ValueError("capability manifest does not match capability hash")
        return self


class RenderPlanState(BaseModel):
    """Mutable revisioned approval/execution state for a render plan."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    plan_id: str
    digest: str
    revision: int = 0
    status: RenderPlanStatus = "awaiting_approval"
    execution_id: str | None = None
    approved_revision: int | None = None

    @field_validator("plan_id")
    @classmethod
    def _plan_id(cls, value: str) -> str:
        if not value:
            raise ValueError("plan_id must not be empty")
        return value

    @field_validator("digest")
    @classmethod
    def _state_digest(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("state digest must be a lowercase SHA-256 digest")
        return value

    @field_validator("revision", "approved_revision")
    @classmethod
    def _revision(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("revision must be non-negative")
        return value


# Kept private so callers cannot accidentally hash a mutable/partial representation.
def _canonical_payload(plan_data: Mapping[str, Any]) -> bytes:
    return json.dumps(
        plan_data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _plan_digest(plan: RenderPlan | Mapping[str, Any]) -> str:
    data = plan.model_dump(mode="json") if isinstance(plan, RenderPlan) else dict(plan)
    data.pop("digest", None)
    return hashlib.sha256(_canonical_payload(data)).hexdigest()


def _canonical_json(data: Mapping[str, Any]) -> bytes:
    return _canonical_payload(data) + b"\n"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        if path.is_symlink() or not path.is_dir():
            raise OSError(f"cannot synchronize directory: {path}")
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _safe_plan_id(plan_id: str) -> None:
    if not isinstance(plan_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", plan_id):
        raise PlanConflict("invalid render plan id")


def _relative_asset(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("asset paths must be project-relative and normalized")
    # Keep one canonical, platform-independent spelling in the plan. Backslash
    # paths are rejected instead of being interpreted differently by each host.
    if "\\" in raw or raw.startswith("/") or raw.startswith("//") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("asset paths must be project-relative and normalized")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("asset paths must be project-relative and normalized")
    return "/".join(parts)


def _reparse_point(path: Path) -> bool:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return False
    # Windows exposes reparse points through this attribute. Linux simply has
    # no such field, while lstat above still catches symlinks.
    return bool(getattr(info, "st_file_attributes", 0) & 0x0400)


def _ensure_project_path(
    root: Path,
    path: Path,
    label: str,
    *,
    kind: Literal["file", "directory"] | None = None,
    allow_missing: bool = False,
) -> None:
    root = Path(root)
    path = Path(path)
    if root.is_symlink() or _reparse_point(root) or not root.is_dir():
        raise PlanConflict("project root is not a regular directory")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PlanConflict(f"{label} is outside the project") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink() or _reparse_point(current):
            raise PlanConflict(f"{label} symlink/reparse point is not allowed")
    try:
        resolved = path.resolve(strict=not allow_missing)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise PlanConflict(f"{label} escapes the project") from exc
    missing = allow_missing and not path.exists()
    if not allow_missing and not path.exists():
        raise PlanConflict(f"{label} is missing")
    if kind == "file" and not missing and (path.is_symlink() or not path.is_file()):
        raise PlanConflict(f"{label} is not a regular file")
    if kind == "directory" and not missing and (path.is_symlink() or not path.is_dir()):
        raise PlanConflict(f"{label} is not a regular directory")


def _asset_source(root: Path, scene_dir: Path, raw: str) -> tuple[Path, str]:
    rel = _relative_asset(raw)
    scene_prefix = f"{scene_dir.relative_to(root).as_posix()}/"
    source = (root if rel.startswith(scene_prefix) else scene_dir).joinpath(*rel.split("/"))
    root_real = root.resolve()
    try:
        source_project_path = source.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("asset is outside the project") from exc

    # Check every component before resolving: even an in-project symlink is not
    # allowed because its target could change after plan creation.
    current = root
    for part in source_project_path.split("/"):
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError as exc:
            raise ValueError(f"asset does not exist: {raw}") from exc
        if stat.S_ISLNK(mode) or _reparse_point(current):
            raise ValueError(f"asset symlink/reparse point is not allowed: {raw}")

    resolved = source.resolve(strict=True)
    try:
        resolved.relative_to(root_real)
    except ValueError as exc:
        raise ValueError(f"asset symlink escapes project: {raw}") from exc
    if not stat.S_ISREG(os.stat(source, follow_symlinks=False).st_mode):
        raise ValueError(f"asset must be a regular file: {raw}")
    return source, source_project_path


def _media_kind(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".npz": "application/x-npz",
        ".json": "application/json",
    }.get(path.suffix.lower(), "application/octet-stream")


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            length += len(chunk)
    return digest.hexdigest(), length


def _copy_immutable(source: Path, destination: Path, expected_sha: str, expected_length: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise PlanConflict(f"immutable asset slot is not a regular file: {destination.name}")
        sha, length = _hash_file(destination)
        if sha != expected_sha or length != expected_length:
            raise PlanConflict(f"immutable asset slot collision: {destination.name}")
        return

    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            shutil.copyfileobj(inp, out, length=1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        copied_sha, copied_length = _hash_file(temp)
        if copied_sha != expected_sha or copied_length != expected_length:
            raise PlanConflict(f"source asset changed while pinning: {destination.name}")
        try:
            # Linking the completed temporary file avoids replacing a file that
            # another creator has already published at this content address.
            os.link(temp, destination)
        except FileExistsError:
            if destination.is_symlink() or not destination.is_file():
                raise PlanConflict(f"immutable asset slot collision: {destination.name}")
            sha, length = _hash_file(destination)
            if sha != expected_sha or length != expected_length:
                raise PlanConflict(f"immutable asset slot collision: {destination.name}")
        _fsync_directory(destination.parent)
    finally:
        temp.unlink(missing_ok=True)


def _substitution_asset_paths(substitutions: Sequence[Mapping[str, Any]]) -> Iterable[str]:
    for substitution in substitutions:
        for key in ("asset", "asset_path", "project_path", "texture", "raw"):
            value = substitution.get(key)
            if isinstance(value, str):
                yield value
        values = substitution.get("assets")
        if isinstance(values, (list, tuple)):
            for value in values:
                if isinstance(value, str):
                    yield value


def _validate_authoritative_file(root: Path, path: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PlanConflict(f"{label} is outside the project") from exc
    current = root
    for part in relative.parts:
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError as exc:
            raise PlanConflict(f"{label} is missing") from exc
        if stat.S_ISLNK(mode) or _reparse_point(current):
            raise PlanConflict(f"{label} symlink/reparse point is not allowed")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise PlanConflict(f"{label} escapes the project") from exc
    if not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode):
        raise PlanConflict(f"{label} is not a regular file")


def _load_meta(root: Path) -> dict[str, Any] | None:
    path = root / "meta.json"
    if path.is_symlink():
        raise PlanConflict("project metadata symlink is not allowed")
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanConflict("project metadata is invalid") from exc
    if not isinstance(value, dict):
        raise PlanConflict("project metadata is invalid")
    return value


def _resolve_version(root: Path, project_id: str, scene_id: str, version_id: str | None):
    if not isinstance(project_id, str) or not project_id or not isinstance(scene_id, str) or not scene_id:
        raise PlanConflict("project and scene are required")
    meta = _load_meta(root)
    if meta is not None:
        if meta.get("id") is not None and meta.get("id") != project_id:
            raise PlanConflict("project id is not authoritative")
        if meta.get("scene") is not None and meta.get("scene") != scene_id:
            raise PlanConflict("scene id is not authoritative")
    manifest = root / "project.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise PlanConflict("project manifest is not a regular file")
    try:
        project = load_project(root)
    except Exception as exc:  # noqa: BLE001 - map malformed project to plan conflict
        raise PlanConflict("project manifest is invalid") from exc
    candidates = [v for v in project.versions if v.scene_file.startswith(f"scenes/{scene_id}/")]
    if not candidates:
        raise PlanConflict("scene version was not found")
    wanted = version_id
    if wanted is None and meta is not None and isinstance(meta.get("version"), str):
        wanted = meta["version"]
    if wanted is None:
        wanted = candidates[-1].id
    version = next((v for v in candidates if v.id == wanted), None)
    if version is None:
        raise PlanConflict("version does not belong to the requested scene")
    if meta is not None and meta.get("version") is not None and meta.get("version") != version.id:
        raise PlanConflict("version is not the server-approved version")
    try:
        scene_file = _relative_asset(version.scene_file)
    except ValueError as exc:
        raise PlanConflict("scene file must be project-relative") from exc
    if not scene_file.startswith(f"scenes/{scene_id}/"):
        raise PlanConflict("version does not belong to the requested scene")
    scene_path = root.joinpath(*scene_file.split("/"))
    _validate_authoritative_file(root, scene_path, "scene file")
    return meta, version, scene_path


def _final_gate(meta: dict[str, Any] | None, version_id: str) -> None:
    if meta is None or meta.get("status") != "approved":
        raise PlanConflict("final render requires an approved project")
    if meta.get("version") != version_id:
        raise PlanConflict("final render requires the approved version")


def create_render_plan(
    root: Path,
    *,
    project_id: str,
    scene_id: str,
    version_id: str | None = None,
    backend: RenderBackend,
    mode: RenderMode,
    direction: str | None = None,
    locked_targets: Sequence[str] = (),
    permitted_operations: Sequence[Mapping[str, Any]] = (),
    effect_schemas: Sequence[Mapping[str, Any]] = (),
    capability_hash: str | None = None,
    connector_capability_hash: str | None = None,
    capability_manifest: Mapping[str, Any] | None = None,
    substitutions: Sequence[Mapping[str, Any]] = (),
    substitutions_acknowledged: bool = False,
    artifact_contract: Mapping[str, Any] | None = None,
    predecessor_id: str | None = None,
    predecessor_digest: str | None = None,
) -> RenderPlan:
    """Resolve authoritative project data, pin assets, and publish one plan."""
    if backend not in ("native", "after_effects"):
        raise ValueError(f"unknown render backend {backend!r}")
    if mode not in ("preview", "final"):
        raise ValueError(f"unknown render mode {mode!r}")
    if direction is not None and not isinstance(direction, str):
        raise TypeError("direction must be a string or None")
    if capability_hash is not None and connector_capability_hash is not None and capability_hash != connector_capability_hash:
        raise PlanConflict("conflicting capability hashes")
    capability_hash = capability_hash if capability_hash is not None else connector_capability_hash
    if backend == "after_effects" and (
        not capability_hash or capability_manifest is None
    ):
        raise PlanConflict(
            "after_effects plans require a capability hash and manifest"
        )
    if capability_manifest is not None:
        if not isinstance(capability_manifest, Mapping):
            raise TypeError("capability manifest must be a mapping or None")
        try:
            capability_manifest = json.loads(
                _canonical_payload(dict(capability_manifest)).decode("utf-8")
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise PlanConflict("capability manifest is invalid") from exc
        if backend == "after_effects" and capability_hash:
            manifest_hash = hashlib.sha256(
                _canonical_payload(capability_manifest)
            ).hexdigest()
            if manifest_hash != capability_hash:
                raise PlanConflict("capability manifest does not match capability hash")
    substitutions = tuple(dict(item) for item in substitutions)
    if substitutions and not substitutions_acknowledged:
        raise PlanConflict("substitution acknowledgement is required")

    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise PlanConflict("project root is not a directory")
    root = root.resolve()
    meta, version, scene_path = _resolve_version(root, project_id, scene_id, version_id)
    if mode == "final":
        _final_gate(meta, version.id)
    try:
        scene = load_scene(scene_path)
    except Exception as exc:  # noqa: BLE001 - malformed source cannot be rendered
        raise PlanConflict("scene file is invalid") from exc
    if scene.id != scene_id:
        raise PlanConflict("scene id does not match the authoritative version")

    scene_sha256, _ = _hash_file(scene_path)
    scene_dir = scene_path.parent
    refs: list[tuple[str, Literal["texture", "raw", "substitution"]]] = []
    for element in scene.elements:
        if element.canonical.texture:
            refs.append((element.canonical.texture, "texture"))
        if element.raw:
            refs.append((element.raw, "raw"))
    if scene.background.kind == "image":
        refs.append((scene.background.value, "texture"))
    refs.extend((raw, "substitution") for raw in _substitution_asset_paths(substitutions))

    assets: list[PlanAsset] = []
    seen_manifest: set[tuple[str, str]] = set()
    sources: dict[str, tuple[Path, str, str]] = {}
    for raw, role in refs:
        source, project_path = _asset_source(root, root if role == "substitution" else scene_dir, raw)
        sha256, length = _hash_file(source)
        suffix = source.suffix.lower()
        asset_id = f"{sha256}{suffix}" if suffix else sha256
        manifest_key = (project_path, role)
        if manifest_key in seen_manifest:
            continue
        seen_manifest.add(manifest_key)
        sources.setdefault(asset_id, (source, sha256, project_path))
        assets.append(
            PlanAsset(
                id=asset_id,
                project_path=project_path,
                sha256=sha256,
                length=length,
                media_kind=_media_kind(source),
                role=role,
            )
        )

    # A random id avoids reusing mutable output paths. The staging directory is
    # published as one rename after every immutable file has been fsynced.
    renders = root / "renders"
    _ensure_project_path(root, renders, "renders directory", kind="directory", allow_missing=True)
    renders.mkdir(parents=True, exist_ok=True)
    _ensure_project_path(root, renders, "renders directory", kind="directory")
    for _ in range(8):
        plan_id = uuid.uuid4().hex
        final_dir = renders / plan_id
        staging = renders / f".{plan_id}.tmp"
        _ensure_project_path(root, final_dir, "render plan directory", allow_missing=True)
        _ensure_project_path(root, staging, "render plan staging directory", allow_missing=True)
        if final_dir.exists() or final_dir.is_symlink() or staging.exists() or staging.is_symlink():
            continue
        try:
            staging.mkdir()
            _ensure_project_path(root, staging, "render plan staging directory", kind="directory")
        except FileExistsError:
            continue
        try:
            artifact = dict(artifact_contract) if artifact_contract is not None else {
                "mode": mode,
                "outputs": ["frames", "mp4"] if mode == "preview" else ["mp4", "project"],
            }
            plan_without_digest = {
                "id": plan_id,
                "project_id": project_id,
                "scene_id": scene_id,
                "version_id": version.id,
                "backend": backend,
                "mode": mode,
                "direction": direction,
                "scene_sha256": scene_sha256,
                "assets": [
                    {
                        "id": asset.id,
                        "project_path": asset.project_path,
                        "sha256": asset.sha256,
                        "length": asset.length,
                        "media_kind": asset.media_kind,
                        "role": asset.role,
                    }
                    for asset in assets
                ],
                "locked_targets": list(locked_targets),
                "permitted_operations": [dict(item) for item in permitted_operations],
                "effect_schemas": [dict(item) for item in effect_schemas],
                "capability_hash": capability_hash,
                "capability_manifest": capability_manifest,
                "substitutions": list(substitutions),
                "substitutions_acknowledged": substitutions_acknowledged,
                "artifact_contract": artifact,
                "predecessor_id": predecessor_id,
                "predecessor_digest": predecessor_digest,
            }
            digest = hashlib.sha256(_canonical_payload(plan_without_digest)).hexdigest()
            plan = RenderPlan.model_validate_json(
                json.dumps({**plan_without_digest, "digest": digest}, ensure_ascii=False, allow_nan=False)
            )
            for asset in assets:
                source, sha256, _ = sources[asset.id]
                _copy_immutable(source, staging / "assets" / asset.id, sha256, asset.length)
            initial_state = RenderPlanState(plan_id=plan.id, digest=plan.digest)
            _atomic_write(staging / "plan.json", _canonical_json(plan.model_dump(mode="json")))
            _atomic_write(staging / "state.json", _canonical_json(initial_state.model_dump(mode="json")))
            _ensure_project_path(root, staging, "render plan staging directory", kind="directory")
            _ensure_project_path(root, final_dir, "render plan directory", allow_missing=True)
            if final_dir.exists() or final_dir.is_symlink():
                raise PlanConflict("render plan directory already exists")
            _fsync_directory(staging)
            os.rename(staging, final_dir)
            _ensure_project_path(root, final_dir, "render plan directory", kind="directory")
            _fsync_directory(renders)
            return plan
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    raise PlanConflict("could not allocate a unique render plan id")


def _plan_dir(root: Path, plan_id: str) -> Path:
    _safe_plan_id(plan_id)
    root = Path(root)
    renders = root / "renders"
    _ensure_project_path(root, renders, "renders directory", kind="directory")
    path = renders / plan_id
    _ensure_project_path(root, path, "render plan directory", kind="directory")
    return path


def _validate_pinned_asset(root: Path, plan: RenderPlan, asset: PlanAsset) -> None:
    plan_dir = _plan_dir(root, plan.id)
    assets_dir = plan_dir / "assets"
    _ensure_project_path(root, assets_dir, "pinned asset directory", kind="directory")
    destination = assets_dir / asset.id
    _ensure_project_path(root, destination, "pinned asset", kind="file")
    sha256, length = _hash_file(destination)
    if sha256 != asset.sha256 or length != asset.length:
        raise PlanConflict(f"pinned asset digest mismatch: {asset.id}")


def load_render_plan(root: Path, plan_id: str) -> RenderPlan:
    """Load and verify the immutable canonical plan and pinned assets."""
    plan_dir = _plan_dir(Path(root), plan_id)
    path = plan_dir / "plan.json"
    _ensure_project_path(Path(root), path, "render plan file", kind="file")
    try:
        plan = RenderPlan.model_validate_json(path.read_bytes())
    except Exception as exc:  # noqa: BLE001 - callers receive one conflict type
        raise PlanConflict("render plan is invalid") from exc
    if plan.id != plan_id or _plan_digest(plan) != plan.digest:
        raise PlanConflict("render plan digest mismatch")
    for asset in plan.assets:
        _validate_pinned_asset(Path(root), plan, asset)
    return plan


def load_render_plan_state(root: Path, plan_id: str) -> RenderPlanState:
    plan = load_render_plan(root, plan_id)
    path = _plan_dir(Path(root), plan.id) / "state.json"
    _ensure_project_path(Path(root), path, "render plan state file", kind="file")
    try:
        state = RenderPlanState.model_validate_json(path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        raise PlanConflict("render plan state is invalid") from exc
    if state.plan_id != plan.id or state.digest != plan.digest:
        raise PlanConflict("render plan state digest mismatch")
    return state


def validate_render_plan_execution(root: Path, plan_id: str, execution_id: str) -> RenderPlanState:
    """Revalidate the approval and authoritative final inputs under the state lock."""
    if not isinstance(execution_id, str) or not execution_id:
        raise PlanConflict("render execution id is invalid")
    plan = load_render_plan(root, plan_id)
    if plan.mode != "final":
        raise PlanConflict("finalization requires a final render plan")
    state_path = _plan_dir(Path(root), plan.id) / "state.json"
    with _state_lock(state_path):
        locked_plan = load_render_plan(root, plan.id)
        state = load_render_plan_state(root, locked_plan.id)
        if locked_plan.mode != "final":
            raise PlanConflict("finalization requires a final render plan")
        if state.status != "approved":
            raise PlanConflict("final render plan is not approved")
        if state.digest != locked_plan.digest:
            raise PlanConflict("approved render plan digest does not match")
        if state.execution_id != execution_id:
            raise PlanConflict("render execution does not match approved execution")
        meta, authoritative_version, scene_path = _resolve_version(
            Path(root),
            locked_plan.project_id,
            locked_plan.scene_id,
            locked_plan.version_id,
        )
        _final_gate(meta, authoritative_version.id)
        scene_sha256, _ = _hash_file(scene_path)
        if scene_sha256 != locked_plan.scene_sha256:
            raise PlanConflict("authoritative scene changed since approval")
        return state


# Short alias for callers that naturally ask for a plan's state.
load_render_state = load_render_plan_state


def _process_lock(path: Path) -> threading.RLock:
    key = str(path.resolve(strict=False))
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


@contextmanager
def _state_lock(path: Path):
    path = Path(path)
    lock_path = path.with_name(f".{path.name}.lock")
    if lock_path.is_symlink() or _reparse_point(lock_path):
        raise PlanConflict("approval lock path is not safe")
    lock = _process_lock(lock_path)
    with lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stream = lock_path.open("a+b")
        except OSError as exc:
            raise PlanConflict("approval lock could not be opened") from exc
        with stream:
            acquired = False
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    if not stream.read(1):
                        stream.seek(0)
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                    unlock = lambda: (stream.seek(0), msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1))
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                    unlock = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                acquired = True
            except (ImportError, AttributeError, OSError) as exc:
                raise PlanConflict("approval lock could not be acquired") from exc
            try:
                yield
            finally:
                if acquired:
                    try:
                        unlock()
                    except (AttributeError, OSError) as exc:
                        raise PlanConflict("approval lock could not be released") from exc


def approve_render_plan(root: Path, plan_id: str, *, digest: str, revision: int) -> RenderPlanState:
    """Consume one matching approval, returning the same execution on retries."""
    root = Path(root)
    plan = load_render_plan(root, plan_id)
    if digest != plan.digest:
        raise PlanConflict("approval digest does not match the render plan")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise PlanConflict("approval revision is invalid")
    state_path = _plan_dir(root, plan.id) / "state.json"
    with _state_lock(state_path):
        locked_plan = load_render_plan(root, plan_id)
        if locked_plan.digest != digest:
            raise PlanConflict("approval digest does not match the render plan")
        state = load_render_plan_state(root, locked_plan.id)
        if state.digest != digest:
            raise PlanConflict("approval digest does not match the render plan state")
        if state.status == "approved":
            if state.approved_revision == revision and state.execution_id:
                return state
            raise PlanConflict("approval revision is stale")
        if state.status != "awaiting_approval":
            raise PlanConflict("render plan is no longer awaiting approval")
        if state.revision != revision:
            raise PlanConflict("approval revision is stale")

        meta, authoritative_version, scene_path = _resolve_version(
            root,
            locked_plan.project_id,
            locked_plan.scene_id,
            locked_plan.version_id,
        )
        if locked_plan.mode == "final":
            _final_gate(meta, authoritative_version.id)
        scene_sha256, _ = _hash_file(scene_path)
        if scene_sha256 != locked_plan.scene_sha256:
            raise PlanConflict("authoritative scene changed since plan creation")

        approved = state.model_copy(
            update={
                "status": "approved",
                "revision": state.revision + 1,
                "execution_id": f"ex-{uuid.uuid4().hex}",
                "approved_revision": revision,
            }
        )
        _atomic_write(state_path, _canonical_json(approved.model_dump(mode="json")))
        return approved




__all__ = [
    "PlanAsset",
    "RenderAsset",
    "PlanConflict",
    "RenderBackend",
    "RenderMode",
    "RenderPlan",
    "RenderPlanState",
    "approve_render_plan",
    "create_render_plan",
    "load_render_plan",
    "load_render_plan_state",
    "load_render_state",
    "validate_render_plan_execution",
]
