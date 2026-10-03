import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from keepframe.analyze.pipeline import AnalyzeOptions, analyze, rerun
from keepframe.analyze.video import render_scene_video
from keepframe.assets import AssetAPIError, AssetResponse
from keepframe.ir.schema import UIComponent, UIModel
from keepframe.ir.store import current_scene, scene_dir
from keepframe.ir.synth import make_synthetic_scene
from keepframe.web.workspace import load_meta, write_meta
from tests.test_jobs_dispatch import RecordingRunner
from tests.test_web_analyze import _post, _setup_token_test
from tests.test_web_server import start


def test_ui_parser_runs_only_for_ui_references(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", "http://127.0.0.1:1")
    called = []
    monkeypatch.setattr("keepframe.analyze.pipeline._parse_ui", lambda frames, scene, sd: called.append(1) or scene)
    gold = make_synthetic_scene(tmp_path / "gold", seed=3, with_text=False, frames=12)
    vid = render_scene_video(gold, tmp_path / "gold", tmp_path / "g.mp4")
    analyze(vid, 0, 11, tmp_path / "mg", AnalyzeOptions(ocr=False, refine=False))
    assert called == []
    analyze(vid, 0, 11, tmp_path / "ui", AnalyzeOptions(ocr=False, refine=False, ui=True))
    assert called == [1]


@pytest.mark.parametrize("ui", [False, True])
def test_rerun_respects_saved_ui_option_and_retains_components(tmp_path, monkeypatch, ui):
    model = UIModel(components=[UIComponent(id="nav", kind="nav", bbox=(0, 0, 100, 24), text="Home")])
    called = []

    class FakeAssets:
        def request(self, **kwargs):
            called.append(kwargs["kind"])
            return AssetResponse("application/json", b"", model)

    monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", "http://127.0.0.1:1")
    monkeypatch.setattr("keepframe.analyze.pipeline.AssetClient", FakeAssets)
    gold = make_synthetic_scene(tmp_path / "gold", seed=3, with_text=False, frames=12)
    vid = render_scene_video(gold, tmp_path / "gold", tmp_path / "g.mp4")
    root = tmp_path / "project"
    analyze(vid, 0, 11, root, AnalyzeOptions(ocr=False, refine=False, ui=ui))
    rerun(root, "s1", "keyframes", "retain UI")
    scene, version = current_scene(root, "s1")
    assert version.id == "v2"
    assert called == (["ui", "ui"] if ui else [])
    assert scene.ui == (model if ui else None)
    if ui:
        assert [(e.id, e.kind) for e in scene.elements] == [("nav", "ui")]
        assert (scene_dir(root, "s1") / scene.elements[0].canonical.texture).is_file()


@pytest.mark.parametrize("stage", ["analyze", "rerun"])
@pytest.mark.parametrize("error", [AssetAPIError("asset_api_unavailable"), RuntimeError("malformed UI response"), RuntimeError("x" * 1000)])
def test_ui_parse_failure_is_reported_and_analysis_continues(tmp_path, monkeypatch, stage, error):
    gold = make_synthetic_scene(tmp_path / "gold", seed=3, with_text=False, frames=12)
    vid = render_scene_video(gold, tmp_path / "gold", tmp_path / "g.mp4")
    root = tmp_path / "project"

    def fail(*args):
        raise error

    if stage == "rerun":
        analyze(vid, 0, 11, root, AnalyzeOptions(ocr=False, refine=False))
    monkeypatch.setattr("keepframe.analyze.pipeline._parse_ui", fail)
    opts = AnalyzeOptions(ocr=False, refine=False, ui=True)
    if stage == "analyze":
        analyze(vid, 0, 11, root, opts)
    else:
        rerun(root, "s1", "keyframes", "UI unavailable", opts)
    scene, version = current_scene(root, "s1")
    assert scene.elements and scene.ui is None
    assert version.id == ("v1" if stage == "analyze" else "v2")
    report = json.loads((scene_dir(root, "s1") / "report.json").read_text())
    message = next(message for message in report["messages"] if message.startswith("UI parse skipped:"))
    assert len(message) <= 200
    assert message == f"UI parse skipped: {type(error).__name__}: {error}"[:200]
    assert report["reconstruction"] and report["confidence"]


@pytest.mark.parametrize("reference", ["ui", "mg", None, "UI"])
def test_full_analyze_persists_reference_and_submits_ui_option(tmp_path, monkeypatch, reference):
    from keepframe.web import server as web_server

    project = _setup_token_test(tmp_path, monkeypatch)
    runner = RecordingRunner()
    monkeypatch.setattr(web_server.JOBS, "_runner", runner)
    srv = start(tmp_path / "ws")
    try:
        code, estimate_body = _post(srv, "/api/estimate", {
            "project_id": project["id"], "mode": "full", "start": 0, "end": 9,
        })
        assert code == 200
        payload = {
            "project_id": project["id"], "mode": "full", "start": 0, "end": 9,
            "confirm_token": estimate_body["confirm_token"],
            "boundary_digest": estimate_body["boundary_digest"],
        }
        if reference is not None:
            payload["reference"] = reference
        code, body = _post(srv, "/api/analyze", payload)
    finally:
        srv.shutdown()
        srv.server_close()

    assert code == 202
    assert body["job"]["status"] == "queued"
    assert runner.specs[0].args["options"] == {"ui": reference == "ui"}
    assert load_meta(tmp_path / "ws", project["id"])["reference"] == ("ui" if reference == "ui" else "mg")


@pytest.mark.parametrize("reference", ["ui", "mg", None])
def test_agent_analysis_uses_persisted_reference(tmp_path, monkeypatch, reference):
    from keepframe.jobs import JobStore
    from keepframe.session.agent import SessionTurn
    from keepframe.session.llm import NullClient
    from tests.test_web_agent import _project, _post as agent_post

    _, scene = _project(tmp_path)
    if reference is not None:
        write_meta(tmp_path / "ws", "p1", reference=reference)
    runner = RecordingRunner()
    monkeypatch.setattr("keepframe.web.server.JOBS", JobStore(runner=runner))
    monkeypatch.setattr("keepframe.web.server.make_llm", lambda *args: NullClient())
    monkeypatch.setattr("keepframe.web.server.probe_video", lambda path: {"frames": scene.frames})

    def request_analysis(self, ctx, message, history):
        ctx.submit_job("analyze", {"reference": "mg" if reference == "ui" else "ui"}, "frames")
        return SessionTurn(reply="queued")

    monkeypatch.setattr("keepframe.web.server.SessionAgent.turn", request_analysis)
    srv = start(tmp_path / "ws")
    try:
        code, body = agent_post(srv, "/api/agent", {"project": "p1", "scene": scene.id, "message": "analyze"})
    finally:
        srv.shutdown()
        srv.server_close()
    assert code == 200, body
    assert runner.specs[0].args["options"] == {"ui": reference == "ui"}


@pytest.mark.parametrize("ui", [False, True])
def test_cli_analyze_ui_flag(tmp_path, monkeypatch, ui):
    from keepframe.cli import main

    called = []

    def fake_analyze(video, first, last, out, options, captioner=None):
        called.append(options)
        assert captioner is None
        return SimpleNamespace(scenes=[SimpleNamespace(id="s1")], versions=[SimpleNamespace(id="v1")])

    monkeypatch.setattr("keepframe.session.llm.vision_llm", lambda: None)
    monkeypatch.setattr("keepframe.analyze.pipeline.analyze", fake_analyze)
    args = ["analyze", "--video", str(tmp_path / "video.mp4"), "--end", "11", "--out", str(tmp_path / "out")]
    assert main(args + (["--ui"] if ui else [])) == 0
    assert called[0].ui is ui


def test_ingest_selection_reaches_post_analyze_body(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    static = Path("keepframe/web/static/js")
    ingest = (static / "ingest.js").read_text()
    analyze_js = (static / "analyze.js").read_text()
    start_analysis = ingest[ingest.index("function startAnalysis()"):ingest.index("\nbindIngest();")]
    analyze_payload = analyze_js[analyze_js.index("function analyzePayload()"):analyze_js.index("\nasync function loadProjectChrome()")]
    script = tmp_path / "reference.mjs"
    script.write_text('''import assert from 'node:assert/strict';
import vm from 'node:vm';
for (const checked of [false, true]) {
  const saved = new Map();
  const context = vm.createContext({
    URLSearchParams, location: {},
    sessionStorage: {setItem: (key, value) => saved.set(key, value)},
    qs: selector => {assert.equal(selector, '[data-reference-ui]'); return {checked};},
    selectedWindow: () => ({mode:'range', start:0, end:11}),
    state: {project:{id:'p1'}, mode:'range', estimateSnapshot:{
      mode:'range', start:0, end:11, confirm_token:'token', boundary_digest:'digest'
    }}
  });
  vm.runInContext(''' + json.dumps(start_analysis + "\nstartAnalysis();") + ''', context);
  const query = new URL(context.location.href, 'http://localhost').search;
  assert.equal(saved.get('keepframe.analyze-start'), query);
  context.params = new URLSearchParams(query);
  context.estimateSnapshot = JSON.parse(saved.get('keepframe.analyze-estimate'));
  vm.runInContext(`const projectId = params.get('job'), token = params.get('token');
    const analyzeMode = params.get('mode'), analyzeStart = params.get('start'), analyzeEnd = params.get('end');`
    + ''' + json.dumps(analyze_payload + "\nglobalThis.payload = analyzePayload();") + ''', context);
  assert.equal(context.payload.reference, checked ? 'ui' : 'mg');
  assert.equal(context.payload.project_id, 'p1');
  assert.equal(context.payload.boundary_digest, 'digest');
}
''')
    result = subprocess.run([node, str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
