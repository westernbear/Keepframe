import cv2
import numpy as np

from keepframe.edit.agent import edit
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import Target
from keepframe.ir.schema import Background, Canonical, Element, Scene
from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene


def _png(h, w):
    img = np.zeros((h, w, 4), np.uint8)
    img[..., 3] = 255
    return cv2.imencode(".png", img)[1].tobytes()


def test_texture_attachment_fits_original_box(tmp_path):
    scene = Scene(id="s1", size=(200, 100), fps=30, frames=10, background=Background(),
                  elements=[Element(id="e1", kind="sprite", canonical=Canonical(width=40, height=20), visible=(0, 9))])
    out = apply_edit(scene, tmp_path, [Target(element="e1", property="texture", value="attachment")], {}, _png(1000, 1000))
    assert (out.element("e1").canonical.width, out.element("e1").canonical.height) == (20.0, 20.0)


def test_confirm_without_attachment_does_not_generate(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=4, frames=12).model_copy(update={"id": "s1"})
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene)
    sprite = next(e for e in scene.elements if e.kind == "sprite")
    monkeypatch.setattr("keepframe.edit.agent.AssetClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("generated")))
    res = edit(root, "s1", "로고 교체", confirm=True,
               intent={"targets": [{"element": sprite.id, "property": "texture", "value": "attachment"}]})
    assert res.status == "failed" and res.error == "attachment_required"
