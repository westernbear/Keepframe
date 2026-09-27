import io
import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from keepframe.after_effects.auth import AEProjectAuth
from keepframe.after_effects.coordinator import AECoordinator

from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.jobs import JobStore
from keepframe.render.plan import create_render_plan
from keepframe.web import server as web_server
from tests.test_native_plan import RecordingRunner
from tests.test_ae_coordinator import _artifact_payload, _committed_checkpoint
from tests.test_web_server import get, start


def _origin(server):
    return f"http://127.0.0.1:{server.server_address[1]}"


def _post(server, path, payload, *, cookie=None, origin=None):
    headers = {"Content-Type": "application/json", "Origin": origin or _origin(server)}
    if cookie is not None:
        headers["Cookie"] = cookie
    request = Request(
        f"{_origin(server)}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request) as response:
        return response.status, json.loads(response.read())


def _pair(server, project="p1", *, origin=None):
    origin = origin or _origin(server)
    request = Request(
        f"{_origin(server)}/api/ae/pairings",
        data=json.dumps({"project": project, "capability_request": {"required": ["ae_version"]}}).encode(),
        headers={"Content-Type": "application/json", "Origin": origin},
        method="POST",
    )
    with urlopen(request) as response:
        set_cookie = response.headers["Set-Cookie"]
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
        assert "Domain=" not in set_cookie
        assert ("Secure" in set_cookie) == origin.startswith("https://")
        return set_cookie.split(";", 1)[0], json.loads(response.read())


def _get_ae(server, path, cookie, *, origin=None):
    request = Request(
        f"{_origin(server)}{path}",
        headers={"Cookie": cookie, "Origin": origin or _origin(server)},
    )
    with urlopen(request) as response:
        return response.status, json.loads(response.read())


def _ae_post(server, cookie, path, payload):
    return _post(server, path, payload, cookie=cookie)


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
    server = start(workspace, ae_relay_url="https://relay.example")
    try:
        cookie, _ = _pair(server)
        payload = {"project": "p1", "digest": plan.digest, "revision": 0}
        first_code, first = _ae_post(
            server, cookie, f"/api/render-plans/{plan.id}/approve", payload
        )
        retry_code, retry = _ae_post(
            server, cookie, f"/api/render-plans/{plan.id}/approve", payload
        )
        state_code, state = _get_ae(
            server, f"/api/render-state?project=p1&plan={plan.id}", cookie
        )
    finally:
        server.shutdown()

    assert first_code == retry_code == 202
    assert first["status"] == retry["status"] == "waiting_for_connector"
    assert first["session"]["id"] == retry["session"]["id"]
    assert first["job"] is None
    assert state_code == 200
    assert state["status"] == "waiting_for_connector"
    assert state["session"]["id"] == first["session"]["id"]


def test_ae_private_controls_enforce_revision_and_transition_table(tmp_path):
    workspace = tmp_path / "ws"
    plan = _plan(workspace, backend="after_effects", mode="final")
    server = start(workspace, ae_relay_url="https://relay.example")
    try:
        cookie, _ = _pair(server)
        _, approved = _ae_post(
            server,
            cookie,
            f"/api/render-plans/{plan.id}/approve",
            {"project": "p1", "digest": plan.digest, "revision": 0},
        )
        with pytest.raises(HTTPError) as negative_checkpoint:
            _ae_post(
                server,
                cookie,
                f"/api/ae/sessions/{approved['session']['id']}/select-checkpoint",
                {"project": "p1", "plan": plan.id, "revision": 0, "checkpoint": -1},
            )
        assert negative_checkpoint.value.code == 400
        with pytest.raises(HTTPError) as invalid_device:
            _ae_post(
                server,
                cookie,
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
        code, stopped = _ae_post(server, cookie, f"/api/ae/sessions/{session.id}/stop", body)
        assert code == 200
        assert stopped["session"]["status"] == "pause_requested"
        with pytest.raises(HTTPError) as stale:
            _ae_post(server, cookie, f"/api/ae/sessions/{session.id}/continue", body)
        assert stale.value.code == 409

        paused = coordinator.state()
        assert paused.status == "paused:user"
        _, manual = _ae_post(
            server,
            cookie,
            f"/api/ae/sessions/{session.id}/begin-manual",
            {"project": "p1", "plan": plan.id, "revision": paused.revision},
        )
        _, syncing = _ae_post(
            server,
            cookie,
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
        _, selected = _ae_post(
            server,
            cookie,
            f"/api/ae/sessions/{session.id}/select-checkpoint",
            {
                "project": "p1",
                "plan": plan.id,
                "revision": synced.revision,
                "checkpoint": 1,
            },
        )
        _, finalizing = _ae_post(
            server,
            cookie,
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


def test_ae_private_control_missing_project_is_not_an_oracle(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    server = start(workspace, ae_relay_url="https://relay.example")
    try:
        with pytest.raises(HTTPError) as missing:
            _post(
                server,
                "/api/ae/sessions/ae-12345678/stop",
                {"project": "missing", "plan": "a" * 32, "revision": 0},
            )
        assert missing.value.code == 401
    finally:
        server.shutdown()


def test_ae_private_control_rejects_missing_resources_and_bad_encoding(tmp_path):
    workspace = tmp_path / "ws"
    plan = _plan(workspace, backend="after_effects")
    server = start(workspace, ae_relay_url="https://relay.example")
    try:
        cookie, _ = _pair(server)
        for plan_id in ("b" * 32, plan.id):
            with pytest.raises(HTTPError) as missing:
                _ae_post(
                    server,
                    cookie,
                    "/api/ae/sessions/ae-12345678/stop",
                    {"project": "p1", "plan": plan_id, "revision": 0},
                )
            assert missing.value.code == 404

        request = Request(
            f"{_origin(server)}/api/ae/sessions/ae-12345678/stop",
            data=b"\xff",
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Origin": _origin(server),
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as malformed:
            urlopen(request)
        assert malformed.value.code == 400
    finally:
        server.shutdown()


def test_ae_browser_routes_require_same_origin_controller_and_revoke_on_unpair(tmp_path):
    workspace = tmp_path / "ws"
    plan = _plan(workspace, backend="after_effects")
    server = start(workspace, ae_relay_url="https://relay.example")
    try:
        pairing_request = Request(
            f"{_origin(server)}/api/ae/pairings",
            data=json.dumps({"project": "p1", "capability_request": {}}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as cross_site_pairing:
            urlopen(pairing_request)
        assert cross_site_pairing.value.code == 403

        cookie, pairing = _pair(server)
        assert pairing["relay_url"] == "https://relay.example"
        assert len(pairing["code"]) >= 22
        unauthorized_repair = Request(
            f"{_origin(server)}/api/ae/pairings",
            data=json.dumps(
                {"project": "p1", "capability_request": {}}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": _origin(server),
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as missing_controller:
            urlopen(unauthorized_repair)
        assert missing_controller.value.code == 401
        approval = {"project": "p1", "digest": plan.digest, "revision": 0}
        with pytest.raises(HTTPError) as missing_cookie:
            _post(server, f"/api/render-plans/{plan.id}/approve", approval)
        assert missing_cookie.value.code == 401
        with pytest.raises(HTTPError) as wrong_origin:
            _post(
                server,
                f"/api/render-plans/{plan.id}/approve",
                approval,
                cookie=cookie,
                origin="http://attacker.example",
            )
        assert wrong_origin.value.code == 403

        _, approved = _ae_post(
            server,
            cookie,
            f"/api/render-plans/{plan.id}/approve",
            approval,
        )
        state_path = f"/api/render-state?project=p1&plan={plan.id}"
        with pytest.raises(HTTPError) as state_without_cookie:
            _get_ae(server, state_path, "")
        assert state_without_cookie.value.code == 401

        auth = AEProjectAuth(workspace / "p1", "p1")
        device = auth.redeem_pairing(pairing["code"])
        assert approved["session"]["status"] == "waiting_for_connector"
        delete = Request(
            f"{_origin(server)}/api/ae/pairings/p1",
            headers={"Cookie": cookie, "Origin": _origin(server)},
            method="DELETE",
        )
        with urlopen(delete) as response:
            assert response.status == 200
            assert "Max-Age=0" in response.headers["Set-Cookie"]
        assert not auth.authenticate_controller(cookie.split("=", 1)[1])
        assert not auth.authenticate_device(device.token)
        with pytest.raises(HTTPError) as revoked:
            _get_ae(server, state_path, cookie)
        assert revoked.value.code == 401
    finally:
        server.shutdown()


def test_ae_artifact_download_requires_controller_and_streams_verified_bytes(tmp_path):
    workspace = tmp_path / "ws"
    plan = _plan(workspace, backend="after_effects")
    server = start(workspace, ae_relay_url="https://relay.example")
    try:
        cookie, pairing = _pair(server)
        _, approved = _ae_post(
            server,
            cookie,
            f"/api/render-plans/{plan.id}/approve",
            {"project": "p1", "digest": plan.digest, "revision": 0},
        )
        device = AEProjectAuth(workspace / "p1", "p1").redeem_pairing(
            pairing["code"]
        )
        coordinator = AECoordinator.cached(workspace / "p1", plan.id)
        coordinator.transition(
            "device_ready",
            revision=approved["session"]["revision"],
            device_id=device.identity.device_id,
        )
        payload = _artifact_payload("png")
        reservation = coordinator.reserve_artifact("png", len(payload))
        artifact = coordinator.publish_artifact(
            device.identity.device_id,
            reservation.id,
            io.BytesIO(payload),
            content_length=len(payload),
        )
        path = (
            f"/api/ae/artifacts/{artifact.id}"
            f"?project=p1&plan={plan.id}"
        )

        for headers, expected in (
            ({"Origin": _origin(server)}, 401),
            (
                {
                    "Origin": "http://attacker.example",
                    "Cookie": cookie,
                },
                403,
            ),
        ):
            request = Request(f"{_origin(server)}{path}", headers=headers)
            with pytest.raises(HTTPError) as denied:
                urlopen(request)
            assert denied.value.code == expected

        request = Request(
            f"{_origin(server)}{path}",
            headers={"Origin": _origin(server), "Cookie": cookie},
        )
        with urlopen(request) as response:
            assert response.read() == payload
            assert response.headers["Content-Type"] == "image/png"
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Content-Disposition"] == (
                f'attachment; filename="keepframe-{artifact.id}.png"'
            )
        (
            workspace
            / "p1"
            / "renders"
            / plan.id
            / "ae"
            / "artifacts"
            / artifact.id
        ).write_bytes(payload + b"tampered")
        with pytest.raises(HTTPError) as tampered:
            urlopen(request)
        assert tampered.value.code == 404
    finally:
        server.shutdown()


def test_ae_replacement_drains_active_lease_before_new_device_can_bind(tmp_path):
    workspace = tmp_path / "ws"
    plan = _plan(workspace, backend="after_effects")
    server = start(workspace, ae_relay_url="https://relay.example")
    try:
        cookie, pairing = _pair(server)
        _, approved = _ae_post(
            server,
            cookie,
            f"/api/render-plans/{plan.id}/approve",
            {"project": "p1", "digest": plan.digest, "revision": 0},
        )
        auth = AEProjectAuth(workspace / "p1", "p1")
        old_device = auth.redeem_pairing(pairing["code"])
        coordinator = AECoordinator.cached(workspace / "p1", plan.id)
        session = coordinator.transition(
            "device_ready",
            revision=approved["session"]["revision"],
            device_id=old_device.identity.device_id,
        )
        command = coordinator.enqueue_command(
            "heartbeat",
            {"probe": True},
            expected_state=session.status,
            expected_checkpoint=None,
            lease_seconds=60,
        )
        leased = coordinator.next_command(old_device.identity.device_id)
        assert leased is not None

        request = Request(
            f"{_origin(server)}/api/ae/pairings",
            data=json.dumps(
                {"project": "p1", "capability_request": {}}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Origin": _origin(server),
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as blocked:
            urlopen(request)
        assert blocked.value.code == 409
        replacement_cookie = blocked.value.headers["Set-Cookie"].split(";", 1)[0]
        assert json.loads(blocked.value.read()) == {
            "error": "connector replacement is waiting for the active command"
        }
        assert auth.authenticate_device(
            old_device.token,
            allow_draining=False,
        ) is None
        draining = auth.authenticate_device(old_device.token)
        assert draining is not None and draining.status == "draining"

        coordinator.accept_result(
            old_device.identity.device_id,
            command.id,
            sequence=leased.sequence,
            result={"ok": True},
        )
        retry = Request(
            f"{_origin(server)}/api/ae/pairings",
            data=json.dumps(
                {"project": "p1", "capability_request": {}}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": replacement_cookie,
                "Origin": _origin(server),
            },
            method="POST",
        )
        with urlopen(retry) as response:
            replacement_cookie = response.headers["Set-Cookie"].split(";", 1)[0]
            replacement = json.loads(response.read())
        new_device = auth.redeem_pairing(replacement["code"])
        assert auth.authenticate_device(old_device.token) is None
        assert new_device.identity.status == "active"

        paused = coordinator.state()
        _, continued = _ae_post(
            server,
            replacement_cookie,
            f"/api/ae/sessions/{paused.id}/continue",
            {"project": "p1", "plan": plan.id, "revision": paused.revision},
        )
        assert continued["session"]["status"] == "waiting_for_connector"
        assert continued["session"]["device_id"] is None
    finally:
        server.shutdown()
