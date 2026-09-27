import json
import os
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from keepframe.after_effects.auth import AEProjectAuth
from keepframe.after_effects.relay import make_relay_server
from tests.test_ae_coordinator import _artifact_payload, _coordinator


DEPLOYMENT_TOKEN = "deployment-" + "a" * 32


def _serve(workspace):
    server = make_relay_server(workspace, deployment_token=DEPLOYMENT_TOKEN)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _request(server, path, *, query=None, token=None, method="GET", payload=None):
    suffix = f"?{urlencode(query)}" if query else ""
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return urlopen(
        Request(
            f"http://127.0.0.1:{server.server_address[1]}{path}{suffix}",
            data=data,
            headers=headers,
            method=method,
        )
    )


def _pair(server, root, *, project="p1"):
    pairing = AEProjectAuth(root, project).create_pairing(None, {"required": ["ae_version"]})
    with _request(
        server,
        "/pair",
        token=DEPLOYMENT_TOKEN,
        method="POST",
        payload={"project": project, "code": pairing.code},
    ) as response:
        return json.loads(response.read())


def test_relay_requires_deployment_and_device_credentials(tmp_path):
    workspace = tmp_path / "ws"
    root, plan, _, _ = _coordinator(workspace)
    with pytest.raises(ValueError, match="token"):
        make_relay_server(workspace, deployment_token="")

    server, thread = _serve(workspace)
    try:
        with _request(server, "/health") as health:
            assert json.loads(health.read()) == {"ok": True, "relay": "after_effects"}
        with pytest.raises(HTTPError) as private_route:
            _request(server, "/api/projects")
        assert private_route.value.code == 404

        pairing = AEProjectAuth(root, "p1").create_pairing(None, {})
        with pytest.raises(HTTPError) as unauthenticated_pair:
            _request(
                server,
                "/pair",
                method="POST",
                payload={"project": "p1", "code": pairing.code},
            )
        assert unauthenticated_pair.value.code == 401
        with pytest.raises(HTTPError) as wrong_deployment_token:
            _request(
                server,
                "/pair",
                token="wrong",
                method="POST",
                payload={"project": "p1", "code": pairing.code},
            )
        assert wrong_deployment_token.value.code == 401

        with _request(
            server,
            "/pair",
            token=DEPLOYMENT_TOKEN,
            method="POST",
            payload={"project": "p1", "code": pairing.code},
        ) as paired_response:
            paired = json.loads(paired_response.read())
        assert paired["project"] == "p1"
        assert paired["capability_request"] == {}
        persisted = (root / "ae-auth.json").read_text(encoding="utf-8")
        assert paired["token"] not in persisted

        query = {"project": "p1", "plan": plan.id}
        with pytest.raises(HTTPError) as missing_device_token:
            _request(server, "/next", query=query)
        assert missing_device_token.value.code == 401
        with pytest.raises(HTTPError) as query_token:
            _request(
                server,
                "/next",
                query={**query, "token": paired["token"]},
            )
        assert query_token.value.code == 401
        with pytest.raises(HTTPError) as deployment_is_not_device:
            _request(server, "/next", query=query, token=DEPLOYMENT_TOKEN)
        assert deployment_is_not_device.value.code == 401
        with _request(server, "/next", query=query, token=paired["token"]) as next_response:
            assert next_response.status == 204
        with pytest.raises(HTTPError) as replayed_pair:
            _request(
                server,
                "/pair",
                token=DEPLOYMENT_TOKEN,
                method="POST",
                payload={"project": "p1", "code": pairing.code},
            )
        assert replayed_pair.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_relay_rejects_cross_project_tokens_nonfinite_wait_and_symlinks(tmp_path):
    workspace = tmp_path / "ws"
    root, plan, _, _ = _coordinator(workspace)
    server, thread = _serve(workspace)
    try:
        paired = _pair(server, root)
        with pytest.raises(HTTPError) as cross_project:
            _request(
                server,
                "/next",
                query={"project": "p2", "plan": plan.id},
                token=paired["token"],
            )
        assert cross_project.value.code == 401
        with pytest.raises(HTTPError) as invalid_wait:
            _request(
                server,
                "/next",
                query={"project": "p1", "plan": plan.id, "wait": "nan"},
                token=paired["token"],
            )
        assert invalid_wait.value.code == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    external_root, external_plan, _, _ = _coordinator(tmp_path / "outside")
    symlink_workspace = tmp_path / "linked"
    symlink_workspace.mkdir()
    try:
        os.symlink(external_root, symlink_workspace / "p1", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")
    linked_server, linked_thread = _serve(symlink_workspace)
    try:
        with pytest.raises(HTTPError) as escaped:
            _request(
                linked_server,
                "/next",
                query={"project": "p1", "plan": external_plan.id},
                token="not-a-device-token",
            )
        assert escaped.value.code == 401
    finally:
        linked_server.shutdown()
        linked_server.server_close()
        linked_thread.join()


def test_draining_device_cannot_lease_but_can_finish_its_active_command(tmp_path):
    workspace = tmp_path / "ws"
    root, plan, coordinator, _ = _coordinator(workspace)
    auth = AEProjectAuth(root, "p1")
    pairing = auth.create_pairing(None, {})
    server, thread = _serve(workspace)
    try:
        with _request(
            server,
            "/pair",
            token=DEPLOYMENT_TOKEN,
            method="POST",
            payload={"project": "p1", "code": pairing.code},
        ) as response:
            paired = json.loads(response.read())
        query = {"project": "p1", "plan": plan.id}
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            assert response.status == 204
        command = coordinator.enqueue_command(
            "heartbeat",
            {"probe": True},
            expected_state="baseline",
            expected_checkpoint=None,
            lease_seconds=60,
        )
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            leased = json.loads(response.read())["command"]
        auth.create_pairing(pairing.controller_token, {})

        with pytest.raises(HTTPError) as no_new_lease:
            _request(server, "/next", query=query, token=paired["token"])
        assert no_new_lease.value.code == 401
        with _request(
            server,
            f"/results/{command.id}",
            query=query,
            token=paired["token"],
            method="POST",
            payload={"sequence": leased["sequence"], "result": {"ok": True}},
        ) as response:
            assert response.status == 200
        assert coordinator.state().applied_command_sequence == leased["sequence"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_expired_replacement_restores_old_connector_rebind(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    root, plan, coordinator, _ = _coordinator(workspace)
    auth = AEProjectAuth(root, "p1")
    pairing = auth.create_pairing(None, {})
    device = auth.redeem_pairing(pairing.code)
    server, thread = _serve(workspace)
    query = {"project": "p1", "plan": plan.id}
    try:
        with _request(server, "/next", query=query, token=device.token) as response:
            assert response.status == 204
        command = coordinator.enqueue_command(
            "heartbeat",
            expected_state="baseline",
            expected_checkpoint=None,
            lease_seconds=60,
        )
        with _request(server, "/next", query=query, token=device.token) as response:
            leased = json.loads(response.read())["command"]
        replacement = auth.create_pairing(pairing.controller_token, {})
        draining = coordinator.detach_device(
            device.identity.device_id,
            reason="replacement",
        )
        assert draining.status == "pause_requested"

        restored = auth.authenticate_device(
            device.token,
            now=replacement.expires_at + 1,
        )
        assert restored is not None and restored.status == "active"
        coordinator.accept_result(
            device.identity.device_id,
            command.id,
            sequence=leased["sequence"],
            result={"ok": True},
        )
        paused = coordinator.state()
        assert paused.status == "paused:replacement"
        waiting = coordinator.transition("continue", revision=paused.revision)
        assert waiting.device_id == device.identity.device_id

        monkeypatch.setattr(
            "keepframe.after_effects.auth.time.time",
            lambda: replacement.expires_at + 1,
        )
        with _request(server, "/next", query=query, token=device.token) as response:
            assert response.status == 204
        rebound = coordinator.state()
        assert rebound.status == "baseline"
        assert rebound.device_id == device.identity.device_id
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_relay_uploads_only_artifacts_referenced_by_live_command(tmp_path):
    workspace = tmp_path / "ws"
    root, plan, coordinator, _ = _coordinator(workspace)
    server, thread = _serve(workspace)
    try:
        paired = _pair(server, root)
        query = {"project": "p1", "plan": plan.id}
        with _request(server, "/next", query=query, token=paired["token"]):
            pass
        payload = _artifact_payload("png")
        allowed = coordinator.reserve_artifact("png", len(payload))
        unrelated = coordinator.reserve_artifact("png", len(payload))
        coordinator.enqueue_command(
            "heartbeat",
            {"artifact_id": allowed.id},
            expected_state="baseline",
            expected_checkpoint=None,
            lease_seconds=60,
        )
        with _request(server, "/next", query=query, token=paired["token"]):
            pass

        def upload(reservation_id):
            suffix = urlencode(query)
            return urlopen(
                Request(
                    (
                        f"http://127.0.0.1:{server.server_address[1]}"
                        f"/artifacts/{reservation_id}?{suffix}"
                    ),
                    data=payload,
                    headers={
                        "Authorization": f"Bearer {paired['token']}",
                        "Content-Type": "application/octet-stream",
                    },
                    method="PUT",
                )
            )

        with pytest.raises(HTTPError) as not_referenced:
            upload(unrelated.id)
        assert not_referenced.value.code == 409
        with upload(allowed.id) as response:
            assert response.status == 201
            assert json.loads(response.read())["artifact"]["id"] == allowed.id
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
