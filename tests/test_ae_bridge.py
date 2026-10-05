import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from keepframe.after_effects.bridge import (
    BRIDGE_SCHEMA_VERSION,
    Bridge,
    BridgeBusy,
    BridgeConflict,
    BridgeResult,
    BridgeUnavailable,
    FIXED_KINDS,
)


def test_bridge_rejects_reparse_components_without_following_links(tmp_path, monkeypatch):
    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir()
    bridge_root = unsafe_parent / "bridge"
    real_stat = Path.stat

    def guarded_stat(path, *args, **kwargs):
        assert kwargs.get("follow_symlinks") is False
        result = real_stat(path, *args, **kwargs)
        if path == unsafe_parent:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_size=result.st_size,
                st_file_attributes=0x0400,
            )
        return result

    monkeypatch.setattr(Path, "stat", guarded_stat)
    with pytest.raises(BridgeUnavailable):
        Bridge(bridge_root)


def test_bridge_writes_bounded_nonce_digest_records_atomically_and_replays(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    command = bridge.write_command(
        "capability_heartbeat",
        {"request": "status"},
        command_id="command-1",
        nonce="nonce-1",
    )
    assert command.schema_version == BRIDGE_SCHEMA_VERSION
    assert command.payload_digest == bridge.payload_digest({"request": "status"})
    assert bridge.read_command() == command
    with pytest.raises(BridgeBusy):
        bridge.write_command("save_checkpoint", {}, command_id="command-2", nonce="nonce-2")

    result = bridge.write_result(
        command.command_id,
        command.nonce,
        {"ae_version": "24.0", "ok": True},
    )
    assert result.command_id == command.command_id
    assert result.nonce == command.nonce
    assert result.kind == command.kind
    assert result.payload_digest == command.payload_digest
    assert bridge.read_result(command.command_id, command.nonce) == result
    assert bridge.replay(command.command_id, command.nonce) == result
    completed = list((tmp_path / "bridge" / "completed").glob("*.json"))
    assert len(completed) == 1
    assert not (tmp_path / "bridge" / "completed.json").exists()
    replay = bridge.write_command(
        "capability_heartbeat",
        {"request": "status"},
        command_id=command.command_id,
        nonce=command.nonce,
    )
    assert isinstance(replay, BridgeResult)
    assert replay == result
    with pytest.raises(BridgeConflict):
        bridge.write_command(
            "capability_heartbeat",
            {"request": "changed"},
            command_id=command.command_id,
            nonce=command.nonce,
        )



def test_bridge_replay_rejects_conflicting_errors(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    command = bridge.write_command("render_preview", {}, command_id="command-1", nonce="nonce-1")
    bridge.write_result(command.command_id, command.nonce, {}, ok=False, error="first")
    with pytest.raises(BridgeConflict):
        bridge.write_result(command.command_id, command.nonce, {}, ok=False, error="changed")


def test_bridge_rejects_nonce_digest_size_and_result_mismatches(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    command = bridge.write_command("render_preview", {"frame": 0}, command_id="command-1", nonce="nonce-1")
    with pytest.raises(BridgeConflict):
        bridge.write_result(command.command_id, "wrong", {"ok": True})
    with pytest.raises(BridgeConflict):
        bridge.write_result(command.command_id, command.nonce, {"__digest__": "bad"}, result_digest="0" * 64)
    with pytest.raises(BridgeConflict):
        bridge.read_result(command.command_id, "wrong")
    with pytest.raises(BridgeConflict):
        bridge.write_command("render_preview", {"blob": "x" * (1024 * 1024)}, command_id="command-2", nonce="nonce-2")


def test_bridge_is_serialized_and_stop_is_cooperative(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    barrier = threading.Barrier(2)
    outcomes = []

    def submit(index):
        barrier.wait()
        try:
            outcomes.append(bridge.write_command("save_checkpoint", {"index": index}, command_id=f"command-{index}", nonce=f"nonce-{index}"))
        except BridgeBusy:
            outcomes.append("busy")

    threads = [threading.Thread(target=submit, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(item == "busy" for item in outcomes) == 1

    bridge.request_stop("user")
    stop = bridge.read_stop()
    assert stop["requested"] is True
    assert stop["reason"] == "user"
    bridge.clear_stop()
    assert bridge.read_stop() is None


def test_fixed_kinds_are_exact_and_result_json_is_bounded(tmp_path):
    assert FIXED_KINDS == {
        "capability_heartbeat",
        "create_or_open_project",
        "import_server_asset",
        "apply_operation_batch",
        "inspect_mapped_layers",
        "save_checkpoint",
        "render_preview",
        "render_final",
        "package_project",
    }
    result = BridgeResult(command_id="command-1", nonce="nonce-1", result={"ok": True})
    parsed = json.loads(result.model_dump_json())
    assert parsed["schema_version"] == BRIDGE_SCHEMA_VERSION


def test_mcp_registry_contains_only_fixed_forwarders_without_importing_mcp():
    import sys

    from keepframe.after_effects import mcp_server

    assert "mcp" not in sys.modules
    calls = []

    class FakeBridge:
        def dispatch(self, kind, payload, *, command_id, nonce, timeout=None):
            calls.append((kind, payload, command_id, nonce, timeout))
            return {"kind": kind, "payload": payload, "command_id": command_id, "nonce": nonce}

    registry = mcp_server.fixed_tool_registry(FakeBridge())
    assert tuple(registry) == (
        "capability_heartbeat",
        "create_or_open_project",
        "import_server_asset",
        "apply_operation_batch",
        "inspect_mapped_layers",
        "save_checkpoint",
        "render_preview",
        "render_final",
        "package_project",
    )
    assert registry["render_preview"](
        {"command_id": "command-1", "nonce": "nonce-1", "payload": {"frame": 0}}
    )["kind"] == "render_preview"
    assert calls == [("render_preview", {"frame": 0}, "command-1", "nonce-1", 1800.0)]


def test_mcp_apply_tool_validates_and_forwards_canonical_envelope():
    from keepframe.after_effects.operations import ApprovedCapabilities
    from keepframe.after_effects import mcp_server

    capability_digest = "a" * 64
    capabilities = ApprovedCapabilities(
        digest=capability_digest,
        properties={"ADBE Opacity": "number"},
    )
    calls = []

    class FakeBridge:
        def dispatch(self, kind, payload, *, command_id, nonce, timeout=None):
            calls.append((kind, payload, command_id, nonce))
            return BridgeResult(
                command_id=command_id,
                nonce=nonce,
                result={"applied": True},
            )

    envelope = {
        "command_id": "command-9",
        "nonce": "nonce-9",
        "payload": {
            "batch": {
                "capability_digest": capability_digest,
                "operations": [
                    {
                        "kind": "set_opacity",
                        "layer_instance_id": "layer-1",
                        "opacity": 0.5,
                    }
                ],
            },
            "approved_capabilities": capabilities.model_dump(mode="json"),
            "scene_frame_count": 10,
            "duration": 5.0,
            "layer_count": 1,
            "baseline": False,
            "locked_source_ids": [],
            "layer_native_ids": {"layer-1": 1},
            "layer_sources": {"layer-1": None},
            "project_id": "project-1",
            "plan_id": "plan-1",
            "session_id": "session-1",
        },
    }
    assert mcp_server.fixed_tool_registry(FakeBridge())["apply_operation_batch"](envelope) == {"applied": True}
    kind, payload, command_id, nonce = calls[0]
    assert kind == "apply_operation_batch"
    assert command_id == "command-9"
    assert nonce == "nonce-9"
    assert payload["batch"]["operations"][0]["kind"] == "set_opacity"
    assert payload["scene_frame_count"] == 10
    assert payload["duration"] == 5.0
    assert payload["layer_count"] == 1
    assert payload["project_id"] == "project-1"
    assert payload["plan_id"] == "plan-1"
    assert payload["session_id"] == "session-1"


def test_mcp_apply_rejects_zero_authoritative_duration():
    from keepframe.after_effects import mcp_server
    from keepframe.after_effects.operations import ApprovedCapabilities

    capability_digest = "d" * 64
    capabilities = ApprovedCapabilities(
        digest=capability_digest,
        properties={"ADBE Opacity": "number"},
    )
    envelope = {
        "command_id": "command-zero-duration",
        "nonce": "nonce-zero-duration",
        "payload": {
            "batch": {
                "capability_digest": capability_digest,
                "operations": [
                    {
                        "kind": "set_opacity",
                        "layer_instance_id": "layer-1",
                        "opacity": 0.5,
                    }
                ],
            },
            "approved_capabilities": capabilities.model_dump(mode="json"),
            "scene_frame_count": 10,
            "duration": 0,
            "layer_count": 1,
            "baseline": False,
            "locked_source_ids": [],
            "layer_native_ids": {"layer-1": 1},
            "layer_sources": {"layer-1": None},
            "project_id": "project-1",
            "plan_id": "plan-1",
            "session_id": "session-1",
        },
    }
    class FakeBridge:
        def dispatch(self, kind, payload, *, command_id, nonce):
            return {"kind": kind, "command_id": command_id, "nonce": nonce}

    with pytest.raises(ValueError, match="duration"):
        mcp_server.fixed_tool_registry(FakeBridge())["apply_operation_batch"](envelope)



def test_mcp_rejects_fill_color_without_fill_effect_capability():
    from keepframe.after_effects import mcp_server
    from keepframe.after_effects.operations import ApprovedCapabilities

    capability_digest = "b" * 64
    capabilities = ApprovedCapabilities(
        digest=capability_digest,
        properties={"ADBE Fill Color": "color"},
    )
    envelope = {
        "command_id": "command-fill",
        "nonce": "nonce-fill",
        "payload": {
            "batch": {
                "capability_digest": capability_digest,
                "operations": [
                    {
                        "kind": "set_color",
                        "layer_instance_id": "layer-1",
                        "color": [1.0, 0.0, 0.0],
                    }
                ],
            },
            "approved_capabilities": capabilities.model_dump(mode="json"),
            "scene_frame_count": 10,
            "duration": 5.0,
            "layer_count": 1,
            "baseline": False,
            "locked_source_ids": [],
            "layer_native_ids": {"layer-1": 1},
            "layer_sources": {"layer-1": None},
            "project_id": "project-1",
            "plan_id": "plan-1",
            "session_id": "session-1",
        },
    }
    with pytest.raises(ValueError, match="requires effect"):
        mcp_server.fixed_tool_registry(object())["apply_operation_batch"](envelope)


def test_mcp_apply_requires_bounds_and_scope_fields():
    from keepframe.after_effects import mcp_server
    from keepframe.after_effects.operations import ApprovedCapabilities

    capability_digest = "c" * 64
    capabilities = ApprovedCapabilities(
        digest=capability_digest,
        properties={"ADBE Opacity": "number"},
    )
    payload = {
        "batch": {
            "capability_digest": capability_digest,
            "operations": [
                {
                    "kind": "set_opacity",
                    "layer_instance_id": "layer-1",
                    "opacity": 0.5,
                }
            ],
        },
        "approved_capabilities": capabilities.model_dump(mode="json"),
        "scene_frame_count": 10,
        "duration": 5.0,
        "layer_count": 1,
        "baseline": False,
        "locked_source_ids": [],
        "layer_native_ids": {"layer-1": 1},
        "layer_sources": {"layer-1": None},
        "project_id": "project-1",
        "plan_id": "plan-1",
        "session_id": "session-1",
    }
    registry = mcp_server.fixed_tool_registry(object())
    for field in payload:
        missing = dict(payload)
        missing.pop(field)
        envelope = {
            "command_id": "command-missing-" + field,
            "nonce": "nonce-missing-" + field,
            "payload": missing,
        }
        with pytest.raises(ValueError, match="requires"):
            registry["apply_operation_batch"](envelope)

    for field, value in {
        "project_id": "../project",
        "plan_id": "",
        "session_id": "x" * 257,
    }.items():
        invalid = dict(payload)
        invalid[field] = value
        envelope = {
            "command_id": "command-invalid-" + field,
            "nonce": "nonce-invalid-" + field,
            "payload": invalid,
        }
        with pytest.raises(ValueError, match="safe identifier"):
            registry["apply_operation_batch"](envelope)


def test_abandoned_heartbeat_does_not_block_the_bridge_forever(tmp_path):
    import os
    import time

    bridge = Bridge(tmp_path / "bridge")
    bridge.write_command("capability_heartbeat", {}, command_id="old", nonce="n-old")
    with pytest.raises(BridgeBusy):  # a fresh heartbeat may still be answered
        bridge.write_command("capability_heartbeat", {}, command_id="new", nonce="n-new")
    stale = time.time() - 120
    os.utime(bridge.root / bridge.command_filename, (stale, stale))
    assert bridge.write_command("capability_heartbeat", {}, command_id="new", nonce="n-new").command_id == "new"

    os.utime(bridge.root / bridge.command_filename, (stale, stale))
    bridge.write_result("new", "n-new", {"ok": True})
    bridge.acknowledge("new", "n-new")
    bridge.write_command("save_checkpoint", {}, command_id="work", nonce="n-work")
    os.utime(bridge.root / bridge.command_filename, (stale, stale))
    with pytest.raises(BridgeBusy):  # only read-only heartbeats are ever dropped
        bridge.write_command("capability_heartbeat", {}, command_id="hb", nonce="n-hb")
