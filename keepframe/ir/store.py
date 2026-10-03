from __future__ import annotations
from pathlib import Path
import threading
import os
import tempfile
from .schema import Project, Scene, SceneRef, Version, dump, load_project_json, load_scene_json


_STORE_LOCK = threading.RLock()

def save_scene(scene: Scene, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(scene))


def load_scene(path: Path) -> Scene:
    return load_scene_json(Path(path).read_text())


def scene_dir(root: Path, scene_id: str) -> Path:
    return Path(root) / "scenes" / scene_id


def load_project(root: Path) -> Project:
    root = Path(root)
    with _STORE_LOCK:
        project = load_project_json((root / "project.json").read_text())
        if not project.analysis_migrated:
            from ..review.overlay import snapshot_from_stages
            for ref in project.scenes:
                versions = _versions_for(project, ref.id)
                if versions and not versions[-1].analysis_file:
                    latest = versions[-1]
                    latest.analysis_file = snapshot_from_stages(scene_dir(root, ref.id), load_scene(root / latest.scene_file))
            project.analysis_migrated = True
            _save_project(root, project)
        return project


def _save_project(root: Path, project: Project) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as stream:
        stream.write(dump(project))
        temporary = Path(stream.name)
    try:
        os.replace(temporary, Path(root) / "project.json")
    finally:
        temporary.unlink(missing_ok=True)


def init_project(root: Path, source: dict, scene: Scene, note: str = "initial analysis",
                 analysis_file: str | None = None) -> Project:
    return init_project_scenes(
        root,
        source,
        [(scene, (0, scene.frames - 1), None)],
        note=note,
        analysis_files={scene.id: analysis_file},
    )


def init_project_scenes(
    root: Path,
    source: dict,
    scenes: list[tuple[Scene, tuple[int, int], dict | None]],
    *,
    links: list[dict] | None = None,
    note: str = "initial analysis",
    analysis_files: dict[str, str | None] | None = None,
) -> Project:
    root = Path(root)
    if (root / "project.json").exists():
        raise FileExistsError(root / "project.json")
    refs: list[SceneRef] = []
    versions: list[Version] = []
    for scene, frame_range, transition_out in scenes:
        rel = f"scenes/{scene.id}/scene.v1.json"
        save_scene(scene, root / rel)
        refs.append(SceneRef(id=scene.id, frames=frame_range, transition_out=transition_out))
        versions.append(Version(id="v1", parent=None, note=note, auto=True, scene_file=rel, analysis_file=(analysis_files or {}).get(scene.id)))
    project = Project(source=source, analysis_migrated=True, scenes=refs, links=links or [], versions=versions)
    _save_project(root, project)
    return project


def approve_scene(root: Path, scene_id: str, version_id: str) -> Project:
    root = Path(root)
    project = load_project(root)
    if not any(
        version.id == version_id and version.scene_file.startswith(f"scenes/{scene_id}/")
        for version in project.versions
    ):
        raise ValueError("version does not belong to scene")
    project.approved_scenes[scene_id] = version_id
    _save_project(root, project)
    return project


def _versions_for(project: Project, scene_id: str) -> list[Version]:
    return [v for v in project.versions if v.scene_file.startswith(f"scenes/{scene_id}/")]


def current_scene(root: Path, scene_id: str) -> tuple[Scene, Version]:
    project = load_project(root)
    v = _versions_for(project, scene_id)[-1]
    return load_scene(Path(root) / v.scene_file), v


def new_version(root: Path, scene_id: str, scene: Scene, note: str, auto: bool = True,
                analysis_file: str | None = None, parent_version: str | None = None) -> Version:
    root = Path(root)
    project = load_project(root)
    prev = _versions_for(project, scene_id)
    parent = next((v for v in prev if v.id == parent_version), None) if parent_version else (prev[-1] if prev else None)
    if parent_version and parent is None:
        raise ValueError("unknown parent version")
    n = len(prev) + 1
    rel = f"scenes/{scene_id}/scene.v{n}.json"
    if (root / rel).exists():
        raise FileExistsError(rel)  # append-only: never overwrite
    save_scene(scene, root / rel)
    v = Version(id=f"v{n}", parent=parent.id if parent else None, note=note, auto=auto, scene_file=rel,
                analysis_file=analysis_file if analysis_file is not None else (parent.analysis_file if parent else None))
    project.versions.append(v)
    _save_project(root, project)
    return v
