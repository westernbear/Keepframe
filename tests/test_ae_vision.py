import base64
import json
import re

import cv2
import numpy as np
import pytest

from keepframe.after_effects.operations import ApprovedCapabilities, OperationValidationError
from keepframe.after_effects.vision import (
    VISION_TOOLS,
    VisionUnsupported,
    frame_to_data_url,
    run_vision_step,
    select_representative_frames,
)


DIGEST = "a" * 64


def _caps(*properties):
    return ApprovedCapabilities(
        digest=DIGEST,
        properties={name: schema for name, schema in properties},
    )


def _png(width=16, height=9, color=(10, 20, 30)):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:] = color
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def test_representative_frames_prioritize_keep_boundaries_and_cap():
    scene = {
        "frames": 40,
        "constraints": [
            {"pred": "left(a,b) @13", "keep": True},
            {"pred": "right(a,b)", "keep": True},
            {"pred": "type(m,type)", "keep": True},
        ],
        "elements": [
            {
                "id": "a",
                "visible": [4, 10],
                "tracks": {"x": {"keys": [{"t": 7, "v": 0}, {"t": 20, "v": 1}]}},
                "z": {"keys": [{"t": 8, "v": 0}]},
            }
        ],
    }
    frames = select_representative_frames(scene)
    assert len(frames) <= 12
    assert frames[:3] == (0, 19, 39)
    assert 13 in frames
    assert 4 in frames and 10 in frames
    assert 7 in frames and 8 in frames and 20 in frames
    assert len(frames) == len(set(frames))
    assert all(0 <= frame < 40 for frame in frames)


def test_frame_to_data_url_scales_down_without_upscaling(tmp_path):
    source = tmp_path / "frame.png"
    source.write_bytes(_png(width=2560, height=1440))
    data_url = frame_to_data_url(source)
    encoded = base64.b64decode(data_url.removeprefix("data:image/png;base64,"))
    image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert image.shape[:2] == (720, 1280)

    small_url = frame_to_data_url(_png(width=80, height=40))
    small = base64.b64decode(small_url.removeprefix("data:image/png;base64,"))
    small_image = cv2.imdecode(np.frombuffer(small, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert small_image.shape[:2] == (40, 80)


class _Client:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0
        self.messages = None
        self.tools = None

    def complete(self, messages, tools):
        self.calls += 1
        self.messages = messages
        self.tools = tools
        if self.error is not None:
            raise self.error
        return self.response
def _reply(name, arguments, content=""):
    return {
        "content": content,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, separators=(",", ":"))},
            }
        ],
    }


def _step(client, **kwargs):
    params = {
        "frames": [_png()],
        "direction": "Preserve the locked motion.",
        "ir_summary": {"scene_id": "scene-1", "frames": 20},
        "locked_source_ids": ["source-locked"],
        "inspection": {"layers": [{"instance_id": "mapped-locked", "source_element_id": "source-locked"}]},
        "previous_violations": [{"predicate": "left(a,b)", "observed": False}],
        "checkpoint_index": 2,
        "approved_capabilities": _caps(("ADBE Position", "vec2")),
        "scene_frame_count": 20,
        "duration": 2.0,
        "layer_sources": {"mapped-locked": "source-locked"},
    }
    params.update(kwargs)
    return run_vision_step(client, **params)


def test_multimodal_payload_and_fixed_tools_are_exact_and_call_once():
    client = _Client(_reply("pause_ae", {"reason": "inspect"}))
    result = _step(client)

    assert result.status == "pause"
    assert result.reason == "inspect"
    assert client.calls == 1
    assert [tool["function"]["name"] for tool in client.tools] == ["apply_ae_batch", "pause_ae"]
    apply_parameters = client.tools[0]["function"]["parameters"]
    assert set(apply_parameters["properties"]) == {"operations"}
    assert "capability_digest" not in apply_parameters["properties"]
    assert len(VISION_TOOLS) == 2

    content = client.messages[-1]["content"]
    assert content[0]["type"] == "text"
    payload = json.loads(content[0]["text"])
    assert payload["direction"] == "Preserve the locked motion."
    assert payload["ir_summary"] == {"scene_id": "scene-1", "frames": 20}
    assert payload["locked_source_ids"] == ["source-locked"]
    assert payload["inspection"]["layers"][0]["instance_id"] == "mapped-locked"
    assert payload["previous_violations"] == [{"predicate": "left(a,b)", "observed": False}]
    assert payload["checkpoint_index"] == 2
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_apply_schema_publishes_batch_local_temporary_id_rule():
    add_layer = VISION_TOOLS[0]["function"]["parameters"]["properties"]["operations"]["items"]["$defs"][
        "AddLayerOperation"
    ]
    pattern = add_layer["properties"]["layer_instance_id"]["pattern"]

    assert pattern == r"^new[-_:][A-Za-z0-9_.:-]{1,255}$"
    assert all(re.fullmatch(pattern, value) for value in ("new-hero", "new_agent:1", "new:effect"))
    assert not any(re.fullmatch(pattern, value) for value in ("layer-1", "agent-layer", "new"))


@pytest.mark.parametrize("temporary_id", ["new-hero", "new_agent:1", "new:effect"])
def test_model_additions_accept_published_temporary_ids(temporary_id):
    response = _reply(
        "apply_ae_batch",
        {
            "operations": [
                {
                    "kind": "add_layer",
                    "layer_instance_id": temporary_id,
                    "layer_type": "null",
                    "name": "Agent layer",
                }
            ]
        },
    )

    result = _step(
        _Client(response),
        layer_sources={},
        locked_source_ids=[],
        id_factory=lambda: "agent-test-01",
    )

    assert result.batch is not None
    assert result.batch.operations[0].layer_instance_id == "agent-test-01"


@pytest.mark.parametrize("temporary_id", ["layer-1", "agent-layer", "new"])
def test_model_additions_reject_ids_outside_published_temporary_pattern(temporary_id):
    response = _reply(
        "apply_ae_batch",
        {
            "operations": [
                {
                    "kind": "add_layer",
                    "layer_instance_id": temporary_id,
                    "layer_type": "null",
                    "name": "Agent layer",
                }
            ]
        },
    )

    with pytest.raises(OperationValidationError, match="temporary id"):
        _step(
            _Client(response),
            layer_sources={},
            locked_source_ids=[],
            id_factory=lambda: "agent-test-01",
        )


def test_apply_allocates_agent_ids_and_returns_canonical_digest():
    response = _reply(
        "apply_ae_batch",
        {
            "operations": [
                {
                    "kind": "add_layer",
                    "layer_instance_id": "new-hero",
                    "layer_type": "null",
                    "name": "Hero",
                },
                {
                    "kind": "set_transform",
                    "layer_instance_id": "new-hero",
                    "property_name": "position",
                    "value": [10, 20],
                },
            ]
        },
    )
    client = _Client(response)
    result = _step(client, layer_sources={}, locked_source_ids=[], id_factory=lambda: "agent-test-01")

    assert result.status == "apply"
    assert result.batch is not None
    assert result.batch.operations[0].layer_instance_id == "agent-test-01"
    assert result.batch.operations[1].layer_instance_id == "agent-test-01"
    repeated = _step(
        _Client(response),
        layer_sources={},
        locked_source_ids=[],
        id_factory=lambda: "agent-test-99",
    )
    assert result.operation_digest == repeated.operation_digest
    assert result.batch.digest != repeated.batch.digest
    assert json.loads(result.response_wire)["tool_calls"][0]["name"] == "apply_ae_batch"


def test_locked_mutation_and_source_bearing_addition_are_rejected():
    locked = _Client(
        _reply(
            "apply_ae_batch",
            {
                "operations": [
                    {
                        "kind": "set_transform",
                        "layer_instance_id": "mapped-locked",
                        "property_name": "position",
                        "value": [1, 2],
                    }
                ]
            },
        )
    )
    with pytest.raises(OperationValidationError, match="locked"):
        _step(locked)

    source_add = _Client(
        _reply(
            "apply_ae_batch",
            {
                "operations": [
                    {
                        "kind": "add_layer",
                        "layer_instance_id": "new-source",
                        "layer_type": "null",
                        "name": "Source",
                        "source_element_id": "source-x",
                    }
                ]
            },
        )
    )
    with pytest.raises(OperationValidationError, match="source-less"):
        _step(source_add, layer_sources={}, locked_source_ids=[], id_factory=lambda: "agent-test-02")


def test_model_cannot_reuse_inventory_or_tombstone_ids():
    collision = _Client(
        _reply(
            "apply_ae_batch",
            {
                "operations": [
                    {
                        "kind": "add_layer",
                        "layer_instance_id": "mapped-existing",
                        "layer_type": "null",
                        "name": "Existing",
                    }
                ]
            },
        )
    )
    with pytest.raises(OperationValidationError, match="reuses"):
        _step(
            collision,
            layer_sources={"mapped-existing": None},
            locked_source_ids=[],
            id_factory=lambda: "agent-test-03",
        )

    tombstone = _Client(
        _reply(
            "apply_ae_batch",
            {
                "operations": [
                    {
                        "kind": "add_layer",
                        "layer_instance_id": "new-dead",
                        "layer_type": "null",
                        "name": "Dead",
                    }
                ]
            },
        )
    )
    with pytest.raises(OperationValidationError, match="reuses"):
        _step(
            tombstone,
            layer_sources={},
            locked_source_ids=[],
            tombstones=["new-dead"],
            id_factory=lambda: "agent-test-04",
        )


def test_image_rejection_is_distinct_and_never_retried():
    client = _Client(error=RuntimeError("multimodal image input is unsupported"))
    with pytest.raises(VisionUnsupported):
        _step(client)
    assert client.calls == 1


def test_noop_is_no_progress_and_pause_is_preserved():
    noop = _Client(_reply("apply_ae_batch", {"operations": []}))
    result = _step(noop)
    assert result.status == "no_progress"
    assert result.batch is not None and result.batch.operations == []

    pause = _Client(_reply("pause_ae", {"reason": "user requested pause"}))
    paused = _step(pause)
    assert paused.status == "pause"
    assert paused.reason == "user requested pause"
    assert paused.operation_digest is None
