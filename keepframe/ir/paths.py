from pathlib import Path


def scene_asset_path(scene_dir: Path, value: str) -> Path:
    relative = Path(value)
    root = Path(scene_dir).resolve()
    resolved = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not resolved.is_relative_to(root):
        raise ValueError("image background path must stay inside the scene directory")
    return resolved
