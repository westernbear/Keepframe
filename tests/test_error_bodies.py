"""R52 on the server and job paths: exception text (paths, tokens) never reaches a client-visible value — only a code."""
import json
import time

import pytest

from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.jobs import JobStore
from tests.test_ae_api import json_request
from tests.test_background_plate import _gradient_clip
from tests.test_failsoft_codes import FRAGMENTS, LEAK, _clean, boom
from tests.test_final_fixes import _post
from tests.test_web_server import get, start


def _project(tmp_path):
    root = tmp_path / "ws" / "p1"
    scene = make_synthetic_scene(root / "gold", seed=11, with_text=False)
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range",
                        "range": [0, scene.frames - 1]}, scene)
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "t", "status": "review"}))
    return tmp_path / "ws", scene


def test_job_runner_generic_error_is_a_code():
    store = JobStore()
    job = store.submit("x", fn=lambda: boom(), project_id="p1")
    for _ in range(100):
        if job.status == "error":
            break
        time.sleep(0.05)
    assert job.error == "job_failed"
    _clean(job.error)


def test_state_500_is_a_code(tmp_path, monkeypatch):
    ws, scene = _project(tmp_path)
    monkeypatch.setattr("keepframe.web.server.review_state_payload", boom)
    srv = start(ws)
    try:
        code, _, body = get(srv, f"/api/state?project=p1&scene={scene.id}")
    finally:
        srv.shutdown()
    assert code == 500 and json.loads(body) == {"error": "internal_error"}
    _clean(body.decode())


def test_edit_400_is_a_code(tmp_path, monkeypatch):
    ws, scene = _project(tmp_path)
    monkeypatch.setattr("keepframe.web.server.run_edit", boom)
    srv = start(ws)
    try:
        code, body = _post(srv, "/api/edit", {"project": "p1", "scene": scene.id, "prompt": "make it red"}, "same")
    finally:
        srv.shutdown()
    assert code == 400 and body == {"error": "invalid_request"}
    _clean(json.dumps(body))


def test_ae_validation_error_is_a_code(tmp_path, monkeypatch):
    from pydantic import BaseModel

    class M(BaseModel):
        x: int

    def leak(*a, **k):
        M.model_validate({"x": LEAK})
    monkeypatch.setattr("keepframe.ae.api.AERoutes._post", leak)
    srv = start(tmp_path)
    try:
        status, body = json_request(srv, "POST", "/api/ae/codes", browser=True)
    finally:
        srv.shutdown()
    assert status == 400 and body == {"error": "invalid_request"}
    _clean(json.dumps(body))


def test_refine_failure_is_a_code(tmp_path, monkeypatch):
    monkeypatch.setattr("keepframe.analyze.refine.torch_available", lambda: True)
    monkeypatch.setattr("keepframe.analyze.refine.refine_affine", boom)
    monkeypatch.setattr("keepframe.analyze.device.resolve_device", lambda: "cpu")
    analyze_scene_frames(_gradient_clip(h=320, w=640, picture=True), 30, tmp_path, "s1",
                         AnalyzeOptions(ocr=False, refine=True, use_ecc=False))
    report = (tmp_path / "scenes" / "s1" / "report.json").read_text()
    assert "refine skipped (refine_failed)" in report
    _clean(report)
