from __future__ import annotations

import pytest

from keepframe.after_effects import mcp_server
from keepframe.after_effects.bridge import BridgeResult
from keepframe.after_effects.operations import ApprovedCapabilities


class _Bridge:
    def __init__(self):
        self.calls = []

    def dispatch(self, kind, payload, *, command_id, nonce, timeout=30.0):
        self.calls.append((kind, payload, command_id, nonce, timeout))
        return BridgeResult(command_id=command_id, nonce=nonce, result={"ok": True})


def _payload(**overrides):
    digest = "a" * 64
    payload = {
        "batch": {
            "capability_digest": digest,
            "operations": [
                {"kind": "set_opacity", "layer_instance_id": "layer-1", "opacity": 0.5}
            ],
        },
        "approved_capabilities": ApprovedCapabilities(
            digest=digest, properties={"ADBE Opacity": "number"}
        ).model_dump(mode="json"),
        "scene_frame_count": 10,
        "duration": 5.0,
        "layer_count": 1,
        "baseline": False,
        "locked_source_ids": [],
        "layer_sources": {"layer-1": None},
        "layer_native_ids": {"layer-1": 41},
        "project_id": "project-1",
        "plan_id": "plan-1",
        "session_id": "session-1",
    }
    payload.update(overrides)
    return payload


def test_mcp_apply_canonicalizes_positive_native_layer_ids():
    bridge = _Bridge()
    envelope = {
        "command_id": "command-1",
        "nonce": "nonce-1",
        "payload": _payload(layer_native_ids={"layer-1": 41}),
    }

    result = mcp_server.fixed_tool_registry(bridge)["apply_operation_batch"](envelope)

    assert result == {"ok": True}
    assert bridge.calls[0][1]["layer_native_ids"] == {"layer-1": 41}


def test_mcp_render_preview_uses_bounded_long_bridge_timeout():
    bridge = _Bridge()
    result = mcp_server.fixed_tool_registry(bridge)["render_preview"](
        {
            "command_id": "command-render",
            "nonce": "nonce-render",
            "payload": {"project_id": "project-1"},
        }
    )

    assert result == {"ok": True}
    assert bridge.calls[0][4] > 30.0



@pytest.mark.parametrize("tool", ["save_checkpoint", "package_project"])
def test_mcp_checkpoint_and_package_tools_use_long_bridge_timeouts(tool):
    bridge = _Bridge()
    result = mcp_server.fixed_tool_registry(bridge)[tool](
        {
            "command_id": f"command-{tool}",
            "nonce": f"nonce-{tool}",
            "payload": {"project_id": "project-1"},
        }
    )

    assert result == {"ok": True}
    assert bridge.calls[0][4] > 30.0


@pytest.mark.parametrize("tool", ["apply_operation_batch", "inspect_mapped_layers"])
def test_mcp_long_running_apply_and_inspect_use_long_bridge_timeouts(tool):
    bridge = _Bridge()
    payload = _payload() if tool == "apply_operation_batch" else {"project_id": "project-1"}
    result = mcp_server.fixed_tool_registry(bridge)[tool](
        {
            "command_id": f"command-{tool}",
            "nonce": f"nonce-{tool}",
            "payload": payload,
        }
    )

    assert result == {"ok": True}
    assert bridge.calls[0][4] > 30.0

@pytest.mark.parametrize(
    "native_ids, message",
    [
        ({"layer-1": 0}, "native"),
        ({"layer-1": True}, "native"),
        ({"layer-1": 41, "layer-2": 41}, "duplicate"),
    ],
)
def test_mcp_apply_rejects_invalid_or_duplicate_native_layer_ids(native_ids, message):
    envelope = {
        "command_id": "command-2",
        "nonce": "nonce-2",
        "payload": _payload(layer_native_ids=native_ids),
    }

    with pytest.raises(ValueError, match=message):
        mcp_server.fixed_tool_registry(_Bridge())["apply_operation_batch"](envelope)


def test_mcp_apply_requires_native_layer_id_map():
    payload = _payload()
    payload.pop("layer_native_ids")
    envelope = {"command_id": "command-3", "nonce": "nonce-3", "payload": payload}

    with pytest.raises(ValueError, match="layer_native_ids"):
        mcp_server.fixed_tool_registry(_Bridge())["apply_operation_batch"](envelope)


def test_bridge_failures_reach_the_client_as_tool_errors():
    import inspect

    from keepframe.after_effects import mcp_server

    class ToolError(Exception):
        pass

    def forward(envelope=None, **kwargs):
        raise TimeoutError("timed out waiting for the AE panel")

    wrapped = mcp_server._reporting(forward, ToolError)
    assert inspect.signature(wrapped) == inspect.signature(forward)
    with pytest.raises(ToolError, match="^TimeoutError: timed out waiting for the AE panel$"):
        wrapped({"command_id": "c", "nonce": "n", "payload": {}})
    assert mcp_server._reporting(forward, None) is forward
