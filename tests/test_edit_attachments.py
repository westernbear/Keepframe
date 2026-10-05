from pathlib import Path

import cv2
import numpy as np
import pytest

from keepframe.edit.agent import edit
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import Target
from keepframe.ir.schema import Background, Canonical, Element, Scene
from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene


@pytest.mark.parametrize("via", ["edit", "api"])
def test_server_attachment_path_string_is_rejected_without_read_or_version(tmp_path, monkeypatch, via):
    from keepframe.ir.store import current_scene, load_project
    from tests.test_chat_attachments import _project
    from tests.test_web_edit import _post
    from tests.test_web_server import start

    root, scene = _project(tmp_path)
    reads = []
    original = Path.read_bytes
    def track_read(path):
        if path == Path("/etc/hostname"):
            reads.append(path)
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", track_read)
    intent = {"targets": [{"element": "e1", "property": "texture", "value": "attachment"}]}
    if via == "edit":
        result = edit(root, "s1", "replace image", confirm=True,
                      attachment="/etc/hostname", intent=intent).to_json()
    else:
        srv = start(root.parent)
        try:
            code, result = _post(srv, "/api/edit", {
                "project": "p1", "scene": "s1", "v": "v1", "prompt": "replace image",
                "confirm": True, "attachment": "/etc/hostname", "intent": intent,
            })
            assert 400 <= code < 500
        finally:
            srv.shutdown()
            srv.server_close()
    assert result["status"] == "failed" and result["error"] == "invalid_attachment"
    assert result["version"] is None
    assert reads == []
    assert current_scene(root, "s1")[0] == scene
    assert [v.id for v in load_project(root).versions] == ["v1"]


def test_cli_attachment_path_object_still_loads_png(tmp_path):
    path = tmp_path / "input.png"
    path.write_bytes(_png(20, 40))
    scene = Scene(id="s1", size=(200, 100), fps=30, frames=10, background=Background(),
                  elements=[Element(id="e1", kind="sprite", canonical=Canonical(width=40, height=20), visible=(0, 9))])
    out = apply_edit(scene, tmp_path, [Target(element="e1", property="texture", value="attachment")], {}, path)
    assert (tmp_path / out.element("e1").canonical.texture).read_bytes().startswith(b"\x89PNG")


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


def test_confirm_attachment_preview_without_file(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=4, with_text=False, frames=12).model_copy(update={"id": "s1"})
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene)
    sprite = next(e for e in scene.elements if e.kind == "sprite")
    prompt = f"{sprite.id} 이미지 교체"
    monkeypatch.setattr("keepframe.edit.agent.AssetClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("generated")))

    preview = edit(root, "s1", prompt, has_attachment=True)
    assert preview.status == "needs_confirm"
    assert preview.intent.targets[0].property == "texture"
    assert preview.intent.targets[0].value == "attachment"

    res = edit(root, "s1", prompt, confirm=True, intent=preview.intent)
    assert res.status == "failed"
    assert res.error == "attachment_required"
    assert res.attempts == 0
