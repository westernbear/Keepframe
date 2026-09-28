from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import math
import re
from typing import Any, Literal

import numpy as np
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from ..ir.schema import Scene
from ..verify.matrix import COLS, Motion, extract_motions, extract_motions_from_matrices
from ..verify.predicates import PredContext, eval_pred, parse_pred


INSPECTION_SCHEMA = "keepframe.ae-inspection/1"
VERIFICATION_SCHEMA = "keepframe.ae-verification/1"
MAX_IDENTIFIER_LENGTH = 256
MAX_LAYERS = 1_000
MAX_SAMPLES = 1_000_000
MAX_FRAME = 1_000_000
MAX_VALUE = 1_000_000.0
MAX_NATIVE_LAYER_ID = 1_000_000
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_MOTION_PREDICATES = {"type", "dir", "mag", "dur", "before", "after", "while"}
_SPATIAL_PREDICATES = {"left", "right", "top", "bottom", "intersect"}


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)


def _bounded_id(value: str | None, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > MAX_IDENTIFIER_LENGTH or not _ID_RE.fullmatch(value):
        raise ValueError("identifier is invalid")
    return value


def _bounded_text(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_LENGTH or "\x00" in value:
        raise ValueError("text is invalid")
    return value


def _bounded_int(value: int, *, minimum: int = 0, maximum: int = MAX_FRAME) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > maximum:
        raise ValueError(f"integer must be between {minimum} and {maximum}")
    return value
 
def _native_layer_id(value: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > MAX_NATIVE_LAYER_ID
    ):
        raise ValueError("native layer id must be a positive integer")
    return value




def _number_tuple(value: Any, size: int, field_name: str) -> tuple[float, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or len(value) != size:
        raise ValueError(f"{field_name} must contain exactly {size} numbers")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{field_name} must contain numbers")
        number = float(item)
        if not math.isfinite(number) or abs(number) > MAX_VALUE:
            raise ValueError(f"{field_name} values must be finite and bounded")
        result.append(number)
    return tuple(result)


def _tuple_values(value: Any, field_name: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a list or tuple")
    return tuple(value)


class AELayerInventory(_StrictFrozen):
    """One authoritative AE layer with a half-open scene-frame interval."""

    layer_instance_id: str = Field(
        validation_alias=AliasChoices("layer_instance_id", "instance_id")
    )
    native_layer_id: int = Field(
        validation_alias=AliasChoices("native_layer_id", "native_id")
    )
    source_element_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("source_element_id", "source_id"),
    )
    kind: str = "unknown"
    index: int = Field(validation_alias=AliasChoices("index", "layer_index"))
    frame_start: int = Field(
        validation_alias=AliasChoices("frame_start", "start_frame", "in_frame", "start")
    )
    frame_end: int = Field(
        validation_alias=AliasChoices("frame_end", "end_frame", "out_frame", "end")
    )
    name: str | None = None
    in_point: float | None = None
    out_point: float | None = None

    _instance_id = field_validator("layer_instance_id")(_bounded_id)
    _native_id = field_validator("native_layer_id")(_native_layer_id)
    _source_id = field_validator("source_element_id")(
        lambda value: _bounded_id(value, optional=True)
    )
    _kind = field_validator("kind")(_bounded_text)
    _index = field_validator("index")(
        lambda value: _bounded_int(value, minimum=1, maximum=MAX_LAYERS)
    )
    _frame_start = field_validator("frame_start")(_bounded_int)
    _frame_end = field_validator("frame_end")(_bounded_int)
    _name = field_validator("name")(
        lambda value: None if value is None else _bounded_text(value)
    )

    @field_validator("in_point", "out_point", mode="before")
    @classmethod
    def _points(cls, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("layer point must be numeric")
        value = float(value)
        if not math.isfinite(value) or abs(value) > MAX_VALUE:
            raise ValueError("layer point must be finite and bounded")
        return value

    @model_validator(mode="after")
    def _valid_interval(self) -> AELayerInventory:
        if self.frame_end < self.frame_start:
            raise ValueError("layer interval must be half-open")
        return self

    @property
    def instance_id(self) -> str:
        return self.layer_instance_id

    @property
    def start(self) -> int:
        return self.frame_start

    @property
    def end(self) -> int:
        return self.frame_end


class AESourceSample(_StrictFrozen):
    """Compact source-level observation for one scene frame.

    ``layer_instance_id`` is present only when a panel returns raw per-instance
    rows.  Source-aggregated rows must carry ``provenance`` naming the layer
    instances that contributed to the row.
    """

    source_element_id: str = Field(
        validation_alias=AliasChoices("source_element_id", "source_id")
    )
    frame: int
    active: bool
    transform: tuple[float, ...] | None = Field(
        default=None, validation_alias=AliasChoices("transform", "matrix")
    )
    world_bounds: tuple[float, ...] | None = Field(
        default=None, validation_alias=AliasChoices("world_bounds", "bounds")
    )
    layer_instance_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("layer_instance_id", "instance_id"),
    )
    provenance: tuple[str, ...] = Field(
        default=(),
        validation_alias=AliasChoices("provenance", "instance_ids", "layer_instance_ids"),
    )

    _source_id = field_validator("source_element_id")(_bounded_id)
    _frame = field_validator("frame")(_bounded_int)
    _transform = field_validator("transform", mode="before")(
        lambda value: _number_tuple(value, len(COLS), "transform")
    )
    _bounds = field_validator("world_bounds", mode="before")(
        lambda value: _number_tuple(value, 4, "world_bounds")
    )
    _instance_id = field_validator("layer_instance_id")(
        lambda value: _bounded_id(value, optional=True)
    )

    @field_validator("provenance", mode="before")
    @classmethod
    def _provenance_tuple(cls, value: Any) -> tuple[str, ...]:
        values = _tuple_values(value, "provenance")
        if len(values) > MAX_LAYERS:
            raise ValueError("provenance exceeds layer limit")
        return tuple(_bounded_id(item) for item in values)

    @field_validator("provenance")
    @classmethod
    def _unique_provenance(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("provenance contains duplicate layer ids")
        return value

    @model_validator(mode="after")
    def _bounds_ordered(self) -> AESourceSample:
        if self.world_bounds is not None:
            x0, y0, x1, y1 = self.world_bounds
            if x1 < x0 or y1 < y0:
                raise ValueError("world_bounds must be ordered xmin,ymin,xmax,ymax")
        if not self.active and (self.transform is not None or self.world_bounds is not None):
            raise ValueError("inactive samples must omit transform and world_bounds")
        if self.layer_instance_id is not None and self.provenance:
            raise ValueError("raw instance samples cannot also carry provenance")
        return self

    @property
    def instance_id(self) -> str | None:
        return self.layer_instance_id



class AEObservationRequest(_StrictFrozen):
    source_element_id: str
    frame: int

    _source_id = field_validator("source_element_id")(_bounded_id)
    _frame = field_validator("frame")(_bounded_int)


def _nested_sample_rows(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("inspection payload must be an object")
    payload = dict(value)
    if "layers" not in payload:
        payload["layers"] = payload.get("layer_inventory", payload.get("inventory", ()))
    payload.pop("layer_inventory", None)
    payload.pop("inventory", None)
    raw_samples = payload.get("samples", payload.get("source_samples", ()))
    if not isinstance(raw_samples, (list, tuple)) or len(raw_samples) > MAX_SAMPLES:
        raise ValueError("inspection samples are outside bounds")
    missing_raw = payload.get("missing_pairs", ())
    if not isinstance(missing_raw, (list, tuple)) or len(missing_raw) > MAX_SAMPLES:
        raise ValueError("inspection missing pairs are outside bounds")
    missing: list[dict[str, Any]] = list(missing_raw)
    flattened: list[dict[str, Any]] = []
    for row in raw_samples:
        if not isinstance(row, Mapping) or "instances" not in row:
            flattened.append(dict(row) if isinstance(row, Mapping) else row)
            continue
        source_id = row.get("source_element_id")
        frame = row.get("frame")
        instances = row.get("instances")
        if not isinstance(instances, (list, tuple)) or len(instances) > MAX_LAYERS:
            raise ValueError("inspection instances are outside bounds")
        if not instances:
            missing.append({"source_element_id": source_id, "frame": frame})
            continue
        for instance in instances:
            if not isinstance(instance, Mapping):
                raise ValueError("inspection instance must be an object")
            if set(instance) - {"layer_instance_id", "active", "sample"}:
                raise ValueError("inspection instance contains unknown fields")
            instance_id = instance.get("layer_instance_id")
            active = instance.get("active", True)
            if not isinstance(active, bool):
                raise ValueError("inspection instance active flag is invalid")
            sample = instance.get("sample")
            if sample is None:
                if active:
                    missing.append({"source_element_id": source_id, "frame": frame})
                flattened.append(
                    {
                        "source_element_id": source_id,
                        "frame": frame,
                        "active": active,
                        "transform": None,
                        "world_bounds": None,
                        "layer_instance_id": instance_id,
                    }
                )
                continue
            if not active:
                raise ValueError("inactive inspection instances must have a null sample")
            if not isinstance(sample, Mapping):
                raise ValueError("inspection geometry sample must be an object or null")
            expected = {
                "x",
                "y",
                "sx",
                "sy",
                "rot",
                "opacity",
                "xmin",
                "ymin",
                "xmax",
                "ymax",
            }
            if set(sample) != expected:
                raise ValueError("inspection geometry sample fields are incomplete")
            flattened.append(
                {
                    "source_element_id": source_id,
                    "frame": frame,
                    "active": True,
                    "transform": (
                        sample["x"],
                        sample["y"],
                        sample["sx"],
                        sample["sy"],
                        sample["rot"],
                        sample["opacity"],
                    ),
                    "world_bounds": (
                        sample["xmin"],
                        sample["ymin"],
                        sample["xmax"],
                        sample["ymax"],
                    ),
                    "layer_instance_id": instance_id,
                }
            )
    payload["samples"] = flattened
    payload.pop("source_samples", None)
    payload["missing_pairs"] = missing
    return payload


class AEInspection(_StrictFrozen):
    """Wire payload for ``keepframe.ae-inspection/1``."""

    schema_version: Literal[INSPECTION_SCHEMA] = Field(
        default=INSPECTION_SCHEMA,
        validation_alias=AliasChoices("schema_version", "schema"),
    )
    frame_count: int | None = None
    fps: float | None = None
    requested: tuple[AEObservationRequest, ...] = ()
    layers: tuple[AELayerInventory, ...] = Field(
        default=(), validation_alias=AliasChoices("layers", "layer_inventory", "inventory")
    )
    layer_sources: dict[str, str | None] = Field(default_factory=dict)
    layer_native_ids: dict[str, int] = Field(default_factory=dict)
    samples: tuple[AESourceSample, ...] = Field(
        default=(), validation_alias=AliasChoices("samples", "source_samples")
    )
    missing_pairs: tuple[AEObservationRequest, ...] = ()
    source_aggregated: bool = True
    provenance: tuple[str, ...] = ()
    heartbeat: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_panel_payload(cls, value: Any) -> Any:
        return _nested_sample_rows(value) if isinstance(value, Mapping) else value

    @field_validator("frame_count")
    @classmethod
    def _frame_count(cls, value: int | None) -> int | None:
        return None if value is None else _bounded_int(value, minimum=1)

    @field_validator("fps", mode="before")
    @classmethod
    def _fps(cls, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("fps must be numeric")
        value = float(value)
        if not math.isfinite(value) or not (0.0 < value <= 1000.0):
            raise ValueError("fps must be finite and bounded")
        return value

    @field_validator("layers", "samples", "requested", "missing_pairs", mode="before")
    @classmethod
    def _records_tuple(cls, value: Any) -> tuple[Any, ...]:
        return _tuple_values(value, "inspection records")

    @field_validator("layer_sources", mode="before")
    @classmethod
    def _layer_sources_map(cls, value: Any) -> dict[str, str | None]:
        if value is None:
            return {}
        if not isinstance(value, Mapping) or len(value) > MAX_LAYERS:
            raise ValueError("layer_sources must be a bounded object")
        return {
            _bounded_id(instance_id): _bounded_id(source_id, optional=True)
            for instance_id, source_id in value.items()
        }
 
    @field_validator("layer_native_ids", mode="before")
    @classmethod
    def _layer_native_ids_map(cls, value: Any) -> dict[str, int]:
        if value is None:
            return {}
        if not isinstance(value, Mapping) or len(value) > MAX_LAYERS:
            raise ValueError("layer_native_ids must be a bounded object")
        result: dict[str, int] = {}
        native_ids: set[int] = set()
        for instance_id, native_id in value.items():
            instance = _bounded_id(instance_id)
            native = _native_layer_id(native_id)
            if native in native_ids:
                raise ValueError("duplicate native layer id")
            native_ids.add(native)
            result[instance] = native
        return result


    @field_validator("missing_pairs", "requested", mode="before")
    @classmethod
    def _pair_records(cls, value: Any) -> tuple[Any, ...]:
        return _tuple_values(value, "observation pairs")

    @field_validator("provenance", mode="before")
    @classmethod
    def _inspection_provenance(cls, value: Any) -> tuple[str, ...]:
        values = _tuple_values(value, "provenance")
        if len(values) > MAX_LAYERS:
            raise ValueError("provenance exceeds layer limit")
        return tuple(_bounded_id(item) for item in values)

    @field_validator("provenance")
    @classmethod
    def _inspection_provenance_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("provenance contains duplicate layer ids")
        return value

    @model_validator(mode="after")
    def _bounded_records(self) -> AEInspection:
        if len(self.layers) > MAX_LAYERS:
            raise ValueError("inspection contains too many layers")
        if len(self.samples) > MAX_SAMPLES:
            raise ValueError("inspection contains too many samples")
        if len(self.requested) > MAX_SAMPLES or len(self.missing_pairs) > MAX_SAMPLES:
            raise ValueError("inspection request exceeds sample limit")
        row_instances: set[str] = set()
        row_native_ids: set[int] = set()
        for layer in self.layers:
            if layer.layer_instance_id in row_instances:
                raise ValueError("duplicate layer inventory row")
            row_instances.add(layer.layer_instance_id)
            if layer.native_layer_id in row_native_ids:
                raise ValueError("duplicate native layer id")
            row_native_ids.add(layer.native_layer_id)
            mapped_native = self.layer_native_ids.get(layer.layer_instance_id)
            if mapped_native != layer.native_layer_id:
                raise ValueError("layer native id inventory conflict")
        if set(self.layer_native_ids) != row_instances:
            raise ValueError("layer_native_ids inventory is incomplete")
        if self.layer_sources:
            expected_sources = {
                layer.layer_instance_id: layer.source_element_id for layer in self.layers
            }
            if self.layer_sources != expected_sources:
                raise ValueError("layer source inventory conflict")
        return self

    @model_validator(mode="after")
    def _unique_requested_pairs(self) -> AEInspection:
        seen: set[tuple[str, int]] = set()
        for pair in self.requested:
            key = (pair.source_element_id, pair.frame)
            if key in seen:
                raise ValueError("inspection requested pairs contain duplicates")
            seen.add(key)
        return self

    @property
    def source_samples(self) -> tuple[AESourceSample, ...]:
        return self.samples


class AEObservedMotion(_StrictFrozen):
    id: str
    element: str
    type: Literal["translation", "rotation", "scale", "opacity"]
    start: int
    end: int
    dir: tuple[float, float] | None = None
    mag: float
    dur: int

    _id = field_validator("id", "element")(_bounded_id)
    _start = field_validator("start", "end", "dur")(_bounded_int)
    _direction = field_validator("dir", mode="before")(
        lambda value: _number_tuple(value, 2, "dir")
    )
    @field_validator("mag")
    @classmethod
    def _mag(cls, value: Any) -> float:
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and abs(float(value)) <= MAX_VALUE
        ):
            return float(value)
        raise ValueError("magnitude must be finite and bounded")


class AEPredicateResult(_StrictFrozen):
    predicate: str = Field(validation_alias=AliasChoices("predicate", "pred"))
    passed: bool
    violations: tuple[str, ...] = ()

    _predicate = field_validator("predicate")(_bounded_text)

    @field_validator("violations", mode="before")
    @classmethod
    def _violations_tuple(cls, value: Any) -> tuple[str, ...]:
        values = _tuple_values(value, "violations")
        return tuple(_bounded_text(item) for item in values)
    @property
    def pred(self) -> str:
        return self.predicate

    def __getitem__(self, key: str) -> Any:
        if key == "pred":
            return self.predicate
        if key == "passed":
            return self.passed
        if key == "violations":
            return list(self.violations)
        raise KeyError(key)



class AEVerificationReport(_StrictFrozen):
    schema_version: Literal[VERIFICATION_SCHEMA] = VERIFICATION_SCHEMA
    passed: bool
    keep_results: tuple[AEPredicateResult, ...] = ()
    violations: tuple[str, ...] = ()
    observed_motions: tuple[AEObservedMotion, ...] = ()
    inspection: AEInspection

    @field_validator("keep_results", "observed_motions", mode="before")
    @classmethod
    def _result_tuple(cls, value: Any) -> tuple[Any, ...]:
        return _tuple_values(value, "report records")

    @field_validator("violations", mode="before")
    @classmethod
    def _report_violations(cls, value: Any) -> tuple[str, ...]:
        values = _tuple_values(value, "violations")
        return tuple(_bounded_text(item) for item in values)

    @property
    def predicates(self) -> tuple[AEPredicateResult, ...]:
        return self.keep_results

    @property
    def inspection_wire(self) -> dict[str, Any]:
        return self.inspection.model_dump(mode="json")


def required_observation_frames(scene: Scene) -> list[tuple[str, int]]:
    """Return deterministic source/frame requests needed by enabled predicates."""
    motions = {motion.id: motion for motion in extract_motions(scene)}
    source_ids = {element.id for element in scene.elements}
    pairs: set[tuple[str, int]] = set()
    for constraint in scene.constraints:
        if not constraint.keep:
            continue
        try:
            name, args, explicit_frame = parse_pred(constraint.pred)
        except Exception as exc:
            raise ValueError(f"malformed keep predicate: {constraint.pred!r}") from exc
        if name in _MOTION_PREDICATES:
            if name in {"before", "after", "while"}:
                if len(args) != 2 or any(not isinstance(arg, str) for arg in args):
                    raise ValueError(f"malformed {name} predicate: {constraint.pred!r}")
                refs = args
            else:
                if len(args) != 2:
                    raise ValueError(f"malformed {name} predicate: {constraint.pred!r}")
                motion_id, value = args
                if not isinstance(motion_id, str):
                    raise ValueError(f"malformed {name} predicate: {constraint.pred!r}")
                if name == "type" and not isinstance(value, str):
                    raise ValueError(f"malformed {name} predicate: {constraint.pred!r}")
                if name == "dir" and (
                    not isinstance(value, list)
                    or len(value) != 2
                    or any(
                        isinstance(item, bool) or not isinstance(item, (int, float))
                        for item in value
                    )
                ):
                    raise ValueError(f"malformed {name} predicate: {constraint.pred!r}")
                if name in {"mag", "dur"} and (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                ):
                    raise ValueError(f"malformed {name} predicate: {constraint.pred!r}")
                refs = [motion_id]
            for motion_id in refs:
                if not isinstance(motion_id, str) or motion_id not in motions:
                    raise ValueError(f"unknown motion reference: {motion_id!r}")
                element_id = motions[motion_id].element
                pairs.update((element_id, frame) for frame in range(scene.frames))
            continue
        if name in _SPATIAL_PREDICATES:
            if len(args) != 2 or any(not isinstance(arg, str) for arg in args):
                raise ValueError(f"malformed {name} predicate: {constraint.pred!r}")
            for element_id in args:
                if element_id not in source_ids:
                    raise ValueError(f"unknown source reference: {element_id!r}")
            frame = scene.frames - 1 if explicit_frame is None else explicit_frame
            if frame < 0 or frame >= scene.frames:
                raise ValueError(f"spatial frame is outside scene: {frame}")
            pairs.update((element_id, frame) for element_id in args)
            continue
        raise ValueError(f"unknown keep predicate: {name!r}")
    return sorted(pairs, key=lambda pair: (pair[0], pair[1]))


def chunk_observation_pairs(
    pairs: Iterable[tuple[str, int]], max_pairs: int = 128
) -> list[list[tuple[str, int]]]:
    """Chunk sorted unique source/frame pairs without crossing the pair cap."""
    if not isinstance(max_pairs, int) or isinstance(max_pairs, bool) or max_pairs <= 0:
        raise ValueError("max_pairs must be a positive integer")
    normalized: set[tuple[str, int]] = set()
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError("observation pair must be (source_element_id, frame)")
        source_id, frame = pair
        _bounded_id(source_id)
        _bounded_int(frame)
        normalized.add((source_id, frame))
    ordered = sorted(normalized, key=lambda pair: (pair[0], pair[1]))
    return [ordered[start : start + max_pairs] for start in range(0, len(ordered), max_pairs)]


def chunk_required_observations(scene: Scene, max_pairs: int = 128) -> list[list[tuple[str, int]]]:
    return chunk_observation_pairs(required_observation_frames(scene), max_pairs=max_pairs)


def _as_inspection(value: AEInspection | Mapping[str, Any]) -> AEInspection:
    if isinstance(value, AEInspection):
        return value
    if isinstance(value, Mapping):
        return AEInspection.model_validate(value)
    raise ValueError("inspection chunk must be an AEInspection or mapping")


def _inspection_identity(
    value: AEInspection | Mapping[str, Any],
) -> tuple[dict[str, str | None], dict[str, int]]:
    inspection = _as_inspection(value)
    sources = dict(inspection.layer_sources)
    if not sources:
        sources = {
            layer.layer_instance_id: layer.source_element_id for layer in inspection.layers
        }
    native_ids = dict(inspection.layer_native_ids)
    if set(native_ids) != set(sources):
        raise ValueError("inspection native identity inventory is incomplete")
    return sources, native_ids


def validate_manual_inventory(
    inspection: AEInspection | Mapping[str, Any],
    *,
    prior_layer_sources: Mapping[str, str | None],
    prior_layer_native_ids: Mapping[str, int],
    allow_new_agent_ids: Iterable[str] = (),
) -> tuple[dict[str, str | None], dict[str, int]]:
    """Validate a manual inventory against the last checkpoint identity.

    Existing rows are bound to both their server instance/source pair and the
    AE-native layer ID.  Source-mapped deletions are forbidden, while absent
    source-less rows are tombstones.  New rows must use the exact server-issued
    agent-ID pool and remain source-less.
    """
    current_sources, current_native_ids = _inspection_identity(inspection)
    prior_sources = {
        _bounded_id(instance): _bounded_id(source, optional=True)
        for instance, source in prior_layer_sources.items()
    }
    prior_native_ids = {
        _bounded_id(instance): _native_layer_id(native)
        for instance, native in prior_layer_native_ids.items()
    }
    if set(prior_sources) != set(prior_native_ids):
        raise ValueError("prior layer native identity inventory is incomplete")
    allowed = tuple(allow_new_agent_ids)
    allowed_set: set[str] = set()
    for instance in allowed:
        _bounded_id(instance)
        if not re.fullmatch(r"agent[-_:][A-Za-z0-9_.:-]{1,255}", instance):
            raise ValueError("manual agent id is invalid")
        if instance in allowed_set:
            raise ValueError("duplicate manual agent id")
        allowed_set.add(instance)

    prior_native_owner = {native: instance for instance, native in prior_native_ids.items()}
    for instance, source in prior_sources.items():
        if instance not in current_sources:
            if source is not None:
                raise ValueError(f"missing source-mapped layer: {instance!r}")
            continue
        if current_sources[instance] != source:
            raise ValueError(f"manual layer source spoof: {instance!r}")
        if current_native_ids[instance] != prior_native_ids[instance]:
            raise ValueError(f"manual layer native id spoof: {instance!r}")

    for instance, source in current_sources.items():
        if instance in prior_sources:
            continue
        if instance not in allowed_set:
            raise ValueError(f"manual layer id is not server-issued: {instance!r}")
        if source is not None:
            raise ValueError(f"new manual layer must be source-less: {instance!r}")
        owner = prior_native_owner.get(current_native_ids[instance])
        if owner is not None and owner != instance:
            raise ValueError(f"manual layer native id was already issued: {instance!r}")
    return dict(sorted(current_sources.items())), dict(sorted(current_native_ids.items()))


def _authoritative_map(
    authoritative_instance_sources: Mapping[str, str | None] | None,
    records: Sequence[AEInspection],
) -> dict[str, str | None]:
    if authoritative_instance_sources is not None:
        result: dict[str, str | None] = {}
        for instance_id, source_id in authoritative_instance_sources.items():
            result[_bounded_id(instance_id)] = _bounded_id(source_id, optional=True)
        return result
    result = {}
    for record in records:
        for instance_id, source_id in record.layer_sources.items():
            previous = result.get(instance_id, "__missing__")
            if previous != "__missing__" and previous != source_id:
                raise ValueError(f"conflicting layer source inventory: {instance_id!r}")
            result[instance_id] = source_id
        for layer in record.layers:
            previous = result.get(layer.layer_instance_id, "__missing__")
            if previous != "__missing__" and previous != layer.source_element_id:
                raise ValueError(f"conflicting layer source inventory: {layer.layer_instance_id!r}")
            result[layer.layer_instance_id] = layer.source_element_id
    return result


def _authoritative_native_map(
    authoritative_instance_native_ids: Mapping[str, int] | None,
    records: Sequence[AEInspection],
) -> dict[str, int]:
    result: dict[str, int] = {}
    native_instances: dict[int, str] = {}
    if authoritative_instance_native_ids is not None:
        for instance_id, native_id in authoritative_instance_native_ids.items():
            instance = _bounded_id(instance_id)
            native = _native_layer_id(native_id)
            owner = native_instances.get(native)
            if owner is not None and owner != instance:
                raise ValueError("duplicate native layer id")
            native_instances[native] = instance
            result[instance] = native
        return result
    for record in records:
        for instance_id, native_id in record.layer_native_ids.items():
            owner = native_instances.get(native_id)
            if owner is not None and owner != instance_id:
                raise ValueError("duplicate native layer id")
            previous = result.get(instance_id)
            if previous is not None and previous != native_id:
                raise ValueError(f"conflicting native layer inventory: {instance_id!r}")
            native_instances[native_id] = instance_id
            result[instance_id] = native_id
    return result


def _validate_layer(
    layer: AELayerInventory,
    authoritative: Mapping[str, str | None],
    native_authoritative: Mapping[str, int],
) -> None:
    expected = authoritative.get(layer.layer_instance_id, "__missing__")
    if expected == "__missing__":
        raise ValueError(f"unknown layer instance {layer.layer_instance_id!r}")
    if layer.source_element_id != expected:
        raise ValueError(
            f"layer {layer.layer_instance_id!r} source spoof: "
            f"{layer.source_element_id!r} != {expected!r}"
        )
    expected_native = native_authoritative.get(layer.layer_instance_id, "__missing__")
    if expected_native == "__missing__":
        raise ValueError(f"unknown native layer instance {layer.layer_instance_id!r}")
    if layer.native_layer_id != expected_native:
        raise ValueError(
            f"layer {layer.layer_instance_id!r} native id spoof: "
            f"{layer.native_layer_id!r} != {expected_native!r}"
        )



def _unwrap_instance_rotations(
    rows: dict[tuple[str, int], AESourceSample],
) -> None:
    by_source: dict[str, list[tuple[tuple[str, int], AESourceSample]]] = {}
    for key, row in rows.items():
        if row.layer_instance_id is None or row.transform is None:
            continue
        source_key = row.source_element_id
        if source_key is None:
            source_key = f"__instance__:{row.layer_instance_id}"
        by_source.setdefault(source_key, []).append((key, row))
    for entries in by_source.values():
        previous_raw: float | None = None
        unwrapped: float | None = None
        for key, row in sorted(entries, key=lambda item: (item[0][1], item[0][0])):
            transform = row.transform
            if transform is None:
                continue
            raw = float(transform[4])
            if previous_raw is None or unwrapped is None:
                unwrapped = raw
            else:
                delta = raw - previous_raw
                while delta > 180.0:
                    delta -= 360.0
                while delta < -180.0:
                    delta += 360.0
                unwrapped += delta
            previous_raw = raw
            values = list(transform)
            values[4] = unwrapped
            rows[key] = row.model_copy(update={"transform": tuple(values)})


def merge_inspection_chunks(
    scene: Scene,
    chunks: Iterable[AEInspection | Mapping[str, Any]],
    *,
    authoritative_instance_sources: Mapping[str, str | None] | None = None,
    authoritative_instance_native_ids: Mapping[str, int] | None = None,
) -> AEInspection:
    """Merge chunks and aggregate only complete per-instance observations."""
    records = [_as_inspection(chunk) for chunk in chunks]
    if records:
        expected_sources, expected_native_ids = _inspection_identity(records[0])
        for record in records[1:]:
            sources, native_ids = _inspection_identity(record)
            if sources != expected_sources or native_ids != expected_native_ids:
                raise ValueError("inspection chunks have conflicting layer identities")

    authoritative = _authoritative_map(authoritative_instance_sources, records)
    native_authoritative = _authoritative_native_map(
        authoritative_instance_native_ids, records
    )
    if set(authoritative) != set(native_authoritative):
        raise ValueError("layer native identity inventory is incomplete")

    bound_requested = any(record.requested for record in records)
    requested_pairs: set[tuple[str, int]] = set()
    record_requests: dict[int, set[tuple[str, int]]] = {}
    for record_index, record in enumerate(records):
        declared = {
            (pair.source_element_id, pair.frame) for pair in record.requested
        }
        if declared:
            if requested_pairs.intersection(declared):
                raise ValueError("inspection chunks contain duplicate requested pairs")
            for source_id, frame in declared:
                if frame >= scene.frames:
                    raise ValueError(
                        f"requested observation frame is outside scene: {frame}"
                    )
            requested_pairs.update(declared)
        record_requests[record_index] = declared

    all_layers: dict[str, AELayerInventory] = {}
    all_source_rows: dict[tuple[str, int], AESourceSample] = {}
    raw_rows: dict[tuple[str, int], AESourceSample] = {}
    raw_source_pairs: set[tuple[str, int]] = set()
    missing_pairs: set[tuple[str, int]] = set()
    record_evidence: dict[int, set[tuple[str, int]]] = {
        index: set() for index in range(len(records))
    }
    for record_index, record in enumerate(records):
        for instance_id, source_id in record.layer_sources.items():
            expected = authoritative.get(instance_id, "__missing__")
            if expected == "__missing__" or expected != source_id:
                raise ValueError(f"layer source inventory spoof: {instance_id!r}")
        for instance_id, native_id in record.layer_native_ids.items():
            expected = native_authoritative.get(instance_id, "__missing__")
            if expected == "__missing__" or expected != native_id:
                raise ValueError(f"layer native inventory spoof: {instance_id!r}")

        local_layer_ids: set[str] = set()
        for layer in record.layers:
            _validate_layer(layer, authoritative, native_authoritative)
            if layer.layer_instance_id in local_layer_ids:
                raise ValueError(
                    f"duplicate layer inventory row: {layer.layer_instance_id!r}"
                )
            local_layer_ids.add(layer.layer_instance_id)
            previous = all_layers.get(layer.layer_instance_id)
            if previous is not None:
                if previous != layer:
                    raise ValueError(
                        f"conflicting layer inventory row: {layer.layer_instance_id!r}"
                    )
                continue
            all_layers[layer.layer_instance_id] = layer
        for pair in record.missing_pairs:
            if pair.frame >= scene.frames:
                raise ValueError(
                    f"missing observation frame is outside scene: {pair.frame}"
                )
            key = (pair.source_element_id, pair.frame)
            if record.requested and key not in record_requests[record_index]:
                raise ValueError("missing observation is outside its requested pairs")
            record_evidence[record_index].add(key)
            missing_pairs.add(key)

    if set(all_layers) != set(authoritative):
        raise ValueError("inspection layer inventory is incomplete")
    for source_id, _frame in requested_pairs:
        if source_id not in {
            value for value in authoritative.values() if value is not None
        }:
            raise ValueError(
                f"requested source is not in the authoritative inventory: {source_id!r}"
            )

    for record_index, record in enumerate(records):
        declared = record_requests[record_index]
        for row in record.samples:
            if row.frame >= scene.frames:
                raise ValueError(f"observation frame is outside scene: {row.frame}")
            source_key = (row.source_element_id, row.frame)
            if record.requested and source_key not in declared:
                raise ValueError("observation is outside its requested pairs")
            record_evidence[record_index].add(source_key)
            if row.layer_instance_id is not None:
                expected = authoritative.get(row.layer_instance_id, "__missing__")
                if expected == "__missing__":
                    raise ValueError(f"unknown layer instance {row.layer_instance_id!r}")
                expected_native = native_authoritative.get(
                    row.layer_instance_id, "__missing__"
                )
                if expected_native == "__missing__":
                    raise ValueError(
                        f"unknown native layer instance {row.layer_instance_id!r}"
                    )
                if row.source_element_id != expected:
                    raise ValueError(
                        f"sample source spoof for {row.layer_instance_id!r}: "
                        f"{row.source_element_id!r} != {expected!r}"
                    )
                key = (row.layer_instance_id, row.frame)
                if key in raw_rows:
                    raise ValueError(f"duplicate raw observation row: {key!r}")
                if source_key in all_source_rows:
                    raise ValueError(
                        f"conflicting raw/source observation row: {source_key!r}"
                    )
                raw_rows[key] = row
                raw_source_pairs.add(source_key)
                continue

            provenance = row.provenance or record.provenance
            if not provenance and row.active:
                raise ValueError("source-aggregated sample requires panel provenance")
            for instance_id in provenance:
                expected = authoritative.get(instance_id, "__missing__")
                if expected == "__missing__":
                    raise ValueError(f"unknown provenance layer {instance_id!r}")
                if instance_id not in native_authoritative:
                    raise ValueError(f"unknown native provenance layer {instance_id!r}")
                if expected != row.source_element_id:
                    raise ValueError(
                        f"sample provenance source spoof for {instance_id!r}: "
                        f"{expected!r} != {row.source_element_id!r}"
                    )
            if tuple(provenance) != row.provenance:
                row = row.model_copy(update={"provenance": tuple(provenance)})
            if source_key in all_source_rows:
                raise ValueError(f"duplicate source observation row: {source_key!r}")
            if source_key in raw_source_pairs:
                raise ValueError(
                    f"conflicting source/raw observation row: {source_key!r}"
                )
            all_source_rows[source_key] = row

        if declared:
            missing_declared = declared - record_evidence[record_index]
            missing_pairs.update(missing_declared)
        elif bound_requested and record_evidence[record_index]:
            raise ValueError("inspection chunk omitted its requested pairs")

    layer_lookup = all_layers
    _unwrap_instance_rotations(raw_rows)
    grouped_raw: dict[tuple[str, int], list[AESourceSample]] = {}
    for (instance_id, frame), row in raw_rows.items():
        layer = layer_lookup.get(instance_id)
        if layer is None:
            raise ValueError(f"raw sample has no inventory row: {instance_id!r}")
        if row.active and not (layer.frame_start <= frame < layer.frame_end):
            raise ValueError(f"active sample is outside layer interval: {instance_id!r}")
        grouped_raw.setdefault((row.source_element_id, frame), []).append(row)

    mapped_instances: dict[str, set[str]] = {}
    for instance_id, source_id in authoritative.items():
        if source_id is not None:
            mapped_instances.setdefault(source_id, set()).add(instance_id)

    candidate_pairs = (
        set(missing_pairs)
        | set(requested_pairs)
        | set(raw_source_pairs)
        | set(all_source_rows)
    )
    incomplete_pairs: set[tuple[str, int]] = set()
    complete_aggregate_pairs: set[tuple[str, int]] = set()
    complete_raw_pairs: set[tuple[str, int]] = set()
    motion_required, bounds_required = _required_geometry_pairs(scene)
    for source_id, frame in candidate_pairs:
        expected_instances = mapped_instances.get(source_id, set())
        raw = grouped_raw.get((source_id, frame), ())
        aggregate = all_source_rows.get((source_id, frame))
        complete = (source_id, frame) not in missing_pairs
        if raw:
            returned_instances = {row.layer_instance_id for row in raw}
            complete = complete and returned_instances == expected_instances
            if (source_id, frame) in motion_required:
                complete = complete and all(
                    not row.active or row.transform is not None for row in raw
                )
            if (source_id, frame) in bounds_required:
                complete = complete and all(
                    not row.active or row.world_bounds is not None for row in raw
                )
            if complete:
                complete_raw_pairs.add((source_id, frame))
        elif aggregate is not None:
            provenance = set(aggregate.provenance)
            complete = complete and not bound_requested
            complete = complete and provenance == expected_instances
            if complete:
                complete_aggregate_pairs.add((source_id, frame))
        else:
            complete = False
        if not complete:
            incomplete_pairs.add((source_id, frame))
    missing_pairs.update(incomplete_pairs)

    merged_rows: list[AESourceSample] = [
        row for key, row in all_source_rows.items() if key in complete_aggregate_pairs
    ]
    for pair, rows in grouped_raw.items():
        if pair not in complete_raw_pairs:
            continue
        active_rows = [row for row in rows if row.active]
        if not active_rows:
            merged_rows.append(
                AESourceSample(
                    source_element_id=pair[0],
                    frame=pair[1],
                    active=False,
                    provenance=tuple(
                        sorted(row.layer_instance_id for row in rows if row.layer_instance_id)
                    ),
                )
            )
            continue
        selected = min(active_rows, key=lambda row: row.layer_instance_id or "")
        bounds = [row.world_bounds for row in active_rows if row.world_bounds is not None]
        union: tuple[float, float, float, float] | None = None
        if bounds:
            union = (
                min(value[0] for value in bounds),
                min(value[1] for value in bounds),
                max(value[2] for value in bounds),
                max(value[3] for value in bounds),
            )
        merged_rows.append(
            AESourceSample(
                source_element_id=pair[0],
                frame=pair[1],
                active=True,
                transform=selected.transform,
                world_bounds=union,
                provenance=tuple(
                    sorted(row.layer_instance_id for row in active_rows if row.layer_instance_id)
                ),
            )
        )

    source_ids = {element.id for element in scene.elements}
    filtered_rows = [row for row in merged_rows if row.source_element_id in source_ids]
    return AEInspection(
        layers=tuple(
            sorted(all_layers.values(), key=lambda row: (row.index, row.layer_instance_id))
        ),
        layer_sources=dict(sorted(authoritative.items())),
        layer_native_ids=dict(sorted(native_authoritative.items())),
        requested=tuple(
            AEObservationRequest(source_element_id=source_id, frame=frame)
            for source_id, frame in sorted(requested_pairs, key=lambda pair: (pair[0], pair[1]))
        ),
        samples=tuple(
            sorted(filtered_rows, key=lambda row: (row.source_element_id, row.frame))
        ),
        missing_pairs=tuple(
            AEObservationRequest(source_element_id=source_id, frame=frame)
            for source_id, frame in sorted(
                missing_pairs, key=lambda pair: (pair[0], pair[1])
            )
        ),
        source_aggregated=True,
    )


def _required_geometry_pairs(
    scene: Scene,
) -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
    """Split validated keep requests by the geometry each predicate needs."""
    required_observation_frames(scene)
    motions = {motion.id: motion for motion in extract_motions(scene)}
    transform_pairs: set[tuple[str, int]] = set()
    bounds_pairs: set[tuple[str, int]] = set()
    for constraint in scene.constraints:
        if not constraint.keep:
            continue
        name, args, explicit_frame = parse_pred(constraint.pred)
        if name in _MOTION_PREDICATES:
            references = args if name in {"before", "after", "while"} else [args[0]]
            for motion_id in references:
                motion = motions[motion_id]
                transform_pairs.update(
                    (motion.element, frame) for frame in range(scene.frames)
                )
        elif name in _SPATIAL_PREDICATES:
            frame = scene.frames - 1 if explicit_frame is None else explicit_frame
            bounds_pairs.update((element_id, frame) for element_id in args)
    return transform_pairs, bounds_pairs


def _observed_arrays(
    scene: Scene, inspection: AEInspection
) -> tuple[
    dict[str, np.ndarray],
    dict[str, np.ndarray],
    set[tuple[str, int]],
    set[tuple[str, int]],
]:
    matrices = {
        element.id: np.full((scene.frames, len(COLS)), np.nan, dtype=float)
        for element in scene.elements
    }
    bboxes = {
        element.id: np.full((scene.frames, 4), np.nan, dtype=float)
        for element in scene.elements
    }
    transform_observed: set[tuple[str, int]] = set()
    bounds_observed: set[tuple[str, int]] = set()
    missing = {(pair.source_element_id, pair.frame) for pair in inspection.missing_pairs}
    for row in inspection.samples:
        if row.source_element_id not in matrices:
            continue
        if row.frame >= scene.frames:
            continue
        pair = (row.source_element_id, row.frame)
        if pair in missing:
            continue
        if not row.active:
            transform_observed.add(pair)
            bounds_observed.add(pair)
            continue
        if row.transform is not None:
            matrices[row.source_element_id][row.frame] = row.transform
            transform_observed.add(pair)
        if row.world_bounds is not None:
            bboxes[row.source_element_id][row.frame] = row.world_bounds
            bounds_observed.add(pair)
    return matrices, bboxes, transform_observed, bounds_observed


def _motion_wire(motion: Motion) -> AEObservedMotion:
    return AEObservedMotion(
        id=motion.id,
        element=motion.element,
        type=motion.type,
        start=motion.start,
        end=motion.end,
        dir=motion.dir,
        mag=motion.mag,
        dur=motion.dur,
    )


def verify_inspection(
    scene: Scene,
    chunks: Iterable[AEInspection | Mapping[str, Any]],
    *,
    authoritative_instance_sources: Mapping[str, str | None] | None = None,
    authoritative_instance_native_ids: Mapping[str, int] | None = None,
) -> AEVerificationReport:
    """Evaluate enabled keep predicates against merged AE observations."""
    inspection = merge_inspection_chunks(
        scene,
        chunks,
        authoritative_instance_sources=authoritative_instance_sources,
        authoritative_instance_native_ids=authoritative_instance_native_ids,
    )
    motion_required, bounds_required = _required_geometry_pairs(scene)
    matrices, bboxes, transform_observed, bounds_observed = _observed_arrays(
        scene, inspection
    )
    missing = sorted(
        (motion_required - transform_observed)
        | (bounds_required - bounds_observed),
        key=lambda pair: (pair[0], pair[1]),
    )
    violations = [
        f"missing observation: {source_id}@{frame}"
        for source_id, frame in missing
    ]

    try:
        motions = extract_motions_from_matrices(scene, matrices)
        context = PredContext(
            scene=scene,
            motions={motion.id: motion for motion in motions},
            bboxes=bboxes,
        )
    except Exception as exc:
        motions = []
        context = None
        violations.append(f"observation matrices invalid: {exc}")

    predicate_results: list[AEPredicateResult] = []
    for constraint in scene.constraints:
        if not constraint.keep:
            continue
        predicate_violations: list[str] = []
        try:
            passed = bool(eval_pred(constraint.pred, context)) if context is not None else False
        except Exception as exc:
            passed = False
            predicate_violations.append(f"predicate error: {exc}")
        if not passed and not predicate_violations:
            predicate_violations.append("predicate failed")
        if predicate_violations:
            violations.extend(f"{constraint.pred}: {item}" for item in predicate_violations)
        predicate_results.append(
            AEPredicateResult(
                predicate=constraint.pred,
                passed=passed,
                violations=tuple(predicate_violations),
            )
        )

    observed_motion_records: list[AEObservedMotion] = []
    for motion in motions:
        try:
            observed_motion_records.append(_motion_wire(motion))
        except Exception as exc:
            violations.append(f"observed motion {motion.id} is invalid: {exc}")

    return AEVerificationReport(
        passed=not violations and all(result.passed for result in predicate_results),
        keep_results=tuple(predicate_results),
        violations=tuple(violations),
        observed_motions=tuple(observed_motion_records),
        inspection=inspection,
    )


# Explicit aliases keep call sites readable while retaining one implementation.
LayerInventory = AELayerInventory
SourceSample = AESourceSample
SourceObservation = AESourceSample
Inspection = AEInspection
VerificationReport = AEVerificationReport
merge_observation_chunks = merge_inspection_chunks
merge_inspections = merge_inspection_chunks
verify_observations = verify_inspection
verify_ae_inspection = verify_inspection
evaluate_inspection = verify_inspection
chunk_observation_frames = chunk_observation_pairs
chunk_observation_requests = chunk_observation_pairs


__all__ = [
    "INSPECTION_SCHEMA",
    "VERIFICATION_SCHEMA",
    "AELayerInventory",
    "AESourceSample",
    "AEObservationRequest",
    "AEInspection",
    "AEObservedMotion",
    "AEPredicateResult",
    "AEVerificationReport",
    "LayerInventory",
    "SourceSample",
    "SourceObservation",
    "Inspection",
    "VerificationReport",
    "validate_manual_inventory",
    "chunk_observation_pairs",
    "chunk_observation_frames",
    "chunk_observation_requests",
    "chunk_required_observations",
    "merge_inspection_chunks",
    "merge_observation_chunks",
    "merge_inspections",
    "verify_inspection",
    "verify_observations",
    "verify_ae_inspection",
    "evaluate_inspection",
]
