import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from keepframe.after_effects.coordinator import AECoordinator

from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.jobs import JobStore
from keepframe.render.plan import create_render_plan
from keepframe.web import server as web_server
from tests.test_native_plan import RecordingRunner
from tests.test_ae_coordinator import _committed_checkpoint
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
    kwargs = {"capability_hash": "a" * 64} if backend == "after_effects" else {}
    return create_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        backend=backend,
        mode=mode,
        **kwargs,
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


def test_ae_plan_approval_creates_durable_waiting_session(tmp_path):
    workspace = tmp_path / "ws"
    plan = _plan(workspace, backend="after_effects")
    server = start(workspace)
    try:
        payload = {"project": "p1", "digest": plan.digest, "revision": 0}
        first_code, first = _post(server, f"/api/render-plans/{plan.id}/approve", payload)
        retry_code, retry = _post(server, f"/api/render-plans/{plan.id}/approve", payload)
        state_code, _, state_body = get(server, f"/api/render-state?project=p1&plan={plan.id}")
    finally:
        server.shutdown()

    assert first_code == retry_code == 202
    assert first["status"] == retry["status"] == "waiting_for_connector"
    assert first["session"]["id"] == retry["session"]["id"]
    assert first["job"] is None
    assert state_code == 200
    state = json.loads(state_body)
    assert state["status"] == "waiting_for_connector"
    assert state["session"]["id"] == first["session"]["id"]


def test_ae_private_controls_enforce_revision_and_transition_table(tmp_path):
    workspace = tmp_path / "ws"
    plan = _plan(workspace, backend="after_effects", mode="final")
    server = start(workspace)
    try:
        _, approved = _post(
            server,
            f"/api/render-plans/{plan.id}/approve",
            {"project": "p1", "digest": plan.digest, "revision": 0},
        )
        with pytest.raises(HTTPError) as negative_checkpoint:
            _post(
                server,
                f"/api/ae/sessions/{approved['session']['id']}/select-checkpoint",
                {"project": "p1", "plan": plan.id, "revision": 0, "checkpoint": -1},
            )
        assert negative_checkpoint.value.code == 400
        with pytest.raises(HTTPError) as invalid_device:
            _post(
                server,
                f"/api/ae/sessions/{approved['session']['id']}/continue",
                {"project": "p1", "plan": plan.id, "revision": 0, "device_id": "../device"},
            )
        assert invalid_device.value.code == 400
        coordinator = AECoordinator.cached(workspace / "p1", plan.id)
        session = coordinator.transition(
            "device_ready", revision=approved["session"]["revision"], device_id="device-1"
        )
        checkpoint = _committed_checkpoint(coordinator, 0, provenance="baseline")
        session = coordinator.transition(
            "baseline_complete", revision=session.revision, checkpoint=checkpoint.model_dump(mode="json")
        )
        body = {"project": "p1", "plan": plan.id, "revision": session.revision}
        code, stopped = _post(server, f"/api/ae/sessions/{session.id}/stop", body)
        assert code == 200
        assert stopped["session"]["status"] == "pause_requested"
        with pytest.raises(HTTPError) as stale:
            _post(server, f"/api/ae/sessions/{session.id}/continue", body)
        assert stale.value.code == 409

        paused = coordinator.state()
        assert paused.status == "paused:user"
        _, manual = _post(
            server,
            f"/api/ae/sessions/{session.id}/begin-manual",
            {"project": "p1", "plan": plan.id, "revision": paused.revision},
        )
        _, syncing = _post(
            server,
            f"/api/ae/sessions/{session.id}/sync-manual",
            {
                "project": "p1",
                "plan": plan.id,
                "revision": manual["session"]["revision"],
            },
        )
        assert syncing["session"]["status"] == "manual_edit"
        assert syncing["command"]["kind"] == "sync_manual"
        checkpoint = _committed_checkpoint(coordinator, 1, provenance="manual")
        leased = coordinator.next_command("device-1")
        assert leased is not None and leased.id == syncing["command"]["id"]
        coordinator.accept_result(
            "device-1",
            leased.id,
            sequence=leased.sequence,
            result={"ok": True, "checkpoint": checkpoint.model_dump(mode="json")},
        )
        synced = coordinator.state()
        _, selected = _post(
            server,
            f"/api/ae/sessions/{session.id}/select-checkpoint",
            {
                "project": "p1",
                "plan": plan.id,
                "revision": synced.revision,
                "checkpoint": 1,
            },
        )
        _, finalizing = _post(
            server,
            f"/api/ae/sessions/{session.id}/finalize",
            {
                "project": "p1",
                "plan": plan.id,
                "revision": selected["session"]["revision"],
            },
        )
    finally:
        server.shutdown()

    assert synced.status == "paused:manual_synced"
    assert finalizing["session"]["status"] == "finalizing"


def test_ae_private_control_missing_project_returns_404(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    server = start(workspace)
    try:
        with pytest.raises(HTTPError) as missing:
            _post(
                server,
                "/api/ae/sessions/ae-12345678/stop",
                {"project": "missing", "plan": "a" * 32, "revision": 0},
            )
        assert missing.value.code == 404
    finally:
        server.shutdown()


def test_ae_private_control_rejects_missing_resources_and_bad_encoding(tmp_path):
    workspace = tmp_path / "ws"
    plan = _plan(workspace, backend="after_effects")
    server = start(workspace)
    try:
        for plan_id in ("b" * 32, plan.id):
            with pytest.raises(HTTPError) as missing:
                _post(
                    server,
                    "/api/ae/sessions/ae-12345678/stop",
                    {"project": "p1", "plan": plan_id, "revision": 0},
                )
            assert missing.value.code == 404

        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/ae/sessions/ae-12345678/stop",
            data=b"\xff",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as malformed:
            urlopen(request)
        assert malformed.value.code == 400
    finally:
        server.shutdown()
