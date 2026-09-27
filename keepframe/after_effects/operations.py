from __future__ import annotations

"""Closed, JSON-only operation contracts for the After Effects bridge.

This module deliberately has no AE or MCP imports.  The same Pydantic models are
used at the server, connector, and panel seams; keeping the wire vocabulary
small is what makes the panel switch dispatch auditable.
"""

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

MAX_OPERATIONS = 128
MAX_RECORD_BYTES = 1 * 1024 * 1024
MAX_STRING_LENGTH = 4096
MAX_LAYERS = 1000
ABSOLUTE_NUMBER_CEILING = 1_000_000
_PROPERTY_SCHEMAS = frozenset(
    {"number", "float", "integer", "int", "boolean", "bool", "string", "str", "enum", "color", "rgba", "vec2", "vector2", "vec3", "vector3", "vec4", "vector4"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_URL_RE = re.compile(r"^(?:https?|file|javascript|data):", re.IGNORECASE)
MAX_LAYER_ID_LENGTH = 256
_FORBIDDEN_KEYS = frozenset(
    {
        "argv",
        "code",
        "command",
        "destination",
        "executable",
        "expression",
        "extend_script",
        "file",
        "file_path",
        "filepath",
        "jsx",
        "local_path",
        "output_path",
        "path",
        "project_path",
        "raw_code",
        "script",
        "source_path",
        "uri",
        "url",
    }
)


class OperationValidationError(ValueError):
    """Raised when a validly shaped operation violates an approved catalog."""


class _StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)


def _number(value: Any, *, label: str, minimum: float | None = None, maximum: float | None = None) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a JSON number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if abs(number) > ABSOLUTE_NUMBER_CEILING:
        raise ValueError(f"{label} exceeds the absolute ceiling")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} is below its minimum")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} is above its maximum")
    return value


def _integer(value: Any, *, label: str, minimum: int = 0, maximum: int = ABSOLUTE_NUMBER_CEILING) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{label} is outside its bounds")
    return value


def _string(
    value: Any,
    *,
    label: str = "string",
    identifier: bool = False,
    nonempty: bool = True,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if (nonempty and not value) or len(value) > MAX_STRING_LENGTH:
        raise ValueError(f"{label} must be at most {MAX_STRING_LENGTH} characters")
    if (
        _URL_RE.match(value)
        or value.startswith(("/", "\\", "./", "../"))
        or "\\" in value
        or "/../" in value
        or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        raise ValueError(f"{label} must not contain an external URL or path")
    if identifier and (len(value) > MAX_LAYER_ID_LENGTH or not _IDENTIFIER_RE.fullmatch(value)):
        raise ValueError(f"{label} is not a safe identifier")
    return value


def _safe_json(value: Any, *, _depth: int = 0) -> Any:
    """Validate JSON values without allowing NaN, paths, code, or giant strings."""
    if _depth > 64:
        raise ValueError("JSON nesting is too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _string(value, nonempty=False)
    if isinstance(value, int):
        _number(value, label="number")
        return value
    if isinstance(value, float):
        _number(value, label="number")
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _string(key, label="JSON key", nonempty=False)
            if key.lower() in _FORBIDDEN_KEYS:
                raise ValueError(f"JSON key {key!r} is not allowed")
            out[key] = _safe_json(item, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [_safe_json(item, _depth=_depth + 1) for item in value]
    # Tuples are useful for Python callers but are not accepted on the wire.
    raise ValueError("value must be JSON data")


def canonical_json(value: Any) -> bytes:
    """Return the one canonical finite JSON encoding used for operation digests."""
    safe = _safe_json(value)
    try:
        encoded = json.dumps(
            safe,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("value is not canonical JSON") from exc
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValueError("operation batch exceeds 1 MiB")
    return encoded


def canonical_operation_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _digest(value: str, *, label: str = "digest") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _identifier(value: str, *, label: str = "identifier") -> str:
    return _string(value, label=label, identifier=True)


def _finite(value: Any, *, label: str = "number") -> float | int:
    return _number(value, label=label)


class Keyframe(_StrictRecord):
    frame: int
    value: Any
    time: float | int | None = None
    ease_in: list[float | int] | None = None
    ease_out: list[float | int] | None = None

    _frame = field_validator("frame")(
        lambda value: _integer(value, label="keyframe frame")
    )
    _time = field_validator("time")(
        lambda value: None if value is None else _number(value, label="keyframe time", minimum=0)
    )
    _value = field_validator("value")(
        lambda value: _safe_json(value)
    )

    @field_validator("ease_in", "ease_out")
    @classmethod
    def _ease(cls, value: list[float | int] | None):
        if value is None:
            return None
        if len(value) != 2:
            raise ValueError("temporal ease must contain two numbers")
        return [
            _number(value[0], label="temporal ease speed"),
            _number(value[1], label="temporal ease influence", minimum=0, maximum=100),
        ]


class SetPropertyOperation(_StrictRecord):
    kind: Literal["set_property"] = "set_property"
    layer_instance_id: str
    property_name: str = Field(
        validation_alias=AliasChoices("property", "property_name"),
        serialization_alias="property_name",
    )
    value: Any
    frame: int | None = None
    time: float | int | None = None

    _layer = field_validator("layer_instance_id")(
        lambda value: _identifier(value, label="layer_instance_id")
    )
    _property = field_validator("property_name")(
        lambda value: _string(value, label="property name")
    )
    _value = field_validator("value")(
        lambda value: _safe_json(value)
    )
    _frame = field_validator("frame")(
        lambda value: None if value is None else _integer(value, label="frame")
    )
    _time = field_validator("time")(
        lambda value: None if value is None else _number(value, label="time", minimum=0)
    )

    @property
    def property(self) -> str:
        return self.property_name


class SetTransformOperation(_StrictRecord):
    kind: Literal["set_transform", "set_layer_transform"] = "set_transform"
    layer_instance_id: str
    property_name: Literal[
        "position",
        "scale",
        "scale_x",
        "scale_y",
        "rotation",
        "skew",
        "skew_x",
        "skew_y",
    ] = Field(
        validation_alias=AliasChoices("property", "property_name"),
        serialization_alias="property_name",
    )
    value: Any
    frame: int | None = None
    time: float | int | None = None

    _layer = field_validator("layer_instance_id")(
        lambda value: _identifier(value, label="layer_instance_id")
    )
    _value = field_validator("value")(
        lambda value: _safe_json(value)
    )
    _frame = field_validator("frame")(
        lambda value: None if value is None else _integer(value, label="frame")
    )
    _time = field_validator("time")(
        lambda value: None if value is None else _number(value, label="time", minimum=0)
    )


class SetOpacityOperation(_StrictRecord):
    kind: Literal["set_opacity", "set_layer_opacity"] = "set_opacity"
    layer_instance_id: str
    opacity: float | int
    frame: int | None = None
    time: float | int | None = None

    _layer = field_validator("layer_instance_id")(
        lambda value: _identifier(value, label="layer_instance_id")
    )
    _opacity = field_validator("opacity")(
        lambda value: _number(value, label="opacity", minimum=0, maximum=1)
    )
    _frame = field_validator("frame")(
        lambda value: None if value is None else _integer(value, label="frame")
    )
    _time = field_validator("time")(
        lambda value: None if value is None else _number(value, label="time", minimum=0)
    )


class SetColorOperation(_StrictRecord):
    kind: Literal["set_color", "set_layer_color"] = "set_color"
    layer_instance_id: str
    color: str | list[float | int]
    frame: int | None = None
    time: float | int | None = None

    _layer = field_validator("layer_instance_id")(
        lambda value: _identifier(value, label="layer_instance_id")
    )

    @field_validator("color")
    @classmethod
    def _color(cls, value: str | list[float | int]):
        if isinstance(value, str):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value):
                raise ValueError("color must be #RRGGBB or #RRGGBBAA")
            return value
        if not isinstance(value, list) or len(value) not in (3, 4):
            raise ValueError("color must contain three or four channels")
        return [
            _number(channel, label="color channel", minimum=0, maximum=1)
            for channel in value
        ]

    _frame = field_validator("frame")(
        lambda value: None if value is None else _integer(value, label="frame")
    )
    _time = field_validator("time")(
        lambda value: None if value is None else _number(value, label="time", minimum=0)
    )


class SetKeyframesOperation(_StrictRecord):
    kind: Literal["set_keyframes", "set_property_keyframes"] = "set_keyframes"
    layer_instance_id: str
    property_name: str = Field(
        validation_alias=AliasChoices("property", "property_name"),
        serialization_alias="property_name",
    )
    keyframes: list[Keyframe]

    _layer = field_validator("layer_instance_id")(
        lambda value: _identifier(value, label="layer_instance_id")
    )
    _property = field_validator("property_name")(
        lambda value: _string(value, label="property name")
    )

    @field_validator("keyframes")
    @classmethod
    def _keyframes(cls, value: list[Keyframe]) -> list[Keyframe]:
        if not value or len(value) > ABSOLUTE_NUMBER_CEILING:
            raise ValueError("keyframe list is outside its bounds")
        return value

    @property
    def property(self) -> str:
        return self.property_name


class SetTextOperation(_StrictRecord):
    kind: Literal["set_text", "set_layer_text"] = "set_text"
    layer_instance_id: str
    text: str
    font_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("font", "font_name"),
        serialization_alias="font_name",
    )

    _layer = field_validator("layer_instance_id")(
        lambda value: _identifier(value, label="layer_instance_id")
    )
    _text = field_validator("text")(
        lambda value: _string(value, label="text", nonempty=False)
    )
    _font = field_validator("font_name")(
        lambda value: None if value is None else _string(value, label="font name")
    )


class SetFontOperation(_StrictRecord):
    kind: Literal["set_font", "set_layer_font"] = "set_font"
    layer_instance_id: str
    font_name: str = Field(
        validation_alias=AliasChoices("font", "font_name"),
        serialization_alias="font_name",
    )

    _layer = field_validator("layer_instance_id")(
        lambda value: _identifier(value, label="layer_instance_id")
    )
    _font = field_validator("font_name")(
        lambda value: _string(value, label="font name")
    )


class SetEffectOperation(_StrictRecord):
    kind: Literal["set_effect", "add_effect", "set_layer_effect"] = "set_effect"
    layer_instance_id: str
    effect_name: str = Field(
        validation_alias=AliasChoices("effect", "effect_name"),
        serialization_alias="effect_name",
    )
    properties: dict[str, Any] = {}

    _layer = field_validator("layer_instance_id")(
        lambda value: _identifier(value, label="layer_instance_id")
    )
    _effect = field_validator("effect_name")(
        lambda value: _string(value, label="effect name")
    )
    _properties = field_validator("properties")(
        lambda value: _safe_json(value)
    )


class AddLayerOperation(_StrictRecord):
    kind: Literal["add_layer"] = "add_layer"
    layer_instance_id: str
    layer_type: Literal["text", "solid", "null"]
    name: str
    source_element_id: str | None = None
    parent_instance_id: str | None = None

    _layer = field_validator("layer_instance_id")(
        lambda value: _identifier(value, label="layer_instance_id")
    )
    _name = field_validator("name")(
        lambda value: _string(value, label="layer name")
    )
    _source = field_validator("source_element_id", "parent_instance_id")(
        lambda value: None if value is None else _identifier(value, label="layer id")
    )


class RemoveLayerOperation(_StrictRecord):
    kind: Literal["remove_layer"] = "remove_layer"
    layer_instance_id: str

    _layer = field_validator("layer_instance_id")(
        lambda value: _identifier(value, label="layer_instance_id")
    )


class SetVisibilityOperation(_StrictRecord):
    kind: Literal["set_visibility"] = "set_visibility"
    layer_instance_id: str
    visible: bool
    frame_start: int | None = None
    frame_end: int | None = None

    _layer = field_validator("layer_instance_id")(
        lambda value: _identifier(value, label="layer_instance_id")
    )
    _start = field_validator("frame_start", "frame_end")(
        lambda value: None if value is None else _integer(value, label="visibility frame")
    )

    @model_validator(mode="after")
    def _range(self) -> "SetVisibilityOperation":
        if self.frame_start is not None and self.frame_end is not None and self.frame_end < self.frame_start:
            raise ValueError("visibility frame range is reversed")
        return self


Operation: TypeAlias = Annotated[
    SetPropertyOperation
    | SetTransformOperation
    | SetOpacityOperation
    | SetColorOperation
    | SetKeyframesOperation
    | SetTextOperation
    | SetFontOperation
    | SetEffectOperation
    | AddLayerOperation
    | RemoveLayerOperation
    | SetVisibilityOperation,
    Field(discriminator="kind"),
]
# A readable alias for callers that prefer an explicit union name.
OperationUnion = Operation


class ApprovedCapabilities(_StrictRecord):
    """The exact catalog and digest approved for one AE render plan."""

    digest: str = Field(validation_alias=AliasChoices("digest", "capability_digest", "capability_hash"))
    fonts: tuple[str, ...] = Field(default=(), validation_alias=AliasChoices("fonts", "font_names"))
    effects: tuple[str, ...] = Field(default=(), validation_alias=AliasChoices("effects", "effect_names"))
    properties: dict[str, str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("properties", "property_schemas"),
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_catalog(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        raw = dict(value)
        digest_value = raw.get("digest")
        if not isinstance(digest_value, str):
            digest_value = raw.get("capability_digest") or raw.get("capability_hash")
        if digest_value is not None:
            raw["digest"] = digest_value
        for alias_name in ("capability_digest", "capability_hash"):
            raw.pop(alias_name, None)
        for field, alias in (("fonts", "font_names"), ("effects", "effect_names")):
            if field not in raw and alias in raw:
                raw[field] = raw[alias]
            raw.pop(alias, None)
        if "properties" not in raw and "property_schemas" in raw:
            raw["properties"] = raw["property_schemas"]
        raw.pop("property_schemas", None)
        for field, alias in (("fonts", "font_names"), ("effects", "effect_names")):
            items = raw.get(field, raw.get(alias, ()))
            if isinstance(items, Mapping):
                normalized: list[str] = []
                for name, metadata in items.items():
                    if isinstance(name, str):
                        normalized.append(name)
                    elif isinstance(metadata, Mapping):
                        candidate = metadata.get("family") or metadata.get("name") or metadata.get("match_name")
                        if isinstance(candidate, str):
                            normalized.append(candidate)
                raw[field] = tuple(normalized)
            elif isinstance(items, Sequence) and not isinstance(items, (str, bytes, bytearray)):
                names: list[str] = []
                for item in items:
                    if isinstance(item, str):
                        names.append(item)
                    elif isinstance(item, Mapping):
                        candidate = item.get("family") or item.get("name") or item.get("match_name")
                        if isinstance(candidate, str):
                            names.append(candidate)
                raw[field] = tuple(names)
        props = raw.get("properties", raw.get("property_schemas", {}))
        if isinstance(props, Sequence) and not isinstance(props, (str, bytes, bytearray)):
            properties: dict[str, str] = {}
            for item in props:
                if isinstance(item, str):
                    properties[item] = "unknown"
                elif isinstance(item, Mapping):
                    name = item.get("name") or item.get("match_name") or item.get("property")
                    if isinstance(name, str):
                        schema = item.get("type") or item.get("schema") or "unknown"
                        properties[name] = schema if isinstance(schema, str) else "unknown"
            props = properties
        elif isinstance(props, Mapping):
            props = {str(name): (schema if isinstance(schema, str) else "unknown") for name, schema in props.items()}
        raw["properties"] = props
        return raw

    _digest = field_validator("digest")(
        lambda value: _digest(value, label="capability digest")
    )

    @field_validator("fonts", "effects")
    @classmethod
    def _catalog_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_string(item, label="catalog name") for item in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("capability catalog contains duplicate names")
        return normalized

    @field_validator("properties")
    @classmethod
    def _property_catalog(cls, values: dict[str, str]) -> dict[str, str]:
        for name, schema in values.items():
            _string(name, label="property name")
            _string(schema, label="property schema")
            if schema.lower() not in _PROPERTY_SCHEMAS:
                raise ValueError(f"property schema is unavailable: {schema}")
        return values

    @property
    def capability_digest(self) -> str:
        return self.digest


# Compatibility names used by coordinator/connector callers.
CapabilityCatalog = ApprovedCapabilities
CapabilitySnapshot = ApprovedCapabilities


def _catalog(value: ApprovedCapabilities | Mapping[str, Any] | Any) -> ApprovedCapabilities:
    if isinstance(value, ApprovedCapabilities):
        return value
    if not isinstance(value, Mapping) and hasattr(value, "model_dump"):
        raw = value.model_dump(mode="json")
        allowed = {
            key: raw[key]
            for key in (
                "digest",
                "capability_digest",
                "capability_hash",
                "fonts",
                "font_names",
                "effects",
                "effect_names",
                "properties",
                "property_schemas",
            )
            if key in raw
        }
        value = allowed
    return ApprovedCapabilities.model_validate(value)


_TRANSFORM_PROPERTIES = {
    "position": "ADBE Position",
    "scale": "ADBE Scale",
    "scale_x": "ADBE Scale X",
    "scale_y": "ADBE Scale Y",
    "rotation": "ADBE Rotate Z",
    "skew": "ADBE Skew",
    "skew_x": "ADBE Skew",
    "skew_y": "ADBE Skew Axis",
}


_REQUIRED_EFFECTS = {
    "ADBE Fill Color": "ADBE Fill",
    "ADBE Fill-0002": "ADBE Fill",
}

_SUPPORTED_EFFECT_PROPERTIES = frozenset({"ADBE Gaussian Blur 2-0001", "ADBE Fill-0002"})
_SUPPORTED_DIRECT_PROPERTIES = frozenset(_TRANSFORM_PROPERTIES.values()) | frozenset(
    {"ADBE Opacity", "ADBE Fill Color"}
)
_EFFECT_PROPERTIES = {
    "ADBE Gaussian Blur 2": frozenset({"ADBE Gaussian Blur 2-0001"}),
    "ADBE Fill": frozenset({"ADBE Fill-0002"}),
}


def _operation_properties(operation: Any) -> list[str]:
    if isinstance(operation, (SetPropertyOperation, SetKeyframesOperation)):
        return [operation.property_name]
    if isinstance(operation, SetTransformOperation):
        return [_TRANSFORM_PROPERTIES[operation.property_name]]
    if isinstance(operation, SetOpacityOperation):
        return ["ADBE Opacity"]
    if isinstance(operation, SetColorOperation):
        return ["ADBE Fill Color"]
    if isinstance(operation, SetEffectOperation):
        return list(operation.properties)
    return []


def _validate_property_value(schema: str, value: Any, *, label: str) -> None:
    if label == "ADBE Opacity":
        if schema.lower() not in {"number", "float"}:
            raise OperationValidationError("ADBE Opacity schema must be numeric")
        _number(value, label=label, minimum=0, maximum=1)
        return
    normalized = schema.lower()
    if normalized in {"number", "float"}:
        _number(value, label=label)
        return
    if normalized in {"integer", "int"}:
        _integer(value, label=label, minimum=-ABSOLUTE_NUMBER_CEILING)
        return
    if normalized in {"boolean", "bool"}:
        if not isinstance(value, bool):
            raise OperationValidationError(f"{label} must be a boolean")
        return
    if normalized in {"string", "str", "enum"}:
        if not isinstance(value, str):
            raise OperationValidationError(f"{label} must be a string")
        _string(value, label=label, nonempty=False)
        return
    if normalized in {"color", "rgba"}:
        if isinstance(value, str):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value):
                raise OperationValidationError(f"{label} is not a valid color")
            return
        if not isinstance(value, list) or len(value) not in (3, 4):
            raise OperationValidationError(f"{label} must contain three or four channels")
        for channel in value:
            _number(channel, label=f"{label} channel", minimum=0, maximum=1)
        return
    dimensions = {"vec2": 2, "vector2": 2, "vec3": 3, "vector3": 3, "vec4": 4, "vector4": 4}
    size = dimensions.get(normalized)
    if size is not None:
        if not isinstance(value, list) or len(value) != size:
            raise OperationValidationError(f"{label} must contain {size} numbers")
        for item in value:
            _number(item, label=f"{label} component")
        return
    raise OperationValidationError(f"property schema is unavailable: {schema}")


def _check_catalog(batch: "OperationBatch", capabilities: ApprovedCapabilities) -> None:
    if batch.capability_digest != capabilities.digest:
        raise OperationValidationError("operation batch capability digest does not match the approved catalog")
    fonts = set(capabilities.fonts)
    effects = set(capabilities.effects)
    properties = capabilities.properties
    for operation in batch.operations:
        if isinstance(operation, (SetFontOperation, SetTextOperation)) and operation.font_name is not None:
            if operation.font_name not in fonts:
                raise OperationValidationError(f"font is not in the approved catalog: {operation.font_name}")
        if isinstance(operation, SetEffectOperation):
            if operation.effect_name not in effects:
                raise OperationValidationError(f"effect is not in the approved catalog: {operation.effect_name}")
            supported = _EFFECT_PROPERTIES.get(operation.effect_name)
            if supported is None:
                raise OperationValidationError(
                    f"effect is not in the fixed catalog: {operation.effect_name}"
                )
            if set(operation.properties) - supported:
                raise OperationValidationError(
                    "effect property does not match the fixed effect translation"
                )
        names = _operation_properties(operation)
        for property_name in names:
            required_effect = _REQUIRED_EFFECTS.get(property_name)
            if required_effect is not None and required_effect not in effects:
                raise OperationValidationError(
                    f"property {property_name} requires effect {required_effect}"
                )
            if property_name in _SUPPORTED_EFFECT_PROPERTIES and not isinstance(operation, SetEffectOperation):
                raise OperationValidationError("effect properties require set_effect")
            if (
                not isinstance(operation, SetEffectOperation)
                and property_name not in _SUPPORTED_DIRECT_PROPERTIES
            ):
                raise OperationValidationError(
                    f"property is not in the fixed catalog: {property_name}"
                )
            if isinstance(operation, SetEffectOperation) and property_name not in _SUPPORTED_EFFECT_PROPERTIES:
                raise OperationValidationError(f"effect property is not in the fixed catalog: {property_name}")
            schema = properties.get(property_name)
            if schema is None:
                raise OperationValidationError(f"property is not in the approved catalog: {property_name}")
            if isinstance(operation, SetEffectOperation):
                value = operation.properties[property_name]
                _validate_property_value(schema, value, label=property_name)
            elif isinstance(operation, SetKeyframesOperation):
                for keyframe in operation.keyframes:
                    _validate_property_value(schema, keyframe.value, label=property_name)
            elif isinstance(operation, SetColorOperation):
                _validate_property_value(schema, operation.color, label=property_name)
            elif isinstance(operation, SetOpacityOperation):
                _validate_property_value(schema, operation.opacity, label=property_name)
            elif isinstance(operation, SetTransformOperation):
                _validate_property_value(schema, operation.value, label=property_name)
            elif isinstance(operation, SetPropertyOperation):
                _validate_property_value(schema, operation.value, label=property_name)


def _check_scene_bounds(
    batch: "OperationBatch",
    scene_frame_count: int | None,
    duration: float | None,
    layer_count: int | None = None,
) -> None:
    if scene_frame_count is not None:
        _integer(scene_frame_count, label="scene frame count", minimum=1)
    if duration is not None:
        _number(duration, label="scene duration", minimum=0)
    if layer_count is not None:
        _integer(layer_count, label="layer count", minimum=0, maximum=MAX_LAYERS)
    frame_limit = scene_frame_count if scene_frame_count is not None else batch.scene_frame_count
    time_limit = duration if duration is not None else batch.duration
    layer_limit = batch.layer_count if layer_count is None else layer_count
    if layer_limit is not None and layer_limit > MAX_LAYERS:
        raise OperationValidationError("layer count exceeds 1000")
    estimated_layers = layer_limit
    added_layers = 0
    for operation in batch.operations:
        if isinstance(operation, AddLayerOperation):
            added_layers += 1
            if estimated_layers is None:
                if added_layers > MAX_LAYERS:
                    raise OperationValidationError("layer count exceeds 1000")
            else:
                estimated_layers += 1
                if estimated_layers > MAX_LAYERS:
                    raise OperationValidationError("layer count exceeds 1000")
        elif isinstance(operation, RemoveLayerOperation) and estimated_layers is not None:
            estimated_layers = max(0, estimated_layers - 1)
        if isinstance(operation, SetKeyframesOperation) and frame_limit is not None:
            if len(operation.keyframes) > frame_limit:
                raise OperationValidationError("keyframe count exceeds the scene frame count")
        values: list[tuple[str, Any]] = []
        for field_name in ("frame", "frame_start", "frame_end"):
            if hasattr(operation, field_name):
                values.append((field_name, getattr(operation, field_name)))
        if isinstance(operation, SetKeyframesOperation):
            values.extend(("keyframe frame", item.frame) for item in operation.keyframes)
            values.extend(("keyframe time", item.time) for item in operation.keyframes)
        if hasattr(operation, "time"):
            values.append(("time", getattr(operation, "time")))
        for label, value in values:
            if value is None:
                continue
            if label.endswith("frame") or label in {"frame", "frame_start", "frame_end"}:
                if frame_limit is not None and value >= frame_limit:
                    raise OperationValidationError(f"{label} is outside the scene frame count")
            elif time_limit is not None and float(value) > float(time_limit):
                raise OperationValidationError(f"{label} is outside the comp duration")


class OperationBatch(_StrictRecord):
    operations: list[Operation]
    capability_digest: str | None = Field(
        default=None,
        validation_alias=AliasChoices("capability_digest", "capability_hash"),
    )
    scene_frame_count: int | None = Field(default=None, validation_alias=AliasChoices("scene_frame_count", "frame_count"))
    layer_count: int | None = Field(default=None, validation_alias=AliasChoices("layer_count", "layers"))
    duration: float | int | None = None

    @field_validator("operations")
    @classmethod
    def _operations(cls, values: list[Operation]) -> list[Operation]:
        if len(values) > MAX_OPERATIONS:
            raise ValueError(f"an operation batch may contain at most {MAX_OPERATIONS} operations")
        return values

    _digest = field_validator("capability_digest")(
        lambda value: None if value is None else _digest(value, label="capability digest")
    )
    _frames = field_validator("scene_frame_count")(
        lambda value: None if value is None else _integer(value, label="scene frame count", minimum=1)
    )
    _duration = field_validator("duration")(
        lambda value: None if value is None else _number(value, label="duration", minimum=0)
    )

    _layers = field_validator("layer_count")(
        lambda value: None if value is None else _integer(value, label="layer count", minimum=0, maximum=MAX_LAYERS)
    )

    @model_validator(mode="after")
    def _batch_size(self) -> "OperationBatch":
        # Dump with aliases so the digest is identical to the JSON wire form.
        encoded = canonical_json(self.model_dump(mode="json", by_alias=True, exclude_none=True))
        if len(encoded) > MAX_RECORD_BYTES:
            raise ValueError("operation batch exceeds 1 MiB")
        _check_scene_bounds(self, self.scene_frame_count, self.duration, self.layer_count)
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json", by_alias=True, exclude_none=True))
        ).hexdigest()

    @property
    def payload_digest(self) -> str:
        return self.digest

    @property
    def operation_digest(self) -> str:
        return self.digest



def validate_operation_batch(
    value: OperationBatch | Mapping[str, Any],
    *,
    approved_capabilities: ApprovedCapabilities | Mapping[str, Any] | None = None,
    capability_digest: str | None = None,
    scene_frame_count: int | None = None,
    duration: float | None = None,
    layer_count: int | None = None,
) -> OperationBatch:
    """Parse and validate a batch against the exact approved catalog and bounds."""
    try:
        batch = value if isinstance(value, OperationBatch) else OperationBatch.model_validate(value)
    except ValidationError:
        raise
    if scene_frame_count is not None or duration is not None or layer_count is not None:
        _check_scene_bounds(batch, scene_frame_count, duration, layer_count)
    if approved_capabilities is not None:
        _check_catalog(batch, _catalog(approved_capabilities))
    elif capability_digest is not None:
        _digest(capability_digest, label="capability digest")
        if batch.capability_digest != capability_digest:
            raise OperationValidationError("operation batch capability digest does not match the approved digest")
    return batch


# Short aliases make the seam convenient without creating a second schema.
validate_batch = validate_operation_batch
parse_operation_batch = validate_operation_batch


__all__ = [
    "ABSOLUTE_NUMBER_CEILING",
    "ApprovedCapabilities",
    "CapabilityCatalog",
    "CapabilitySnapshot",
    "Keyframe",
    "MAX_LAYERS",
    "MAX_OPERATIONS",
    "MAX_RECORD_BYTES",
    "MAX_STRING_LENGTH",
    "Operation",
    "OperationBatch",
    "OperationUnion",
    "OperationValidationError",
    "SetColorOperation",
    "SetEffectOperation",
    "SetFontOperation",
    "SetKeyframesOperation",
    "SetLayerOperation",
    "SetOpacityOperation",
    "SetPropertyOperation",
    "SetTextOperation",
    "SetTransformOperation",
    "SetVisibilityOperation",
    "AddLayerOperation",
    "RemoveLayerOperation",
    "canonical_json",
    "canonical_operation_digest",
    "parse_operation_batch",
    "validate_batch",
    "validate_operation_batch",
]

# The aliases below intentionally point at the closed models rather than adding
# new discriminants.  They keep older connector code source-compatible.
SetLayerOperation = AddLayerOperation
