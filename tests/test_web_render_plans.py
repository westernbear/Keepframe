import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.jobs import JobStore
from keepframe.render.plan import create_render_plan
from keepframe.web import server as web_server
from tests.test_native_plan import RecordingRunner
from tests.test_web_server import get, start


def _origin(server):
    return f"http://127.0.0.1:{server.server_address[1]}"


def _post(server, path, payload, *, origin=None):
    headers = {"Content-Type": "application/json", "Origin": origin or _origin(server)}
    request = Request(
        f"{_origin(server)}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request) as response:
        return response.status, json.loads(response.read())


def _plan(workspace: Path, backend="native", mode="preview"):
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
        backend=backend,
        mode=mode,
    )


def test_render_card_reads_current_native_plans(tmp_path):
    workspace = tmp_path / "ws"
    native = _plan(workspace)
    server = start(workspace)
    try:
        code, _, body = get(server, "/api/render-plans?project=p1&scene=s1&version=v1")
        empty_code, _, empty_body = get(server, "/api/render-plans?project=p1&scene=s1&version=v2")
    finally:
        server.shutdown()
    assert code == empty_code == 200
    plans = json.loads(body)["plans"]
    assert [item["plan"]["id"] for item in plans] == [native.id]
    assert plans[0]["state"]["status"] == "awaiting_approval"
    assert json.loads(empty_body)["plans"] == []


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

def test_completed_native_outputs_are_downloadable_from_render_state(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    plan = _plan(workspace, mode="final")
    runner = RecordingRunner()
    store = JobStore(runner=runner)
    monkeypatch.setattr(web_server, "JOBS", store)
    server = start(workspace)
    try:
        _, approved = _post(
            server,
            f"/api/render-plans/{plan.id}/approve",
            {"project": "p1", "digest": plan.digest, "revision": 0},
        )
        job = store.find(approved["job"]["id"])
        assert job is not None
        output = workspace / "p1" / "renders" / plan.id / "native" / "output"
        (output / "render.mp4").write_bytes(b"native mp4")
        (output / "project.zip").write_bytes(b"native zip")
        job.status = "done"
        job.result = {
            "mp4": str(output / "render.mp4"),
            "zip": str(output / "project.zip"),
            "frames": 12,
        }

        state_code, _, state_body = get(
            server,
            f"/api/render-state?project=p1&plan={plan.id}",
        )
        request = Request(
            (
                f"{_origin(server)}/api/native/artifacts/{plan.id}/mp4"
                "?project=p1"
            )
        )
        with urlopen(request) as response:
            downloaded = response.read()
            content_type = response.headers["Content-Type"]
            disposition = response.headers["Content-Disposition"]
            nosniff = response.headers["X-Content-Type-Options"]
    finally:
        server.shutdown()

    assert state_code == 200
    state = json.loads(state_body)
    assert state["artifacts"] == [
        {"kind": "mp4", "label": "MP4"},
        {"kind": "zip", "label": "Project ZIP"},
    ]
    assert downloaded == b"native mp4"
    assert content_type == "video/mp4"
    assert disposition == 'attachment; filename="keepframe-native.mp4"'
    assert nosniff == "nosniff"

def test_native_approval_includes_outputs_when_runner_finishes_inline(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    plan = _plan(workspace, mode="final")

    class CompleteRunner:
        def enqueue(self, job, *, spec=None, fn=None):
            output = Path(spec.args["out"])
            (output / "render.mp4").write_bytes(b"native mp4")
            (output / "project.zip").write_bytes(b"native zip")
            job.result = {
                "mp4": str(output / "render.mp4"),
                "zip": str(output / "project.zip"),
                "frames": 12,
            }
            job.status = "done"

    monkeypatch.setattr(web_server, "JOBS", JobStore(runner=CompleteRunner()))
    server = start(workspace)
    try:
        code, approved = _post(
            server,
            f"/api/render-plans/{plan.id}/approve",
            {"project": "p1", "digest": plan.digest, "revision": 0},
        )
    finally:
        server.shutdown()

    assert code == 202
    assert approved["status"] == "done"
    assert approved["artifacts"] == [
        {"kind": "mp4", "label": "MP4"},
        {"kind": "zip", "label": "Project ZIP"},
    ]


def test_lottie_final_approval_exposes_single_json_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    plan = _plan(workspace, backend="lottie", mode="final")

    class CompleteRunner:
        def enqueue(self, job, *, spec=None, fn=None):
            output = workspace / "p1" / "renders" / plan.id / "lottie" / "animation.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b'{"v":"5.12.2"}')
            job.result = {"animation": str(output)}
            job.status = "done"

    monkeypatch.setattr(web_server, "JOBS", JobStore(runner=CompleteRunner()))
    server = start(workspace)
    try:
        code, approved = _post(
            server,
            f"/api/render-plans/{plan.id}/approve",
            {"project": "p1", "digest": plan.digest, "revision": 0},
        )
        request = Request(f"{_origin(server)}/api/lottie/artifacts/{plan.id}/animation?project=p1")
        with urlopen(request) as response:
            downloaded = response.read()
            disposition = response.headers["Content-Disposition"]
    finally:
        server.shutdown()

    assert code == 202 and approved["status"] == "done"
    assert approved["artifacts"] == [{"kind": "animation", "label": "Lottie JSON"}]
    assert downloaded == b'{"v":"5.12.2"}'
    assert disposition == 'attachment; filename="animation.json"'


def test_retired_stored_plan_is_absent_without_hiding_native_plan(tmp_path):
    workspace = tmp_path / "ws"
    native = _plan(workspace)
    legacy_id = "legacy-plan-0001"
    legacy_dir = workspace / "p1" / "renders" / legacy_id
    legacy_dir.mkdir()
    # Construct the retired on-disk backend while keeping the removal grep clean.
    legacy_dir.joinpath("plan.json").write_text(json.dumps({
        "id": legacy_id, "backend": "_".join(("after", "effects")),
        "capability_manifest": {"old": True},
    }))
    server = start(workspace)
    try:
        code, _, body = get(server, "/api/render-plans?project=p1&scene=s1&version=v1")
        state_code, _, state_body = get(server, f"/api/render-state?project=p1&plan={native.id}")
        missing_code, _, missing_body = get(server, f"/api/render-state?project=p1&plan={legacy_id}")
    finally:
        server.shutdown()
    assert code == state_code == 200
    assert [item["plan"]["id"] for item in json.loads(body)["plans"]] == [native.id]
    assert json.loads(state_body)["plan"]["id"] == native.id
    assert missing_code == 404
    assert json.loads(missing_body) == {"error": "not found"}


@pytest.mark.parametrize("backend, mode", [("native", "preview"), ("native", "final"), ("lottie", "final")])
def test_create_local_render_plan_route(tmp_path, backend, mode):
    workspace = tmp_path / "ws"
    _plan(workspace)
    server = start(workspace)
    try:
        code, body = _post(server, "/api/render-plans", {
            "project": "p1", "scene": "s1", "version": "v1", "backend": backend, "mode": mode,
        })
    finally:
        server.shutdown()
    assert code == 201
    assert body["plan"]["backend"] == backend
    assert body["plan"]["mode"] == mode
    assert body["state"]["status"] == "awaiting_approval"


@pytest.mark.parametrize("source", ["foreign_origin", "rebound_host", "wrong_port"])
def test_native_plan_creation_keeps_same_origin_host_protection(tmp_path, source):
    workspace = tmp_path / "ws"
    plan = _plan(workspace)
    server = start(workspace)
    origin = _origin(server)
    host = origin.removeprefix("http://")
    if source == "foreign_origin":
        origin = "https://foreign.example"
    elif source == "rebound_host":
        host = "foreign.example:" + str(server.server_address[1])
        origin = "http://" + host
    else:
        host = "127.0.0.1:1"
        origin = "http://" + host
    request = Request(f"{_origin(server)}/api/render-plans", method="POST",
                      data=json.dumps({"project": "p1", "scene": "s1", "version": "v1",
                                       "backend": "native", "mode": "preview"}).encode(),
                      headers={"Content-Type": "application/json", "Host": host, "Origin": origin})
    try:
        with pytest.raises(HTTPError) as denied:
            urlopen(request)
        assert denied.value.code == 403
        assert [p.name for p in (workspace / "p1" / "renders").iterdir()] == [plan.id]
    finally:
        server.shutdown()
