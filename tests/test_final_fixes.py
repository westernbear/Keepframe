import base64
import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np
import pytest

from keepframe.analyze.composite import composite_scene
from keepframe.analyze.fonts import installed_families
from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames, rerun
from keepframe.analyze.text import TextBox, TextTrack, text_props
from keepframe.compose.composer import compose
from keepframe.edit.agent import EditResult
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import Target
from keepframe.edit.textraster import render_lines
from keepframe.ir.schema import Background, Canonical, Constraint, Element, Scene, load_scene_json
from keepframe.ir.store import current_scene, init_project, load_project, scene_dir
from keepframe.render.lottie import preflight_lottie, write_lottie
from keepframe.render.plan import PlanConflict
from keepframe.session.llm import make_llm
from keepframe.session.provider import ProviderConfig, load_llm_settings, save_llm_settings
from keepframe.session.tools import SessionContext, _json_payload, run_tool
from keepframe.verify.verifier import verify
from tests.test_background_plate import _gradient_clip
from tests.test_web_server import start

ROOT = Path(__file__).resolve().parents[1]


def _scene():
    return Scene(id="s1", size=(200, 150), fps=30, frames=4, background=Background(), elements=[
        Element(id="e1", kind="sprite", canonical=Canonical(width=70, height=115), visible=(0, 3)),
    ])


def _many_keeps(failed):
    scene = _scene()
    scene.constraints = [Constraint(pred="intersect(e1,e1)", keep=True) for _ in range(1000 - failed)] + [
        Constraint(pred=f"left(e1,e1)@{i % 4}", keep=True) for i in range(failed)]
    scene.constraints.append(Constraint(pred="left(e1,e1)", keep=False))
    return scene


@pytest.mark.parametrize("failed", [0, 75, 1000])
def test_verify_tool_bounds_thousand_kept_predicates(tmp_path, failed):
    scene = _many_keeps(failed)
    init_project(tmp_path, {"file": "ref.mp4"}, scene)
    result = run_tool("verify", SessionContext(tmp_path, "s1"), {})
    payload = result["payload"]["verify"]
    assert len(json.dumps(result).encode()) < 10_000
    assert "keep_results" not in payload
    assert (payload["keep_total"], payload["keep_failed"]) == (1000, failed)
    assert payload["keep_pass_rate"] == pytest.approx((1000 - failed) / 1000)
    expected = [{"pred": c.pred, "passed": False} for c in scene.constraints if c.keep and c.pred.startswith("left(")]
    assert payload["keep_failures"] == expected[:20]


@pytest.mark.parametrize("failed", [0, 75, 1000])
def test_edit_result_bounds_failures_in_all_serializers(tmp_path, failed):
    report = verify(_many_keeps(failed), tmp_path)
    result = EditResult(status="failed", verify=report)
    for payload in (result.to_json(), result.model_dump(), json.loads(result.model_dump_json()), _json_payload(result)):
        compact = payload["verify"]
        assert (compact["keep_total"], compact["keep_failed"]) == (1000, failed)
        assert compact["keep_results"] == [r for r in report.keep_results if not r["passed"]][:50]
    assert len(report.keep_results) == 1000


def test_verify_chip_uses_counts_and_legacy_fallback(tmp_path):
    node = shutil.which("node")
    assert node, "node is needed to verify the chip behavior"
    src = (ROOT / "keepframe/web/static/js/agent.js").read_text()
    src = src[src.index("function isKeepPassed("):src.index("function appendAgent(")]
    script = tmp_path / "verify-chip.cjs"
    script.write_text("""const assert = require('node:assert/strict');
const KEEP_PASS_RATE = 1, CONFIDENCE_PERCENT = 100;
const document = {createElement: () => ({})};
const chips = [], logEl = {appendChild: c => chips.push(c)};
const T = key => key;
""" + src + r"""
appendVerify({keep_total:1000, keep_failed:0, keep_pass_rate:1, keep_results:[]});
assert.match(chips.at(-1).textContent, /PASS.*100% \(1000\)/);
assert.match(chips.at(-1).className, /--pass/);
appendVerify({keep_total:1000, keep_failed:75, keep_pass_rate:1, keep_results:[]});
assert.match(chips.at(-1).textContent, /FAIL.*93% \(1000\)/);
appendVerify({keep_total:0, keep_failed:0, keep_results:[]});
assert.equal(chips.at(-1).textContent, 'agent.verifyNoKeep');
appendVerify({keep_pass_rate:1, keep_results:[{passed:true}, {passed:true}]});
assert.match(chips.at(-1).textContent, /PASS.*100% \(2\)/);
appendVerify({keep_pass_rate:0.5, keep_results:[{passed:true}, {passed:false}]});
assert.match(chips.at(-1).textContent, /FAIL.*50% \(2\)/);
""")
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True)


def test_alternating_texture_swaps_fit_analysis_crop(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    original = np.full((115, 70, 4), 255, np.uint8)
    cv2.imwrite(str(assets / "e1.png"), original)
    original_bytes = (assets / "e1.png").read_bytes()
    scene = _scene()
    target = Target(element="e1", property="texture", value="attachment")
    for w, h in [(200, 100), (100, 200)] * 2:
        attachment = cv2.imencode(".png", np.full((h, w, 4), 255, np.uint8))[1].tobytes()
        scene = apply_edit(scene, tmp_path, [target], {}, attachment)
        canonical = scene.element("e1").canonical
        scale = min(70 / w, 115 / h)
        assert (canonical.width, canonical.height) == pytest.approx((w * scale, h * scale))
        assert canonical.width <= 70 and canonical.height <= 115
    assert (assets / "e1.png").read_bytes() == original_bytes


@pytest.mark.parametrize("family", ["sans-serif", "DejaVu Serif"])
def test_font_guess_requires_clear_improvement_over_sans(family):
    if family != "sans-serif" and family not in installed_families():
        pytest.skip("needs installed DejaVu Serif")
    text, size = "Launch faster", 40
    img = render_lines([text], size, (255, 255, 255), family)
    assert img is not None
    alpha = img[..., 3] > 127
    ys, xs = np.nonzero(alpha)
    stroke = alpha[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = max(50, stroke.shape[0]), stroke.shape[1]
    frames = np.zeros((1, h, w, 3), np.uint8)
    frames[0, :stroke.shape[0]] = stroke[..., None] * 255
    track = TextTrack(id=1, boxes={0: TextBox(0, text, (0, 0, w, h), 0.99)}, text=text)
    font = text_props(track, frames, (0, 0, 0), 1, 0)[3]
    assert font.candidates
    assert font.family_guess == family


@pytest.mark.parametrize("holes,expected", [(4, "sans-serif"), (5, "DejaVu Serif")])
def test_font_guess_clear_margin_boundary(monkeypatch, holes, expected):
    from keepframe.analyze import fonts
    candidate = np.full((1, 100, 4), 255, np.uint8)
    baseline = candidate.copy()
    baseline[0, 1:holes + 1, 3] = 0
    monkeypatch.setattr(fonts, "installed_families", lambda: ("DejaVu Serif",))
    monkeypatch.setattr(fonts, "render_lines", lambda lines, size, rgb, family: baseline if family == "sans-serif" else candidate)
    frames = np.full((1, 1, 100, 3), 255, np.uint8)
    track = TextTrack(id=1, boxes={0: TextBox(0, "Text", (0, 0, 100, 1), 0.99)}, text="Text")
    font = text_props(track, frames, (0, 0, 0), 1, 0)[3]
    assert font.family_guess == expected and font.candidates == ["DejaVu Serif"]


def test_lottie_plate_is_embedded_bottom_image_layer(tmp_path):
    scene = _scene()
    scene.background = Background(kind="image", value="plate.png")
    (tmp_path / "assets").mkdir()
    cv2.imwrite(str(tmp_path / "assets/e1.png"), np.full((115, 70, 4), 255, np.uint8))
    scene.element("e1").canonical.texture = "assets/e1.png"
    plate = np.full((150, 200, 3), (30, 70, 100), np.uint8)
    cv2.imwrite(str(tmp_path / "plate.png"), plate)
    animation = json.loads(write_lottie(scene, tmp_path, tmp_path / "animation.json").read_text())
    assert len(animation["layers"]) == 2
    assert animation["layers"][0]["nm"] == "e1"
    layer = animation["layers"][-1]
    assert layer["ty"] == 2 and layer["nm"] == "background"
    assert (layer["ip"], layer["op"]) == (0, scene.frames)
    assert layer["ks"]["p"]["k"] == [0, 0, 0]
    asset = next(a for a in animation["assets"] if a["id"] == layer["refId"])
    assert (asset["w"], asset["h"]) == scene.size
    assert asset["e"] == 1 and asset["p"].startswith("data:image/png;base64,")
    assert base64.b64decode(asset["p"].split(",", 1)[1]) == (tmp_path / "plate.png").read_bytes()
    scene.background = scene.background.model_copy(update={"kind": "unknown"})
    with pytest.raises(PlanConflict):
        preflight_lottie(scene)


@pytest.mark.parametrize("stage", ["analyze", "rerun"])
def test_plate_analysis_and_rerun_skip_torch_refine(tmp_path, monkeypatch, stage):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("refine must not run over a plate")
    monkeypatch.setattr("keepframe.analyze.refine.torch_available", lambda: True)
    monkeypatch.setattr("keepframe.analyze.refine.refine_affine", forbidden)
    opts = AnalyzeOptions(ocr=False, refine=stage == "analyze", use_ecc=False)
    frames = _gradient_clip(h=320, w=640)
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", opts)
    if stage == "rerun":
        init_project(tmp_path, {"file": "ref.mp4"}, scene)
        rerun(tmp_path, "s1", "sprites", note="check plate refine", options=AnalyzeOptions(ocr=False, refine=True, use_ecc=False))
        scene, _ = current_scene(tmp_path, "s1")
    assert scene.background.kind == "image"
    assert any(e.kind == "sprite" for e in scene.elements)
    assert calls == []
    report = json.loads((scene_dir(tmp_path, "s1") / "report.json").read_text())
    assert "refine skipped: background plate scenes are not supported by refine yet" in report["messages"]


def _post(server, route, payload, origin):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = base if origin == "same" else origin
    req = Request(base + route, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urlopen(req) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.mark.parametrize("route,success", [("/api/edit", 200), ("/api/keep", 200), ("/api/correct", 202)])
def test_mutating_core_endpoints_require_same_origin(tmp_path, monkeypatch, route, success):
    root = tmp_path / "p1"
    scene = _many_keeps(75)
    init_project(root, {"file": "ref.mp4"}, scene)
    calls = []
    report = verify(scene, scene_dir(root, "s1"))
    def fake_edit(*args, **kwargs):
        calls.append("edit")
        return EditResult(status="failed", verify=report)
    monkeypatch.setattr("keepframe.web.server.run_edit", fake_edit)
    monkeypatch.setattr("keepframe.web.server.ReviewState.run_correction", lambda *a: calls.append("correct"))
    payload = {"project": "p1", "prompt": "e1 color", "preset": "none", "op": "text", "args": {}}
    server = start(tmp_path)
    try:
        for origin in ("http://attacker.example", None):
            code, body = _post(server, route, payload, origin)
            assert code == 403 and "origin" in body["error"]
            assert calls == [] and len(load_project(root).versions) == 1
        code, body = _post(server, route, payload, "same")
        assert code == success
        if route == "/api/edit":
            compact = body["verify"]
            assert (compact["keep_total"], compact["keep_failed"]) == (1000, 75)
            assert len(compact["keep_results"]) == 50 and all(not r["passed"] for r in compact["keep_results"])
        elif route == "/api/keep":
            assert len(load_project(root).versions) == 2
        else:
            assert calls == ["correct"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("family,expected", [("Noto_Sans.Regular", "sans-serif"), ("x,y", "sans-serif"),
    *[(f"x{char}y", "sans-serif") for char in ";:()'\"\\<>"],
    ("Noto Sans CJK KR", "Noto Sans CJK KR"), ("sans-serif", "sans-serif")])
def test_legacy_scene_font_family_is_sanitized(family, expected, caplog):
    data = _scene().model_dump(by_alias=True)
    data["elements"][0]["canonical"]["font"] = {"family_guess": family}
    loaded = load_scene_json(json.dumps(data))
    assert loaded.element("e1").canonical.font.family_guess == expected
    if family != expected:
        assert any(record.levelname == "WARNING" and "font family" in record.message for record in caplog.records)


@pytest.mark.parametrize("consumer", ["compose", "composite"])
@pytest.mark.parametrize("path_kind", ["absolute", "parent", "internal_parent", "symlink"])
def test_image_background_rejects_unsafe_path(tmp_path, consumer, path_kind):
    directory = tmp_path / "scene"
    directory.mkdir()
    outside = tmp_path / "secret.png"
    cv2.imwrite(str(outside), np.full((150, 200, 3), 255, np.uint8))
    cv2.imwrite(str(directory / "plate.png"), np.full((150, 200, 3), 128, np.uint8))
    (directory / "assets").mkdir()
    (directory / "escape.png").symlink_to(outside)
    path = {"absolute": str(outside), "parent": "../secret.png", "internal_parent": "assets/../plate.png",
            "symlink": "escape.png"}[path_kind]
    scene = _scene()
    scene.background = Background(kind="image", value=path)
    with pytest.raises(ValueError):
        if consumer == "compose":
            compose(scene, directory, directory / "composition.html")
        else:
            composite_scene(scene, directory, 0)


@pytest.mark.parametrize("provider,model", [("chatgpt", "admin-selected-model"), ("openai", "admin-model")])
@pytest.mark.parametrize("factory", ["direct", "server_admin"])
def test_oauth_refresh_preserves_saved_admin_settings(tmp_path, monkeypatch, provider, model, factory):
    from keepframe.admin.memory import MemoryAdmin
    from keepframe.web.server import make_server

    config = ProviderConfig(provider="chatgpt", auth="oauth", model="gpt-6.1-sol", api_key="old-access",
                            refresh_token="old-refresh", id_token="old-id", account_id="account", oauth_expires_at=0)
    save_llm_settings(tmp_path, config)
    admin, server = None, None
    if factory == "direct":
        client = make_llm(config, workspace=tmp_path)
    else:
        admin = MemoryAdmin(workspace=tmp_path)
        admin.set_llm_settings(config, "test")
        factories = []
        def workflow(workspace, factory):
            factories.append(factory)
            return object()
        monkeypatch.setattr("keepframe.web.server.AEWorkflowService", workflow)
        server = make_server(tmp_path, port=0, admin_svc=admin)
        client = factories[0]()
    edited = config.model_copy(update={"provider": provider, "model": model, "base_url": "https://admin.example",
                                       "extra": {"reasoning_effort": "high"}})
    def refresh(token):
        assert token == "old-refresh"
        if admin is None:
            save_llm_settings(tmp_path, edited)
        else:
            admin.set_llm_settings(edited, "test")
        return {"access_token": "new-access", "refresh_token": "new-refresh", "id_token": "new-id", "expires_in": 3600}
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", refresh)
    try:
        client._refresh()
    finally:
        if server is not None:
            server.server_close()
    saved = load_llm_settings(tmp_path)
    assert (saved.provider, saved.model, saved.base_url, saved.extra) == (provider, model, edited.base_url, edited.extra)
    assert (saved.api_key, saved.refresh_token, saved.id_token, saved.account_id) == ("new-access", "new-refresh", "new-id", "account")
    assert saved.oauth_expires_at > time.time() + 3500
    if admin is not None:
        assert admin.get_llm_settings() == saved


def test_core_flow_docs_state_verified_semantics():
    readme = (ROOT / "README.md").read_text()
    qa = (ROOT / "docs/qa/core-flow/README.md").read_text()
    assert "Keep predicates required on every edit" not in readme
    assert "| `all` |" in readme and "`none` disables keep checks" in readme
    assert "--reference" in readme and "by element id" in readme.lower()
    assert "valid typed target" in qa and "not verified to be the right element" in qa
    assert "font family now only replaces sans-serif when it clearly matches better (final fix)" in qa
    assert "text stroke/opacity use the mean plate colour on gradient scenes" in qa
    assert "GPU refine is skipped on plate scenes" in qa
