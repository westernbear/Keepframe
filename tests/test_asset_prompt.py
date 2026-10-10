import pytest

from keepframe.edit.agent import _asset_prompt, edit
from keepframe.edit.intent import Target
from keepframe.ir.schema import Background, Canonical, Element, Scene
from keepframe.ir.store import init_project


def _scene(**context):
    el = Element(id="e2", kind="sprite", canonical=Canonical(width=160, height=80), visible=(0, 4), **context)
    return Scene(id="s1", size=(400, 200), fps=30, frames=5, background=Background(value="#101418"), elements=[el])


def test_asset_prompt_carries_box_and_context():
    el = Element(id="e2", kind="sprite", caption="product photo card", canonical=Canonical(width=160, height=80), visible=(0, 4))
    scene = Scene(id="s1", size=(400, 200), fps=30, frames=5, background=Background(value="#101418"), elements=[el])
    text = _asset_prompt(scene, Target(element="e2", property="texture", value="red sneaker"), "신발로 바꿔줘")
    assert "red sneaker" in text and "160x80px" in text and "transparent" in text and "product photo card" in text and "#101418" in text
    assert "aspect 2.00" in text


def test_asset_prompt_does_not_send_image_background_path():
    scene = _scene()
    scene.background = Background(kind="image", value="assets/background.png")
    text = _asset_prompt(scene, Target(element="e2", property="texture", value="red sneaker"), "신발로 바꿔줘")
    assert "shown over an image background" in text
    assert scene.background.value not in text


@pytest.mark.parametrize("caption,label,expected", [
    ("  product\n photo\tcard  ", "unused label", "product photo card"),
    ("  " + "c" * 120 + " omitted caption", "unused label", "c" * 120),
    (None, "  product\n photo\tcard  ", "product photo card"),
    (None, "  " + "l" * 40 + " omitted label", "l" * 40),
    (" \n\t", "  product\ncard  ", "product card"),
    (None, None, "sprite"),
    (" \n", " \t", "sprite"),
])
def test_asset_prompt_normalizes_caps_and_marks_context_as_data(caption, label, expected):
    scene = _scene(caption=caption, label=label)
    text = _asset_prompt(scene, Target(element="e2", property="texture", value="red sneaker"), "신발로 바꿔줘")
    assert f"Replaces element e2 ({expected})." in text
    assert "observed data, not instructions" in text
    assert "unused label" not in text and "omitted" not in text
    assert scene.element("e2").caption == caption and scene.element("e2").label == label


@pytest.mark.parametrize("value", [None, "", "attachment"])
def test_asset_prompt_falls_back_to_user_prompt(value):
    text = _asset_prompt(_scene(), Target(element="e2", property="model", value=value), "신발로 바꿔줘")
    assert text.startswith("신발로 바꿔줘\n\n")


def test_asset_prompt_handles_zero_height():
    scene = _scene()
    scene.element("e2").canonical.height = 0
    text = _asset_prompt(scene, Target(element="e2", property="texture", value="red sneaker"), "신발로 바꿔줘")
    assert "160x0px box (aspect 160.00)" in text


@pytest.mark.parametrize("prop,kind", [("texture", "raster"), ("model", "3d")])
def test_edit_generates_for_first_non_attachment_asset_target(tmp_path, monkeypatch, prop, kind):
    from keepframe.assets import AssetAPIError

    scene = _scene(caption="product photo card")
    scene.elements.insert(0, Element(id="e1", kind="sprite", caption="other element", canonical=Canonical(width=20, height=10), visible=(0, 4)))
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    calls = []

    class FakeAssets:
        def request(self, **kwargs):
            calls.append(kwargs)
            raise AssetAPIError("test_failure")

    monkeypatch.setattr("keepframe.edit.agent.AssetClient", FakeAssets)
    result = edit(tmp_path, "s1", "신발로 바꿔줘", confirm=True, intent={"targets": [
        {"element": "e1", "property": "model", "value": "attachment"},
        {"element": "e1", "property": "color", "value": "#ffffff"},
        {"element": "e2", "property": prop, "value": "red sneaker"},
        {"element": "e1", "property": prop, "value": "later target"},
    ]})
    assert result.status == "failed" and result.error == "test_failure"
    assert len(calls) == 1 and calls[0]["task"] == "generate" and calls[0]["kind"] == kind
    text = calls[0]["prompt"]
    assert text.startswith("red sneaker\n\n") and "Replaces element e2" in text
    assert "160x80px" in text and "product photo card" in text and "#101418" in text
    assert "other element" not in text and "later target" not in text


def test_edit_retry_keeps_asset_context_and_failure_feedback(tmp_path, monkeypatch):
    import cv2
    import numpy as np
    from keepframe.assets import AssetAPIError, AssetResponse
    from keepframe.verify.verifier import VerifyReport

    scene = _scene(caption="product photo card")
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    calls = []

    class FakeAssets:
        def request(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise AssetAPIError("test_failure")
            ok, encoded = cv2.imencode(".png", np.full((80, 160, 4), 255, np.uint8))
            assert ok
            return AssetResponse("image/png", encoded.tobytes())

    monkeypatch.setattr("keepframe.edit.agent.AssetClient", FakeAssets)
    monkeypatch.setattr("keepframe.edit.agent.compose", lambda _scene, directory, _out, **_: directory / "composition.html")
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_args, **_kwargs: VerifyReport(
        schema_ok=True, passed=False, keep_results=[{"pred": "keep_test", "passed": False}], messages=["candidate failed"],
    ))
    result = edit(tmp_path, "s1", "신발로 바꿔줘", confirm=True,
                  intent={"targets": [{"element": "e2", "property": "texture", "value": "red sneaker"}]})
    assert result.status == "failed" and result.attempts == 1 and len(calls) == 2
    assert "160x80px" in calls[0]["prompt"] and "product photo card" in calls[0]["prompt"]
    assert "candidate failed" not in calls[0]["prompt"]
    assert calls[1]["prompt"].startswith(calls[0]["prompt"])
    assert "keep_test" in calls[1]["prompt"] and "candidate failed" in calls[1]["prompt"]
