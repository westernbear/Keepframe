import json
from pathlib import Path
from urllib.request import Request, urlopen

from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.jobs import JobStore
from keepframe.render.plan import create_render_plan
from keepframe.web import server as web_server
from tests.test_native_plan import RecordingRunner
from tests.test_web_server import get, start


def _post(server, path, payload):
    request = Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request) as response:
        return response.status, json.loads(response.read())


def _plan(workspace: Path):
    root = workspace / "p1"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=9, with_text=False, frames=12)
    scene = scene.model_copy(update={"id": "s1"})
    init_project(
        root,
        {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]},
        scene,
    )
    (root / "meta.json").write_text(
        json.dumps({"id": "p1", "status": "approved", "version": "v1", "scene": "s1"}),
        encoding="utf-8",
    )
    return create_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        backend="native",
        mode="preview",
    )


def test_native_plan_approval_is_the_only_idempotent_execution_gate(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    plan = _plan(workspace)
    runner = RecordingRunner()
    store = JobStore(runner=runner)
    monkeypatch.setattr(web_server, "JOBS", store)
    server = start(workspace)
    try:
        payload = {"project": "p1", "digest": plan.digest, "revision": 0}
        first_code, first = _post(server, f"/api/render-plans/{plan.id}/approve", payload)
        retry_code, retry = _post(server, f"/api/render-plans/{plan.id}/approve", payload)
        state_code, _, state_body = get(server, f"/api/render-state?project=p1&plan={plan.id}")
    finally:
        server.shutdown()

    assert first_code == retry_code == 202
    assert first["job"]["id"] == retry["job"]["id"]
    assert first["status"] == retry["status"] == "queued"
    assert len(runner.specs) == 1
    assert state_code == 200
    state = json.loads(state_body)
    assert state["plan"]["id"] == plan.id
    assert state["state"]["execution_id"] == first["state"]["execution_id"]
    assert state["job"]["id"] == first["job"]["id"]
    assert state["status"] == "queued"
