import json
import os
import socket
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from keepframe.after_effects.auth import AEProjectAuth
from keepframe.after_effects.models import AECapabilities
from keepframe.after_effects.relay import _MAX_RESULT_BODY, make_relay_server
from tests.test_ae_coordinator import (
    _AE_CAPABILITY_MANIFEST,
    _artifact_payload,
    _coordinator,
)


def _capability_snapshot() -> dict[str, object]:
    return {**_AE_CAPABILITY_MANIFEST, "project_open": True}


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


def _pair(server, root, *, project="p1", publish_capabilities=True):
    pairing = AEProjectAuth(root, project).create_pairing(
        None,
        {"required": ["ae_version"]},
    )
    with _request(
        server,
        "/pair",
        token=DEPLOYMENT_TOKEN,
        method="POST",
        payload={"project": project, "code": pairing.code},
    ) as response:
        grant = json.loads(response.read())
    if publish_capabilities:
        AEProjectAuth(root, project).publish_capabilities(
            grant["device"],
            AECapabilities.model_validate(_capability_snapshot()),
        )
    return grant


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




def test_relay_rejects_incomplete_json_body(tmp_path):
    workspace = tmp_path / "ws"
    _coordinator(workspace)
    server, thread = _serve(workspace)
    try:
        with socket.create_connection(server.server_address, timeout=2) as client:
            client.sendall(
                b"POST /pair HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Authorization: Bearer {DEPLOYMENT_TOKEN}\r\n".encode()
                + b"Content-Type: application/json\r\n"
                b"Content-Length: 10\r\n"
                b"\r\n"
                b"{}"
            )
            client.shutdown(socket.SHUT_WR)
            response = client.recv(4096)
        assert b" 400 " in response
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_relay_allows_large_results_but_bounds_result_body(tmp_path):
    workspace = tmp_path / "ws"
    root, plan, coordinator, _ = _coordinator(workspace)
    server, thread = _serve(workspace)
    query = {"project": "p1", "plan": plan.id}
    try:
        paired = _pair(server, root)
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            assert response.status == 204

        command = coordinator.enqueue_command(
            "heartbeat",
            expected_state="baseline",
            expected_checkpoint=None,
            lease_seconds=60,
        )
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            leased = json.loads(response.read())["command"]
        large_result = {"payload": "x" * (64 * 1024 + 1)}
        with _request(
            server,
            f"/results/{command.id}",
            query=query,
            token=paired["token"],
            method="POST",
            payload={"sequence": leased["sequence"], "result": large_result},
        ) as response:
            assert response.status == 200
        assert coordinator.state().applied_command_sequence == leased["sequence"]

        oversized = coordinator.enqueue_command(
            "heartbeat",
            expected_state="baseline",
            expected_checkpoint=None,
            lease_seconds=60,
        )
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            oversized_leased = json.loads(response.read())["command"]
        too_large_result = {"payload": "x" * _MAX_RESULT_BODY}
        with pytest.raises(HTTPError) as too_large:
            _request(
                server,
                f"/results/{oversized.id}",
                query=query,
                token=paired["token"],
                method="POST",
                payload={
                    "sequence": oversized_leased["sequence"],
                    "result": too_large_result,
                },
            )
        assert too_large.value.code == 413
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_next_discovers_the_project_plan_for_the_connector(tmp_path):
    workspace = tmp_path / "ws"
    root, _, coordinator, _ = _coordinator(workspace)
    server, thread = _serve(workspace)
    try:
        paired = _pair(server, root)
        query = {"project": "p1"}
        with _request(
            server,
            "/next",
            query=query,
            token=paired["token"],
        ) as response:
            assert response.status == 204
        session = coordinator.state()
        command = coordinator.enqueue_command(
            "heartbeat",
            {},
            expected_state=session.status,
            expected_checkpoint=None,
        )
        with _request(
            server,
            "/next",
            query=query,
            token=paired["token"],
        ) as response:
            assert response.status == 200
            assert json.loads(response.read())["command"]["id"] == command.id
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_next_pauses_without_binding_when_capabilities_are_missing(tmp_path):
    workspace = tmp_path / "ws"
    root, _, coordinator, _ = _coordinator(workspace)
    server, thread = _serve(workspace)
    try:
        paired = _pair(server, root, publish_capabilities=False)
        query = {"project": "p1", "plan": coordinator.plan.id}
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            assert response.status == 204
        paused = coordinator.state()
        assert paused.status == "paused:capabilities_changed"
        assert paused.device_id is None
        revision = paused.revision
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            assert response.status == 204
        assert coordinator.state().revision == revision
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_next_does_not_lease_a_command_after_capability_change(tmp_path):
    workspace = tmp_path / "ws"
    root, plan, coordinator, _ = _coordinator(workspace)
    auth = AEProjectAuth(root, "p1")
    server, thread = _serve(workspace)
    try:
        paired = _pair(server, root)
        query = {"project": "p1", "plan": plan.id}
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            assert response.status == 204
        command = coordinator.enqueue_command(
            "heartbeat",
            expected_state="baseline",
            expected_checkpoint=None,
        )
        changed = _capability_snapshot()
        changed["version"] = "24.2.0"
        auth.publish_capabilities(
            paired["device"],
            AECapabilities.model_validate(changed),
        )
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            assert response.status == 204
        paused = coordinator.state()
        assert paused.status == "paused:capabilities_changed"
        commands = json.loads(
            (root / "renders" / plan.id / "ae" / "commands.json").read_text(
                encoding="utf-8"
            )
        )
        assert commands[0]["id"] == command.id
        assert commands[0]["status"] == "queued"
        revision = paused.revision
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            assert response.status == 204
        assert coordinator.state().revision == revision
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_next_leases_a_command_when_capabilities_match(tmp_path):
    workspace = tmp_path / "ws"
    root, plan, coordinator, _ = _coordinator(workspace)
    server, thread = _serve(workspace)
    try:
        paired = _pair(server, root)
        query = {"project": "p1", "plan": plan.id}
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            assert response.status == 204
        command = coordinator.enqueue_command(
            "heartbeat",
            expected_state="baseline",
            expected_checkpoint=None,
        )
        with _request(server, "/next", query=query, token=paired["token"]) as response:
            assert response.status == 200
            assert json.loads(response.read())["command"]["id"] == command.id
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
        auth.publish_capabilities(
            paired["device"],
            AECapabilities.model_validate(_capability_snapshot()),
        )
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
    auth.publish_capabilities(
        device.identity.device_id,
        AECapabilities.model_validate(_capability_snapshot()),
    )
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

        def upload(reservation_id, body=payload):
            suffix = urlencode(query)
            return urlopen(
                Request(
                    (
                        f"http://127.0.0.1:{server.server_address[1]}"
                        f"/artifacts/{reservation_id}?{suffix}"
                    ),
                    data=body,
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
            committed = json.loads(response.read())["artifact"]
        with upload(allowed.id) as response:
            assert response.status == 201
            assert json.loads(response.read())["artifact"] == committed
        with pytest.raises(HTTPError) as different_length:
            upload(allowed.id, payload[:-1])
        assert different_length.value.code == 409

    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_relay_publishes_validated_capabilities_for_current_device(tmp_path):
    workspace = tmp_path / "ws"
    root, _, _, _ = _coordinator(workspace)
    server, thread = _serve(workspace)
    try:
        paired = _pair(server, root)
        snapshot = _capability_snapshot()
        expected = AECapabilities.model_validate(snapshot)
        with _request(
            server,
            "/capabilities",
            query={"project": "p1"},
            token=paired["token"],
            method="POST",
            payload=snapshot,
        ) as response:
            published = json.loads(response.read())
        assert published == {"capability_hash": expected.capability_hash}

        reloaded = AEProjectAuth(root, "p1").read_capabilities()
        assert reloaded is not None
        assert reloaded.capability_hash == expected.capability_hash

        with pytest.raises(HTTPError) as wrong_device:
            _request(
                server,
                "/capabilities",
                query={"project": "p1"},
                token="wrong-device-token",
                method="POST",
                payload=snapshot,
            )
        assert wrong_device.value.code == 401

        with pytest.raises(HTTPError) as malformed:
            _request(
                server,
                "/capabilities",
                query={"project": "p1"},
                token=paired["token"],
                method="POST",
                payload={**snapshot, "unexpected": True},
            )
        assert malformed.value.code == 400

        with pytest.raises(HTTPError) as oversized:
            _request(
                server,
                "/capabilities",
                query={"project": "p1"},
                token=paired["token"],
                method="POST",
                payload={"x": "x" * (1024 * 1024)},
            )
        assert oversized.value.code == 413
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
