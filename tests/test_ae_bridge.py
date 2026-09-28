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


def test_panel_capabilities_use_runtime_catalogs_and_fixed_metadata():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    assert "allFonts" in panel
    assert "app.effects" in panel
    assert "font_names" in panel
    assert "effect_names" in panel
    assert "property_schemas" in panel
    assert "plugin_versions" in panel
    assert "ADBE Gaussian Blur 2-0001" in panel
    assert "ADBE Fill-0002" in panel
    assert "effect.property(1)" in panel
    assert "fill.property(2)" in panel
    for installed_font in ('"Arial"', '"Helvetica"', '"Verdana"'):
        assert installed_font not in panel
    assert 'property("Blurriness")' not in panel
    assert 'property("Color")' not in panel
    fixed_schema_source = panel.split("function fixedPropertySchemas()", 1)[1].split(
        "function fontMetadata", 1
    )[0]
    assert '"ADBE Fill Color": "color"' not in fixed_schema_source
    assert 'catalogName(effects, "effect_names", "ADBE Fill")' in panel
    assert "operation.keyframes.length > keyframeLimit" in panel
    assert "operation.keyframes.length > 128" not in panel
    assert "function validateAuthoritativeBounds" in panel
    assert "var MAX_KEYFRAMES = 1000000;" in panel
    assert "property requires an approved effect" in panel
    assert "propertyTime(frame, time)" in panel
    assert "setTemporalEaseAtKey" in panel
    assert "Number(operation.value) * 100" in panel
    assert "Number(keyframe.value) * 100" in panel
    assert "temporal easing is unavailable" in panel
    assert "source_element_id: sourceId || null" in panel
    assert "keepframe:layer=" in panel and ";source_element_id=" in panel
    assert "--([A-Za-z0-9]" in panel
    assert "assertEffectPropertyOperation" in panel
    assert "effect properties require set_effect" in panel
    assert "SESSION_COMP_MARKER" in panel
    assert "refusing to reuse an unrelated After Effects project" in panel
    assert "requireSessionComposition()" in panel
    assert r"/^([A-Za-z0-9][A-Za-z0-9_.-]{0,255})--([A-Za-z0-9][A-Za-z0-9_.-]{0,255})\.json$/" in panel
    assert "completed bridge record is invalid" in panel
    assert 'var INFLIGHT_DIR = Folder(BRIDGE_ROOT.fsName + "/inflight");' in panel
    assert "function inflightAttempt(command)" in panel
    assert "command outcome is indeterminate after panel interruption" in panel
    poll_source = panel.split("function KeepframeBridge_poll()", 1)[1]
    assert poll_source.index("completedResult(command)") < poll_source.index("inflightAttempt(command)")
    assert poll_source.index("markAttempt(command)") < poll_source.index("dispatchCommand(command)")
    assert poll_source.index("markCompleted(response)") < poll_source.index("writeAtomic(RESULT_FILE, response)")
    assert "Leave the command available for a later retry" not in panel

def test_panel_binds_scope_before_dispatch_and_rejects_stale_comp_bounds():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    marker_source = panel.split("function sessionMarker", 1)[1].split(
        "function hasSessionMarker", 1
    )[0]
    assert "typeof projectId !== \"string\"" in marker_source
    assert "typeof planId !== \"string\"" in marker_source
    assert "typeof sessionId !== \"string\"" in marker_source
    assert "SESSION_COMP_MARKER_PREFIX + JSON.stringify([projectId, planId, sessionId])" in marker_source
    assert "sessionMarker(payload.project_id, payload.plan_id, payload.session_id)" in panel
    create_source = panel.split("function createOrOpenProject", 1)[1].split(
        "function importServerAsset", 1
    )[0]
    assert "comp.comment = marker" in create_source
    assert "hasSessionMarker(comp, marker)" in create_source
    assert "heartbeat(false)" in create_source
    inspect_source = panel.split("function inspectLayers", 1)[1].split(
        "function saveCheckpoint", 1
    )[0]
    assert "heartbeat(false)" in inspect_source
    assert "capability_hash: liveHeartbeat.capability_hash" in inspect_source
    assert "capabilities: liveHeartbeat.capabilities" in inspect_source
    assert "ready: liveHeartbeat.ready" in inspect_source
    scope_source = panel.split("function validateCommandScope", 1)[1].split(
        "function requireSessionComposition", 1
    )[0]
    assert "requireSessionComposition()" in scope_source
    assert "hasAnyScope = hasProject || hasPlan || hasSession" in scope_source
    assert "kind === \"capability_heartbeat\" && !hasAnyScope" in scope_source
    assert "Keepframe command scope is incomplete" in scope_source

    dispatch_source = panel.split("function dispatchCommand(command)", 1)[1].split(
        "function KeepframeBridge_poll", 1
    )[0]
    assert "validateCommandScope(command.kind, command.payload || {})" in dispatch_source
    assert dispatch_source.index("validateCommandScope") < dispatch_source.index("switch (command.kind)")
    assert "var includeLocalPaths = validateCommandScope(command.kind, command.payload || {});" in dispatch_source
    assert "return heartbeat(includeLocalPaths);" in dispatch_source
    assert "heartbeat(true)" not in panel
    poll_source = panel.split("function KeepframeBridge_poll", 1)[1]
    assert poll_source.index("validateCommandScope") < poll_source.index("markAttempt")
    assert "removeStop();" not in poll_source
    batch_source = panel.split("function applyBatch(payload)", 1)[1].split(
        "function createOrOpenProject", 1
    )[0]
    assert "payload.scene_frame_count" in batch_source
    assert "payload.duration" in batch_source
    assert "payload.layer_count" in batch_source
    bounds_source = panel.split("function validateAuthoritativeBounds", 1)[1].split(
        "function validateTiming", 1
    )[0]
    assert "comp.duration * comp.frameRate" in bounds_source
    assert "comp.numLayers !== layerCount" in bounds_source
    assert "Math.floor(sceneFrameCount) !== sceneFrameCount" in bounds_source
    assert "duration <= 0" in bounds_source
    assert "Math.floor(layerCount) !== layerCount" in bounds_source
    assert "batch.scene_frame_count" not in panel
    assert "batch.frame_count" not in panel
    assert "batch.duration" not in panel
    assert batch_source.index("validateAuthoritativeBounds") < batch_source.index("app.beginUndoGroup")
    assert "removeStop();" in batch_source
    assert batch_source.index("stopped = STOP_FILE.exists") < batch_source.index("removeStop();")

    font_source = panel.split("function fontLocalPath", 1)[1].split(
        "function enumerateFonts", 1
    )[0]
    assert "font.location" in font_source
    assert "font.file" in font_source
    assert "includeLocalPath === true" in font_source
    assert "fontLocalPath(font)" in font_source
    assert "sha256" not in font_source.lower()


def test_panel_matches_ae_operation_bounds_digest_binding_and_text_anchor_boundary():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    assert "var SHA256_PATTERN = /^[0-9a-f]{64}$/;" in panel
    assert "batch.capability_digest === approved_capabilities.digest" in panel
    assert "influence < 0.1" in panel
    assert "value < 4 || value > 30000" in panel
    assert "hasOwn(removed, instanceId)" in panel
    assert "inventorySize(inventory) > 1000" in panel
    assert "instanceof TextLayer" in panel
    assert "sourceRectAtTime(0, false)" in panel
    assert "rect.left" in panel
    assert "rect.top" in panel
    assert "setTimedProperty(property, value, frame, time, keyframe)" in panel

def test_panel_rejects_alpha_colors_and_enforces_project_settings_limits():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    color_source = panel.split("function assertColor", 1)[1].split(
        "function assertSafeValue", 1
    )[0]
    assert "/^#[0-9a-fA-F]{6}$/" in color_source
    assert "value.length !== 3" in color_source
    property_color_source = panel.split("function capabilityProperty", 1)[1].split(
        "function catalogName", 1
    )[0]
    assert 'assertColor(value, "property color")' in property_color_source
    normalized_source = panel.split("function normalizedColor", 1)[1].split(
        "function setColor", 1
    )[0]
    assert "channels.push(1)" not in normalized_source
    settings_source = panel.split("function projectSettings", 1)[1].split(
        "function projectSettingsMatch", 1
    )[0]
    assert 'assertSolidDimension(payload.width, "width")' in settings_source
    assert 'assertSolidDimension(payload.height, "height")' in settings_source
    assert "payload.frame_rate < 1" in settings_source
    assert "payload.frame_rate > 99" in settings_source
    assert "payload.duration > 10800" in settings_source
    create_source = panel.split("function createOrOpenProject", 1)[1].split(
        "function importServerAsset", 1
    )[0]
    assert create_source.index("projectSettings(payload)") < create_source.index(
        "app.newProject()"
    )


def test_panel_preflights_target_kinds_and_footage_before_opening_undo_group():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    preflight = panel.split("function preflightBatch", 1)[1].split(
        "function captureBatchSnapshot", 1
    )[0]
    batch = panel.split("function applyBatch(payload)", 1)[1].split(
        "function createOrOpenProject", 1
    )[0]
    assert "instanceof TextLayer" in preflight
    assert "findAssetItem(operation.asset_id)" in preflight
    assert "preflightBatch(operations)" in batch
    assert batch.index("preflightBatch(operations)") < batch.index(
        "app.beginUndoGroup"
    )


def test_panel_rolls_back_failed_batch_and_verifies_inventory_before_throwing():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    batch = panel.split("function applyBatch(payload)", 1)[1].split(
        "function createOrOpenProject", 1
    )[0]
    assert "captureBatchSnapshot()" in batch
    assert "app.endUndoGroup()" in batch
    assert 'app.executeCommand(app.findMenuCommandId("Undo"))' in batch
    assert "verifyBatchSnapshot(snapshot)" in batch
    assert "apply failed" in batch
    assert "rollback failed" in batch
    assert batch.index("app.endUndoGroup()") < batch.index(
        'app.executeCommand(app.findMenuCommandId("Undo"))'
    )
    assert batch.index('app.executeCommand(app.findMenuCommandId("Undo"))') < batch.index(
        "verifyBatchSnapshot(snapshot)"
    )
    assert batch.index("verifyBatchSnapshot(snapshot)") < batch.index(
        'throw new Error("apply failed: " + batchErrorText(applyError) + "; rollback succeeded")'
    )


def test_panel_text_operations_require_text_layers_and_add_fields_stay_typed():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    assert "function requireTextLayer" in panel
    assert "requireTextLayer(layer)" in panel
    add_source = panel.split("function addMappedLayer", 1)[1].split(
        "function hasOwn", 1
    )[0]
    assert 'case "text":' in add_source
    assert "comp.layers.addText(operation.name)" in add_source
    assert 'case "footage":' in add_source
    assert "findAssetItem(operation.asset_id)" in add_source

def test_panel_inspection_request_and_samples_are_strict_and_authoritative():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    inspect_source = panel.split("function inspectLayers", 1)[1].split(
        "function saveCheckpoint", 1
    )[0]
    validation_source = panel.split("function validateInspectRequest", 1)[1].split(
        "function hasOwnValue", 1
    )[0]
    assert "function validateInspectRequest" in panel
    assert 'assertKeys(payload, {' in validation_source
    assert "frame_count: true" in validation_source
    assert "fps: true" in validation_source
    assert "requested: true" in validation_source
    assert "layer_sources: true" in validation_source
    assert "allow_new_agent_ids: true" in validation_source
    assert "requested.length > 4096" in validation_source
    assert "frameCount > MAX_KEYFRAMES" in validation_source
    assert "frame >= frameCount" in validation_source
    assert "payload.allow_new_agent_ids.length > 1000" in validation_source
    inventory_source = panel.split("function inspectLayerInventory", 1)[1].split(
        "function finiteCompPoint", 1
    )[0]
    sample_source = panel.split("function inspectLayerSample", 1)[1].split(
        "function inspectLayers", 1
    )[0]
    dispatch_source = panel.split("function inspectLayers", 1)[1].split(
        "function saveCheckpoint", 1
    )[0]
    assert "keepframe.ae-inspection/1" in inspect_source
    assert "source drift" in inventory_source
    assert "duplicate manual agent id" in inventory_source
    assert "allow_new_agent_ids pool is exhausted" in inventory_source
    assert "instanceId = request.manual_ids[manualIndex]" in inventory_source
    assert "manualAssignments.push" in inventory_source
    assert "toComp" in sample_source
    assert "sourceRectAtTime" in sample_source
    assert "Math.atan2" in sample_source
    assert "unwrapWorldRotation" in panel
    assert "rotationStates[metadata.layer_instance_id]" in inspect_source
    assert "requestOrder.sort" in inspect_source
    assert "frame_start" in inventory_source and "frame_end" in inventory_source
    assert "in_point" not in inventory_source and "out_point" not in inventory_source
    assert "sample === null ? null : sample" in dispatch_source
    assert "requested: request.requested" in dispatch_source
    assert "layerSources[rows[rowIndex].layer_instance_id]" in dispatch_source
    assert "missing_pairs: missingPairs" in dispatch_source
    assert "source_aggregated: false" in dispatch_source


def test_panel_preview_is_full_sequence_resolution_bounded_and_restores_comp():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    render_source = panel.split("function renderPreview", 1)[1].split(
        "function packageProject", 1
    )[0]
    assert "checkpoint_index: true" in render_source
    assert "frame_count: true" in render_source
    assert "fps: true" in render_source
    assert "representative_frames: true" in render_source
    assert "resolutionFactor" in render_source
    assert "try {" in render_source and "finally" in render_source
    assert "saveFrameToPng" in render_source
    assert "checkpoint-" in render_source
    assert "%06d" in render_source
    assert "for (frame = 0; frame < frameCount; frame += 1)" in render_source
    assert "representative.length < 1" in render_source
    assert "representative.length > 12" in render_source
    assert "Math.ceil(comp.width / factor)" in render_source
    assert "comp.resolutionFactor = restoreResolution" in render_source


def test_panel_opens_only_private_deterministic_checkpoint_and_rebinds_session():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    create_source = panel.split("function createOrOpenProject", 1)[1].split(
        "function importServerAsset", 1
    )[0]
    assert "checkpoint_index" in create_source
    assert 'assertKeys(payload, {' in create_source
    assert "background_color: true" in create_source
    assert "checkpoint_index: true" in create_source
    assert 'CHECKPOINT_DIR.fsName + "/checkpoint-"' in create_source
    assert "app.open(file)" in create_source
    assert "findSessionComposition(marker)" in create_source
    assert "projectSettingsMatch(comp, settings)" in create_source
    assert "checkpoint index is invalid" in create_source


def test_panel_scopes_files_and_distinguishes_inactive_from_unsampled_layers():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    assert 'BRIDGE_ROOT.fsName + "/projects"' in panel
    assert "function bindSessionDirectories" in panel
    assert "SESSION_ID_PATTERN.test(projectId)" in panel
    assert 'plansRoot = Folder(projectRoot.fsName + "/plans")' in panel
    assert 'sessionsRoot = Folder(planRoot.fsName + "/sessions")' in panel
    assert "originalComments" in panel
    assert "throw inspectError" in panel
    assert "layer.enabled !== false" in panel
    inventory_source = panel.split("function inspectLayerInventory", 1)[1].split(
        "function finiteCompPoint", 1
    )[0]
    assert "request.layer_sources[instanceId] !== null" in inventory_source
    sample_source = panel.split("function inspectLayerSample", 1)[1].split(
        "function inspectLayers", 1
    )[0]
    assert "transform.anchorPoint.valueAtTime(time, false)" in sample_source
    assert "determinant < 0" in sample_source
    assert "transform.opacity.valueAtTime(time, false)" in sample_source
    dispatch_source = panel.split("function inspectLayers", 1)[1].split(
        "function saveCheckpoint", 1
    )[0]
    assert "active: active" in dispatch_source
    assert "if (active)" in dispatch_source

def test_panel_binds_apply_and_inspection_to_native_layer_ids():
    panel = (
        Path(__file__).parents[1]
        / "keepframe"
        / "after_effects"
        / "assets"
        / "keepframe_panel.jsx"
    ).read_text(encoding="utf-8")
    inventory_source = panel.split("function inspectLayerInventory", 1)[1].split(
        "function finiteCompPoint", 1
    )[0]
    inspect_validation = panel.split("function validateInspectRequest", 1)[1].split(
        "function hasOwnValue", 1
    )[0]
    apply_validation = panel.split("function validateApplyContext", 1)[1].split(
        "function authorizeOperations", 1
    )[0]
    assert "nativeLayerId(layer)" in inventory_source
    assert "native_layer_id" in inventory_source
    assert "layer_native_ids: layerNativeIds" in panel
    assert "layer_native_ids: true" in inspect_validation
    assert "mappedLayerNativeInventory" in apply_validation
    assert "native layer id does not match" in apply_validation
