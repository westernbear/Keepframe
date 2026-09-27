import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from keepframe.after_effects.auth import AEAuthError, AEProjectAuth
from tests.test_ae_coordinator import _coordinator


def test_pairing_secrets_are_hashed_bound_and_consumed_once(tmp_path):
    root = tmp_path / "p1"
    root.mkdir()
    auth = AEProjectAuth(root, "p1")

    pairing = auth.create_pairing(None, {"required": ["ae_version", "fonts"]}, now=100.0)
    persisted = (root / "ae-auth.json").read_text(encoding="utf-8")
    assert pairing.code not in persisted
    assert pairing.controller_token not in persisted
    assert auth.authenticate_controller(pairing.controller_token)

    def redeem():
        try:
            return auth.redeem_pairing(pairing.code, now=101.0)
        except AEAuthError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _index: redeem(), range(2)))
    grants = [attempt for attempt in attempts if attempt is not None]
    assert len(grants) == 1
    device = grants[0]
    persisted = (root / "ae-auth.json").read_text(encoding="utf-8")
    assert device.token not in persisted
    assert device.capability_request == {"required": ["ae_version", "fonts"]}
    assert auth.authenticate_device(device.token) == device.identity
    with pytest.raises(AEAuthError, match="invalid or expired"):
        auth.redeem_pairing(pairing.code, now=102.0)


def test_existing_controller_cannot_be_replaced_without_its_token(tmp_path):
    root = tmp_path / "p1"
    root.mkdir()
    auth = AEProjectAuth(root, "p1")
    pairing = auth.create_pairing(None, {})
    device = auth.redeem_pairing(pairing.code)
    before = (root / "ae-auth.json").read_bytes()

    for token in (None, "wrong-controller-token"):
        with pytest.raises(AEAuthError, match="controller authorization"):
            auth.create_pairing(token, {})
        assert (root / "ae-auth.json").read_bytes() == before
        assert auth.authenticate_controller(pairing.controller_token)
        assert auth.authenticate_device(device.token) == device.identity


def test_pairing_expiry_replacement_and_unpair_preserve_safe_cutover(tmp_path):
    root = tmp_path / "p1"
    root.mkdir()
    auth = AEProjectAuth(root, "p1")

    expired = auth.create_pairing(None, {}, now=100.0)
    with pytest.raises(AEAuthError, match="invalid or expired"):
        auth.redeem_pairing(expired.code, now=701.0)

    first = auth.create_pairing(expired.controller_token, {}, now=800.0)
    old_device = auth.redeem_pairing(first.code, now=801.0)
    replacement = auth.create_pairing(first.controller_token, {"required": ["plugins"]}, now=802.0)
    draining = auth.authenticate_device(old_device.token, now=803.0)
    assert draining is not None and draining.status == "draining"
    assert replacement.controller_token == first.controller_token

    new_device = auth.redeem_pairing(replacement.code, now=804.0)
    assert not auth.authenticate_device(old_device.token, now=805.0)
    assert auth.authenticate_device(new_device.token, now=805.0) == new_device.identity

    abandoned = auth.create_pairing(first.controller_token, {}, now=900.0)
    assert auth.authenticate_device(new_device.token, now=901.0).status == "draining"
    restored = auth.authenticate_device(new_device.token, now=abandoned.expires_at + 1)
    assert restored is not None and restored.status == "active"

    revoking_device = auth.begin_unpair(first.controller_token)
    assert revoking_device == new_device.identity.device_id
    assert auth.authenticate_device(new_device.token, allow_draining=True).status == "draining"
    assert auth.finish_unpair(first.controller_token, revoking_device) == revoking_device
    assert not auth.authenticate_controller(first.controller_token)
    assert not auth.authenticate_device(new_device.token)
    assert json.loads((root / "ae-auth.json").read_text(encoding="utf-8"))["pending"] is None


@pytest.mark.parametrize("reason", ["replacement", "unpair"])
def test_device_detach_waits_for_lease_and_allows_authenticated_rebind(tmp_path, reason):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    command = coordinator.enqueue_command(
        "heartbeat",
        expected_state="baseline",
        device_id="device-1",
        expected_checkpoint=None,
        revision=session.revision,
        lease_seconds=30,
    )
    leased = coordinator.next_command("device-1", now=100.0)
    assert leased is not None and leased.id == command.id

    draining = coordinator.detach_device("device-1", reason=reason, now=101.0)
    assert draining.status == "pause_requested"
    assert draining.device_id == "device-1"
    coordinator.accept_result(
        "device-1",
        leased.id,
        sequence=leased.sequence,
        result={"ok": True},
    )
    detached = coordinator.detach_device("device-1", reason=reason, now=102.0)
    assert detached.status == f"paused:{reason}"
    assert detached.device_id is None
    waiting = coordinator.transition("continue", revision=detached.revision)
    assert waiting.status == "waiting_for_connector"
    assert waiting.device_id is None
    rebound = coordinator.transition(
        "device_ready",
        revision=waiting.revision,
        device_id="device-2",
    )
    assert rebound.device_id == "device-2"
