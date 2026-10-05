import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from keepframe.assets import validate_glb
from keepframe.ir.store import current_scene, load_project, scene_dir
from keepframe.web.workspace import list_projects
from tests.test_three import _triangle_glb


def test_live_check_rejects_missing_clip_before_creating_workspace(tmp_path):
    from scripts import ae_live_check

    workspace = tmp_path / "workspace"
    with pytest.raises(FileNotFoundError):
        ae_live_check.prepare_checks(tmp_path / "missing.mp4", workspace)
    assert not workspace.exists()


@pytest.mark.browser
def test_live_check_cli_prepares_reviewable_projects_and_native_comparison_frames(tmp_path, capsys):
    from scripts import ae_live_check

    # A short gradient clip runs the real branch analysis/plate path without
    # depending on the controller's evaluation directory or optional OCR/GPU.
    clip = tmp_path / "gradient.mp4"
    writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 30, (640, 320))
    assert writer.isOpened()
    ramp = np.linspace(0, 255, 640, dtype=np.uint8)
    background = np.tile(np.stack([ramp, ramp[::-1], np.full(640, 80, np.uint8)], -1), (320, 1, 1))
    for frame in range(12):
        image = background.copy()
        image[20:36, 5 + frame * 8:21 + frame * 8] = 255
        writer.write(image)
    writer.release()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = workspace / "existing.txt"
    sentinel.write_text("preserve")
    assert ae_live_check.main(["--clip", str(clip), "--workspace", str(workspace), "--ig2-frames", "12"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert Path(output["workspace"]) == workspace
    assert sentinel.read_text() == "preserve"
    assert len(list_projects(workspace)) == 3
    checks = {item["check"]: item for item in output["projects"]}
    assert set(checks) == {"reveal", "plate", "model"}
    for check in checks.values():
        root = workspace / check["project_id"]
        scene, version = current_scene(root, check["scene_id"])
        assert version.id == "v1"
        assert not load_project(root).approved_scenes
        assert check["review_url"] == f"/review?project={root.name}&scene={scene.id}&v=v1"
        assert check["ae_export_url"] == f"/agent?project={root.name}&scene={scene.id}&v=v1"
        assert check["frames"][0] == 0 and check["frames"][-1] == scene.frames - 1
        assert all(0 <= frame < scene.frames for frame in check["frames"])
        assert all((root / path).is_file() for path in check["native_frames"].values())
    reveal_root = workspace / checks["reveal"]["project_id"]
    reveal, _ = current_scene(reveal_root, "s1")
    assert len(reveal.elements) == 1 and reveal.elements[0].kind == "text"
    assert [(key.t, key.v) for key in reveal.elements[0].tracks["reveal"].keys] == [(0, 0), (30, 1)]
    images = np.load(scene_dir(reveal_root, "s1") / "stages/frames.npy")
    foreground_counts = [np.count_nonzero(images[frame].max(axis=2) > 100) for frame in [0, 15, 30]]
    assert foreground_counts[0] == 0 < foreground_counts[1] < foreground_counts[2]
    plate_root = workspace / checks["plate"]["project_id"]
    plate, _ = current_scene(plate_root, "s1")
    assert plate.background.kind == "image"
    assert (scene_dir(plate_root, "s1") / plate.background.value).is_file()
    assert (plate_root / "source.mp4").read_bytes() == clip.read_bytes()
    model_root = workspace / checks["model"]["project_id"]
    model, _ = current_scene(model_root, "s1")
    assert len(model.elements) == 1 and model.elements[0].kind == "3d"
    element = model.elements[0]
    assert [(key.t, key.v) for key in element.tracks["ry"].keys] == [(0, 0), (60, 360)]
    model_dir = scene_dir(model_root, "s1")
    assert validate_glb((model_dir / element.canonical.model).read_bytes()) == _triangle_glb()
    assert (model_dir / element.canonical.texture).is_file()
    images = np.load(model_dir / "stages/frames.npy")
    assert np.array_equal(images[0], images[60])
    assert not np.array_equal(images[0], images[10])
