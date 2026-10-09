"""Task 12R: a background colour edit applies as asked (no forced choice) and reports structured facts the LLM
agent reads; an attached PNG can become the background; Keepframe adds no reply wording of its own."""
import base64
import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest

from keepframe.edit.agent import edit
from keepframe.edit.intent import Intent, Target, plan
from keepframe.ir.schema import Background, Canonical, Element, Gradient, GradientStop, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, init_project, load_project, load_scene, scene_dir
from keepframe.render.renderer import RenderResult
from keepframe.session.tools import SessionContext, run_tool
from keepframe.verify.verifier import VerifyReport

NAVY = "#1a2a6c"
W, H = 160, 90


def _png(rgb) -> bytes:
    ok, data = cv2.imencode(".png", np.ascontiguousarray(rgb[..., ::-1]))
    assert ok
    return data.tobytes()


def _picture():
    ys, xs = np.mgrid[0:H, 0:W]
    return np.dstack([40 + xs, 80 + ys, 160 + 0 * xs]).clip(0, 255).astype(np.uint8)


def _background(kind, sd):
    (sd / "assets").mkdir(parents=True, exist_ok=True)
    if kind == "color":
        return Background(kind="color", value="#203040")
    (sd / "assets" / "background.png").write_bytes(_png(_picture()))
    if kind == "image":
        return Background(kind="image", value="assets/background.png", confidence=0.8)
    if kind == "gradient":
        g = Gradient(kind="linear", angle=135, stops=[GradientStop(offset=0, color="#f6d365"), GradientStop(offset=1, color="#5b247a")])
        return Background(kind="gradient", value="#c06a80", gradient=g, poster="assets/background.png")
    (sd / "assets" / "background.webm").write_bytes(b"webm")
    return Background(kind="video", value="assets/background.webm", poster="assets/background.png")


def _project(tmp_path, kind, monkeypatch):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=20, height=10, color="#ffffff"), visible=(0, 4),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=40)]), "y": Track(keys=[Keyframe(t=0, v=40)])})
    scene = Scene(id="s1", size=(W, H), fps=30, frames=5, background=_background(kind, sd), elements=[el])
    init_project(root, {"file": "ref.mp4", "fps": 30, "size": [W, H]}, scene)
    monkeypatch.setattr("keepframe.edit.agent.render", lambda html, scene, out_dir: RenderResult(
        frames_dir=out_dir / "frames", frames=list(range(scene.frames)), hashes=[], bboxes={"e1": [[30, 35, 50, 45]] * scene.frames}))
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_a, **_k: VerifyReport(
        schema_ok=True, keep_pass_rate=1, temporal=1, layer_probe_complete=True, passed=True))
    return root, scene_dir(root, "s1"), scene


def _assets(sd):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((sd / "assets").iterdir())}


def _colour_intent(**kw):
    return Intent(targets=[Target(property="background", value=NAVY, **kw)])


BEFORE = {"image": {"kind": "image", "color": None, "asset": "assets/background.png"},
          "gradient": {"kind": "gradient", "color": "#c06a80", "asset": "assets/background.png"},
          "video": {"kind": "video", "color": None, "asset": "assets/background.webm"},
          "color": {"kind": "color", "color": "#203040", "asset": None}}


@pytest.mark.parametrize("kind", ["image", "gradient", "video"])
def test_background_color_edit_on_picture_applies_and_reports_facts(tmp_path, monkeypatch, kind):
    root, sd, scene = _project(tmp_path, kind, monkeypatch)
    before = _assets(sd)
    assert not plan(scene, _colour_intent()).conflicts                     # no forced tint / replace / cancel
    preview = edit(root, "s1", "make the background navy", intent=_colour_intent())
    assert preview.status == "needs_confirm" and not preview.plan.conflicts
    assert preview.background == {"mode": "replace", "color": NAVY, "before": BEFORE[kind], "after": None}
    done = edit(root, "s1", "make the background navy", confirm=True, intent=_colour_intent())
    assert done.status == "done" and done.version.id == "v2", done
    assert done.background == {"mode": "replace", "color": NAVY, "before": BEFORE[kind],
                               "after": {"kind": "color", "color": NAVY, "asset": None}}
    assert done.to_json()["background"] == done.background                 # the HTTP edit response carries it
    edited, _ = current_scene(root, "s1")
    assert edited.background == Background(kind="color", value=NAVY, confidence=1.0)
    assert load_scene(root / load_project(root).versions[0].scene_file) == scene   # undo = the version history
    assert _assets(sd) == before


def test_background_color_edit_on_color_reports_facts(tmp_path, monkeypatch):
    root, sd, scene = _project(tmp_path, "color", monkeypatch)
    done = edit(root, "s1", "배경을 남색으로", confirm=True, intent=_colour_intent())
    assert done.status == "done", done
    assert done.background == {"mode": "replace", "color": NAVY, "before": BEFORE["color"],
                               "after": {"kind": "color", "color": NAVY, "asset": None}}
    text = edit(root, "s1", "x", intent=Intent(targets=[Target(element="e1", property="color", value="#ff0000")]))
    assert text.background is None                                          # facts only for background edits


def test_agent_tool_result_carries_background_facts(tmp_path, monkeypatch):
    from keepframe.session.agent import _scene_summary
    from keepframe.session.tools import TOOL_SCHEMAS
    root, sd, scene = _project(tmp_path, "gradient", monkeypatch)
    ctx = SessionContext(root, "s1")
    assert "background gradient linear 135°" in _scene_summary(ctx)        # the agent sees the kind before acting
    tool = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "edit")["function"]
    assert "A background colour edit replaces the whole background" in tool["description"]
    assert tool["parameters"]["properties"]["targets"]["items"]["properties"]["mode"]["anyOf"][0]["enum"] == ["replace", "tint"]
    res = run_tool("edit", ctx, {"prompt": "navy", "targets": [{"property": "background", "value": NAVY}]})
    assert res["ok"] and res["needs_confirm"] and not res["needs_choice"]
    assert res["payload"]["background"] == {"mode": "replace", "color": NAVY, "before": BEFORE["gradient"], "after": None}
    tinted = run_tool("edit", ctx, {"prompt": "navy", "targets": [{"property": "background", "value": NAVY, "mode": "tint"}]})
    assert tinted["payload"]["background"]["mode"] == "tint" and tinted["payload"]["intent"]["targets"][0]["mode"] == "tint"
    bad = run_tool("edit", ctx, {"prompt": "x", "targets": [{"element": "e1", "property": "color", "value": NAVY, "mode": "tint"}]})
    assert not bad["ok"]                                                      # mode belongs to a background colour
    assert current_scene(root, "s1")[1].id == "v1"


def test_background_image_attachment_replaces_background(tmp_path, monkeypatch):
    root, sd, scene = _project(tmp_path, "gradient", monkeypatch)
    (sd / "assets" / "background.img1.png").write_bytes(b"an earlier asset of the same name")
    before = _assets(sd)
    picture = _png(np.full((H, W, 3), (200, 40, 90), np.uint8))
    intent = Intent(targets=[Target(property="background", value="attachment")])
    missing = edit(root, "s1", "use my picture", confirm=True, intent=intent)
    assert missing.status == "failed" and missing.error == "attachment_required"
    for bad in (b"GIF89a" + bytes(40), b"\x89PNG\r\n\x1a\n" + bytes(40), "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff").decode()):
        res = edit(root, "s1", "use my picture", confirm=True, intent=intent, attachment=bad)
        assert res.status == "failed" and res.error == "invalid_attachment", res
    assert current_scene(root, "s1")[1].id == "v1" and _assets(sd) == before
    url = "data:image/png;base64," + base64.b64encode(picture).decode()
    done = edit(root, "s1", "use my picture", confirm=True, intent=intent, attachment=url)
    assert done.status == "done" and done.version.id == "v2", done
    bg = current_scene(root, "s1")[0].background
    assert bg == Background(kind="image", value="assets/background.img2.png", confidence=1.0)
    assert (sd / "assets" / "background.img2.png").read_bytes() == picture
    after = _assets(sd)
    assert {k: after[k] for k in before} == before                          # nothing overwritten
    assert done.background == {"mode": "image", "color": None, "before": BEFORE["gradient"],
                               "after": {"kind": "image", "color": None, "asset": "assets/background.img2.png"}}


def test_no_forced_choice_or_code_made_wording_left():
    """The tint / replace / cancel prompt, the `cancelled` status and Task 12's reply wording are gone from the
    server and both pages; the agent writes its own replies."""
    root = Path(__file__).resolve().parents[1] / "keepframe"
    agent_py = (root / "edit" / "agent.py").read_text()
    assert '"cancelled"' not in agent_py and "background_kind" not in (root / "edit" / "intent.py").read_text()
    static = root / "web" / "static" / "js"
    for name in ("agent.js", "edit-status.js", "review/edit-form.js", "i18n.js"):
        src = (static / name).read_text()
        for gone in ("cancelled", "background_kind", "editChoice.tint", "toolPending", "bgTinted", "editBgChoose"):
            assert gone not in src, (name, gone)
