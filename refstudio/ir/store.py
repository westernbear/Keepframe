from __future__ import annotations
from pathlib import Path
from .schema import Project, Scene, SceneRef, Version, dump, load_project_json, load_scene_json


def save_scene(scene: Scene, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(scene))


def load_scene(path: Path) -> Scene:
    return load_scene_json(Path(path).read_text())


def scene_dir(root: Path, scene_id: str) -> Path:
    return Path(root) / "scenes" / scene_id


def load_project(root: Path) -> Project:
    return load_project_json((Path(root) / "project.json").read_text())


def _save_project(root: Path, project: Project) -> None:
    (Path(root) / "project.json").write_text(dump(project))


def init_project(root: Path, source: dict, scene: Scene, note: str = "initial analysis") -> Project:
    root = Path(root)
    if (root / "project.json").exists():
        raise FileExistsError(root / "project.json")
    rel = f"scenes/{scene.id}/scene.v1.json"
    save_scene(scene, root / rel)
    project = Project(source=source, scenes=[SceneRef(id=scene.id, frames=(0, scene.frames - 1))],
                      versions=[Version(id="v1", parent=None, note=note, auto=True, scene_file=rel)])
    _save_project(root, project)
    return project


def _versions_for(project: Project, scene_id: str) -> list[Version]:
    return [v for v in project.versions if v.scene_file.startswith(f"scenes/{scene_id}/")]


def current_scene(root: Path, scene_id: str) -> tuple[Scene, Version]:
    project = load_project(root)
    v = _versions_for(project, scene_id)[-1]
    return load_scene(Path(root) / v.scene_file), v


def new_version(root: Path, scene_id: str, scene: Scene, note: str, auto: bool = True) -> Version:
    root = Path(root)
    project = load_project(root)
    prev = _versions_for(project, scene_id)
    n = len(prev) + 1
    rel = f"scenes/{scene_id}/scene.v{n}.json"
    if (root / rel).exists():
        raise FileExistsError(rel)  # append-only: never overwrite
    save_scene(scene, root / rel)
    v = Version(id=f"v{n}", parent=prev[-1].id if prev else None, note=note, auto=auto, scene_file=rel)
    project.versions.append(v)
    _save_project(root, project)
    return v
