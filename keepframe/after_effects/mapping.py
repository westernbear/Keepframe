from __future__ import annotations

"""Deterministic IR to After Effects baseline operation mapping.

The mapper is deliberately a pure server-side function.  It only consumes the
already pinned :class:`PlanAsset` manifest and emits closed operation records;
it never opens scene files or emits paths, expressions, or ExtendScript.
"""

import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ..ir.schema import DEFAULTS, PROPS, Element, Scene, Track
from ..ir.tracks import eval_z
from ..render.plan import PlanAsset
from .compatibility import _safe_asset_reference
from .models import AECapabilities, AESubstitution
from .operations import (
    MAX_LAYERS,
    MAX_OPERATIONS,
    MAX_SOLID_DIMENSION,
    MIN_SOLID_DIMENSION,
    LINEAR_WIPE_PROPERTIES,
    ApprovedCapabilities,
    AddLayerOperation,
    Keyframe as AEKeyframe,
    Operation,
    OperationBatch,
    SetEffectOperation,
    SetKeyframesOperation,
    SetOpacityOperation,
    SetTextOperation,
    SetTransformOperation,
    SetVisibilityOperation,
    validate_operation_batch,
)


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_LAYER_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")
_MIN_COMP_DIMENSION = 4
_MAX_COMP_DIMENSION = 30_000
_MIN_COMP_FPS = 1.0
_MAX_COMP_FPS = 99.0
_MAX_COMP_DURATION = 10_800.0
_MAX_NUMBER = 1_000_000.0
LINEAR_WIPE_ANGLE = 270.0


class AEMappingError(ValueError):
    """Raised when IR cannot be represented by the approved AE contract."""


# A shorter name is useful to callers that do not need the AE prefix.
MappingError = AEMappingError


class _FrozenRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CompositionSettings(_FrozenRecord):
    width: int
    height: int
    frame_rate: float
    duration: float
    background_color: str

    @field_validator("width", "height")
    @classmethod
    def _dimension(cls, value: int) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not _MIN_COMP_DIMENSION <= value <= _MAX_COMP_DIMENSION
        ):
            raise ValueError(
                f"composition dimensions must be between {_MIN_COMP_DIMENSION} "
                f"and {_MAX_COMP_DIMENSION}"
            )
        return value

    @field_validator("frame_rate")
    @classmethod
    def _frame_rate(cls, value: float) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not _MIN_COMP_FPS <= float(value) <= _MAX_COMP_FPS
        ):
            raise ValueError(
                f"composition fps must be between {_MIN_COMP_FPS:g} and {_MAX_COMP_FPS:g}"
            )
        return float(value)

    @field_validator("duration")
    @classmethod
    def _duration(cls, value: float) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 < float(value) <= _MAX_COMP_DURATION
        ):
            raise ValueError(
                f"composition duration must be positive and at most {_MAX_COMP_DURATION:g} seconds"
            )
        return float(value)

    @field_validator("background_color")
    @classmethod
    def _background(cls, value: str) -> str:
        if not isinstance(value, str) or _COLOR_RE.fullmatch(value) is None:
            raise ValueError("composition background must be #RRGGBB")
        return value.upper()

    @property
    def fps(self) -> float:
        return self.frame_rate


class LayerInventory(_FrozenRecord):
    """One mapped layer and its exact half-open active frame interval."""

    instance_id: str
    source_element_id: str | None
    frame_start: int
    frame_end: int

    @field_validator("instance_id")
    @classmethod
    def _instance(cls, value: str) -> str:
        if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
            raise ValueError("layer instance id is invalid")
        return value

    @field_validator("source_element_id")
    @classmethod
    def _source(cls, value: str | None) -> str | None:
        if value is not None and (not isinstance(value, str) or _ID_RE.fullmatch(value) is None):
            raise ValueError("layer source id is invalid")
        return value

    @field_validator("frame_start", "frame_end")
    @classmethod
    def _frame(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _MAX_NUMBER:
            raise ValueError("layer frame is outside bounds")
        return value

    @model_validator(mode="after")
    def _interval(self) -> "LayerInventory":
        if self.frame_end <= self.frame_start:
            raise ValueError("layer interval must be nonempty and half-open")
        return self

    @property
    def layer_instance_id(self) -> str:
        return self.instance_id

    @property
    def source_id(self) -> str | None:
        return self.source_element_id

    @property
    def active_interval(self) -> tuple[int, int]:
        return (self.frame_start, self.frame_end)


class BaselineBatch(_FrozenRecord):
    """A validated baseline operation chunk and its starting inventory."""

    index: int
    capability_digest: str
    scene_frame_count: int
    duration: float
    operations: tuple[Operation, ...]
    layer_sources: dict[str, str | None]
    layer_count: int

    @field_validator("index", "layer_count")
    @classmethod
    def _bounded_integer(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_LAYERS:
            raise ValueError("baseline batch integer is outside bounds")
        return value

    @field_validator("operations")
    @classmethod
    def _operation_count(cls, value: tuple[Operation, ...]) -> tuple[Operation, ...]:
        if not value or len(value) > MAX_OPERATIONS:
            raise ValueError("baseline batch operation count is outside bounds")
        return value

    @model_validator(mode="after")
    def _inventory_count(self) -> "BaselineBatch":
        if self.layer_count != len(self.layer_sources):
            raise ValueError("baseline batch inventory count does not match layer_count")
        return self

    @property
    def start_layer_sources(self) -> dict[str, str | None]:
        return dict(self.layer_sources)

    def to_payload(self) -> dict[str, Any]:
        return OperationBatch(
            operations=list(self.operations),
            capability_digest=self.capability_digest,
            scene_frame_count=self.scene_frame_count,
            duration=self.duration,
            layer_count=self.layer_count,
        ).model_dump(mode="json", by_alias=True, exclude_none=True)


class BaselineMapping(_FrozenRecord):
    """Complete immutable result of :func:`map_baseline`."""

    composition: CompositionSettings
    imported_asset_ids: tuple[str, ...]
    batches: tuple[BaselineBatch, ...]
    final_inventory: tuple[LayerInventory, ...]

    @property
    def comp(self) -> CompositionSettings:
        return self.composition

    @property
    def imports(self) -> tuple[str, ...]:
        return self.imported_asset_ids

    @property
    def baseline_batches(self) -> tuple[BaselineBatch, ...]:
        return self.batches
    @property
    def layer_inventory(self) -> tuple[LayerInventory, ...]:
        return self.final_inventory

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return self.imported_asset_ids

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# Compatibility aliases for callers that use the longer nouns from the render
# plan contract.  They are aliases, not additional schemas.
AECompositionSettings = CompositionSettings
AELayerInventory = LayerInventory
AEBaselineBatch = BaselineBatch
AEBaselineMapping = BaselineMapping


@dataclass(frozen=True)
class _AssetResolver:
    prefix: str
    by_path: Mapping[str, PlanAsset]
    imported: list[str]

    def resolve(self, reference: str, *, label: str) -> str:
        relative = _relative_reference(reference, label=label)
        path = "/".join(part for part in (self.prefix, relative) if part)
        asset = self.by_path.get(path)
        if asset is None:
            raise AEMappingError(f"{label} does not match a pinned plan asset: {path}")
        if asset.id not in self.imported:
            self.imported.append(asset.id)
        return asset.id



@dataclass(frozen=True)
class _SourceSegment:
    interval_index: int
    frame_start: int
    frame_end: int
    sample_frame: int


@dataclass(frozen=True)
class _Representation:
    source_id: str
    source_kind: str
    element: Element | None
    layer_type: str
    name: str
    frame_start: int
    frame_end: int
    segment_index: int
    interval_index: int
    substitution_index: int
    scene_order: int
    z: int
    substituted: bool = False
    parent_source_id: str | None = None
    asset_id: str | None = None
    width: int | None = None
    height: int | None = None
    color: str | None = None
    text: str | None = None
    font_name: str | None = None
    font_size: float | None = None
    text_color: str | None = None
    effects: tuple[Any, ...] = ()
    role: str = "element"

    @property
    def instance_id(self) -> str:
        if self.role == "background":
            raw = f"mapped:{self.source_id}:background:{self.substitution_index}"
        elif self.source_kind == "group":
            raw = f"mapped:{self.source_id}:group"
        else:
            raw = (
                f"mapped:{self.source_id}:segment:{self.segment_index}:"
                f"substitution:{self.substitution_index}"
            )
        _safe_identifier(raw, label="generated layer instance id")
        return raw


@dataclass(frozen=True)
class _SourcePayload:
    layer_type: str
    name: str
    asset_id: str | None = None
    width: int | None = None
    height: int | None = None
    color: str | None = None
    text: str | None = None
    font_name: str | None = None
    font_size: float | None = None
    text_color: str | None = None
    effects: tuple[Any, ...] = ()


@dataclass(frozen=True)
class _MappingDomains:
    width: int
    height: int
    fps: float
    frames: int
    approved: ApprovedCapabilities
    fonts: set[str]
    substitutions: dict[str, AESubstitution]
    group_ids: list[str]
    member_parent: dict[str, str]
    group_parent: dict[str, str | None]
    native_font_names: dict[str, str | None]
    dynamic_z_sources: frozenset[str]
    intervals: tuple[tuple[int, int], ...]

def _safe_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise AEMappingError(f"{label} is not a safe identifier")
    return value


def _finite(value: Any, *, label: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AEMappingError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or abs(result) > _MAX_NUMBER or (positive and result <= 0):
        raise AEMappingError(f"{label} is outside finite bounds")
    return result


def _color(
    value: Any,
    *,
    label: str,
    default: str | None = None,
    allow_alpha: bool = False,
) -> str:
    if value is None:
        if default is None:
            raise AEMappingError(f"{label} is required")
        value = default
    pattern = _LAYER_COLOR_RE if allow_alpha else _COLOR_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise AEMappingError(f"{label} must be #RRGGBB")
    return value.upper()


def _is_nonopaque_color(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _LAYER_COLOR_RE.fullmatch(value) is not None
        and len(value) == 9
        and value[-2:].lower() != "ff"
    )


def _opaque_color(
    value: Any,
    *,
    label: str,
    default: str | None = None,
) -> str:
    color = _color(value, label=label, default=default, allow_alpha=True)
    if len(color) == 9:
        if color[-2:] != "FF":
            raise AEMappingError(f"{label} must be opaque")
        return color[:7]
    return color


def _normalize_prefix(value: str | os.PathLike[str] | None) -> str:
    if value is None:
        return ""
    raw = os.fspath(value)
    if not isinstance(raw, str) or "\x00" in raw or "\\" in raw:
        raise AEMappingError("scene directory must be a normalized project-relative prefix")
    if raw in ("", "."):
        return ""
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise AEMappingError("scene directory must be project-relative")
    parts = raw.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise AEMappingError("scene directory must be normalized")
    return "/".join(parts)


def _relative_reference(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise AEMappingError(f"{label} must be a normalized relative reference")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise AEMappingError(f"{label} must be project-relative")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise AEMappingError(f"{label} must be normalized")
    return "/".join(parts)


def _normalize_assets(assets: Sequence[PlanAsset] | Mapping[str, PlanAsset]) -> dict[str, PlanAsset]:
    values = list(assets.values()) if isinstance(assets, Mapping) else list(assets)
    by_path: dict[str, PlanAsset] = {}
    by_id: dict[str, PlanAsset] = {}
    for raw in values:
        try:
            asset = raw if isinstance(raw, PlanAsset) else PlanAsset.model_validate(raw)
        except Exception as exc:  # Pydantic details are not mapping semantics.
            raise AEMappingError(f"invalid pinned plan asset: {exc}") from exc
        previous = by_id.get(asset.id)
        if previous is not None and (
            previous.sha256,
            previous.length,
            previous.media_kind,
        ) != (asset.sha256, asset.length, asset.media_kind):
            raise AEMappingError(f"conflicting pinned asset identity: {asset.id}")
        if asset.project_path in by_path:
            raise AEMappingError(f"ambiguous pinned asset path: {asset.project_path}")
        by_id.setdefault(asset.id, asset)
        by_path[asset.project_path] = asset
    return by_path


def _approved_capabilities(value: Any) -> ApprovedCapabilities:
    if isinstance(value, ApprovedCapabilities):
        return value
    if isinstance(value, AECapabilities):
        catalog = value.capabilities
        properties = catalog.property_schemas or catalog.properties
        return ApprovedCapabilities(
            digest=value.digest,
            fonts=tuple(catalog.font_names),
            effects=tuple(catalog.effect_names),
            properties=dict(properties),
            model_layers=catalog.model_layers,
        )
    if isinstance(value, Mapping):
        raw = dict(value)
        nested = raw.get("capabilities")
        if isinstance(nested, Mapping):
            raw.pop("capabilities", None)
            raw.update(nested)
        try:
            return ApprovedCapabilities.model_validate(raw)
        except Exception as exc:
            raise AEMappingError(f"invalid approved AE capabilities: {exc}") from exc
    raise AEMappingError("capabilities must be a full AECapabilities snapshot")


def _validate_scene(scene: Scene) -> tuple[int, int, float, int]:
    if not isinstance(scene, Scene):
        raise AEMappingError("scene must be a Scene")
    if not isinstance(scene.size, tuple) or len(scene.size) != 2:
        raise AEMappingError("scene size is invalid")
    width, height = scene.size
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or not _MIN_COMP_DIMENSION <= width <= _MAX_COMP_DIMENSION
    ):
        raise AEMappingError(
            f"scene width must be between {_MIN_COMP_DIMENSION} and {_MAX_COMP_DIMENSION}"
        )
    if (
        isinstance(height, bool)
        or not isinstance(height, int)
        or not _MIN_COMP_DIMENSION <= height <= _MAX_COMP_DIMENSION
    ):
        raise AEMappingError(
            f"scene height must be between {_MIN_COMP_DIMENSION} and {_MAX_COMP_DIMENSION}"
        )
    fps = _finite(scene.fps, label="scene fps", positive=True)
    if not _MIN_COMP_FPS <= fps <= _MAX_COMP_FPS:
        raise AEMappingError(
            f"scene fps must be between {_MIN_COMP_FPS:g} and {_MAX_COMP_FPS:g}"
        )
    if (
        isinstance(scene.frames, bool)
        or not isinstance(scene.frames, int)
        or not 0 < scene.frames <= _MAX_NUMBER
    ):
        raise AEMappingError("scene frames must be a positive bounded integer")
    if float(scene.frames) / fps > _MAX_COMP_DURATION:
        raise AEMappingError(
            f"scene duration must be at most {_MAX_COMP_DURATION:g} seconds"
        )
    _safe_identifier(scene.id, label="scene id")
    return width, height, fps, scene.frames


def _validate_element_values(element: Element, frames: int) -> None:
    _safe_identifier(element.id, label="source element id")
    width = _finite(element.canonical.width, label=f"{element.id} width", positive=True)
    height = _finite(element.canonical.height, label=f"{element.id} height", positive=True)
    _finite(element.canonical.anchor[0], label=f"{element.id} anchor x")
    _finite(element.canonical.anchor[1], label=f"{element.id} anchor y")
    if width > _MAX_NUMBER or height > _MAX_NUMBER:
        raise AEMappingError(f"{element.id} dimensions are outside bounds")
    start, end = element.visible
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)):
        raise AEMappingError(f"{element.id} visibility is invalid")
    if start > end or max(abs(start), abs(end)) > _MAX_NUMBER:
        raise AEMappingError(f"{element.id} visibility is outside bounds")
    for property_name, track in element.tracks.items():
        if property_name not in PROPS:
            raise AEMappingError(f"{element.id} has unsupported track: {property_name}")
        _validate_track(track, element.id, property_name, frames)
    _validate_track(element.z, element.id, "z", frames)
    if element.canonical.font is not None:
        font_size = _finite(element.canonical.font.size_px, label=f"{element.id} font size", positive=True)
        if font_size < 0.000001:
            raise AEMappingError(f"{element.id} font size is outside AE bounds")
    _validate_operation_domains(element, width=width, height=height)


def _validate_operation_domains(element: Element, *, width: float, height: float) -> None:
    anchor_x = _finite(element.canonical.anchor[0], label=f"{element.id} anchor x") * width
    anchor_y = _finite(element.canonical.anchor[1], label=f"{element.id} anchor y") * height
    if any(not math.isfinite(value) or abs(value) > _MAX_NUMBER for value in (anchor_x, anchor_y)):
        raise AEMappingError(f"{element.id} anchor outcome is outside AE bounds")

    for property_name in ("sx", "sy"):
        track = element.tracks.get(property_name)
        values = track.keys if track is not None else ()
        for key in values:
            scaled = float(key.v) * 100.0
            if not math.isfinite(scaled) or abs(scaled) > _MAX_NUMBER:
                raise AEMappingError(f"{element.id} {property_name} scale outcome is outside AE bounds")

    opacity = element.tracks.get("opacity")
    if opacity is not None and any(not 0.0 <= float(key.v) <= 1.0 for key in opacity.keys):
        raise AEMappingError(f"{element.id} opacity is outside AE bounds")
    reveal = element.tracks.get("reveal")
    if reveal is not None and any(not 0 <= key.v <= 1 for key in reveal.keys):
        raise AEMappingError(f"{element.id} reveal is outside the unit interval")

def _validate_track(track: Track, source_id: str, property_name: str, frames: int) -> None:
    if not track.keys:
        raise AEMappingError(f"{source_id} {property_name} track is empty")
    for key in track.keys:
        if isinstance(key.t, bool) or not isinstance(key.t, int) or key.t < 0 or key.t >= frames:
            raise AEMappingError(f"{source_id} {property_name} keyframe is outside the scene")
        _finite(key.v, label=f"{source_id} {property_name} value")
        if key.ease is not None:
            if len(key.ease) != 4 or any(not math.isfinite(float(part)) for part in key.ease):
                raise AEMappingError(f"{source_id} {property_name} easing is invalid")


def _clip_visible(element: Element, frames: int) -> tuple[int, int] | None:
    start, end = element.visible
    clipped_start = max(0, start)
    clipped_end = min(frames, end + 1)
    if clipped_start >= clipped_end:
        return None
    return clipped_start, clipped_end


def _normalize_substitutions(
    substitutions: Sequence[AESubstitution] | None,
    scene: Scene,
    elements: Mapping[str, Element],
) -> dict[str, AESubstitution]:
    result: dict[str, AESubstitution] = {}
    for raw in substitutions or ():
        try:
            if isinstance(raw, AESubstitution):
                proposal = raw
            elif isinstance(raw, Mapping):
                record = dict(raw)
                for field_name in ("proposed_layers", "proposed_effects", "lost_semantics"):
                    if isinstance(record.get(field_name), list):
                        record[field_name] = tuple(record[field_name])
                proposal = AESubstitution.model_validate(record)
            else:
                raise TypeError("substitution must be an AESubstitution")
        except Exception as exc:
            raise AEMappingError(f"invalid AE substitution: {exc}") from exc
        if not proposal.acknowledged:
            raise AEMappingError(f"substitution for {proposal.source_element_id} is not acknowledged")
        if not proposal.proposed_layers:
            raise AEMappingError(f"substitution for {proposal.source_element_id} has no proposed layers")
        source_id = proposal.source_element_id
        if source_id in result:
            raise AEMappingError(f"duplicate substitution for source: {source_id}")
        if source_id != scene.id and source_id not in elements:
            raise AEMappingError(f"substitution references unknown source: {source_id}")
        expected_type = "background" if source_id == scene.id else elements[source_id].kind
        if proposal.source_type != expected_type:
            raise AEMappingError(
                f"substitution source type mismatch for {source_id}: expected {expected_type}"
            )
        result[source_id] = proposal
    return result


def _validate_groups(
    scene: Scene,
    elements: Mapping[str, Element],
    substitutions: Mapping[str, AESubstitution],
) -> tuple[list[str], dict[str, str], dict[str, str | None]]:
    group_ids: list[str] = []
    group_element_ids = [element.id for element in scene.elements if element.kind == "group"]
    for element_id in group_element_ids:
        _safe_identifier(element_id, label="group id")
        if element_id not in group_ids:
            group_ids.append(element_id)
    group_specs: dict[str, Any] = {}
    for group in scene.groups:
        _safe_identifier(group.id, label="group id")
        if group.id == scene.id:
            raise AEMappingError("group id collides with the scene id")
        if group.id in elements and group.id not in group_element_ids:
            raise AEMappingError(f"group id collides with non-group element: {group.id}")
        if group.id not in group_ids:
            group_ids.append(group.id)
        if group.id in group_specs:
            raise AEMappingError(f"duplicate group id: {group.id}")
        group_specs[group.id] = group
    known_members = set(elements) | set(group_ids)
    member_parent: dict[str, str] = {}
    for group_id, group in group_specs.items():
        for member in group.members:
            _safe_identifier(member, label="group member id")
            if member not in known_members:
                raise AEMappingError(f"group {group_id} references unknown member: {member}")
            if member in member_parent:
                raise AEMappingError(f"overlapping group membership: {member}")
            member_parent[member] = group_id
    for source_id in group_element_ids:
        if source_id in substitutions:
            raise AEMappingError(f"group substitutions are not supported: {source_id}")
        element = elements[source_id]
        for property_name, track in element.tracks.items():
            default = DEFAULTS[property_name]
            if any(abs(float(key.v) - default) > 1e-12 or key.ease is not None for key in track.keys):
                raise AEMappingError(f"group {source_id} must remain a neutral null")
    # A group can contain another group, but cycles cannot produce a valid parent tree.
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(group_id: str) -> None:
        if group_id in visited:
            return
        if group_id in visiting:
            raise AEMappingError("group membership contains a cycle")
        visiting.add(group_id)
        parent = member_parent.get(group_id)
        if parent is not None:
            visit(parent)
        visiting.remove(group_id)
        visited.add(group_id)

    for group_id in group_ids:
        visit(group_id)
    return group_ids, member_parent, {group_id: member_parent.get(group_id) for group_id in group_ids}


def _source_interval(element: Element, frames: int) -> tuple[int, int] | None:
    return _clip_visible(element, frames)


def _mapping_intervals(
    scene: Scene,
    frames: int,
) -> tuple[frozenset[str], tuple[tuple[int, int], ...]]:
    dynamic_z_sources = frozenset(
        element.id
        for element in scene.elements
        if element.kind != "group" and len(element.z.keys) > 1
    )
    boundaries = [0, frames]
    boundaries.extend(
        key.t
        for element in scene.elements
        if element.id in dynamic_z_sources
        for key in element.z.keys
        if 0 < key.t < frames
    )
    ordered_boundaries = sorted(set(boundaries))
    intervals = tuple(zip(ordered_boundaries, ordered_boundaries[1:]))
    if not intervals:
        raise AEMappingError("scene has no active frame interval")
    return dynamic_z_sources, intervals


def _source_segments(
    element: Element,
    frames: int,
    dynamic_z_sources: frozenset[str],
    intervals: Sequence[tuple[int, int]],
) -> tuple[_SourceSegment, ...]:
    visible = _source_interval(element, frames)
    if visible is None:
        return ()
    if element.id not in dynamic_z_sources:
        return (_SourceSegment(0, visible[0], visible[1], 0),)
    return tuple(
        _SourceSegment(
            interval_index=index,
            frame_start=max(visible[0], interval_start),
            frame_end=min(visible[1], interval_end),
            sample_frame=interval_start,
        )
        for index, (interval_start, interval_end) in enumerate(intervals)
        if max(visible[0], interval_start) < min(visible[1], interval_end)
    )


def _solid_dimensions(element: Element | None, scene: Scene) -> tuple[int, int]:
    if element is None:
        width, height = scene.size
    else:
        width = _finite(element.canonical.width, label=f"{element.id} solid width", positive=True)
        height = _finite(element.canonical.height, label=f"{element.id} solid height", positive=True)
        if not width.is_integer() or not height.is_integer():
            raise AEMappingError(f"{element.id} solid dimensions must be integers")
        width, height = int(width), int(height)
    if not MIN_SOLID_DIMENSION <= width <= MAX_SOLID_DIMENSION:
        label = scene.id if element is None else element.id
        raise AEMappingError(
            f"{label} solid width must be between {MIN_SOLID_DIMENSION} and {MAX_SOLID_DIMENSION}"
        )
    if not MIN_SOLID_DIMENSION <= height <= MAX_SOLID_DIMENSION:
        label = scene.id if element is None else element.id
        raise AEMappingError(
            f"{label} solid height must be between {MIN_SOLID_DIMENSION} and {MAX_SOLID_DIMENSION}"
        )
    return int(width), int(height)


def _representation_count(scene: Scene, domains: _MappingDomains) -> int:
    count = 0
    if scene.background.kind == "image" or scene.id in domains.substitutions:
        background_substitution = domains.substitutions.get(scene.id)
        count += (
            len(background_substitution.proposed_layers)
            if background_substitution is not None
            else 1
        )
    if count > MAX_LAYERS:
        raise AEMappingError(f"baseline mapping exceeds {MAX_LAYERS} layers")
    count += len(domains.group_ids)
    if count > MAX_LAYERS:
        raise AEMappingError(f"baseline mapping exceeds {MAX_LAYERS} layers")
    for element in scene.elements:
        if element.kind == "group":
            continue
        segments = _source_segments(
            element,
            domains.frames,
            domains.dynamic_z_sources,
            domains.intervals,
        )
        substitution = domains.substitutions.get(element.id)
        layers_per_segment = (
            len(substitution.proposed_layers) if substitution is not None else 1
        )
        count += len(segments) * layers_per_segment
        if count > MAX_LAYERS:
            raise AEMappingError(f"baseline mapping exceeds {MAX_LAYERS} layers")
    if count > MAX_LAYERS:
        raise AEMappingError(f"baseline mapping exceeds {MAX_LAYERS} layers")
    return count


def _native_font_name(
    element: Element,
    capabilities: Any,
    fonts: set[str],
) -> str | None:
    """Resolve native text to the capability catalog's exact match name."""
    canonical_font = element.canonical.font
    if element.kind != "text" or canonical_font is None:
        return None
    if isinstance(capabilities, AECapabilities):
        metadata = capabilities.capabilities.fonts
        if any(font.family or font.style for font in metadata):
            try:
                from .compatibility import resolve_ae_font
            except ImportError:
                pass
            else:
                try:
                    resolved = resolve_ae_font(canonical_font, capabilities)
                except ValueError as exc:
                    raise AEMappingError(f"unsupported semantics for {element.id}: unavailable font") from exc
                if resolved not in fonts:
                    raise AEMappingError(f"unsupported semantics for {element.id}: unavailable font")
                return resolved
    return canonical_font.family_guess


def _prepare_mapping_domains(
    scene: Scene,
    capabilities: AECapabilities | ApprovedCapabilities | Mapping[str, Any] | None,
    substitutions: Sequence[AESubstitution] | None = None,
) -> _MappingDomains:
    width, height, fps, frames = _validate_scene(scene)
    if capabilities is None:
        raise AEMappingError("approved AE capabilities are required")
    approved = _approved_capabilities(capabilities)

    elements: dict[str, Element] = {}
    for element in scene.elements:
        if element.id in elements:
            raise AEMappingError(f"duplicate source element id: {element.id}")
        _validate_element_values(element, frames)
        elements[element.id] = element
    if scene.id in elements:
        raise AEMappingError("scene id collides with an element id")

    substitutions_by_source = _normalize_substitutions(substitutions, scene, elements)
    for source_id, substitution in substitutions_by_source.items():
        source_element = None if source_id == scene.id else elements[source_id]
        for raw in substitution.proposed_layers:
            layer_type = (
                raw
                if isinstance(raw, str)
                else raw.get("layer_type")
                if isinstance(raw, Mapping)
                else None
            )
            if layer_type == "solid":
                _solid_dimensions(source_element, scene)
    background_substitution = substitutions_by_source.get(scene.id)
    if background_substitution is not None and scene.background.kind == "color":
        if not _is_nonopaque_color(scene.background.value):
            raise AEMappingError(
                "background substitutions require a non-opaque color or image background"
            )
        for index, raw in enumerate(background_substitution.proposed_layers):
            if raw == "solid":
                raise AEMappingError(
                    f"{scene.id} background substitution {index} requires an explicit opaque color"
                )
            if isinstance(raw, Mapping) and raw.get("layer_type") == "solid":
                if "color" not in raw:
                    raise AEMappingError(
                        f"{scene.id} background substitution {index} requires an explicit opaque color"
                    )
                _opaque_color(
                    raw["color"],
                    label=f"{scene.id} background substitution {index} color",
                )

    for element in scene.elements:
        _validate_scale_pair(element, substitutions_by_source)
    group_ids, member_parent, group_parent = _validate_groups(
        scene,
        elements,
        substitutions_by_source,
    )

    fonts = set(approved.fonts)
    native_font_names = {
        element.id: _native_font_name(element, capabilities, fonts)
        for element in scene.elements
        if element.id not in substitutions_by_source
        and element.kind == "text"
        and element.canonical.text
        and element.canonical.font is not None
    }
    for element in scene.elements:
        if element.kind == "group":
            continue
        substitution = substitutions_by_source.get(element.id)
        reveal = element.tracks.get("reveal")
        if reveal is not None and any(key.v != 1.0 for key in reveal.keys) and (substitution is None or "reveal" not in substitution.lost_semantics):
            if "ADBE Linear Wipe" not in approved.effects or any(
                approved.properties.get(name) not in {"number", "float"}
                for name in LINEAR_WIPE_PROPERTIES
            ):
                raise AEMappingError(f"unsupported semantics for {element.id}: reveal requires Linear Wipe")
        if substitution is not None:
            continue
        if element.kind == "3d":
            if not approved.model_layers:
                raise AEMappingError(f"unsupported semantics for {element.id}: model_layers capability is unavailable")
            if not element.canonical.model or not element.canonical.model.lower().endswith(".glb"):
                raise AEMappingError(f"unsupported semantics for {element.id}: a GLB model asset is required")
            if any(approved.properties.get(name) not in {"number", "float"} for name in ("ADBE Rotate X", "ADBE Rotate Y")):
                raise AEMappingError(f"unsupported semantics for {element.id}: Rotate X/Y schemas are unavailable")
            skx = element.tracks.get("skx")
            if skx is not None and any(key.v != 0.0 for key in skx.keys):
                raise AEMappingError(f"unsupported semantics for {element.id}: model X skew")
        sky = element.tracks.get("sky")
        if sky is not None and any(abs(float(key.v)) > 1e-12 for key in sky.keys):
            raise AEMappingError(f"unsupported semantics for {element.id}: nonzero Y skew")
        if element.kind == "text" and element.canonical.text and element.canonical.font:
            if native_font_names.get(element.id) not in fonts:
                raise AEMappingError(f"unsupported semantics for {element.id}: unavailable font")
        if element.kind in {"sprite", "ui"} and not element.canonical.texture:
            raise AEMappingError(f"unsupported semantics for {element.id}: missing texture")
        if element.kind == "text":
            usable = bool(element.canonical.text) and element.canonical.font is not None
            if not usable and not element.canonical.texture:
                raise AEMappingError(f"unsupported semantics for {element.id}: text has no usable fallback")

    dynamic_z_sources, intervals = _mapping_intervals(scene, frames)
    domains = _MappingDomains(
        width=width,
        height=height,
        fps=fps,
        frames=frames,
        approved=approved,
        fonts=fonts,
        substitutions=substitutions_by_source,
        group_ids=group_ids,
        member_parent=member_parent,
        group_parent=group_parent,
        native_font_names=native_font_names,
        dynamic_z_sources=dynamic_z_sources,
        intervals=intervals,
    )
    _representation_count(scene, domains)
    return domains


def validate_mapping_domains(
    scene: Scene,
    capabilities: AECapabilities | ApprovedCapabilities | Mapping[str, Any] | None,
    substitutions: Sequence[AESubstitution] = (),
) -> None:
    """Validate mapper inputs that do not depend on pinned assets."""
    _prepare_mapping_domains(scene, capabilities, substitutions)


def _default_payload(
    element: Element,
    *,
    fonts: set[str],
    resolver: _AssetResolver,
    font_name: str | None = None,
) -> _SourcePayload:
    canonical = element.canonical
    if element.kind == "3d":
        return _SourcePayload(
            layer_type="model", name=element.id,
            asset_id=resolver.resolve(canonical.model, label=f"{element.id} model"),
        )
    if element.kind == "group":
        return _SourcePayload(layer_type="null", name=element.id)
    if element.kind == "text":
        usable_text = isinstance(canonical.text, str) and bool(canonical.text)
        resolved_font_name = font_name
        if resolved_font_name is None and canonical.font is not None:
            resolved_font_name = canonical.font.family_guess
        usable_font = canonical.font is not None and bool(resolved_font_name)
        if usable_text and usable_font:
            if resolved_font_name not in fonts:
                raise AEMappingError(f"unsupported semantics for {element.id}: unavailable font")
            font_size = _finite(canonical.font.size_px, label=f"{element.id} font size", positive=True)
            return _SourcePayload(
                layer_type="text",
                name=element.id,
                text=canonical.text,
                font_name=resolved_font_name,
                font_size=font_size,
                text_color=_opaque_color(
                    canonical.color,
                    label=f"{element.id} text color",
                    default="#000000",
                ),
            )
        if canonical.texture:
            return _SourcePayload(
                layer_type="footage",
                name=element.id,
                asset_id=resolver.resolve(canonical.texture, label=f"{element.id} texture"),
            )
        raise AEMappingError(f"unsupported semantics for {element.id}: text has no usable fallback")
    if element.kind in {"sprite", "ui"}:
        if not canonical.texture:
            raise AEMappingError(f"unsupported semantics for {element.id}: missing texture")
        return _SourcePayload(
            layer_type="footage",
            name=element.id,
            asset_id=resolver.resolve(canonical.texture, label=f"{element.id} texture"),
        )
    raise AEMappingError(f"unsupported semantics for {element.id}: {element.kind}")


def _proposal_payload(
    raw: str | dict[str, Any],
    *,
    source_id: str,
    element: Element | None,
    scene: Scene,
    resolver: _AssetResolver,
    fonts: set[str],
    proposal_index: int,
) -> _SourcePayload:
    if isinstance(raw, str):
        layer_type = raw
        name = f"{source_id}:substitution:{proposal_index}"
        layer_data: Mapping[str, Any] = {}
    elif isinstance(raw, Mapping):
        layer_data = raw
        layer_type = layer_data.get("layer_type")
        name = layer_data.get("name")
        if set(layer_data) - {"layer_type", "name", "color", "texture", "font_name"}:
            raise AEMappingError(f"substitution layer has unsupported fields for {source_id}")
        if not isinstance(name, str) or not name:
            raise AEMappingError(f"substitution layer name is invalid for {source_id}")
    else:
        raise AEMappingError(f"substitution layer is invalid for {source_id}")
    if layer_type not in {"text", "solid", "footage", "null"}:
        raise AEMappingError(f"substitution layer type is unavailable for {source_id}")
    if "font_name" in layer_data:
        if layer_type != "text":
            raise AEMappingError(f"font_name is only valid for text substitutions: {source_id}")
        proposed_font_name = layer_data["font_name"]
        if not isinstance(proposed_font_name, str) or not proposed_font_name or len(proposed_font_name) > 4096:
            raise AEMappingError(f"substitution font_name is invalid for {source_id}")
        if proposed_font_name not in fonts:
            raise AEMappingError(f"substitution font_name is unavailable for {source_id}: {proposed_font_name}")
    else:
        proposed_font_name = None
    if not isinstance(name, str) or not name or len(name) > 4096:
        raise AEMappingError(f"substitution layer name is invalid for {source_id}")
    canonical = element.canonical if element is not None else None
    if layer_type == "footage":
        reference = layer_data.get("texture") if isinstance(layer_data, Mapping) else None
        if reference is None and canonical is not None:
            reference = canonical.texture
        if reference is None and element is None:
            try:
                reference = _safe_asset_reference(scene.background.value, label="background image")
            except ValueError as exc:
                raise AEMappingError(str(exc)) from exc
        if not reference:
            raise AEMappingError(f"footage substitution for {source_id} has no pinned texture")
        return _SourcePayload(
            layer_type="footage",
            name=name,
            asset_id=resolver.resolve(reference, label=f"{source_id} substitution texture"),
        )
    if layer_type == "solid":
        width, height = _solid_dimensions(element, scene)
        color = layer_data.get("color") if isinstance(layer_data, Mapping) else None
        if color is None and canonical is not None:
            color = canonical.color
        return _SourcePayload(
            layer_type="solid",
            name=name,
            width=width,
            height=height,
            color=_opaque_color(
                color,
                label=f"{source_id} substitution color",
                default="#000000",
            ),
        )
    if layer_type == "text":
        text = canonical.text if canonical is not None and isinstance(canonical.text, str) else ""
        font_name = proposed_font_name
        font_size = 1.0
        text_color = "#000000"
        if canonical is not None:
            if canonical.font is not None:
                font_size = _finite(canonical.font.size_px, label=f"{source_id} font size", positive=True)
                if font_name is None and canonical.font.family_guess in fonts:
                    font_name = canonical.font.family_guess
            text_color = _opaque_color(
                canonical.color,
                label=f"{source_id} text color",
                default="#000000",
            )
        if font_name is None:
            raise AEMappingError(f"text substitution for {source_id} requires an approved font_name")
        if isinstance(layer_data, Mapping) and "color" in layer_data:
            text_color = _opaque_color(
                layer_data["color"],
                label=f"{source_id} substitution text color",
            )
        return _SourcePayload(
            layer_type="text",
            name=name,
            text=text,
            font_name=font_name,
            font_size=font_size,
            text_color=text_color,
        )
    return _SourcePayload(layer_type="null", name=name)


def _track_layout(track: Track) -> tuple[tuple[int, tuple[float, float, float, float] | None], ...]:
    return tuple((key.t, key.ease) for key in track.keys)


def _validate_scale_pair(element: Element, substitutions: Mapping[str, AESubstitution]) -> None:
    sx = element.tracks.get("sx")
    sy = element.tracks.get("sy")
    if sx is not None and sy is not None and _track_layout(sx) != _track_layout(sy):
        if element.id not in substitutions:
            raise AEMappingError(f"unsupported semantics for {element.id}: unpaired scale tracks")


def _temporal_ease(
    start: Any,
    end: Any,
    delta: float,
    fps: float,
) -> tuple[list[float], list[float]] | None:
    ease = start.ease
    if ease is None:
        return None
    x1, y1, x2, y2 = (float(item) for item in ease)
    if (x1, y1, x2, y2) == (0.0, 0.0, 1.0, 1.0):
        return None
    if not all(math.isfinite(item) and abs(item) <= _MAX_NUMBER for item in (x1, y1, x2, y2)):
        raise AEMappingError("temporal easing contains a non-finite value")
    # Endpoint derivatives are required by KeyframeEase.  x1 == 0 or x2 == 1
    # makes one endpoint speed undefined; x1 <= x2 is the monotone cubic rule.
    if not (0.0 < x1 <= x2 < 1.0):
        raise AEMappingError("temporal easing x controls are degenerate or non-monotone")
    frame_delta = end.t - start.t
    if frame_delta <= 0:
        raise AEMappingError("temporal easing segment has zero duration")
    scale = float(delta) * fps / float(frame_delta)
    outgoing_speed = scale * y1 / x1
    incoming_speed = scale * (1.0 - y2) / (1.0 - x2)
    if abs(outgoing_speed) > _MAX_NUMBER or abs(incoming_speed) > _MAX_NUMBER:
        raise AEMappingError("temporal easing speed is outside AE bounds")
    return [outgoing_speed, x1 * 100.0], [incoming_speed, (1.0 - x2) * 100.0]


def _track_keyframes(
    track: Track,
    *,
    converter: Any,
    fps: float,
    paired_track: Track | None = None,
    substituted: bool = False,
) -> list[AEKeyframe]:
    if paired_track is not None and _track_layout(track) != _track_layout(paired_track):
        raise AEMappingError("paired scale tracks have different keyframe layouts")
    source_keys = list(track.keys)
    paired_keys = list(paired_track.keys) if paired_track is not None else None
    if source_keys[0].t > 0:
        source_keys = [source_keys[0].model_copy(update={"t": 0, "ease": None}), *source_keys]
        if paired_keys is not None:
            paired_keys = [paired_keys[0].model_copy(update={"t": 0, "ease": None}), *paired_keys]
    output: list[AEKeyframe] = []
    for index, key in enumerate(source_keys):
        value: Any = converter(key.v)
        if paired_keys is not None:
            value = [converter(key.v), converter(paired_keys[index].v)]
        ease_in: list[float] | list[list[float]] | None = None
        ease_out: list[float] | list[list[float]] | None = None
        if not substituted and index > 0 and source_keys[index - 1].ease is not None:
            previous = source_keys[index - 1]
            if paired_keys is None:
                _, ease_in = _temporal_ease(
                    previous,
                    key,
                    float(converter(key.v)) - float(converter(previous.v)),
                    fps,
                ) or (None, None)
            else:
                previous_pair = paired_keys[index - 1]
                _, in_x = _temporal_ease(
                    previous,
                    key,
                    float(converter(key.v)) - float(converter(previous.v)),
                    fps,
                ) or (None, None)
                _, in_y = _temporal_ease(
                    previous_pair,
                    paired_keys[index],
                    float(converter(paired_keys[index].v)) - float(converter(previous_pair.v)),
                    fps,
                ) or (None, None)
                if in_x is not None and in_y is not None:
                    ease_in = [in_x, in_y]
        if not substituted and index + 1 < len(source_keys) and key.ease is not None:
            following = source_keys[index + 1]
            if paired_keys is None:
                ease_out, _ = _temporal_ease(
                    key,
                    following,
                    float(converter(following.v)) - float(converter(key.v)),
                    fps,
                ) or (None, None)
            else:
                following_pair = paired_keys[index + 1]
                out_x, _ = _temporal_ease(
                    key,
                    following,
                    float(converter(following.v)) - float(converter(key.v)),
                    fps,
                ) or (None, None)
                out_y, _ = _temporal_ease(
                    paired_keys[index],
                    following_pair,
                    float(converter(following_pair.v)) - float(converter(paired_keys[index].v)),
                    fps,
                ) or (None, None)
                if out_x is not None and out_y is not None:
                    ease_out = [out_x, out_y]
        output.append(
            AEKeyframe(
                frame=key.t,
                value=value,
                time=float(key.t) / fps,
                ease_in=ease_in,
                ease_out=ease_out,
            )
        )
    return output


def _neutral_group_operations(layer_id: str) -> list[Operation]:
    """Make AE's null defaults an identity parent before child transforms."""
    return [
        SetTransformOperation(layer_instance_id=layer_id, property_name="anchor", value=[0.0, 0.0]),
        SetTransformOperation(layer_instance_id=layer_id, property_name="position_x", value=0.0),
        SetTransformOperation(layer_instance_id=layer_id, property_name="position_y", value=0.0),
        SetTransformOperation(layer_instance_id=layer_id, property_name="scale", value=[100.0, 100.0]),
        SetTransformOperation(layer_instance_id=layer_id, property_name="rotation", value=0.0),
        SetTransformOperation(layer_instance_id=layer_id, property_name="skew_x", value=0.0),
    ]

def _transform_operations(
    element: Element,
    layer_id: str,
    *,
    fps: float,
    substituted: bool = False,
    lost_semantics: Sequence[str] = (),
) -> list[Operation]:
    canonical = element.canonical
    anchor = [
        _finite(canonical.anchor[0], label=f"{element.id} anchor x") * float(canonical.width),
        _finite(canonical.anchor[1], label=f"{element.id} anchor y") * float(canonical.height),
    ]
    operations: list[Operation] = [SetTransformOperation(layer_instance_id=layer_id, property_name="anchor", value=anchor)]

    def scalar(
        property_name: str,
        ae_property: str,
        converter: Any,
        *,
        opacity: bool = False,
    ) -> None:
        track = element.tracks.get(property_name)
        if track is not None and len(track.keys) > 1:
            keyframes = _track_keyframes(
                track,
                converter=converter,
                fps=fps,
                substituted=substituted,
            )
            operations.append(
                SetKeyframesOperation(layer_instance_id=layer_id, property_name=ae_property, keyframes=keyframes)
            )
            return
        value = converter(track.keys[0].v if track is not None else DEFAULTS[property_name])
        if opacity:
            operations.append(SetOpacityOperation(layer_instance_id=layer_id, opacity=value))
        else:
            operations.append(
                SetTransformOperation(
                    layer_instance_id=layer_id,
                    property_name={
                        "x": "position_x",
                        "y": "position_y",
                        "rot": "rotation",
                        "rx": "rotation_x",
                        "ry": "rotation_y",
                        "skx": "skew_x",
                    }.get(property_name, property_name),
                    value=value,
                )
            )

    scalar("x", "ADBE Position X", lambda value: _finite(value, label=f"{element.id} x"))
    scalar("y", "ADBE Position Y", lambda value: _finite(value, label=f"{element.id} y"))

    sx = element.tracks.get("sx")
    sy = element.tracks.get("sy")
    sx_dynamic = sx is not None and len(sx.keys) > 1
    sy_dynamic = sy is not None and len(sy.keys) > 1
    if sx_dynamic and sy_dynamic:
        if substituted and _track_layout(sx) != _track_layout(sy):
            operations.append(
                SetKeyframesOperation(
                    layer_instance_id=layer_id,
                    property_name="ADBE Scale X",
                    keyframes=_track_keyframes(
                        sx,
                        converter=lambda value: _finite(value, label=f"{element.id} scale x") * 100.0,
                        fps=fps,
                        substituted=substituted,
                    ),
                )
            )
            operations.append(
                SetKeyframesOperation(
                    layer_instance_id=layer_id,
                    property_name="ADBE Scale Y",
                    keyframes=_track_keyframes(
                        sy,
                        converter=lambda value: _finite(value, label=f"{element.id} scale y") * 100.0,
                        fps=fps,
                        substituted=substituted,
                    ),
                )
            )
        else:
            keyframes = _track_keyframes(
                sx,
                paired_track=sy,
                converter=lambda value: _finite(value, label=f"{element.id} scale") * 100.0,
                fps=fps,
                substituted=substituted,
            )
            operations.append(
                SetKeyframesOperation(
                    layer_instance_id=layer_id,
                    property_name="ADBE Scale",
                    keyframes=keyframes,
                )
            )
    elif sx_dynamic or sy_dynamic:
        static_sx = _finite(sx.keys[0].v if sx is not None else DEFAULTS["sx"], label=f"{element.id} scale x") * 100.0
        static_sy = _finite(sy.keys[0].v if sy is not None else DEFAULTS["sy"], label=f"{element.id} scale y") * 100.0
        if sx_dynamic:
            operations.append(SetTransformOperation(layer_instance_id=layer_id, property_name="scale_y", value=static_sy))
            operations.append(
                SetKeyframesOperation(
                    layer_instance_id=layer_id,
                    property_name="ADBE Scale X",
                    keyframes=_track_keyframes(
                        sx,
                        converter=lambda value: _finite(value, label=f"{element.id} scale x") * 100.0,
                        fps=fps,
                        substituted=substituted,
                    ),
                )
            )
        else:
            operations.append(SetTransformOperation(layer_instance_id=layer_id, property_name="scale_x", value=static_sx))
            operations.append(
                SetKeyframesOperation(
                    layer_instance_id=layer_id,
                    property_name="ADBE Scale Y",
                    keyframes=_track_keyframes(
                        sy,
                        converter=lambda value: _finite(value, label=f"{element.id} scale y") * 100.0,
                        fps=fps,
                        substituted=substituted,
                    ),
                )
            )
    else:
        static_sx = _finite(sx.keys[0].v if sx is not None else DEFAULTS["sx"], label=f"{element.id} scale x") * 100.0
        static_sy = _finite(sy.keys[0].v if sy is not None else DEFAULTS["sy"], label=f"{element.id} scale y") * 100.0
        operations.append(SetTransformOperation(layer_instance_id=layer_id, property_name="scale", value=[static_sx, static_sy]))

    scalar("rot", "ADBE Rotate Z", lambda value: _finite(value, label=f"{element.id} rotation"))
    if element.kind == "3d" and not substituted:
        scalar("rx", "ADBE Rotate X", lambda value: _finite(value, label=f"{element.id} rotation x"))
        scalar("ry", "ADBE Rotate Y", lambda value: _finite(value, label=f"{element.id} rotation y"))
    else:
        scalar("skx", "ADBE Skew", lambda value: _finite(value, label=f"{element.id} skew x"))
    sky = element.tracks.get("sky")
    if sky is not None and any(abs(float(key.v)) > 1e-12 for key in sky.keys):
        # Y skew is an explicitly acknowledged lost semantic when this source
        # arrived through a substitution; no fixed operation represents it.
        pass
    scalar("opacity", "ADBE Opacity", lambda value: _finite(value, label=f"{element.id} opacity"), opacity=True)
    reveal = element.tracks.get("reveal")
    if "reveal" not in lost_semantics and reveal is not None and any(key.v != 1.0 for key in reveal.keys):
        def completion(value: float) -> float:
            return (1.0 - value) * 100.0

        operations.append(SetEffectOperation(
            layer_instance_id=layer_id,
            effect_name="ADBE Linear Wipe",
            properties={
                "ADBE Linear Wipe-0001": completion(reveal.keys[0].v),
                "ADBE Linear Wipe-0002": LINEAR_WIPE_ANGLE,
                "ADBE Linear Wipe-0003": 0.0,
            },
        ))
        if len(reveal.keys) > 1:
            operations.append(SetKeyframesOperation(
                layer_instance_id=layer_id, property_name="ADBE Linear Wipe-0001",
                keyframes=_track_keyframes(reveal, converter=completion, fps=fps, substituted="easing" in lost_semantics),
            ))
    return operations




def _substitution_effects(
    rep: _Representation,
    *,
    siblings: Sequence[_Representation],
) -> list[Operation]:
    if not rep.effects:
        return []
    operations: list[Operation] = []
    for raw in rep.effects:
        if isinstance(raw, str):
            effect_name = raw
            properties: dict[str, Any] = {}
            target_index = 0
        elif isinstance(raw, Mapping):
            effect_name = raw.get("effect_name")
            properties = raw.get("properties", {})
            target_index = raw.get("layer_index", 0)
        else:
            raise AEMappingError(f"substitution effect is invalid for {rep.source_id}")
        if not isinstance(effect_name, str) or not effect_name:
            raise AEMappingError(f"substitution effect name is invalid for {rep.source_id}")
        if isinstance(target_index, bool) or not isinstance(target_index, int) or not 0 <= target_index < len(siblings):
            raise AEMappingError(f"substitution effect target is outside layer bounds for {rep.source_id}")
        if not isinstance(properties, Mapping):
            raise AEMappingError(f"substitution effect properties are invalid for {rep.source_id}")
        operations.append(
            SetEffectOperation(
                layer_instance_id=siblings[target_index].instance_id,
                effect_name=effect_name,
                properties=dict(properties),
            )
        )
    return operations


def _build_batches(
    operations: Sequence[Operation],
    *,
    capabilities: ApprovedCapabilities,
    scene_frames: int,
    duration: float,
    locked_source_ids: Sequence[str],
    final_inventory: Sequence[LayerInventory],
) -> tuple[BaselineBatch, ...]:
    current: dict[str, str | None] = {}
    batches: list[BaselineBatch] = []
    for offset in range(0, len(operations), MAX_OPERATIONS):
        chunk = list(operations[offset : offset + MAX_OPERATIONS])
        starting = dict(current)
        try:
            validated = validate_operation_batch(
                OperationBatch(
                    operations=chunk,
                    capability_digest=capabilities.digest,
                    scene_frame_count=scene_frames,
                    duration=duration,
                    layer_count=len(starting),
                ),
                approved_capabilities=capabilities,
                scene_frame_count=scene_frames,
                duration=duration,
                layer_count=len(starting),
                baseline=True,
                locked_source_ids=locked_source_ids,
                layer_sources=starting,
            )
        except Exception as exc:
            raise AEMappingError(f"baseline operation validation failed: {exc}") from exc
        batches.append(
            BaselineBatch(
                index=len(batches),
                capability_digest=capabilities.digest,
                scene_frame_count=scene_frames,
                duration=duration,
                operations=tuple(validated.operations),
                layer_sources=starting,
                layer_count=len(starting),
            )
        )
        for operation in validated.operations:
            if isinstance(operation, AddLayerOperation):
                if operation.layer_instance_id in current:
                    raise AEMappingError(f"duplicate generated layer id: {operation.layer_instance_id}")
                current[operation.layer_instance_id] = operation.source_element_id
            elif operation.kind == "remove_layer":
                current.pop(operation.layer_instance_id, None)
    expected = {record.instance_id: record.source_element_id for record in final_inventory}
    if current != expected:
        raise AEMappingError("final operation inventory does not match mapped layers")
    return tuple(batches)


def map_baseline(
    scene: Scene,
    assets: Sequence[PlanAsset] | Mapping[str, PlanAsset],
    scene_directory: str | os.PathLike[str] = "",
    capabilities: AECapabilities | ApprovedCapabilities | Mapping[str, Any] | None = None,
    substitutions: Sequence[AESubstitution] | None = None,
    locked_source_ids: Sequence[str] = (),
) -> BaselineMapping:
    """Build and strictly validate a deterministic baseline AE mapping."""
    domains = _prepare_mapping_domains(scene, capabilities, substitutions)
    width = domains.width
    height = domains.height
    fps = domains.fps
    frames = domains.frames
    approved = domains.approved
    substitutions_by_source = domains.substitutions
    group_ids = domains.group_ids
    member_parent = domains.member_parent
    group_parent = domains.group_parent
    fonts = domains.fonts
    native_font_names = domains.native_font_names
    prefix = _normalize_prefix(scene_directory)
    by_path = _normalize_assets(assets)
    imported: list[str] = []
    resolver = _AssetResolver(prefix=prefix, by_path=by_path, imported=imported)

    background_color = (
        "#000000"
        if scene.background.kind == "image" or scene.id in substitutions_by_source
        else _opaque_color(scene.background.value, label="scene background")
    )
    composition = CompositionSettings(
        width=width,
        height=height,
        frame_rate=fps,
        duration=float(frames) / fps,
        background_color=background_color,
    )


    dynamic_z_sources = domains.dynamic_z_sources
    intervals = domains.intervals

    representations: list[_Representation] = []
    # Background is intentionally one pinned layer at the back, even when
    # other elements need dynamic z segmentation.
    if scene.background.kind == "image" or scene.id in substitutions_by_source:
        proposal = substitutions_by_source.get(scene.id)
        if proposal is None:
            try:
                _safe_asset_reference(scene.background.value, label="background image")
            except ValueError as exc:
                raise AEMappingError(str(exc)) from exc
        payloads = (
            tuple(
                _proposal_payload(
                    raw,
                    source_id=scene.id,
                    element=None,
                    scene=scene,
                    resolver=resolver,
                    fonts=fonts,
                    proposal_index=index,
                )
                for index, raw in enumerate(proposal.proposed_layers)
            )
            if proposal is not None
            else (_SourcePayload(
                layer_type="footage",
                name=scene.id,
                asset_id=resolver.resolve(scene.background.value, label="background image"),
            ),)
        )
        for sub_index, payload in enumerate(payloads):
            representations.append(
                _Representation(
                    source_id=scene.id,
                    source_kind="background",
                    element=None,
                    layer_type=payload.layer_type,
                    name=payload.name,
                    frame_start=0,
                    frame_end=frames,
                    segment_index=0,
                    interval_index=-1,
                    substitution_index=sub_index,
                    scene_order=-1,
                    z=-2_000_000,
                    asset_id=payload.asset_id,
                    width=payload.width,
                    height=payload.height,
                    color=payload.color,
                    text=payload.text,
                    font_name=payload.font_name,
                    font_size=payload.font_size,
                    text_color=payload.text_color,
                    effects=(
                        proposal.proposed_effects if proposal is not None and sub_index == 0 else ()
                    ),
                    role="background",
                )
            )

    # Group nulls are added before children so parent_instance_id always refers
    # to an already-created layer.  Their transforms remain neutral by design.
    group_by_id = {element.id: element for element in scene.elements if element.kind == "group"}
    ordered_groups: list[str] = []
    seen_groups: set[str] = set()

    def append_group(group_id: str) -> None:
        if group_id in seen_groups:
            return
        parent = group_parent.get(group_id)
        if parent is not None:
            append_group(parent)
        seen_groups.add(group_id)
        ordered_groups.append(group_id)

    for group_id in group_ids:
        append_group(group_id)
    for group_id in ordered_groups:
        group_element = group_by_id.get(group_id)
        group_interval = (
            _clip_visible(group_element, frames)
            if group_element is not None
            else (0, frames)
        )
        if group_interval is None:
            group_interval = (0, frames)
        representations.append(
            _Representation(
                source_id=group_id,
                source_kind="group",
                element=group_element,
                layer_type="null",
                name=group_id,
                frame_start=group_interval[0],
                frame_end=group_interval[1],
                segment_index=0,
                interval_index=-1,
                substitution_index=0,
                scene_order=-1,
                z=-1_000_000,
                parent_source_id=group_parent.get(group_id),
            )
        )
    group_instance_ids = {rep.source_id: rep.instance_id for rep in representations if rep.source_kind == "group"}

    visual_representations: list[_Representation] = []
    for scene_order, element in enumerate(scene.elements):
        if element.kind == "group":
            continue
        segments = _source_segments(
            element,
            frames,
            dynamic_z_sources,
            intervals,
        )
        if not segments:
            continue
        substitution = substitutions_by_source.get(element.id)
        if substitution is not None:
            payloads = tuple(
                _proposal_payload(
                    raw,
                    source_id=element.id,
                    element=element,
                    scene=scene,
                    resolver=resolver,
                    fonts=fonts,
                    proposal_index=index,
                )
                for index, raw in enumerate(substitution.proposed_layers)
            )
            effects = substitution.proposed_effects
        else:
            payloads = (
                _default_payload(
                    element,
                    fonts=fonts,
                    resolver=resolver,
                    font_name=native_font_names.get(element.id),
                ),
            )
            effects = ()
        for segment in segments:
            z = int(eval_z(element, segment.sample_frame))
            for sub_index, payload in enumerate(payloads):
                visual_representations.append(
                    _Representation(
                        source_id=element.id,
                        source_kind=element.kind,
                        element=element,
                        layer_type=payload.layer_type,
                        name=payload.name,
                        frame_start=segment.frame_start,
                        frame_end=segment.frame_end,
                        segment_index=segment.interval_index,
                        interval_index=segment.interval_index,
                        substitution_index=sub_index,
                        scene_order=scene_order,
                        z=z,
                        substituted=substitution is not None,
                        parent_source_id=member_parent.get(element.id),
                        asset_id=payload.asset_id,
                        width=payload.width,
                        height=payload.height,
                        color=payload.color,
                        text=payload.text,
                        font_name=payload.font_name,
                        font_size=payload.font_size,
                        text_color=payload.text_color,
                        effects=effects if sub_index == 0 else (),
                    )
                )

    visual_representations.sort(
        key=lambda rep: (
            rep.z,
            rep.scene_order,
            rep.source_id,
            rep.substitution_index,
            rep.interval_index,
        )
    )
    representations.extend(visual_representations)

    if len(representations) > MAX_LAYERS:
        raise AEMappingError("baseline mapping exceeds 1000 layers")
    if len({rep.instance_id for rep in representations}) != len(representations):
        raise AEMappingError("generated layer instance ids are not unique")

    # Build parent references now that group instance IDs are known.  The nulls
    # are neutral, so this relationship does not alter mapped world transforms.
    operations: list[Operation] = []
    for rep in representations:
        parent_instance_id = None
        if rep.parent_source_id is not None:
            parent_instance_id = group_instance_ids.get(rep.parent_source_id)
            if parent_instance_id is None:
                raise AEMappingError(f"parent group is not mapped: {rep.parent_source_id}")
        operations.append(
            AddLayerOperation(
                layer_instance_id=rep.instance_id,
                layer_type=rep.layer_type,
                name=rep.name,
                source_element_id=rep.source_id,
                parent_instance_id=parent_instance_id,
                asset_id=rep.asset_id,
                width=rep.width,
                height=rep.height,
                color=rep.color,
            )
        )
    sibling_groups: dict[tuple[str, int, int], list[_Representation]] = {}
    for rep in representations:
        sibling_groups.setdefault((rep.source_id, rep.interval_index, rep.segment_index), []).append(rep)
    for rep in representations:
        operations.append(
            SetVisibilityOperation(
                layer_instance_id=rep.instance_id,
                visible=True,
                frame_start=rep.frame_start,
                frame_end=rep.frame_end,
            )
        )
        if rep.layer_type == "text":
            operations.append(
                SetTextOperation(
                    layer_instance_id=rep.instance_id,
                    text=rep.text or "",
                    font_name=rep.font_name,
                    font_size=rep.font_size,
                    color=rep.text_color,
                )
            )
        if rep.source_kind == "group":
            operations.extend(_neutral_group_operations(rep.instance_id))
        elif rep.role == "background" and scene.id not in substitutions_by_source:
            # ponytail: generated plates have composition dimensions; fitting
            # external images needs source dimensions added to the pinned manifest.
            operations.extend(_neutral_group_operations(rep.instance_id))
            operations.append(SetOpacityOperation(layer_instance_id=rep.instance_id, opacity=1.0))
        elif rep.element is not None:
            operations.extend(
                _transform_operations(
                    rep.element,
                    rep.instance_id,
                    fps=fps,
                    substituted=rep.substituted,
                    lost_semantics=substitutions_by_source[rep.source_id].lost_semantics if rep.substituted else (),
                )
            )
        if rep.effects:
            operations.extend(
                _substitution_effects(
                    rep,
                    siblings=sibling_groups[(rep.source_id, rep.interval_index, rep.segment_index)],
                )
            )

    inventory = tuple(
        LayerInventory(
            instance_id=rep.instance_id,
            source_element_id=rep.source_id,
            frame_start=rep.frame_start,
            frame_end=rep.frame_end,
        )
        for rep in representations
    )
    batches = _build_batches(
        operations,
        capabilities=approved,
        scene_frames=frames,
        duration=composition.duration,
        locked_source_ids=locked_source_ids,
        final_inventory=inventory,
    )
    return BaselineMapping(
        composition=composition,
        imported_asset_ids=tuple(imported),
        batches=batches,
        final_inventory=inventory,
    )


__all__ = [
    "AEMappingError",
    "MappingError",
    "CompositionSettings",
    "AECompositionSettings",
    "LayerInventory",
    "AELayerInventory",
    "BaselineBatch",
    "AEBaselineBatch",
    "BaselineMapping",
    "AEBaselineMapping",
    "validate_mapping_domains",
    "map_baseline",
]
