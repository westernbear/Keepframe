from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

from keepframe.compose.composer import compose
from keepframe.ir.store import load_project, load_scene
from keepframe.jobs.spec import JobSpec

from .plan import PlanConflict, _state_lock, load_render_plan, load_render_plan_state


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_NAME = "stage-manifest.json"
_MANIFEST_VERSION = 1


def _reparse_point(path: Path) -> bool:
    """Return whether a path is a Windows reparse point.

    Keeping this local lets stage validation use the same check for every path
    and makes platform-specific filesystem tests deterministic.
    """
    try:
        info = Path(path).stat(follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(info, "st_file_attributes", 0) & 0x0400)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _check_absolute_components(path: Path, label: str) -> None:
    path = _absolute(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink() or _reparse_point(current):
            raise PlanConflict(f"{label} contains a symlink/reparse point")


def _safe_root(root: Path) -> Path:
    root = _absolute(Path(root))
    _check_absolute_components(root, "project root")
    if root.is_symlink() or _reparse_point(root) or not root.is_dir():
        raise PlanConflict("project root is not a regular directory")
    try:
        return root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PlanConflict("project root cannot be resolved") from exc


def _safe_path(
    root: Path,
    path: Path,
    label: str,
    *,
    kind: str | None = None,
    allow_missing: bool = False,
) -> Path:
    """Validate every component, reparse point, and resolved containment."""
    root = Path(root)
    if root.is_symlink() or _reparse_point(root) or not root.is_dir():
        raise PlanConflict(f"{label} root is not a safe directory")
    path = Path(path)
    if not path.is_absolute():
        raise PlanConflict(f"{label} must be absolute")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PlanConflict(f"{label} is outside the project") from exc

    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink() or _reparse_point(current):
            raise PlanConflict(f"{label} contains a symlink/reparse point")

    exists = path.exists() or path.is_symlink()
    if not exists:
        if not allow_missing:
            raise FileNotFoundError(path)
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(root.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise PlanConflict(f"{label} escapes the project") from exc
        return path

    if kind == "file" and not path.is_file():
        raise PlanConflict(f"{label} is not a regular file")
    if kind == "directory" and not path.is_dir():
        raise PlanConflict(f"{label} is not a regular directory")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise PlanConflict(f"{label} escapes the project") from exc
    return path




def _make_dir(root: Path, path: Path, label: str) -> Path:
    """Create a directory tree only after checking each existing component."""
    root = Path(root)
    path = Path(path)
    _safe_path(root, path, label, kind="directory", allow_missing=True)
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.exists() or current.is_symlink() or _reparse_point(current):
            _safe_path(root, current, label, kind="directory")
        else:
            current.mkdir()
            _safe_path(root, current, label, kind="directory")
    return path


def _copy_file(
    source: Path,
    destination: Path,
    label: str,
    *,
    source_root: Path,
    destination_root: Path,
) -> None:
    _safe_path(source_root, source, label, kind="file")
    _make_dir(destination_root, destination.parent, f"staged {label} parent")
    _safe_path(destination_root, destination, f"staged {label}", kind="file", allow_missing=True)
    shutil.copyfile(source, destination)
    _safe_path(destination_root, destination, f"staged {label}", kind="file")


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            length += len(chunk)
    return digest.hexdigest(), length


def _project_relative(raw: str, label: str) -> str:
    if (
        not isinstance(raw, str)
        or not raw
        or "\x00" in raw
        or "\\" in raw
        or raw.startswith("/")
        or re.match(r"^[A-Za-z]:", raw)
    ):
        raise PlanConflict(f"{label} must be project-relative")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PlanConflict(f"{label} must be project-relative")
    return "/".join(parts)


def _scene_reference(root: Path, plan) -> tuple[str, Path]:
    _safe_path(root, root / "project.json", "project manifest", kind="file")
    try:
        project = load_project(root)
    except Exception as exc:  # noqa: BLE001 - map malformed project data to one conflict type
        raise PlanConflict("project manifest is invalid") from exc
    versions = [
        version
        for version in project.versions
        if version.id == plan.version_id and version.scene_file.startswith(f"scenes/{plan.scene_id}/")
    ]
    if len(versions) != 1:
        raise PlanConflict("plan version is not authoritative for its scene")
    relative = _project_relative(versions[0].scene_file, "scene file")
    path = root.joinpath(*relative.split("/"))
    _safe_path(root, path, "scene file", kind="file")
    return relative, path


def _selected_scene(root: Path, plan) -> tuple[str, Path]:
    relative, path = _scene_reference(root, plan)
    digest, _ = _hash_file(path)
    if digest != plan.scene_sha256:
        raise PlanConflict("authoritative scene changed since plan approval")
    return relative, path


def _validate_snapshot_assets(plan, relative_scene: str) -> None:
    reserved = {"project.json", "meta.json", "source.mp4", "composition.html", relative_scene}
    seen: dict[str, tuple[str, int]] = {}
    for asset in plan.assets:
        relative = _project_relative(asset.project_path, f"asset {asset.id} path")
        if relative in reserved:
            raise PlanConflict("pinned asset path conflicts with the native snapshot")
        if relative == "renders" or relative.startswith("renders/"):
            raise PlanConflict("native snapshot may not contain renders")
        identity = (asset.sha256, asset.length)
        if relative in seen and seen[relative] != identity:
            raise PlanConflict("pinned asset path has conflicting contents")
        seen[relative] = identity


def _validate_final_metadata(root: Path, plan) -> None:
    if plan.mode != "final":
        return
    try:
        metadata = _safe_path(root, root / "meta.json", "project metadata", kind="file")
        meta = json.loads(metadata.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - map malformed metadata to one conflict type
        raise PlanConflict("project metadata is invalid") from exc
    if not isinstance(meta, dict) or meta.get("status") != "approved" or meta.get("version") != plan.version_id:
        raise PlanConflict("final render requires the approved version")


def _expected_members(plan, relative_scene: str, *, source_present: bool) -> set[str]:
    _validate_snapshot_assets(plan, relative_scene)
    members = {"project.json", "meta.json", "composition.html", relative_scene}
    if source_present:
        members.add("source.mp4")
    members.update(asset.project_path for asset in plan.assets)
    return members


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _manifest_entries(snapshot: Path, members: set[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for relative in sorted(members):
        normalized = _project_relative(relative, "stage member")
        path = snapshot.joinpath(*normalized.split("/"))
        _safe_path(snapshot, path, f"stage member {normalized}", kind="file")
        digest, length = _hash_file(path)
        entries.append({"length": length, "path": normalized, "sha256": digest})
    return entries


def _write_stage_manifest(
    stage_root: Path,
    snapshot: Path,
    plan,
    relative_scene: str,
    *,
    source_present: bool,
) -> Path:
    members = _expected_members(plan, relative_scene, source_present=source_present)
    payload = {
        "members": _manifest_entries(snapshot, members),
        "source_present": source_present,
        "version": _MANIFEST_VERSION,
    }
    manifest = stage_root / _MANIFEST_NAME
    _make_dir(stage_root, manifest.parent, "stage manifest parent")
    _safe_path(stage_root, manifest, "stage manifest", kind="file", allow_missing=True)
    manifest.write_bytes(_canonical_json(payload))
    _safe_path(stage_root, manifest, "stage manifest", kind="file")
    return manifest


def _read_stage_manifest(manifest: Path) -> tuple[bool, dict[str, dict[str, Any]]]:
    raw = manifest.read_bytes()
    try:
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanConflict("native stage manifest is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"members", "source_present", "version"}:
        raise PlanConflict("native stage manifest is invalid")
    if payload["version"] != _MANIFEST_VERSION or not isinstance(payload["source_present"], bool):
        raise PlanConflict("native stage manifest is invalid")
    if _canonical_json(payload) != raw:
        raise PlanConflict("native stage manifest is not canonical")
    members = payload["members"]
    if not isinstance(members, list):
        raise PlanConflict("native stage manifest is invalid")
    result: dict[str, dict[str, Any]] = {}
    for entry in members:
        if not isinstance(entry, dict) or set(entry) != {"length", "path", "sha256"}:
            raise PlanConflict("native stage manifest is invalid")
        path = _project_relative(entry["path"], "stage member")
        length = entry["length"]
        digest = entry["sha256"]
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            raise PlanConflict("native stage manifest is invalid")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise PlanConflict("native stage manifest is invalid")
        if path in result:
            raise PlanConflict("native stage manifest has duplicate members")
        result[path] = {"length": length, "path": path, "sha256": digest}
    return payload["source_present"], result


def _snapshot_entries(snapshot: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    for directory, children, names in os.walk(snapshot, topdown=True, followlinks=False):
        directory_path = Path(directory)
        _safe_path(snapshot, directory_path, "native snapshot directory", kind="directory")
        relative_directory = directory_path.relative_to(snapshot).as_posix()
        if relative_directory != ".":
            directories.add(relative_directory)
        for name in children:
            child = directory_path / name
            _safe_path(snapshot, child, "native snapshot directory", kind="directory")
        for name in names:
            child = directory_path / name
            _safe_path(snapshot, child, "native snapshot member", kind="file")
            files.add(child.relative_to(snapshot).as_posix())
    return files, directories


def _same_path(left: Path, right: Path) -> bool:
    return _absolute(left) == _absolute(right)


def _verify_stage_files(
    root: Path,
    plan,
    native: Path,
    snapshot: Path,
    manifest: Path,
    output: Path,
    relative_scene: str,
    *,
    scene: Path | None = None,
    html: Path | None = None,
    package_root: Path | None = None,
    manifest_sha256: str | None = None,
) -> None:
    plan_dir = root / "renders" / plan.id
    _safe_path(root, plan_dir, "render plan directory", kind="directory")
    _safe_path(plan_dir, native, "native staging directory", kind="directory")
    _safe_path(native, snapshot, "native snapshot directory", kind="directory")
    _safe_path(native, output, "native output directory", kind="directory")
    expected_manifest = native / _MANIFEST_NAME
    if not _same_path(manifest, expected_manifest):
        raise PlanConflict("native stage manifest path is invalid")
    _safe_path(native, manifest, "stage manifest", kind="file")
    if manifest_sha256 is not None:
        digest, _ = _hash_file(manifest)
        if digest != manifest_sha256:
            raise PlanConflict("native stage manifest digest mismatch")

    source_present, entries = _read_stage_manifest(manifest)
    expected = _expected_members(plan, relative_scene, source_present=source_present)
    if set(entries) != expected:
        raise PlanConflict("native stage member set does not match the approved plan")
    actual_files, actual_directories = _snapshot_entries(snapshot)
    expected_directories: set[str] = set()
    for relative in expected:
        parts = relative.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            expected_directories.add("/".join(parts[:index]))
    if actual_files != expected or actual_directories != expected_directories:
        raise PlanConflict("native stage contains unapproved members")

    for relative in sorted(expected):
        path = snapshot.joinpath(*relative.split("/"))
        _safe_path(snapshot, path, f"native stage member {relative}", kind="file")
        digest, length = _hash_file(path)
        entry = entries[relative]
        if digest != entry["sha256"] or length != entry["length"]:
            raise PlanConflict(f"native stage member digest mismatch: {relative}")
    compatibility = native / "composition.html"
    if compatibility.exists() or compatibility.is_symlink() or _reparse_point(compatibility):
        _safe_path(native, compatibility, "composition compatibility copy", kind="file")
        compatibility_digest, compatibility_length = _hash_file(compatibility)
        composition_entry = entries["composition.html"]
        if (compatibility_digest, compatibility_length) != (composition_entry["sha256"], composition_entry["length"]):
            raise PlanConflict("native composition compatibility copy digest mismatch")

    scene_entry = entries[relative_scene]
    if scene_entry["sha256"] != plan.scene_sha256:
        raise PlanConflict("native staged scene digest mismatch")
    for asset in plan.assets:
        entry = entries[asset.project_path]
        if entry["sha256"] != asset.sha256 or entry["length"] != asset.length:
            raise PlanConflict(f"native staged asset digest mismatch: {asset.id}")

    expected_scene = snapshot.joinpath(*relative_scene.split("/"))
    expected_html = snapshot / "composition.html"
    if scene is not None:
        _safe_path(snapshot, scene, "native scene input", kind="file")
        if not _same_path(scene, expected_scene):
            raise PlanConflict("native scene input is outside the staged snapshot")
    if html is not None:
        _safe_path(snapshot, html, "native composition input", kind="file")
        if not _same_path(html, expected_html):
            raise PlanConflict("native composition input is outside the staged snapshot")
    if package_root is not None and not _same_path(package_root, snapshot):
        raise PlanConflict("native package root is not the staged snapshot")


def _stage_native(root: Path, plan):
    plan_dir = root / "renders" / plan.id
    _safe_path(root, plan_dir, "render plan directory", kind="directory")
    native = plan_dir / "native"
    if native.exists() or native.is_symlink() or _reparse_point(native):
        _safe_path(plan_dir, native, "native staging directory", kind="directory")
        snapshot = native / "snapshot"
        manifest = native / _MANIFEST_NAME
        output = native / "output"
        try:
            _safe_path(native, snapshot, "native snapshot directory", kind="directory")
            relative_scene, _ = _scene_reference(snapshot, plan)
            _validate_snapshot_assets(plan, relative_scene)
            _verify_stage_files(root, plan, native, snapshot, manifest, output, relative_scene)
            return native, snapshot, relative_scene, snapshot / "composition.html", output, manifest
        except FileNotFoundError:
            # A process may have been interrupted before publishing the stage.
            # Rebuild it below while holding the plan state lock.
            shutil.rmtree(native)

    relative_scene, source_scene = _selected_scene(root, plan)
    _validate_snapshot_assets(plan, relative_scene)
    staging = plan_dir / f".native.{uuid.uuid4().hex}.tmp"
    _safe_path(plan_dir, staging, "native staging path", kind="directory", allow_missing=True)
    _make_dir(plan_dir, staging, "native staging path")
    staged_snapshot = staging / "snapshot"
    staged_output = staging / "output"
    try:
        _make_dir(staging, staged_snapshot, "native snapshot directory")
        _make_dir(staging, staged_output, "native output directory")
        _copy_file(root / "project.json", staged_snapshot / "project.json", "project manifest", source_root=root, destination_root=staging)
        _copy_file(root / "meta.json", staged_snapshot / "meta.json", "project metadata", source_root=root, destination_root=staging)

        source = root / "source.mp4"
        source_present = bool(source.exists() or source.is_symlink() or _reparse_point(source))
        if source_present:
            _copy_file(source, staged_snapshot / "source.mp4", "source video", source_root=root, destination_root=staging)
        _copy_file(source_scene, staged_snapshot / relative_scene, "scene file", source_root=root, destination_root=staging)

        pinned_root = root / "renders" / plan.id / "assets"
        _safe_path(root, pinned_root, "pinned asset directory", kind="directory")
        for asset in plan.assets:
            pinned = pinned_root / asset.id
            _safe_path(root, pinned, f"pinned asset {asset.id}", kind="file")
            sha256, length = _hash_file(pinned)
            if sha256 != asset.sha256 or length != asset.length:
                raise PlanConflict(f"pinned asset digest mismatch: {asset.id}")
            _copy_file(pinned, staged_snapshot / asset.project_path, f"asset {asset.id}", source_root=root, destination_root=staging)

        staged_scene = staged_snapshot / relative_scene
        composition = staged_snapshot / "composition.html"
        _safe_path(staged_snapshot, staged_scene, "staged scene file", kind="file")
        compose(load_scene(staged_scene), staged_scene.parent, composition)
        _safe_path(staged_snapshot, composition, "staged composition", kind="file")
        manifest = _write_stage_manifest(staging, staged_snapshot, plan, relative_scene, source_present=source_present)
        compatibility = staging / "composition.html"
        _safe_path(staging, compatibility, "composition compatibility copy", kind="file", allow_missing=True)
        try:
            os.link(composition, compatibility)
        except OSError:
            _copy_file(composition, compatibility, "composition compatibility copy", source_root=staging, destination_root=staging)
        _verify_stage_files(root, plan, staging, staged_snapshot, manifest, staged_output, relative_scene, scene=staged_scene, html=composition)
        os.replace(staging, native)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return native, native / "snapshot", relative_scene, native / "snapshot" / "composition.html", native / "output", native / _MANIFEST_NAME


def _manifest_arg(args: Mapping[str, Any]) -> Any:
    stage_manifest = args.get("stage_manifest")
    manifest = args.get("manifest")
    if stage_manifest is not None and manifest is not None and stage_manifest != manifest:
        raise PlanConflict("native stage manifest paths conflict")
    return stage_manifest if stage_manifest is not None else manifest


def verify_native_stage(args: Mapping[str, Any]) -> None:
    """Verify a plan-scoped native stage before a worker reads any artifact."""
    required = ("project_root", "plan_id", "plan_digest", "scene_relative", "snapshot", "scene", "html", "out")
    values = {key: args.get(key) for key in required}
    manifest_value = _manifest_arg(args)
    values["stage_manifest"] = manifest_value
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise PlanConflict("native stage arguments are incomplete")
    if not isinstance(args.get("stage_manifest_sha256"), str) or not _SHA256_RE.fullmatch(args["stage_manifest_sha256"]):
        raise PlanConflict("native stage manifest digest is invalid")
    path_values = [manifest_value, values["snapshot"], values["scene"], values["html"], values["out"]]
    package_value = args.get("package_root")
    if package_value is not None:
        if not isinstance(package_value, str) or not package_value:
            raise PlanConflict("native package root is invalid")
        path_values.append(package_value)
    if any(not Path(value).is_absolute() for value in path_values):
        raise PlanConflict("native stage paths must be absolute")

    root = _safe_root(Path(values["project_root"]))
    plan_id = values["plan_id"]
    if not _ID_RE.fullmatch(plan_id):
        raise PlanConflict("native plan id is invalid")
    plan = load_render_plan(root, plan_id)
    if plan.backend != "native" or plan.digest != values["plan_digest"]:
        raise PlanConflict("native stage plan identity does not match")
    state = load_render_plan_state(root, plan.id)
    if state.status != "approved":
        raise PlanConflict("native render plan must be approved")
    relative_scene = _project_relative(values["scene_relative"], "native scene path")
    if not relative_scene.startswith(f"scenes/{plan.scene_id}/"):
        raise PlanConflict("native scene path does not belong to the approved scene")
    manifest = Path(manifest_value)
    native = manifest.parent
    snapshot = Path(values["snapshot"])
    output = Path(values["out"])
    scene = Path(values["scene"])
    html = Path(values["html"])
    package_root = Path(package_value) if package_value is not None else None
    expected_native = root / "renders" / plan.id / "native"
    if not _same_path(native, expected_native):
        raise PlanConflict("native stage directory is not plan-scoped")
    _verify_stage_files(
        root,
        plan,
        native,
        snapshot,
        manifest,
        output,
        relative_scene,
        scene=scene,
        html=html,
        package_root=package_root,
        manifest_sha256=args["stage_manifest_sha256"],
    )


def prepare_native_job(root: Path, plan_id: str) -> JobSpec:
    """Prepare one approved native plan in an immutable, plan-scoped stage."""
    if not isinstance(plan_id, str) or not _ID_RE.fullmatch(plan_id):
        raise PlanConflict("invalid render plan id")
    root = _safe_root(Path(root))
    plan = load_render_plan(root, plan_id)
    if plan.backend != "native":
        raise PlanConflict("only native render plans can use the native job runner")

    plan_dir = root / "renders" / plan.id
    state_path = plan_dir / "state.json"
    # The plan state lock serializes approval-time staging across server threads
    # and processes, while the published directory rename keeps readers atomic.
    with _state_lock(state_path):
        state = load_render_plan_state(root, plan.id)
        if state.status != "approved":
            raise PlanConflict("native render plan must be approved")
        _validate_final_metadata(root, plan)
        native, snapshot, relative_scene, html, output, manifest = _stage_native(root, plan)
        manifest_digest, _ = _hash_file(manifest)
        scene = snapshot / relative_scene
        args: dict[str, Any] = {
            "project_root": str(root),
            "plan_id": plan.id,
            "plan_digest": plan.digest,
            "scene_relative": relative_scene,
            "snapshot": str(snapshot),
            "stage_manifest": str(manifest),
            "manifest": str(manifest),
            "stage_manifest_sha256": manifest_digest,
            "scene": str(scene),
            "html": str(html),
            "out": str(output),
        }
        if plan.mode == "final":
            args["package_root"] = str(snapshot)
            return JobSpec(kind="export", args=args)
        args["mp4"] = True
        return JobSpec(kind="render", args=args)


__all__ = ["prepare_native_job", "verify_native_stage"]
