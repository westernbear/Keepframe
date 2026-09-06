import pytest
from refstudio.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from refstudio.ir.store import init_project, load_project, current_scene, new_version, scene_dir, load_scene


def scene():
    return Scene(id="s1", size=(64, 32), fps=30, frames=10, background=Background(),
                 elements=[Element(id="e1", kind="sprite", canonical=Canonical(width=8, height=8), visible=(0, 9))])


def test_init_and_versions_are_append_only(tmp_scene_dir):
    root = tmp_scene_dir
    p = init_project(root, {"file": "x.mp4", "fps": 30, "size": [64, 32], "mode": "range", "range": [0, 9]}, scene())
    assert (root / "project.json").exists()
    assert (scene_dir(root, "s1") / "scene.v1.json").exists()
    s, v = current_scene(root, "s1")
    assert v.id == "v1" and v.parent is None and s.id == "s1"
    s2 = s.model_copy(deep=True)
    s2.elements[0].tracks["x"] = Track(keys=[Keyframe(t=0, v=3.0)])
    v2 = new_version(root, "s1", s2, note="moved e1", auto=False)
    assert v2.id == "v2" and v2.parent == "v1" and v2.auto is False
    assert load_scene(scene_dir(root, "s1") / "scene.v1.json").elements[0].tracks == {}   # v1 untouched
    assert load_project(root).versions[-1].id == "v2"
    s3, v3 = current_scene(root, "s1")
    assert v3.id == "v2" and s3.elements[0].tracks["x"].keys[0].v == 3.0
