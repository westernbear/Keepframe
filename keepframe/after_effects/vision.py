from __future__ import annotations

import base64
import json
import math
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
from pydantic import BaseModel, TypeAdapter

from ..ir.schema import Scene
from ..verify.predicates import parse_pred
from .models import AECapabilities
from .operations import (
    ApprovedCapabilities,
    OperationBatch,
    OperationValidationError,
    Operation,
    canonical_operation_digest,
    validate_operation_batch,
)


MAX_VISION_FRAMES = 12
MAX_IMAGE_WIDTH = 1280
MAX_IMAGE_HEIGHT = 720
MAX_VISION_REASON_LENGTH = 4096
_AGENT_ID_RE = re.compile(r"^agent[-_:][A-Za-z0-9_.:-]{1,255}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_TEMP_ID_RE = re.compile(r"^new[-_:][A-Za-z0-9_.:-]{1,255}$")
_SPATIAL_PREDICATES = frozenset({"left", "right", "top", "bottom", "intersect"})


class VisionError(RuntimeError):
    """Base class for one non-successful vision-loop step."""


class VisionUnsupported(VisionError):
    """The configured model rejected the multimodal image input."""


class VisionModelError(VisionError):
    """The configured model failed for a reason other than image support."""


class VisionProtocolError(VisionError):
    """The model response did not contain the one fixed tool call we require."""


class VisionFrameError(VisionError):
    """A rendered preview frame was not a readable PNG image."""


@dataclass(frozen=True)
class VisionStepResult:
    """Immutable outcome of one model call.

    ``response_wire`` is canonical JSON rather than the mutable provider object.  The
    convenience properties decode a fresh object for callers that need to inspect it.
    """

    status: Literal["apply", "pause", "no_progress"]
    response_wire: str
    operation_digest: str | None = None
    batch: OperationBatch | None = None
    reason: str | None = None

    @property
    def model_response_wire(self) -> str:
        return self.response_wire

    @property
    def canonical_model_response(self) -> str:
        return self.response_wire

    @property
    def model_response(self) -> dict[str, Any]:
        return json.loads(self.response_wire)

    @property
    def wire(self) -> str:
        return self.response_wire


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"value is not JSON data: {type(value).__name__}")


def _canonical_text(value: Any) -> str:
    try:
        return json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise VisionProtocolError("vision payload is not finite JSON") from exc


def _compact_ir_summary(value: Any) -> Any:
    """Keep the authoritative IR facts while dropping binary/verbose source fields."""
    if value is None:
        return {}
    if isinstance(value, Scene):
        scene = value
        return {
            "schema": scene.schema_version,
            "id": scene.id,
            "size": list(scene.size),
            "fps": scene.fps,
            "frames": scene.frames,
            "background": scene.background.model_dump(mode="json"),
            "elements": [
                {
                    "id": element.id,
                    "kind": element.kind,
                    "role": element.role,
                    "visible": list(element.visible),
                    "tracks": {
                        name: [key.t for key in track.keys]
                        for name, track in sorted(element.tracks.items())
                    },
                    "z": [key.t for key in element.z.keys],
                }
                for element in scene.elements
            ],
            "constraints": [item.model_dump(mode="json") for item in scene.constraints],
        }
    if not isinstance(value, Mapping):
        return _jsonable(value)
    # Callers may already provide a compact authoritative summary.  Only trim a full
    # scene-shaped mapping, where media paths/textures are irrelevant to the model.
    if "elements" not in value:
        return _jsonable(value)
    result: dict[str, Any] = {}
    for key in ("schema", "schema_version", "id", "scene_id", "size", "fps", "frames", "background"):
        if key in value:
            result[key] = _jsonable(value[key])
    elements = value.get("elements")
    if isinstance(elements, Sequence) and not isinstance(elements, (str, bytes, bytearray)):
        compact_elements: list[dict[str, Any]] = []
        for item in elements:
            if not isinstance(item, Mapping):
                continue
            row: dict[str, Any] = {
                key: _jsonable(item[key])
                for key in ("id", "kind", "role", "visible")
                if key in item
            }
            tracks = item.get("tracks")
            if isinstance(tracks, Mapping):
                row["tracks"] = {
                    str(name): [
                        _field(key, "t")
                        for key in (_field(track, "keys", ()) or ())
                        if _field(key, "t") is not None
                    ]
                    for name, track in sorted(tracks.items())
                }
            z = item.get("z")
            if z is not None:
                row["z"] = [
                    _field(key, "t")
                    for key in (_field(z, "keys", ()) or ())
                    if _field(key, "t") is not None
                ]
            compact_elements.append(row)
        result["elements"] = compact_elements
    constraints = value.get("constraints")
    if isinstance(constraints, Sequence) and not isinstance(constraints, (str, bytes, bytearray)):
        result["constraints"] = [
            _jsonable(item)
            for item in constraints
            if isinstance(item, Mapping) and item.get("keep", False)
        ]
    return result


def _frame_count(scene: Any, inspection: Any, explicit: int | None) -> int:
    value = explicit
    if value is None and isinstance(scene, int) and not isinstance(scene, bool):
        value = scene
    if value is None:
        value = _field(scene, "frames")
    if value is None:
        value = _field(inspection, "scene_frame_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("scene frame count must be a positive integer")
    return value


def _add_clamped(out: list[int], seen: set[int], value: Any, count: int) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    if not math.isfinite(float(value)):
        return
    frame = max(0, min(count - 1, int(round(float(value)))))
    if frame not in seen:
        seen.add(frame)
        out.append(frame)


def _predicate_rows(scene: Any, keep_predicates: Sequence[Any] | None) -> Sequence[Any]:
    if keep_predicates is not None:
        return keep_predicates
    rows = _field(scene, "constraints", ())
    return rows if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)) else ()


def _predicate_frame(row: Any) -> tuple[str, int | None] | None:
    raw = row
    if isinstance(row, str):
        enabled = True
    elif isinstance(row, Mapping):
        enabled = row.get("keep", row.get("enabled", False))
        raw = row.get("pred", row.get("predicate"))
    else:
        enabled = _field(row, "keep", False)
        raw = _field(row, "pred", _field(row, "predicate"))
    if not enabled or not isinstance(raw, str):
        return None
    try:
        name, _args, frame = parse_pred(raw)
    except (TypeError, ValueError):
        return None
    return name, frame


def _add_visibility_boundaries(out: list[int], seen: set[int], obj: Any, count: int) -> None:
    visible = _field(obj, "visible")
    if isinstance(visible, Sequence) and not isinstance(visible, (str, bytes, bytearray)) and len(visible) >= 2:
        _add_clamped(out, seen, visible[0], count)
        _add_clamped(out, seen, visible[1], count)
    for name in ("frame_start", "frame_end"):
        value = _field(obj, name)
        if value is not None:
            _add_clamped(out, seen, value, count)


def _add_track_boundaries(out: list[int], seen: set[int], obj: Any, count: int) -> None:
    tracks = _field(obj, "tracks", {})
    if isinstance(tracks, Mapping):
        values = list(tracks.values())
    else:
        values = []
    z = _field(obj, "z")
    if z is not None:
        values.append(z)
    for track in values:
        keys = _field(track, "keys", ())
        if isinstance(keys, Sequence) and not isinstance(keys, (str, bytes, bytearray)):
            for key in keys:
                _add_clamped(out, seen, _field(key, "t", _field(key, "frame")), count)


def _inspection_layers(inspection: Any) -> Sequence[Any]:
    for name in ("layers", "layer_inventory", "inventory", "instances"):
        rows = _field(inspection, name)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
            return rows
    return ()


def _uniform_frame(out: list[int], seen: set[int], count: int, target: int) -> None:
    if target not in seen:
        _add_clamped(out, seen, target, count)
        return
    for distance in range(1, count):
        left, right = target - distance, target + distance
        if left >= 0 and left not in seen:
            _add_clamped(out, seen, left, count)
            return
        if right < count and right not in seen:
            _add_clamped(out, seen, right, count)
            return


def select_representative_frames(
    scene: Any = None,
    inspection: Any = None,
    *,
    frame_count: int | None = None,
    keep_predicates: Sequence[Any] | None = None,
    cap: int = MAX_VISION_FRAMES,
) -> tuple[int, ...]:
    """Select at most twelve deterministic frame indexes by semantic priority."""
    count = _frame_count(scene, inspection, frame_count)
    limit = max(0, min(MAX_VISION_FRAMES, int(cap)))
    if limit == 0:
        return ()
    selected: list[int] = []
    seen: set[int] = set()
    for value in (0, (count - 1) // 2, count - 1):
        _add_clamped(selected, seen, value, count)

    for row in _predicate_rows(scene, keep_predicates):
        parsed = _predicate_frame(row)
        if parsed is not None:
            name, frame = parsed
            if name in _SPATIAL_PREDICATES:
                _add_clamped(selected, seen, count - 1 if frame is None else frame, count)

    elements = _field(scene, "elements", ())
    if isinstance(elements, Sequence) and not isinstance(elements, (str, bytes, bytearray)):
        for element in elements:
            _add_visibility_boundaries(selected, seen, element, count)
            _add_track_boundaries(selected, seen, element, count)
    for layer in _inspection_layers(inspection):
        _add_visibility_boundaries(selected, seen, layer, count)
        _add_track_boundaries(selected, seen, layer, count)

    selected = selected[:limit]
    if len(selected) < limit and count > 1:
        slots = limit - len(selected)
        for index in range(1, slots + 1):
            if len(selected) >= limit:
                break
            target = int(round(index * (count - 1) / (slots + 1)))
            _uniform_frame(selected, seen, count, target)
    if len(selected) < limit:
        for frame in range(count):
            if len(selected) >= limit:
                break
            _add_clamped(selected, seen, frame, count)
    return tuple(selected[:limit])


def _decode_png(frame: bytes | bytearray | memoryview | str | Path | np.ndarray) -> np.ndarray:
    if isinstance(frame, np.ndarray):
        image = frame
    elif isinstance(frame, (str, Path)):
        image = cv2.imread(str(frame), cv2.IMREAD_UNCHANGED)
    elif isinstance(frame, (bytes, bytearray, memoryview)):
        image = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    else:
        raise VisionFrameError("frame must be PNG bytes, a path, or an image array")
    if image is None or not isinstance(image, np.ndarray) or image.ndim not in (2, 3):
        raise VisionFrameError("frame is not a readable PNG image")
    if image.shape[0] < 1 or image.shape[1] < 1:
        raise VisionFrameError("frame has no pixels")
    return image


def frame_to_png_bytes(frame: bytes | bytearray | memoryview | str | Path | np.ndarray) -> bytes:
    image = _decode_png(frame)
    height, width = image.shape[:2]
    scale = min(1.0, MAX_IMAGE_WIDTH / width, MAX_IMAGE_HEIGHT / height)
    if scale < 1.0:
        target = (
            max(1, int(round(width * scale))),
            max(1, int(round(height * scale))),
        )
        image = cv2.resize(image, target, interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".png", np.ascontiguousarray(image))
    if not ok:
        raise VisionFrameError("could not encode preview PNG")
    return encoded.tobytes()


def frame_to_data_url(frame: bytes | bytearray | memoryview | str | Path | np.ndarray) -> str:
    return "data:image/png;base64," + base64.b64encode(frame_to_png_bytes(frame)).decode("ascii")


def _frame_items(frames: Sequence[Any] | Mapping[Any, Any]) -> list[tuple[Any, Any]]:
    if isinstance(frames, Mapping):
        try:
            return sorted(frames.items(), key=lambda item: int(item[0]))
        except (TypeError, ValueError):
            return sorted(frames.items(), key=lambda item: str(item[0]))
    return list(enumerate(frames))


def build_vision_messages(
    *,
    direction: str | None,
    ir_summary: Any,
    locked_source_ids: Sequence[str],
    inspection: Any,
    previous_violations: Sequence[Any],
    checkpoint_index: int,
    frames: Sequence[Any] | Mapping[Any, Any],
    scene: Any = None,
) -> list[dict[str, Any]]:
    if direction is not None and (not isinstance(direction, str) or len(direction) > MAX_VISION_REASON_LENGTH):
        raise ValueError("direction must be at most 4096 characters")
    if isinstance(checkpoint_index, bool) or not isinstance(checkpoint_index, int) or checkpoint_index < 0:
        raise ValueError("checkpoint index must be a non-negative integer")
    items = _frame_items(frames)
    text = {
        "checkpoint_index": checkpoint_index,
        "direction": direction or "",
        "ir_summary": _compact_ir_summary(ir_summary if ir_summary is not None else scene),
        "locked_source_ids": sorted(set(locked_source_ids)),
        "inspection": _jsonable(inspection),
        "previous_violations": _jsonable(previous_violations),
        "frame_indices": [_jsonable(index) for index, _ in items],
    }
    content: list[dict[str, Any]] = [{"type": "text", "text": _canonical_text(text)}]
    content.extend(
        {
            "type": "image_url",
            "image_url": {"url": frame_to_data_url(frame)},
        }
        for _index, frame in items
    )
    return [{"role": "user", "content": content}]


_OPERATION_SCHEMA = TypeAdapter(Operation).json_schema(mode="serialization")
_OPERATION_SCHEMA["$defs"]["AddLayerOperation"]["properties"]["layer_instance_id"].update(
    {
        "pattern": _TEMP_ID_RE.pattern,
        "description": (
            "Batch-local temporary ID matching new[-_:][A-Za-z0-9_.:-]{1,255}; "
            "the server rewrites it to a stable agent ID."
        ),
    }
)
VISION_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "apply_ae_batch",
            "description": "Apply one validated After Effects operation batch.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "operations": {
                        "type": "array",
                        "items": _OPERATION_SCHEMA,
                        "maxItems": 128,
                    }
                },
                "required": ["operations"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pause_ae",
            "description": "Pause the After Effects vision loop with a user-readable reason.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "reason": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_VISION_REASON_LENGTH,
                    }
                },
                "required": ["reason"],
            },
        },
    },
]


def _approved_capabilities(value: Any) -> ApprovedCapabilities:
    if isinstance(value, ApprovedCapabilities):
        return value
    if isinstance(value, AECapabilities):
        catalog = value.capabilities
        return ApprovedCapabilities(
            digest=value.digest,
            fonts=tuple(catalog.font_names),
            effects=tuple(catalog.effect_names),
            properties=dict(catalog.property_schemas or catalog.properties),
            model_layers=catalog.model_layers,
        )
    if isinstance(value, Mapping):
        raw = dict(value)
        nested = raw.get("capabilities")
        if isinstance(nested, Mapping):
            digest = raw.get("digest") or raw.get("capability_digest") or raw.get("capability_hash")
            raw = {
                "digest": digest,
                "fonts": nested.get("font_names", nested.get("fonts", ())),
                "effects": nested.get("effect_names", nested.get("effects", ())),
                "properties": nested.get("property_schemas", nested.get("properties", {})),
                "model_layers": nested.get("model_layers", False),
            }
        return ApprovedCapabilities.model_validate(raw)
    if hasattr(value, "model_dump"):
        return _approved_capabilities(value.model_dump(mode="json"))
    raise TypeError("approved capabilities are required")


def _default_id_factory() -> str:
    return "agent-" + secrets.token_hex(16)


def _rewrite_model_additions(
    operations: Sequence[Any],
    *,
    layer_sources: Mapping[str, str | None],
    tombstones: Sequence[str],
    declared_add_ids: Sequence[str],
    id_factory: Callable[[], str],
) -> list[Any]:
    inventory_ids = set(layer_sources)
    tombstone_ids = set(tombstones)
    declared = set(declared_add_ids)
    temporary_to_agent: dict[str, str] = {}
    allocated: set[str] = set()
    for operation in operations:
        if not isinstance(operation, Mapping) or operation.get("kind") != "add_layer":
            continue
        raw_id = operation.get("layer_instance_id")
        if not isinstance(raw_id, str):
            continue
        if raw_id in inventory_ids or raw_id in tombstone_ids:
            raise OperationValidationError(f"add_layer reuses layer instance id: {raw_id}")
        if not (_TEMP_ID_RE.fullmatch(raw_id) or raw_id in declared):
            raise OperationValidationError("model additions require a batch-local temporary id")
        if raw_id in temporary_to_agent:
            raise OperationValidationError(f"duplicate batch-local layer id: {raw_id}")
        while True:
            candidate = id_factory()
            if (
                isinstance(candidate, str)
                and _AGENT_ID_RE.fullmatch(candidate)
                and candidate not in inventory_ids
                and candidate not in tombstone_ids
                and candidate not in allocated
            ):
                break
            if not isinstance(candidate, str):
                raise OperationValidationError("layer id factory returned an invalid id")
            raise OperationValidationError(f"layer id factory returned a colliding or invalid id: {candidate}")
        temporary_to_agent[raw_id] = candidate
        allocated.add(candidate)

    rewritten: list[Any] = []
    for operation in operations:
        if not isinstance(operation, Mapping):
            rewritten.append(operation)
            continue
        row = dict(operation)
        for key in ("layer_instance_id", "parent_instance_id"):
            value = row.get(key)
            if value in temporary_to_agent:
                row[key] = temporary_to_agent[value]
        rewritten.append(row)
    return rewritten


def _response_parts(response: Any) -> tuple[str, list[Any]]:
    if isinstance(response, Mapping):
        content = response.get("content") or ""
        calls = response.get("tool_calls")
        if calls is None:
            choices = response.get("choices")
            if isinstance(choices, Sequence) and choices:
                message = choices[0].get("message", {}) if isinstance(choices[0], Mapping) else {}
                if isinstance(message, Mapping):
                    content = message.get("content") or ""
                    calls = message.get("tool_calls")
    else:
        content = getattr(response, "content", "") or ""
        calls = getattr(response, "tool_calls", None)
        if calls is None:
            choices = getattr(response, "choices", None)
            if choices:
                message = getattr(choices[0], "message", None)
                if message is not None:
                    content = getattr(message, "content", "") or ""
                    calls = getattr(message, "tool_calls", None)
    if calls is None:
        calls = []
    if not isinstance(content, str):
        content = str(content)
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes, bytearray)):
        raise VisionProtocolError("model tool_calls must be an array")
    return content, list(calls)


def _call_parts(call: Any) -> tuple[Any, Any, Any]:
    if isinstance(call, Mapping):
        function = call.get("function")
        if isinstance(function, Mapping):
            return call.get("id"), function.get("name"), function.get("arguments", {})
        return call.get("id"), call.get("name"), call.get("arguments", {})
    function = getattr(call, "function", None)
    if function is not None:
        return getattr(call, "id", None), getattr(function, "name", None), getattr(function, "arguments", {})
    return getattr(call, "id", None), getattr(call, "name", None), getattr(call, "arguments", {})


def _parse_arguments(arguments: Any) -> Mapping[str, Any]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(
                arguments,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VisionProtocolError("model tool arguments are not valid JSON") from exc
    if not isinstance(arguments, Mapping):
        raise VisionProtocolError("model tool arguments must be an object")
    return arguments


def _response_wire(content: str, call_id: Any, name: str, arguments: Mapping[str, Any]) -> str:
    if call_id is not None and not isinstance(call_id, str):
        call_id = str(call_id)
    return _canonical_text(
        {
            "content": content,
            "tool_calls": [{"id": call_id, "name": name, "arguments": arguments}],
        }
    )


def _image_rejection(error: BaseException) -> bool:
    if isinstance(error, (VisionUnsupported, NotImplementedError)):
        return True
    name = type(error).__name__.lower()
    text = str(error).lower()
    if any(token in name for token in ("vision", "multimodal", "image")):
        return True
    if any(
        token in text
        for token in ("image input", "image_url", "multimodal", "vision input", "vision is not", "does not support image")
    ):
        return True
    if any(token in text for token in ("unsupported", "not support", "does not support")):
        return any(token in text for token in ("image", "vision", "multimodal"))
    return False


def run_vision_step(
    client: Any,
    frames: Sequence[Any] | Mapping[Any, Any] = (),
    *,
    direction: str | None = None,
    ir_summary: Any = None,
    scene: Any = None,
    locked_source_ids: Sequence[str] = (),
    inspection: Any = None,
    previous_violations: Sequence[Any] = (),
    checkpoint_index: int = 0,
    approved_capabilities: ApprovedCapabilities | AECapabilities | Mapping[str, Any] | None = None,
    scene_frame_count: int | None = None,
    duration: float | None = None,
    layer_count: int | None = None,
    layer_sources: Mapping[str, str | None] | None = None,
    tombstones: Sequence[str] = (),
    declared_add_ids: Sequence[str] = (),
    id_factory: Callable[[], str] | None = None,
) -> VisionStepResult:
    """Call the configured model once and validate its one fixed tool call."""
    messages = build_vision_messages(
        direction=direction,
        ir_summary=ir_summary,
        locked_source_ids=locked_source_ids,
        inspection=inspection,
        previous_violations=previous_violations,
        checkpoint_index=checkpoint_index,
        frames=frames,
        scene=scene,
    )
    try:
        response = client.complete(messages, VISION_TOOLS)
    except Exception as exc:  # noqa: BLE001 - preserve the provider seam's error class
        if _image_rejection(exc):
            raise VisionUnsupported("configured LLM does not support image input") from exc
        raise VisionModelError("configured LLM request failed") from exc

    content, calls = _response_parts(response)
    if len(calls) != 1:
        raise VisionProtocolError("vision model must return exactly one tool call")
    call_id, name, raw_arguments = _call_parts(calls[0])
    if name not in {"apply_ae_batch", "pause_ae"}:
        raise VisionProtocolError("vision model returned an unknown tool")
    arguments = _parse_arguments(raw_arguments)
    wire = _response_wire(content, call_id, name, arguments)

    if name == "pause_ae":
        if set(arguments) != {"reason"}:
            raise VisionProtocolError("pause_ae accepts only reason")
        reason = arguments["reason"]
        if not isinstance(reason, str) or not reason or len(reason) > MAX_VISION_REASON_LENGTH:
            raise VisionProtocolError("pause_ae reason is outside bounds")
        return VisionStepResult(status="pause", response_wire=wire, reason=reason)

    if set(arguments) != {"operations"}:
        raise VisionProtocolError("apply_ae_batch accepts only operations")
    raw_operations = arguments["operations"]
    if not isinstance(raw_operations, list):
        raise VisionProtocolError("apply_ae_batch operations must be an array")
    if approved_capabilities is None:
        raise ValueError("approved capabilities are required for an AE vision step")
    approved = _approved_capabilities(approved_capabilities)
    inventory = dict(layer_sources or {})
    if layer_count is not None and layer_count != len(inventory):
        raise OperationValidationError("layer count does not match the authoritative inventory")
    effective_layer_count = len(inventory) if layer_count is None else layer_count
    rewritten = _rewrite_model_additions(
        raw_operations,
        layer_sources=inventory,
        tombstones=tombstones,
        declared_add_ids=declared_add_ids,
        id_factory=id_factory or _default_id_factory,
    )
    batch = validate_operation_batch(
        {
            "operations": rewritten,
            "capability_digest": approved.digest,
            "scene_frame_count": scene_frame_count,
            "duration": duration,
            "layer_count": effective_layer_count,
        },
        approved_capabilities=approved,
        scene_frame_count=scene_frame_count,
        duration=duration,
        layer_count=effective_layer_count,
        baseline=False,
        locked_source_ids=locked_source_ids,
        layer_sources=inventory,
    )
    status: Literal["apply", "no_progress"] = "no_progress" if not batch.operations else "apply"
    return VisionStepResult(
        status=status,
        response_wire=wire,
        operation_digest=canonical_operation_digest({"operations": raw_operations}),
        batch=batch,
    )


__all__ = [
    "MAX_IMAGE_HEIGHT",
    "MAX_IMAGE_WIDTH",
    "MAX_VISION_FRAMES",
    "VisionError",
    "VisionFrameError",
    "VisionModelError",
    "VisionProtocolError",
    "VisionStepResult",
    "VisionUnsupported",
    "VISION_TOOLS",
    "build_vision_messages",
    "frame_to_data_url",
    "frame_to_png_bytes",
    "run_vision_step",
    "select_representative_frames",
]
