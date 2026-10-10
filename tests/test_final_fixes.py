import base64
import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import cv2
import numpy as np
import pytest

from keepframe.analyze.composite import composite_scene
from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames, rerun
from keepframe.compose.composer import compose
from keepframe.edit.agent import EditResult
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import Target
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
from tests.test_web_agent import capture_agent_client

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
    script.write_text(r"""const assert = require('node:assert/strict');
const KEEP_PASS_RATE = 1, CONFIDENCE_PERCENT = 100;
const document = {createElement: () => ({dataset:{}, setAttribute(name, value) {this[name] = value;}})};
const chips = [], logEl = {appendChild: c => chips.push(c)};
const copy = {
  'agent.verifyPassed': 'Checks passed · keep {rate}%',
  'agent.verifyFailed': 'Checks failed · keep {rate}%',
  'agent.verifyNoKeep': 'No keep rules to check',
  'agent.verifyDetails': '{summary}. {n} keep rules. Maximum error {error}px',
};
const T = key => copy[key] || key;
const Tf = (key, values) => T(key).replace(/\{(\w+)\}/g, (_, name) => values[name]);
""" + src + r"""
appendVerify({keep_total:1000, keep_failed:0, keep_pass_rate:1, keep_results:[]});
assert.equal(chips.at(-1).textContent, 'Checks passed · keep 100%');
assert.equal(chips.at(-1).title, 'Checks passed · keep 100%. 1000 keep rules. Maximum error 0.00px');
assert.equal(chips.at(-1)['aria-label'], chips.at(-1).title);
assert.match(chips.at(-1).className, /--pass/);
appendVerify({keep_total:1000, keep_failed:75, keep_pass_rate:1, keep_results:[]});
assert.equal(chips.at(-1).textContent, 'Checks failed · keep 93%');
assert.match(chips.at(-1).title, /1000 keep rules/);
assert.match(chips.at(-1).className, /--fail/);
appendVerify({keep_total:0, keep_failed:0, keep_results:[]});
assert.equal(chips.at(-1).textContent, 'No keep rules to check');
assert.match(chips.at(-1).className, /--warn/);
appendVerify({keep_pass_rate:1, keep_results:[{passed:true}, {passed:true}]});
assert.equal(chips.at(-1).textContent, 'Checks passed · keep 100%');
assert.match(chips.at(-1).title, /2 keep rules/);
appendVerify({keep_pass_rate:0.5, keep_results:[{passed:true}, {passed:false}]});
assert.equal(chips.at(-1).textContent, 'Checks failed · keep 50%');
assert.match(chips.at(-1).title, /2 keep rules/);
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
@pytest.mark.parametrize("torch_available", [True, False])
def test_plate_analysis_and_rerun_refine_when_torch_available(tmp_path, monkeypatch, stage, torch_available):
    calls = []
    def fake_refine(frames, bg, raws, *args, plate=None, **kwargs):
        refined = {k: r.copy() for k, r in raws.items()}
        for raw in refined.values():
            raw[:, 0] += 0.25
        calls.append((plate, refined))
        return refined
    monkeypatch.setattr("keepframe.analyze.refine.torch_available", lambda: torch_available)
    monkeypatch.setattr("keepframe.analyze.refine.refine_affine", fake_refine)
    monkeypatch.setattr("keepframe.analyze.device.resolve_device", lambda: "cpu")
    opts = AnalyzeOptions(ocr=False, refine=stage == "analyze", use_ecc=False)
    frames = _gradient_clip(h=320, w=640, picture=True)   # a picture plate (a plain ramp is a gradient, D4)
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", opts)
    if stage == "rerun":
        init_project(tmp_path, {"file": "ref.mp4"}, scene)
        rerun(tmp_path, "s1", "sprites", note="check plate refine", options=AnalyzeOptions(ocr=False, refine=True, use_ecc=False))
        scene, _ = current_scene(tmp_path, "s1")
    assert scene.background.kind == "image"
    assert any(e.kind == "sprite" for e in scene.elements)
    assert len(calls) == int(torch_available)
    sd = scene_dir(tmp_path, "s1")
    if calls:
        plate, refined = calls[0]
        saved_plate = cv2.cvtColor(cv2.imread(str(sd / scene.background.value)), cv2.COLOR_BGR2RGB)
        assert np.array_equal(plate, saved_plate)
        saved_raw = np.load(sd / scene.elements[0].raw)["raw"]
        assert np.allclose(saved_raw, next(iter(refined.values())), equal_nan=True)
    report = json.loads((sd / "report.json").read_text())
    message = "refine skipped: background plate scenes are not supported by refine yet"
    assert message not in report["messages"]


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


@pytest.mark.parametrize("change", ["model", "provider", "account"])
@pytest.mark.parametrize("factory", ["direct", "server_admin"])
def test_oauth_refresh_preserves_saved_admin_settings(tmp_path, monkeypatch, caplog, change, factory):
    from keepframe.admin.memory import MemoryAdmin
    from keepframe.web.server import make_server

    config = ProviderConfig(provider="chatgpt", auth="oauth", model="gpt-6.1-sol", api_key="old-access",
                            refresh_token="old-refresh", id_token="old-id", account_id=uuid4().hex, oauth_expires_at=0)
    save_llm_settings(tmp_path, config)
    admin, server = None, None
    if factory == "direct":
        client = make_llm(config, workspace=tmp_path)
    else:
        admin = MemoryAdmin(workspace=tmp_path)
        admin.set_llm_settings(config, "test")
        server = make_server(tmp_path, port=0, admin_svc=admin)
        client = capture_agent_client(server, tmp_path, monkeypatch)
    provider = "openai" if change == "provider" else "chatgpt"
    model = "admin-selected-model"
    edited = config.model_copy(update={"provider": provider, "model": model, "base_url": "https://admin.example",
                                       "extra": {"reasoning_effort": "high"}})
    if change == "account":
        for field in ("api_key", "refresh_token", "id_token", "account_id"):
            setattr(edited, field, uuid4().hex)
        edited.oauth_expires_at = time.time() + 7200
    writes = []
    def save(workspace, refreshed):
        writes.append(True)
        save_llm_settings(workspace, refreshed)
    monkeypatch.setattr("keepframe.session.chatgpt_client.save_llm_settings", save)
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
    if change == "model":
        tokens_merged = (saved.api_key, saved.refresh_token, saved.id_token, saved.account_id) == (
            "new-access", "new-refresh", "new-id", config.account_id)
        assert tokens_merged
        assert saved.oauth_expires_at > time.time() + 3500
        assert writes == [True]
    else:
        settings_preserved = saved == edited
        assert settings_preserved
        assert writes == []
        assert any(record.levelname == "WARNING" and record.message ==
                   "chatgpt refresh not persisted: saved settings changed provider/account" for record in caplog.records)
        credentials = [getattr(cfg, field) for cfg in (config, edited, client.config)
                       for field in ("api_key", "refresh_token", "id_token", "account_id")]
        secrets_hidden = all(not value or value not in caplog.text for value in credentials)
        assert secrets_hidden
    if admin is not None:
        admin_matches_saved = admin.get_llm_settings() == saved
        assert admin_matches_saved


def test_oauth_refresh_admin_callback_handles_missing_saved_settings(tmp_path, monkeypatch):
    from keepframe.admin.memory import MemoryAdmin
    from keepframe.web.server import make_server

    config = ProviderConfig(provider="chatgpt", auth="oauth", api_key=uuid4().hex,
                            refresh_token=uuid4().hex, account_id=uuid4().hex, oauth_expires_at=0)
    admin = MemoryAdmin(workspace=tmp_path)
    admin.set_llm_settings(config, "test")
    def forbidden(*args):
        raise AssertionError("admin must not receive missing or stale settings")
    monkeypatch.setattr("keepframe.web.server.load_llm_settings", lambda workspace: None)
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", lambda token: {
        "access_token": uuid4().hex, "refresh_token": uuid4().hex, "expires_in": 3600})
    monkeypatch.setattr(admin, "set_llm_settings", forbidden)
    server = make_server(tmp_path, port=0, admin_svc=admin)
    try:
        capture_agent_client(server, tmp_path, monkeypatch)._refresh()
    finally:
        server.server_close()


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
