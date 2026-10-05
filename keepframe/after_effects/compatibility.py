from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from ..ir.schema import DEFAULTS, Element, FontGuess, Scene
from .models import AECapabilities, AECapabilityCatalog, AESubstitution
from .operations import LINEAR_WIPE_PROPERTIES


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_MAX_STRING_LENGTH = 4096
_MAX_LAYERS = 1000
_MAX_NUMBER = 1_000_000
_AE_MIN_INFLUENCE_PERCENT = 0.1
_AE_MIN_INFLUENCE = _AE_MIN_INFLUENCE_PERCENT / 100.0
_AE_MIN_DIMENSION = 4
_AE_MAX_DIMENSION = 30_000
_AE_MIN_FPS = 1.0
_AE_MAX_FPS = 99.0
_AE_MAX_DURATION = 10_800.0

# These are the only layer kinds that the mapper can construct from a
# substitution proposal.  In particular, a proposal cannot smuggle a camera,
# expression, or executable/script layer through a descriptive dictionary.
_LAYER_TYPES = frozenset({"text", "solid", "footage", "null"})
_LAYER_KEYS = frozenset({"layer_type", "name", "color", "texture", "font_name", "match_name"})
_EFFECT_KEYS = frozenset({"effect_name", "properties", "layer_index"})
_FIXED_EFFECTS = frozenset({"ADBE Gaussian Blur 2", "ADBE Fill"})


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AEFontResolutionError(ValueError):
    """The IR font does not identify one approved AE font record."""

    def __init__(self, message: str, *, reason: str, candidates: Sequence[str] = ()):
        super().__init__(message)
        self.reason = reason
        self.candidates = tuple(candidates)


def _safe_text(value: Any, *, label: str, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if len(value) > _MAX_STRING_LENGTH or (nonempty and not value):
        raise ValueError(f"{label} must be a bounded string")
    if "\x00" in value:
        raise ValueError(f"{label} contains NUL")
    return value


def _safe_identifier(value: Any, *, label: str) -> str:
    value = _safe_text(value, label=label)
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{label} is not a safe identifier")
    return value


def _safe_untrusted_text(value: Any, *, label: str, nonempty: bool = True) -> str:
    value = _safe_text(value, label=label, nonempty=nonempty)
    lowered = value.lower()
    if (
        _URL_RE.match(value)
        or value.startswith(("/", "\\", "./", "../"))
        or "\\" in value
        or "/../" in value
        or _DRIVE_RE.match(value)
        or any(token in lowered for token in ("app.project", "eval(", "function ", "#target", "<script", "javascript:"))
    ):
        raise ValueError(f"{label} contains an external path, URL, or code")
    return value


def _safe_asset_reference(value: Any, *, label: str = "texture") -> str:
    value = _safe_text(value, label=label)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains a control character")
    if not value or _URL_RE.match(value) or _DRIVE_RE.match(value):
        raise ValueError(f"{label} must be a scene-relative asset reference")
    if value.startswith(("/", "\\", "./", "../")) or "\\" in value:
        raise ValueError(f"{label} must be a scene-relative asset reference")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError(f"{label} must be a normalized scene-relative asset reference")
    return value


def _field_layout(track: Any) -> tuple[tuple[int, Any], ...]:
    return tuple((key.t, key.ease) for key in track.keys)

_AE_ANIMATED_TRACKS = frozenset({"x", "y", "sx", "sy", "rot", "skx", "sky", "opacity", "reveal", "rx", "ry"})


def _ease_is_mapper_safe(ease: Any) -> bool:
    if ease is None or ease == (0.0, 0.0, 1.0, 1.0):
        return True
    try:
        x1, y1, x2, y2 = (float(part) for part in ease)
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(part) and abs(part) <= _MAX_NUMBER for part in (x1, y1, x2, y2))
        and _AE_MIN_INFLUENCE <= x1 <= x2 <= 1.0 - _AE_MIN_INFLUENCE
    )


class AECompatibilityIssue(_StrictFrozen):
    """One deterministic semantic that the AE baseline cannot preserve."""

    source_element_id: str
    source_type: str
    semantic_key: str
    reason: str
    lost_semantics: tuple[str, ...]

    _source_element_id = field_validator("source_element_id")(
        lambda value: _safe_identifier(value, label="source element id")
    )
    _source_type = field_validator("source_type")(
        lambda value: _safe_identifier(value, label="source type")
    )
    _semantic_key = field_validator("semantic_key")(
        lambda value: _safe_identifier(value, label="semantic key")
    )
    _reason = field_validator("reason")(
        lambda value: _safe_untrusted_text(value, label="reason")
    )

    @field_validator("lost_semantics")
    @classmethod
    def _lost(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("lost semantics must not be empty")
        normalized = tuple(_safe_identifier(value, label="lost semantic") for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("lost semantics must not contain duplicates")
        return normalized


def _catalog(value: AECapabilities | AECapabilityCatalog) -> AECapabilityCatalog:
    if isinstance(value, AECapabilities):
        return value.capabilities
    if isinstance(value, AECapabilityCatalog):
        return value
    raise TypeError("capabilities must be AECapabilities or AECapabilityCatalog")


_FONT_WEIGHT_TOKENS = (
    ("ultrablack", 900),
    ("extrablack", 900),
    ("ultrabold", 800),
    ("extrabold", 800),
    ("ultralight", 200),
    ("extralight", 200),
    ("semibold", 600),
    ("demibold", 600),
    ("medium", 500),
    ("black", 900),
    ("heavy", 900),
    ("bold", 700),
    ("light", 300),
    ("thin", 100),
)


def _font_style_weight(style: str) -> int | None:
    normalized = re.sub(r"[\s_-]+", "", style.lower())
    match = re.search(r"(?<!\d)([1-9]00)(?!\d)", normalized)
    if match:
        return int(match.group(1))
    for token, weight in _FONT_WEIGHT_TOKENS:
        if token in normalized:
            return weight
    if normalized in {"regular", "normal", "roman", "book", "plain"}:
        return 400
    if normalized.endswith(("italic", "oblique")):
        return 400
    return None


def resolve_ae_font(
    font: FontGuess,
    capabilities: AECapabilities | AECapabilityCatalog,
) -> str:
    """Resolve one IR font guess to the exact approved AE match name."""

    if not isinstance(font, FontGuess):
        raise TypeError("font must be a FontGuess")
    catalog = _catalog(capabilities)
    family = font.family_guess
    weight = font.weight
    candidates = [
        record
        for record in catalog.fonts
        if record.family == family and _font_style_weight(record.style) == weight
    ]
    # Older snapshots only contained the PostScript name. It is safe to
    # retain that exact identity for the default weight, but never guess a
    # non-default weight from an unstyled record.
    if not candidates and weight == 400:
        candidates = [
            record
            for record in catalog.fonts
            if not record.family and not record.style and record.match_name == family
        ]
    if len(candidates) == 1:
        return candidates[0].match_name
    if len(candidates) > 1:
        names = tuple(record.match_name for record in candidates)
        raise AEFontResolutionError(
            f"font family/style is ambiguous for {family!r} weight {weight}: {names}",
            reason="ambiguous",
            candidates=names,
        )
    raise AEFontResolutionError(
        f"no exact AE font match for {family!r} weight {weight}",
        reason="missing",
    )


def _nonopaque_color(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _COLOR_RE.fullmatch(value) is not None
        and len(value) == 9
        and value[-2:].lower() != "ff"
    )


def _ease_failure_reason(start: Any, end: Any, fps: float, delta_scale: float = 1.0) -> str | None:
    ease = start.ease
    if ease is None or ease == (0.0, 0.0, 1.0, 1.0):
        return None
    if not _ease_is_mapper_safe(ease):
        return "temporal easing influence endpoints must be at least 0.1% and controls must be finite and monotone"
    try:
        x1, y1, x2, y2 = (float(part) for part in ease)
        frame_delta = end.t - start.t
        delta = (float(end.v) - float(start.v)) * delta_scale
        if frame_delta <= 0 or not math.isfinite(float(fps)) or fps <= 0:
            return "temporal easing segment has no positive frame duration"
        scale = delta * fps / float(frame_delta)
        outgoing_speed = scale * y1 / x1
        incoming_speed = scale * (1.0 - y2) / (1.0 - x2)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return "temporal easing speed is outside AE bounds"
    if not all(
        math.isfinite(speed) and abs(speed) <= _MAX_NUMBER
        for speed in (outgoing_speed, incoming_speed)
    ):
        return "temporal easing speed is outside AE bounds"
    return None


def _issue(
    element_id: str,
    source_type: str,
    semantic_key: str,
    reason: str,
    *lost_semantics: str,
) -> AECompatibilityIssue:
    # The key is included in lost_semantics so a proposal can be matched to one
    # issue even when an element has more than one independent converter gap.
    return AECompatibilityIssue(
        source_element_id=element_id,
        source_type=source_type,
        semantic_key=semantic_key,
        reason=reason,
        lost_semantics=(semantic_key, *lost_semantics),
    )


def _element_issues(
    element: Element,
    catalog: AECapabilityCatalog,
    fps: float,
) -> list[AECompatibilityIssue]:
    issues: list[AECompatibilityIssue] = []
    lost_spin = tuple(
        prop for prop in ("rx", "ry")
        if prop in element.tracks and any(key.v != 0.0 for key in element.tracks[prop].keys)
    )
    if element.kind == "3d":
        if not catalog.model_layers:
            issues.append(_issue(element.id, element.kind, "3d", "AE 24.1 or newer model_layers capability is unavailable; propose static texture footage", *lost_spin))
        elif not element.canonical.model or not element.canonical.model.lower().endswith(".glb"):
            issues.append(_issue(element.id, element.kind, "3d", "3D element requires a GLB model asset; propose static texture footage", *lost_spin))
        elif any(catalog.property_schemas.get(name) not in {"number", "float"} for name in ("ADBE Rotate X", "ADBE Rotate Y")):
            issues.append(_issue(element.id, element.kind, "3d", "model layers require approved AE Rotate X/Y schemas; propose static texture footage", *lost_spin))
    reveal = element.tracks.get("reveal")
    if reveal is not None and any(key.v != 1.0 for key in reveal.keys):
        if any(not 0 <= key.v <= 1 for key in reveal.keys):
            issues.append(_issue(element.id, element.kind, "reveal", "reveal visible fraction must be between zero and one"))
        elif "ADBE Linear Wipe" not in catalog.effect_names or any(
            catalog.property_schemas.get(name) not in {"number", "float"}
            for name in LINEAR_WIPE_PROPERTIES
        ):
            issues.append(_issue(element.id, element.kind, "reveal", "reveal requires the approved AE Linear Wipe effect and property schemas"))
    if element.kind == "3d":
        skx = element.tracks.get("skx")
        if skx is not None and any(key.v != 0.0 for key in skx.keys):
            issues.append(_issue(element.id, element.kind, "skx", "AE model layers do not support X skew"))

    if element.kind == "group":
        non_neutral = any(
            property_name in DEFAULTS
            and any(
                not math.isfinite(float(key.v))
                or abs(float(key.v) - DEFAULTS[property_name]) > 1e-12
                or key.ease is not None
                for key in track.keys
            )
            for property_name, track in element.tracks.items()
        )
        if non_neutral:
            issues.append(
                _issue(
                    element.id,
                    element.kind,
                    "group_transform",
                    "group transforms must remain neutral AE null transforms",
                )
            )

    if element.kind == "text":
        canonical = element.canonical
        usable_text = isinstance(canonical.text, str) and bool(canonical.text)
        if usable_text and canonical.font is not None:
            try:
                resolve_ae_font(canonical.font, catalog)
            except AEFontResolutionError as exc:
                issues.append(
                    _issue(
                        element.id,
                        element.kind,
                        "font",
                        f"text font has no exact approved AE match: {exc}",
                    )
                )
        elif not canonical.texture:
            issues.append(_issue(element.id, element.kind, "text", "text has no usable font/text or texture fallback"))

    if _nonopaque_color(element.canonical.color):
        issues.append(
            _issue(
                element.id,
                element.kind,
                "alpha",
                "non-opaque source color cannot be preserved by the AE pipeline",
            )
        )

    if element.kind in {"sprite", "ui"} and not element.canonical.texture:
        issues.append(_issue(element.id, element.kind, "texture", "sprite or UI element has no required texture"))

    sx = element.tracks.get("sx")
    sy = element.tracks.get("sy")
    if sx is not None and sy is not None and _field_layout(sx) != _field_layout(sy):
        issues.append(
            _issue(
                element.id,
                element.kind,
                "scale",
                "independent scale tracks have different keyframe timing or easing",
            )
        )

    for property_name, track in element.tracks.items():
        if property_name not in _AE_ANIMATED_TRACKS:
            continue
        if property_name in {"rx", "ry"} and element.kind != "3d":
            continue
        reason: str | None = None
        for index, key in enumerate(track.keys):
            if key.ease is None:
                continue
            if index + 1 < len(track.keys):
                reason = _ease_failure_reason(
                    key,
                    track.keys[index + 1],
                    fps,
                    100.0 if property_name in {"sx", "sy", "reveal"} else 1.0,
                )
            elif not _ease_is_mapper_safe(key.ease):
                reason = "temporal easing influence endpoints must be at least 0.1% and controls must be finite and monotone"
            if reason is not None:
                break
        if reason is not None:
            issues.append(_issue(element.id, element.kind, "easing", reason))

    sky = element.tracks.get("sky")
    if sky is not None and any(key.v != 0 for key in sky.keys):
        issues.append(_issue(element.id, element.kind, "sky", "nonzero Y skew has no fixed AE mapping"))
    return issues


def _composition_issue(scene: Scene) -> AECompatibilityIssue | None:
    problems: list[str] = []
    size = scene.size
    if (
        not isinstance(size, (tuple, list))
        or len(size) != 2
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not _AE_MIN_DIMENSION <= value <= _AE_MAX_DIMENSION
            for value in size
        )
    ):
        problems.append("width/height must be between 4 and 30000")

    fps = scene.fps
    fps_valid = (
        not isinstance(fps, bool)
        and isinstance(fps, (int, float))
        and math.isfinite(float(fps))
        and _AE_MIN_FPS <= float(fps) <= _AE_MAX_FPS
    )
    if not fps_valid:
        problems.append("fps must be between 1 and 99")

    frames = scene.frames
    frames_valid = (
        isinstance(frames, int)
        and not isinstance(frames, bool)
        and 1 <= frames <= _MAX_NUMBER
    )
    if not frames_valid:
        problems.append(f"frame count must be between 1 and {_MAX_NUMBER}")
    elif fps_valid:
        try:
            exceeds_duration = frames > _AE_MAX_DURATION * float(fps)
        except OverflowError:
            exceeds_duration = True
        if exceeds_duration:
            problems.append("duration must be at most 10800 seconds")
    if not problems:
        return None
    return _issue(
        scene.id,
        "scene",
        "composition",
        "AE composition limits: " + "; ".join(problems),
        "scene_composition",
    )


def analyze_ae_compatibility(
    scene: Scene,
    capabilities: AECapabilities | AECapabilityCatalog,
) -> tuple[AECompatibilityIssue, ...]:
    """Return deterministic converter gaps in scene order."""

    if not isinstance(scene, Scene):
        raise TypeError("scene must be a Scene")
    _safe_identifier(scene.id, label="scene id")
    for element in scene.elements:
        _safe_identifier(element.id, label="source element id")
    catalog = _catalog(capabilities)
    issues: list[AECompatibilityIssue] = []
    composition_issue = _composition_issue(scene)
    if composition_issue is not None:
        issues.append(composition_issue)
    if scene.background.kind == "color" and _nonopaque_color(scene.background.value):
        issues.append(
            _issue(
                scene.id,
                "background",
                "alpha",
                "non-opaque source background color cannot be preserved by the AE pipeline",
            )
        )
    if scene.background.kind == "image":
        try:
            _safe_asset_reference(scene.background.value, label="background image")
        except ValueError:
            issues.append(
                _issue(
                    scene.id,
                    "background",
                    "background",
                    "image background has no fixed scene-relative asset mapping",
                )
            )
    fps = float(scene.fps) if isinstance(scene.fps, (int, float)) and math.isfinite(float(scene.fps)) else 0.0
    for element in scene.elements:
        issues.extend(_element_issues(element, catalog, fps))
    return tuple(issues)


def _validated_layer(
    value: Any,
    catalog: AECapabilityCatalog | None = None,
) -> str | dict[str, Any]:
    if isinstance(value, str):
        if value not in _LAYER_TYPES:
            raise ValueError(f"unsupported substitution layer type: {value}")
        if value == "text":
            raise ValueError("text substitution layer requires an exact approved font_name")
        return value
    if not isinstance(value, Mapping):
        raise ValueError("substitution layer must be a fixed layer type or object")
    raw = dict(value)
    if set(raw) - _LAYER_KEYS or not {"layer_type", "name"}.issubset(raw):
        raise ValueError("substitution layer has unsupported fields")
    layer_type = raw.get("layer_type")
    if layer_type not in _LAYER_TYPES:
        raise ValueError("substitution layer type is unavailable")
    name = _safe_untrusted_text(raw.get("name"), label="substitution layer name")
    normalized: dict[str, Any] = {"layer_type": layer_type, "name": name}

    font_fields = tuple(field for field in ("font_name", "match_name") if field in raw)
    if layer_type == "text":
        if not font_fields:
            raise ValueError("text substitution layer requires an exact approved font_name")
        if len(font_fields) == 2 and raw["font_name"] != raw["match_name"]:
            raise ValueError("text substitution font_name and match_name must agree")
        if catalog is None:
            raise ValueError("text substitution layer requires an approved font catalog")
        match_name = _safe_text(raw[font_fields[0]], label="substitution font_name")
        records = [font for font in catalog.fonts if font.match_name == match_name]
        if len(records) != 1:
            raise ValueError("text substitution font_name is not in the approved catalog")
        normalized["font_name"] = records[0].match_name
    elif font_fields:
        raise ValueError("only text substitution layers may carry a font_name")

    if "color" in raw:
        color = raw["color"]
        if not isinstance(color, str) or not _COLOR_RE.fullmatch(color):
            raise ValueError("substitution layer color is invalid")
        if _nonopaque_color(color):
            raise ValueError("substitution layer alpha cannot be preserved")
        normalized["color"] = color[:7].upper()
    if "texture" in raw:
        if layer_type != "footage":
            raise ValueError("only footage substitution layers may carry texture")
        normalized["texture"] = _safe_asset_reference(raw["texture"])
    return normalized


def _number(value: Any, *, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a JSON number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} is outside finite bounds") from exc
    if not math.isfinite(number) or abs(number) > _MAX_NUMBER:
        raise ValueError(f"{label} is outside finite bounds")
    return value


def _property_value(value: Any, schema: str, *, label: str) -> Any:
    normalized = schema.lower()
    if normalized in {"number", "float"}:
        return _number(value, label=label)
    if normalized in {"integer", "int"}:
        if isinstance(value, bool) or not isinstance(value, int) or abs(value) > _MAX_NUMBER:
            raise ValueError(f"{label} must be a bounded integer")
        return value
    if normalized in {"boolean", "bool"}:
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be boolean")
        return value
    if normalized in {"string", "str", "enum"}:
        return _safe_untrusted_text(value, label=label, nonempty=False)
    if normalized in {"color", "rgba"}:
        if isinstance(value, str):
            if not _COLOR_RE.fullmatch(value):
                raise ValueError(f"{label} must be a fixed color")
            if _nonopaque_color(value):
                raise ValueError(f"{label} alpha cannot be preserved")
            return value[:7].upper()
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError(f"{label} must contain three RGB channels")
        channels = [_number(channel, label=f"{label} channel") for channel in value]
        if any(float(channel) < 0 or float(channel) > 1 for channel in channels):
            raise ValueError(f"{label} channel is outside bounds")
        return channels
    dimensions = {"vec2": 2, "vector2": 2, "vec3": 3, "vector3": 3, "vec4": 4, "vector4": 4}
    size = dimensions.get(normalized)
    if size is not None:
        if not isinstance(value, list) or len(value) != size:
            raise ValueError(f"{label} must contain {size} numbers")
        return [_number(item, label=f"{label} component") for item in value]
    raise ValueError(f"approved property schema is unavailable: {schema}")


def _validated_effect(value: Any, catalog: AECapabilityCatalog, layer_count: int) -> str | dict[str, Any]:
    effect_names = set(catalog.effect_names)
    if isinstance(value, str):
        if value not in effect_names or value not in _FIXED_EFFECTS:
            raise ValueError(f"effect is not in the fixed approved catalog: {value}")
        return value
    if not isinstance(value, Mapping):
        raise ValueError("substitution effect must be an approved name or object")
    raw = dict(value)
    if set(raw) - _EFFECT_KEYS or not {"effect_name", "properties"}.issubset(raw):
        raise ValueError("substitution effect has unsupported fields")
    name = raw["effect_name"]
    if name not in effect_names or name not in _FIXED_EFFECTS:
        raise ValueError(f"effect is not in the fixed approved catalog: {name}")
    metadata = next((effect for effect in catalog.effects if effect.match_name == name), None)
    if metadata is None:
        raise ValueError(f"effect metadata is unavailable: {name}")
    properties = raw["properties"]
    if not isinstance(properties, Mapping):
        raise ValueError("effect properties must be an object")
    normalized_properties: dict[str, Any] = {}
    for property_name, property_value in properties.items():
        if not isinstance(property_name, str) or property_name not in metadata.properties:
            raise ValueError(f"effect property is not in the approved effect catalog: {property_name}")
        normalized_properties[property_name] = _property_value(
            property_value,
            metadata.properties[property_name],
            label=f"effect property {property_name}",
        )
    normalized: dict[str, Any] = {"effect_name": name, "properties": normalized_properties}
    layer_index = raw.get("layer_index", 0)
    if isinstance(layer_index, bool) or not isinstance(layer_index, int) or not 0 <= layer_index < layer_count:
        raise ValueError("effect layer_index is outside proposed layer bounds")
    if "layer_index" in raw:
        normalized["layer_index"] = layer_index
    return normalized


def _proposal_items(payload: Any) -> list[Any]:
    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > 1 * 1024 * 1024:
            raise ValueError("AE substitution response exceeds 1 MiB")
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("AE substitution response is not valid JSON") from exc
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        keys = set(payload)
        if keys == {"substitutions"} and isinstance(payload["substitutions"], list):
            return payload["substitutions"]
        # A single AESubstitution is a useful response shape, but no wrapper
        # with arbitrary fields is accepted.
        return [payload]
    raise ValueError("AE substitution response must be a JSON array or object")


def _wire_record(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    record = dict(value)
    for field in ("proposed_layers", "proposed_effects", "lost_semantics"):
        if isinstance(record.get(field), list):
            record[field] = tuple(record[field])
    return record


def parse_ae_substitutions(
    payload: Any,
    issues: Sequence[AECompatibilityIssue],
    capabilities: AECapabilities | AECapabilityCatalog,
) -> tuple[AESubstitution, ...]:
    """Validate one model proposal for every deterministic compatibility issue."""

    issue_list = tuple(
        issue
        if isinstance(issue, AECompatibilityIssue)
        else AECompatibilityIssue.model_validate(_wire_record(issue))
        for issue in issues
    )
    items = _proposal_items(payload)
    if len(items) > len(issue_list):
        raise ValueError("substitution response contains extra issues")
    catalog = _catalog(capabilities)
    proposals: list[AESubstitution] = []
    matched: set[int] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("each substitution proposal must be an object")
        raw = _wire_record(item)
        proposal = AESubstitution.model_validate(raw)
        if proposal.acknowledged:
            raise ValueError("model substitutions must be unacknowledged until user approval")
        if proposal.reason is not None:
            _safe_untrusted_text(proposal.reason, label="substitution reason")
        for semantic in proposal.lost_semantics:
            _safe_untrusted_text(semantic, label="lost semantic")
        layers = tuple(_validated_layer(layer, catalog) for layer in proposal.proposed_layers)
        if not layers or len(layers) > _MAX_LAYERS:
            raise ValueError("substitution must contain at least one bounded layer")
        effects = tuple(_validated_effect(effect, catalog, len(layers)) for effect in proposal.proposed_effects)
        lost = set(proposal.lost_semantics)
        candidates = [
            index
            for index, issue in enumerate(issue_list)
            if index not in matched
            and proposal.source_element_id == issue.source_element_id
            and proposal.source_type == issue.source_type
            and set(issue.lost_semantics).issubset(lost)
            and issue.semantic_key in lost
        ]
        if len(candidates) != 1:
            if not candidates:
                raise ValueError("substitution does not match an outstanding compatibility issue")
            raise ValueError("substitution matches multiple compatibility issues")
        matched.add(candidates[0])
        proposals.append(
            proposal.model_copy(update={"proposed_layers": layers, "proposed_effects": effects})
        )
    if len(matched) != len(issue_list):
        raise ValueError("substitution response must contain exactly one proposal per issue")
    return tuple(proposals)


def propose_ae_substitutions(
    client: Any,
    issues_or_scene: Sequence[AECompatibilityIssue] | Scene,
    capabilities: AECapabilities | AECapabilityCatalog,
) -> tuple[AESubstitution, ...]:
    """Propose static 3D textures, then ask the client for remaining gaps."""

    static: list[AESubstitution] = []
    if isinstance(issues_or_scene, Scene):
        issues = analyze_ae_compatibility(issues_or_scene, capabilities)
        elements = {element.id: element for element in issues_or_scene.elements}
        for issue in issues:
            element = elements.get(issue.source_element_id)
            if element is not None and element.kind == "3d" and issue.semantic_key == "3d" and element.canonical.texture:
                texture = _safe_asset_reference(element.canonical.texture)
                static.append(AESubstitution(
                    source_element_id=element.id, source_type=element.kind,
                    proposed_layers=({"layer_type": "footage", "name": element.id, "texture": texture},),
                    lost_semantics=issue.lost_semantics,
                    reason="Use the static texture as footage; model depth and X/Y spin are lost",
                ))
    else:
        issues = tuple(
            issue
            if isinstance(issue, AECompatibilityIssue)
            else AECompatibilityIssue.model_validate(_wire_record(issue))
            for issue in issues_or_scene
        )
    if not issues:
        return ()
    all_issues = issues
    issues = tuple(
        issue for issue in issues if not any(
            proposal.source_element_id == issue.source_element_id
            and issue.semantic_key in proposal.lost_semantics
            for proposal in static
        )
    )
    if not issues:
        return parse_ae_substitutions([proposal.model_dump() for proposal in static], all_issues, capabilities)
    if client is None:
        raise ValueError("an LLM client is required for AE compatibility proposals")
    catalog = _catalog(capabilities)
    request = {
        "issues": [issue.model_dump(mode="json") for issue in issues],
        "capabilities": {
            "font_names": list(catalog.font_names),
            "effect_names": list(catalog.effect_names),
            "property_schemas": dict(sorted(catalog.property_schemas.items())),
            "effects": [
                {
                    "match_name": effect.match_name,
                    "properties": dict(sorted(effect.properties.items())),
                }
                for effect in catalog.effects
            ],
        },
    }
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    messages = [
        {
            "role": "system",
            "content": "Return exactly one JSON array or object of AE substitution proposals. Use only the supplied fixed layer/effect vocabulary.",
        },
        {"role": "user", "content": encoded},
    ]
    reply = client.complete(messages, tools=[])
    content = getattr(reply, "content", reply)
    if not isinstance(content, str):
        raise ValueError("AE substitution response content must be text JSON")
    if len(content.encode("utf-8")) > 1 * 1024 * 1024:
        raise ValueError("AE substitution response exceeds 1 MiB")
    try:
        decoded = _extract_json(content)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("AE substitution response is not valid JSON") from exc
    generated = parse_ae_substitutions(decoded, issues, capabilities)
    static_issues = tuple(issue for issue in all_issues if issue not in issues)
    validated_static = parse_ae_substitutions([proposal.model_dump() for proposal in static], static_issues, capabilities)
    combined = [*validated_static, *generated]
    return tuple(next(proposal for proposal in combined if proposal.source_element_id == issue.source_element_id and issue.semantic_key in proposal.lost_semantics) for issue in all_issues)


def _extract_json(content: str) -> Any:
    content = content.strip()
    if not content:
        raise ValueError("empty response")
    value = json.loads(content)
    if not isinstance(value, (list, dict)):
        raise ValueError("response must contain one JSON array or object")
    return value


# Explicit names for callers that prefer parser/validator terminology.
validate_ae_substitutions = parse_ae_substitutions
request_ae_substitutions = propose_ae_substitutions


__all__ = [
    "AECompatibilityIssue",
    "AEFontResolutionError",
    "analyze_ae_compatibility",
    "parse_ae_substitutions",
    "propose_ae_substitutions",
    "request_ae_substitutions",
    "resolve_ae_font",
    "validate_ae_substitutions",
]
