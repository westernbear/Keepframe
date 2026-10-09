import base64
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytest

from keepframe.analyze.solid_assets import choose_solid, generate_solid_assets, solid_errors
from keepframe.assets import AssetClient
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from tests.test_three import _triangle_glb


@contextmanager
def asset_server(mode="glb"):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if mode == "timeout":
                time.sleep(0.15)
            data = _triangle_glb() if mode in ("glb", "timeout") else b"<html>not a model</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if mode == "html" else "model/gltf-binary")
            self.send_header("Content-Length", str(51 * 1024 * 1024 if mode == "large" else len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def pending_scene(sd, count=1):
    (sd / "assets").mkdir(parents=True, exist_ok=True)
    crop = np.full((24, 32, 4), (30, 80, 220, 255), np.uint8)
    elements = []
    for i in range(count):
        eid = f"e{i + 1}"
        cv2.imwrite(str(sd / "assets" / f"{eid}.png"), crop)
        elements.append(Element(id=eid, kind="sprite", pending_asset="3d", visible=(0, 11),
                                tracks={"x": Track(keys=[Keyframe(t=0, v=20 + i * 50)]),
                                        "y": Track(keys=[Keyframe(t=0, v=40)])},
                                canonical=Canonical(width=32, height=24, texture=f"assets/{eid}.png")))
    return Scene(id="s1", size=(100, 80), fps=30, frames=12, background=Background(), elements=elements)


def test_reference_generation_saves_versioned_glb_keeps_crop_and_reuses_it(tmp_path):
    scene = pending_scene(tmp_path)
    with asset_server() as (url, calls):
        messages = generate_solid_assets(scene, tmp_path, AssetClient(url, timeout=310))
        el = scene.elements[0]
        assert el.kind == "3d" and el.pending_asset is None
        assert el.canonical.model == "assets/e1.model1.glb"
        assert el.canonical.texture == "assets/e1.png"
        assert (tmp_path / el.canonical.model).read_bytes() == _triangle_glb()
        assert messages
        assert calls[0]["task"] == "generate" and calls[0]["kind"] == "3d"
        assert base64.b64decode(calls[0]["input_image"]["data"]) == (tmp_path / el.canonical.texture).read_bytes()
        assert calls[0]["size"] == {"width": 32, "height": 24}
        calls.clear()
        rebuilt = pending_scene(tmp_path)
        generate_solid_assets(rebuilt, tmp_path, AssetClient(url, timeout=310))
        assert calls == [] and rebuilt.elements[0].canonical.model == el.canonical.model


@pytest.mark.parametrize("mode, code", [("html", "asset_mime_not_allowed"), ("timeout", "TimeoutError"),
                                       ("large", "asset_too_large"), ("invalid", "invalid_glb")])
def test_generation_failures_fall_back(tmp_path, mode, code):
    scene = pending_scene(tmp_path)
    with asset_server(mode) as (url, calls):
        messages = generate_solid_assets(scene, tmp_path, AssetClient(url, timeout=0.03))
        assert len(calls) == 2
    assert scene.elements[0].kind == "sprite" and scene.elements[0].pending_asset == "3d"
    assert not list((tmp_path / "assets").glob("*.glb"))
    assert messages and all(code in m for m in messages)


def test_missing_generator_and_analysis_generation_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("KEEPFRAME_ASSET_API_URL", raising=False)
    scene = pending_scene(tmp_path, count=5)
    assert generate_solid_assets(scene, tmp_path, None) == ["3D 후보 5개: 생성기 미설정"]
    # Rank by canonical area * visible frames, rather than element order or area alone.
    scene.elements[3].canonical.width = 64
    scene.elements[4].canonical.width = 96
    scene.elements[4].visible = (0, 1)
    with asset_server() as (url, calls):
        messages = generate_solid_assets(scene, tmp_path, AssetClient(url, timeout=310))
        assert len(calls) == 2
        assert [c["size"]["width"] for c in calls] == [64, 32]
        assert [e.id for e in scene.elements if e.kind == "3d"] == ["e1", "e4"]
        pending = [e for e in scene.elements if e.pending_asset == "3d"]
        assert len(pending) == 3 and all(e.kind == "sprite" for e in pending)
        assert messages.count("3D 생성 상한 도달: 3개는 검수의 '3D 생성' 버튼으로 생성하세요") == 1


@pytest.mark.parametrize("reused, requests", [(range(5), 0), ([1, 3, 4], 2)])
def test_reused_glbs_do_not_spend_generation_budget(tmp_path, reused, requests):
    from keepframe.analyze.solid_assets import _record_model, _signature
    from keepframe.ir.tracks import element_bbox

    scene = pending_scene(tmp_path, count=5)
    for i in reused:
        el = scene.elements[i]
        model = f"assets/{el.id}.model1.glb"
        (tmp_path / model).write_bytes(_triangle_glb())
        _record_model(tmp_path, model, _signature(el.visible[0], element_bbox(el, el.visible[0])))
    with asset_server() as (url, calls):
        messages = generate_solid_assets(scene, tmp_path, AssetClient(url))
        assert len(calls) == requests
    assert all(e.kind == "3d" and e.pending_asset is None for e in scene.elements)
    assert not any("상한" in m for m in messages)


@pytest.mark.parametrize("still, choice", [(0.1, "fragments"), (0.015, "still")])
def test_quota_skipped_solids_obey_fidelity_guard(tmp_path, monkeypatch, still, choice):
    from keepframe.analyze.solid_assets import finish_solid_assets

    scene = pending_scene(tmp_path, count=5)
    (tmp_path / "stages").mkdir()
    ids, props = {}, {}
    for i, el in enumerate(scene.elements):
        key = f"solid{i + 1}"
        ids[key] = el.id
        raw = np.full((12, 11), np.nan)
        raw[:, :8] = (20 + i * 50, 40, 1, 1, 0, 0, 0, 1)
        fragment = dict(first=0, raw=raw.copy(), canon=np.full((24, 32, 4), 255, np.uint8), kind="sprite")
        props[key] = dict(first=0, raw=raw, canon=fragment["canon"], fragments={f"fragment{i + 1}": fragment})
    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors",
                        lambda _f, _b, _p, glb: {"fragments": 0.01, "still": still, "model": 0.0 if glb else None})
    messages = []
    with asset_server() as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        reports = finish_solid_assets(scene, tmp_path, np.zeros((12, 80, 100, 3), np.uint8),
                                      props, ids, {}, messages)
        assert len(calls) == 2
    assert len(scene.elements) == 5
    assert sum(e.kind == "3d" for e in scene.elements) == 2
    assert sum(e.kind == "sprite" and e.pending_asset == "3d" for e in scene.elements) == 3
    assert [r["choice"] for r in reports] == ["model", "model", choice, choice, choice]
    if choice == "fragments":
        assert all(ids[f"fragment{i}"] in {e.id for e in scene.elements} for i in (3, 4, 5))
    assert messages.count("3D 생성 상한 도달: 3개는 검수의 '3D 생성' 버튼으로 생성하세요") == 1


def test_explicit_edit_has_its_own_two_request_job_budget(tmp_path, monkeypatch):
    from keepframe.edit.agent import edit
    from keepframe.ir.store import init_project
    from keepframe.verify.verifier import VerifyReport

    sd = tmp_path / "scenes/s1"
    scene = pending_scene(sd, count=5)
    monkeypatch.setattr("keepframe.edit.agent.compose", lambda _s, d, _p, **_: d / "composition.html")
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_a, **_k: None)
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_a, **_k: VerifyReport(schema_ok=True, passed=False))
    with asset_server() as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        generate_solid_assets(scene, sd, AssetClient(url))
        assert len(calls) == 2 and scene.element("e3").pending_asset == "3d"
        init_project(tmp_path, {"file": "ref.mp4", "fps": 30, "size": [100, 80]}, scene)
        for _ in range(2):
            calls.clear()
            result = edit(tmp_path, "s1", "3D 모델로 바꿔줘", element="e3", confirm=True)
            assert result.status == "failed" and len(calls) == 2


@pytest.mark.parametrize("errors, expected", [
    ({"fragments": 0.10, "still": 0.08, "model": 0.085}, "model"),
    ({"fragments": 0.10, "still": 0.08, "model": 0.085001}, "still"),
    ({"fragments": 0.08, "still": 0.085, "model": None}, "still"),
    ({"fragments": 0.08, "still": 0.085001, "model": 0.3}, "fragments"),
    ({"fragments": 0.1, "still": 0.2, "model": 0.0}, "model"),
])
def test_guard_picks_lowest_error(errors, expected):
    assert choose_solid(errors) == expected


def test_local_error_uses_visible_samples_and_only_the_solid_box():
    frames = np.full((12, 60, 80, 3), 255, np.uint8)
    frames[:, 20:40, 30:50] = (255, 0, 0)
    frames[5, 20:40, 30:50] = (0, 0, 255)
    raw = np.full((12, 11), np.nan)
    raw[:, :8] = (40, 30, 1, 1, 0, 0, 0, 1)
    raw[:, 9:] = 0
    canon = np.full((20, 20, 4), (255, 0, 0, 255), np.uint8)
    props = dict(raw=raw, canon=canon, cf=0, first=0, last=11, kind="sprite", fragments={},
                 boxes=[(30, 20, 50, 40)] * 12)
    errors = solid_errors(frames, (0, 0, 0), props, None)
    assert errors["still"] == pytest.approx(2 / 9)
    assert errors["fragments"] == pytest.approx(1 / 3)
    assert errors["model"] is None


@pytest.mark.parametrize("prompt", ["3D 모델로 바꿔줘", "3D 모델로 교체해", "Convert this element to a 3D model"])
def test_reference_edit_waits_for_confirm_and_sends_crop(tmp_path, monkeypatch, prompt):
    from keepframe.edit.agent import edit
    from keepframe.ir.store import current_scene, init_project
    from keepframe.verify.verifier import VerifyReport

    sd = tmp_path / "scenes" / "s1"
    scene = pending_scene(sd)
    init_project(tmp_path, {"file": "ref.mp4", "fps": 30, "size": [100, 80]}, scene)
    monkeypatch.setattr("keepframe.edit.agent.compose", lambda _s, d, _p, **_: d / "composition.html")
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_a, **_k: None)
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_a, **_k: VerifyReport(
        schema_ok=True, keep_pass_rate=1, temporal=1, layer_probe_complete=True, passed=True))
    with asset_server() as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        preview = edit(tmp_path, "s1", prompt, element="e1")
        assert preview.status == "needs_confirm" and not calls
        assert preview.intent.targets[0].value == "reference"
        result = edit(tmp_path, "s1", prompt, element="e1", confirm=True, intent=preview.intent)
        assert result.status == "done" and len(calls) == 1
        assert base64.b64decode(calls[0]["input_image"]["data"]) == (sd / "assets" / "e1.png").read_bytes()
        edited, _ = current_scene(tmp_path, "s1")
        assert edited.element("e1").pending_asset is None


def test_rerun_reuses_glb_without_generation_and_records_guard(tmp_path, monkeypatch):
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames, rerun
    from keepframe.ir.store import current_scene, init_project
    from keepframe.ir.synth import make_spinning_sphere_video

    frames = make_spinning_sphere_video(frames=16)
    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors",
                        lambda *_a, **_k: {"fragments": 0.2, "still": 0.1, "model": 0.08})
    with asset_server() as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        scene = analyze_scene_frames(frames, 30, tmp_path, "s1", AnalyzeOptions(
            bg_override="#101418", ocr=False, refine=False, use_ecc=False))
        assert len(calls) == 1
        model = next(e for e in scene.elements if e.kind == "3d")
        init_project(tmp_path, {"file": "unused.mp4", "fps": 30, "size": [240, 180]}, scene)
        calls.clear()
        rerun(tmp_path, "s1", "solids", "reuse 3D asset")
        assert calls == []
        rebuilt, _ = current_scene(tmp_path, "s1")
        assert rebuilt.element(model.id).canonical.model == model.canonical.model
    report = json.loads((tmp_path / "scenes/s1/report.json").read_text())
    assert report["solids"][0]["choice"] == "model"
    assert report["solids"][0]["model"] == 0.08
    assert report["solids"][0]["still"] == 0.1
    assert report["solids"][0]["fragments"] == 0.2


def test_guard_restores_fragments_and_marks_largest(tmp_path, monkeypatch):
    import pickle
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
    from keepframe.ir.synth import make_spinning_sphere_video

    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors",
                        lambda *_a, **_k: {"fragments": 0.01, "still": 0.1, "model": None})
    scene = analyze_scene_frames(make_spinning_sphere_video(frames=16), 30, tmp_path, "s1", AnalyzeOptions(
        bg_override="#101418", ocr=False, refine=False, use_ecc=False, generate_3d=False))
    sd = tmp_path / "scenes" / "s1"
    props = pickle.loads((sd / "stages/props.pkl").read_bytes())
    ids = json.loads((sd / "stages/ids.json").read_text())
    solid = next(p for p in props.values() if "fragments" in p)
    assert all(ids[key] in {e.id for e in scene.elements} for key in solid["fragments"])
    largest = max(solid["fragments"], key=lambda k: np.count_nonzero(solid["fragments"][k]["canon"][..., 3]))
    assert scene.element(ids[largest]).pending_asset == "3d"
    assert json.loads((sd / "report.json").read_text())["solids"][0]["choice"] == "fragments"


@pytest.mark.parametrize("choice", ["still", "fragments"])
def test_rejected_generated_model_is_detached_but_reused_by_signature(tmp_path, monkeypatch, choice):
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
    from keepframe.ir.synth import make_spinning_sphere_video
    from keepframe.render.lottie import preflight_lottie

    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors",
                        lambda *_a, **_k: {"fragments": 0.01 if choice == "fragments" else 0.2,
                                           "still": 0.1, "model": 0.8})
    frames = make_spinning_sphere_video(frames=12)
    opts = AnalyzeOptions(bg_override="#101418", ocr=False, refine=False, use_ecc=False)
    with asset_server() as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        scene = analyze_scene_frames(frames, 30, tmp_path, "s1", opts)
        assert len(calls) == 1
        assert all(e.kind == "sprite" and e.canonical.model is None for e in scene.elements)
        preflight_lottie(scene)
        sd = tmp_path / "scenes/s1"
        models = list((sd / "assets").glob("*.glb"))
        assert len(models) == 1 and models[0].read_bytes() == _triangle_glb()
        assert (sd / f"assets/{models[0].name}.solid.json").is_file()
        calls.clear()
        rebuilt = analyze_scene_frames(frames, 30, tmp_path, "s1", opts)
        assert calls == []
        assert all(e.canonical.model is None for e in rebuilt.elements)
        report = json.loads((sd / "report.json").read_text())
        assert any("3D 재사용" in message for message in report["messages"])
        assert report["solids"][0]["choice"] == choice


@pytest.mark.browser
def test_different_colour_glb_is_rejected_and_browser_displays_fallback(tmp_path, monkeypatch):
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
    from keepframe.compose.composer import compose
    from keepframe.ir.synth import make_spinning_sphere_video
    from keepframe.render.renderer import render, load_frame

    frames = make_spinning_sphere_video(frames=16)
    with asset_server() as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        scene = analyze_scene_frames(frames, 30, tmp_path, "s1", AnalyzeOptions(
            bg_override="#101418", ocr=False, refine=False, use_ecc=False))
        assert len(calls) == 1
    sd = tmp_path / "scenes" / "s1"
    report = json.loads((sd / "report.json").read_text())
    guard = report["solids"][0]
    assert guard["choice"] in ("still", "fragments")
    assert guard["model"] > min(guard["still"], guard["fragments"]) + 0.005
    assert all(e.kind == "sprite" for e in scene.elements)
    assert all(e.canonical.model is None for e in scene.elements)
    from keepframe.render.lottie import preflight_lottie
    preflight_lottie(scene)
    assert any(e.pending_asset == "3d" for e in scene.elements)
    html = compose(scene, sd, sd / "fallback.html")
    result = render(html, scene, sd / "fallback-render", frames=[0])
    image = load_frame(result.frames_dir / "f_00000.png")
    assert np.mean(np.abs(image - np.array((16, 20, 24)) / 255)) > 0.02


@pytest.mark.parametrize("mode, code", [("html", "asset_mime_not_allowed"), ("timeout", "TimeoutError"),
                                       ("large", "asset_too_large"), ("invalid", "invalid_glb")])
def test_analysis_completes_after_generation_failure(tmp_path, monkeypatch, mode, code):
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
    from keepframe.ir.synth import make_spinning_sphere_video

    # Deterministic alternative scores isolate transport failures from measured texture quality.
    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors",
                        lambda *_a, **_k: {"fragments": 0.2, "still": 0.1, "model": None})
    timeouts = []

    def local_client(**kwargs):
        timeouts.append(kwargs["timeout"])
        return AssetClient(timeout=0.03)

    monkeypatch.setattr("keepframe.analyze.solid_assets.AssetClient", local_client)
    with asset_server(mode) as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        scene = analyze_scene_frames(make_spinning_sphere_video(frames=12), 30, tmp_path, "s1", AnalyzeOptions(
            bg_override="#101418", ocr=False, refine=False, use_ecc=False))
        assert len(calls) == 2
    assert timeouts == [310]
    assert any(e.kind == "sprite" and e.pending_asset == "3d" for e in scene.elements)
    report = json.loads((tmp_path / "scenes/s1/report.json").read_text())
    assert any(code in m for m in report["messages"])
    assert not any("3D render failed" in m for m in report["messages"])


def test_reference_edit_of_solid_obeys_fidelity_guard(tmp_path, monkeypatch):
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
    from keepframe.edit.agent import edit
    from keepframe.ir.store import current_scene, init_project
    from keepframe.ir.synth import make_spinning_sphere_video
    from keepframe.verify.verifier import VerifyReport

    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors",
                        lambda *_a, **_k: {"fragments": 0.2, "still": 0.1, "model": None})
    scene = analyze_scene_frames(make_spinning_sphere_video(frames=12), 30, tmp_path, "s1", AnalyzeOptions(
        bg_override="#101418", ocr=False, refine=False, use_ecc=False, generate_3d=False))
    target = next(e for e in scene.elements if e.pending_asset == "3d")
    init_project(tmp_path, {"file": "unused.mp4", "fps": 30, "size": [240, 180]}, scene)
    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors",
                        lambda *_a, **_k: {"fragments": 0.2, "still": 0.1, "model": 0.8})
    monkeypatch.setattr("keepframe.edit.agent.compose", lambda _s, d, _p, **_: d / "composition.html")
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_a, **_k: None)
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_a, **_k: VerifyReport(
        schema_ok=True, keep_pass_rate=1, temporal=1, layer_probe_complete=True, passed=True))
    with asset_server() as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        result = edit(tmp_path, "s1", "3D 모델로 바꿔줘", element=target.id, confirm=True)
        assert result.status == "done" and len(calls) == 1
    edited, _ = current_scene(tmp_path, "s1")
    el = edited.element(target.id)
    assert el.kind == "sprite" and el.pending_asset == "3d"
    assert el.canonical.model is None
    assert len(list((tmp_path / "scenes/s1/assets").glob("*.model1.glb"))) == 1
    assert any("model=0.800000" in m and "still" in m for m in result.messages)
    report = json.loads((tmp_path / "scenes/s1/report.json").read_text())
    assert report["solids"][0]["choice"] == "still" and report["solids"][0]["model"] == 0.8


def test_regenerated_fragment_model_survives_repeated_reruns(tmp_path, monkeypatch):
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames, rerun
    from keepframe.edit.agent import edit
    from keepframe.ir.store import current_scene, init_project
    from keepframe.ir.synth import make_spinning_sphere_video
    from keepframe.verify.verifier import VerifyReport

    scores = {"fragments": 0.01, "still": 0.1, "model": None}
    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors", lambda *_a, **_k: scores.copy())
    scene = analyze_scene_frames(make_spinning_sphere_video(frames=12), 30, tmp_path, "s1", AnalyzeOptions(
        bg_override="#101418", ocr=False, refine=False, use_ecc=False, generate_3d=False))
    target = next(e for e in scene.elements if e.pending_asset == "3d")
    init_project(tmp_path, {"file": "unused.mp4", "fps": 30, "size": [240, 180]}, scene)
    monkeypatch.setattr("keepframe.edit.agent.compose", lambda _s, d, _p, **_: d / "composition.html")
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_a, **_k: None)
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_a, **_k: VerifyReport(
        schema_ok=True, keep_pass_rate=1, temporal=1, layer_probe_complete=True, passed=True))
    scores["model"] = 0.8
    with asset_server() as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        result = edit(tmp_path, "s1", "3D 모델로 바꿔줘", element=target.id, confirm=True,
                      choices={"keep_violation": "release_keep"})
        assert result.status == "done" and len(calls) == 1
        edited, _ = current_scene(tmp_path, "s1")
        assert edited.element(target.id).canonical.model is None
        paths = list((tmp_path / "scenes/s1/assets").glob("*.model1.glb"))
        assert len(paths) == 1
        path = paths[0]
        calls.clear()
        for _ in range(2):
            rerun(tmp_path, "s1", "keyframes", "reuse rejected reference model")
            rebuilt, _ = current_scene(tmp_path, "s1")
            assert rebuilt.element(target.id).canonical.model is None
            assert path.exists()
            report = json.loads((tmp_path / "scenes/s1/report.json").read_text())
            assert any("3D 재사용" in message for message in report["messages"])
        assert calls == []


def test_multiscene_analysis_shares_generation_cap(tmp_path, monkeypatch):
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze
    from keepframe.ir.synth import make_spinning_sphere_video

    sphere = make_spinning_sphere_video(frames=12)
    clips = []
    for scale in (0.5, 1, 0.75):
        clip = np.full_like(sphere, (16, 20, 24))
        for f, frame in enumerate(sphere):
            small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
            clip[f, :small.shape[0], :small.shape[1]] = small
        clips.append(clip)
    frames = np.concatenate(clips)
    video = tmp_path / "spheres.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 30, (240, 180))
    assert writer.isOpened()
    try:
        for frame in frames:
            writer.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
    finally:
        writer.release()
    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors",
                        lambda _f, _b, _p, glb: {"fragments": 0.2, "still": 0.1, "model": 0.08 if glb else None})
    with asset_server() as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        analyze(video, 0, 35, tmp_path / "project", AnalyzeOptions(
            bg_override="#101418", ocr=False, refine=False, use_ecc=False),
            scenes=[{"id": f"s{i + 1}", "frames": [12 * i, 12 * i + 11]} for i in range(3)])
        assert len(calls) == 2
        assert calls[0]["size"]["width"] > calls[1]["size"]["width"] > 48
    report = json.loads((tmp_path / "project/scenes/s1/report.json").read_text())
    assert "3D 생성 상한 도달: 1개는 검수의 '3D 생성' 버튼으로 생성하세요" in report["messages"]


def test_reanalysis_generates_first_model_then_reuses_it(tmp_path, monkeypatch):
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
    from keepframe.ir.store import init_project
    from keepframe.ir.synth import make_spinning_sphere_video

    frames = make_spinning_sphere_video(frames=12)
    options = AnalyzeOptions(bg_override="#101418", ocr=False, refine=False, use_ecc=False)
    monkeypatch.delenv("KEEPFRAME_ASSET_API_URL", raising=False)
    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors",
                        lambda _f, _b, _p, glb: {"fragments": 0.2, "still": 0.1, "model": 0.08 if glb else None})
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", options)
    init_project(tmp_path, {"file": "unused.mp4", "fps": 30, "size": [240, 180]}, scene)
    with asset_server() as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        first = analyze_scene_frames(frames, 30, tmp_path, "s1", options)
        assert len(calls) == 1 and any(e.kind == "3d" for e in first.elements)
        calls.clear()
        second = analyze_scene_frames(frames, 30, tmp_path, "s1", options)
        assert calls == [] and any(e.kind == "3d" for e in second.elements)


@pytest.mark.parametrize("initial, choice", [("still", "model"), ("still", "still"),
                                          ("fragments", "model"), ("fragments", "fragments"),
                                          ("fragments", "still"), ("still", "fragments")])
def test_solid_reference_edit_uses_whole_crop_and_preserves_current_state(tmp_path, monkeypatch, initial, choice):
    import pickle
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
    from keepframe.edit.agent import edit
    from keepframe.ir.store import current_scene, init_project
    from keepframe.ir.synth import make_spinning_sphere_video
    from keepframe.ir.tracks import element_bbox
    from keepframe.verify.verifier import VerifyReport

    scores = {"fragments": 0.01 if initial == "fragments" else 0.2, "still": 0.1, "model": None}
    measured = []
    def score(_f, _b, p, _glb):
        measured.append(p)
        return scores.copy()
    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors", score)
    scene = analyze_scene_frames(make_spinning_sphere_video(frames=12), 30, tmp_path, "s1", AnalyzeOptions(
        bg_override="#101418", ocr=False, refine=False, use_ecc=False, generate_3d=False))
    sd = tmp_path / "scenes/s1"
    props = pickle.loads((sd / "stages/props.pkl").read_bytes())
    key, p = next((k, p) for k, p in props.items() if "fragments" in p)
    ids = json.loads((sd / "stages/ids.json").read_text())
    target = next(e for e in scene.elements if e.pending_asset == "3d")
    target.role, target.label, target.caption, target.confidence = "primary", "manual label", "manual caption", 0.73
    target.canonical.color = "#abcdef"
    target.visible = (1, 10)
    target.provenance = "manual"
    target.tracks["opacity"] = Track(keys=[Keyframe(t=0, v=0.73)])
    before = {e.id: e.model_dump() for e in scene.elements}
    init_project(tmp_path, {"file": "unused.mp4", "fps": 30, "size": [240, 180]}, scene)
    scores.update(fragments=0.01 if choice == "fragments" else 0.2, model=0.0 if choice == "model" else 0.8)
    monkeypatch.setattr("keepframe.edit.agent.compose", lambda _s, d, _p, **_: d / "composition.html")
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_a, **_k: None)
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_a, **_k: VerifyReport(
        schema_ok=True, keep_pass_rate=1, temporal=1, layer_probe_complete=True, passed=True))
    with asset_server() as (url, calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        result = edit(tmp_path, "s1", "3D 모델로 바꿔줘", element=target.id, confirm=True,
                      choices={"keep_violation": "release_keep"})
    assert result.status == "done" and len(calls) == 1
    data = base64.b64decode(calls[0]["input_image"]["data"])
    crop = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    assert crop.shape[:2] == p["canon"].shape[:2]
    assert min(crop.shape[:2]) >= 90
    assert calls[0]["size"] == {"width": crop.shape[1], "height": crop.shape[0]}
    assert "model_props" not in measured[-1]
    edited, _ = current_scene(tmp_path, "s1")
    el = edited.element(target.id) if initial == "fragments" or choice != "fragments" else next(e for e in edited.elements if e.pending_asset)
    if choice != "model":
        assert all(e.kind == "sprite" and e.canonical.model is None for e in edited.elements)
    assert (el.role, el.label, el.caption, el.confidence) == ("primary", "manual label", "manual caption", 0.73)
    assert el.canonical.color == "#abcdef"
    if choice == "model" or (initial == "fragments" and choice == "still"):
        assert el.kind == ("3d" if choice == "model" else "sprite")
        assert el.pending_asset == (None if choice == "model" else "3d")
        if initial == "fragments":
            assert element_bbox(el, 0) == pytest.approx(p["boxes"][0], abs=0.5)
            assert el.visible == (0, 11)
            assert el.canonical.texture == f"assets/{ids[key]}.png"
            assert (el.canonical.height, el.canonical.width) == p["canon"].shape[:2]
            assert el.tracks["opacity"] == target.tracks["opacity"]
            assert el.provenance == "manual"
            related = {ids[key], *(ids[k] for k in p["fragments"])}
            assert {e.id for e in edited.elements} & related == {target.id}
            np.testing.assert_allclose(np.load(sd / el.raw)["raw"], p["raw"], equal_nan=True)
        else:
            assert el.visible == (1, 10) and el.tracks == target.tracks
    elif initial == "fragments" or choice == "still":
        assert el.visible == (1, 10) and el.tracks == target.tracks
        assert el.canonical.texture == target.canonical.texture
        for sibling in edited.elements:
            if sibling.id != target.id and sibling.id in before:
                assert sibling.model_dump() == before[sibling.id]


def test_model_render_failure_is_reported_separately(tmp_path, monkeypatch):
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
    from keepframe.ir.synth import make_spinning_sphere_video

    def fail_render(*_a, **_k):
        raise RuntimeError("do not expose upstream detail")
    monkeypatch.setattr("keepframe.analyze.solid_assets.render", fail_render)
    with asset_server() as (url, _calls):
        monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", url)
        analyze_scene_frames(make_spinning_sphere_video(frames=12), 30, tmp_path, "s1", AnalyzeOptions(
            bg_override="#101418", ocr=False, refine=False, use_ecc=False))
    report = json.loads((tmp_path / "scenes/s1/report.json").read_text())
    assert any("3D render failed" in m for m in report["messages"])
    assert not any("do not expose" in m for m in report["messages"])


def test_model_reuse_follows_solid_signature_across_element_ids(tmp_path):
    from keepframe.ir.schema import Keyframe, Track

    scene = pending_scene(tmp_path, count=2)
    for i, el in enumerate(scene.elements):
        el.tracks["x"] = Track(keys=[Keyframe(t=0, v=20 + i * 50)])
        el.tracks["y"] = Track(keys=[Keyframe(t=0, v=40)])
    with asset_server() as (url, calls):
        generate_solid_assets(scene, tmp_path, AssetClient(url))
        old_paths = {e.tracks["x"].keys[0].v: e.canonical.model for e in scene.elements}
        rebuilt = pending_scene(tmp_path, count=2)
        for new, old in zip(rebuilt.elements, reversed(scene.elements)):
            new.tracks = old.tracks
        calls.clear()
        generate_solid_assets(rebuilt, tmp_path, AssetClient(url))
        assert calls == []
        assert all(e.canonical.model == old_paths[e.tracks["x"].keys[0].v] for e in rebuilt.elements)


def test_generation_retries_share_the_analysis_cap(tmp_path):
    from keepframe.assets import AssetAPIError, AssetResponse

    scene = pending_scene(tmp_path, count=3)
    class Client:
        calls = 0
        def request(self, **_kwargs):
            self.calls += 1
            if self.calls % 2:
                raise AssetAPIError("asset_api_unavailable")
            return AssetResponse(mime="model/gltf-binary", data=_triangle_glb())
    client = Client()
    messages = generate_solid_assets(scene, tmp_path, client)
    assert client.calls == 2
    assert scene.elements[0].kind == "3d"
    assert all(e.kind == "sprite" and e.pending_asset == "3d" for e in scene.elements[1:])
    assert sum("재시도" in m for m in messages) == 1
    assert "3D 생성 상한 도달: 2개는 검수의 '3D 생성' 버튼으로 생성하세요" in messages


def test_changed_signature_never_reuses_model_for_same_element_id(tmp_path):
    scene = pending_scene(tmp_path)
    with asset_server() as (url, calls):
        generate_solid_assets(scene, tmp_path, AssetClient(url))
        rebuilt = pending_scene(tmp_path)
        rebuilt.elements[0].tracks["x"].keys[0].v += 30
        calls.clear()
        generate_solid_assets(rebuilt, tmp_path, AssetClient(url))
        assert len(calls) == 1
        assert rebuilt.elements[0].canonical.model == "assets/e1.model2.glb"


def test_legacy_project_rerun_builds_missing_solid_props(tmp_path, monkeypatch):
    import pickle
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames, rerun
    from keepframe.ir.store import current_scene, init_project
    from keepframe.ir.synth import make_spinning_sphere_video

    monkeypatch.setattr("keepframe.analyze.solid_assets.solid_errors",
                        lambda *_a, **_k: {"fragments": 0.2, "still": 0.1, "model": None})
    scene = analyze_scene_frames(make_spinning_sphere_video(frames=12), 30, tmp_path, "s1", AnalyzeOptions(
        bg_override="#101418", ocr=False, refine=False, use_ecc=False, generate_3d=False))
    sd = tmp_path / "scenes/s1"
    props = pickle.loads((sd / "stages/props.pkl").read_bytes())
    fragments = next(p["fragments"] for p in props.values() if "fragments" in p)
    (sd / "stages/props.pkl").write_bytes(pickle.dumps(fragments))
    (sd / "stages/solids.pkl").unlink()
    init_project(tmp_path, {"file": "unused.mp4", "fps": 30, "size": [240, 180]}, scene)
    rerun(tmp_path, "s1", "keyframes", "upgrade pre-solid stages")
    rebuilt, _ = current_scene(tmp_path, "s1")
    assert any(e.pending_asset == "3d" for e in rebuilt.elements)
    assert json.loads((sd / "report.json").read_text())["solids"]
