from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ..ir.store import load_scene
from ..render.plan import (
    PlanConflict,
    RenderMode,
    _final_gate,
    _resolve_version,
    RenderPlan,
    create_render_plan,
    load_render_plan,
)
from .compatibility import (
    AECompatibilityIssue,
    analyze_ae_compatibility,
    parse_ae_substitutions,
    propose_ae_substitutions,
)
from .locks import resolve_locked_source_ids
from .mapping import AEMappingError, validate_mapping_domains
from .models import AECapabilities, AESubstitution
from .operations import Operation


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")



class AERenderDraft(BaseModel):
    """A compatibility proposal that is deliberately not an approvable plan."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    project_id: str
    scene_id: str
    version_id: str
    mode: RenderMode
    direction: str | None = None
    capability_hash: str
    locked_source_ids: tuple[str, ...] = ()
    compatibility_issues: tuple[AECompatibilityIssue, ...] = ()
    substitutions: tuple[AESubstitution, ...] = ()
    substitutions_acknowledged: Literal[False] = False
    expected_outputs: tuple[str, ...] = ()

    @field_validator("project_id", "scene_id", "version_id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise ValueError("render draft identifier is invalid")
        return value

    @field_validator("direction")
    @classmethod
    def _direction(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 4096:
            raise ValueError("direction is too long")
        return value

    @field_validator("capability_hash")
    @classmethod
    def _capability_hash(cls, value: str) -> str:
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ValueError("capability hash is invalid")
        return value

    @field_validator("locked_source_ids")
    @classmethod
    def _locked_source_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(values)
        if any(not isinstance(value, str) or not _ID_RE.fullmatch(value) for value in normalized):
            raise ValueError("locked source id is invalid")
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("locked source ids must be sorted and unique")
        return normalized

    @field_validator("expected_outputs")
    @classmethod
    def _expected_outputs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(values)
        if any(not isinstance(value, str) or not value for value in normalized):
            raise ValueError("expected output is invalid")
        if len(set(normalized)) != len(normalized):
            raise ValueError("expected outputs must be unique")
        return normalized

    @model_validator(mode="before")
    @classmethod
    def _wire_sequences(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        raw = dict(value)
        for field in ("locked_source_ids", "expected_outputs"):
            if isinstance(raw.get(field), list):
                raw[field] = tuple(raw[field])
        issues = raw.get("compatibility_issues")
        if isinstance(issues, list):
            normalized_issues = []
            for item in issues:
                item = dict(item) if isinstance(item, Mapping) else item
                if isinstance(item, dict) and isinstance(item.get("lost_semantics"), list):
                    item["lost_semantics"] = tuple(item["lost_semantics"])
                normalized_issues.append(item)
            raw["compatibility_issues"] = tuple(normalized_issues)
        substitutions = raw.get("substitutions")
        if isinstance(substitutions, list):
            normalized_substitutions = []
            for item in substitutions:
                item = dict(item) if isinstance(item, Mapping) else item
                if isinstance(item, dict):
                    for field in ("proposed_layers", "proposed_effects", "lost_semantics"):
                        if isinstance(item.get(field), list):
                            item[field] = tuple(item[field])
                normalized_substitutions.append(item)
            raw["substitutions"] = tuple(normalized_substitutions)
        return raw


    @property
    def project(self) -> str:
        return self.project_id

    @property
    def scene(self) -> str:
        return self.scene_id

    @property
    def version(self) -> str:
        return self.version_id

    @property
    def locked_targets(self) -> tuple[str, ...]:
        return self.locked_source_ids


def _capability_manifest(capabilities: AECapabilities) -> dict[str, Any]:
    return capabilities.model_dump(
        mode="json",
        exclude={"capability_hash", "project_open", "timestamp"},
    )


def _operation_models() -> tuple[type[BaseModel], ...]:
    operation_union = get_args(Operation)[0]
    return tuple(get_args(operation_union))


_HARD_COMPATIBILITY_KEYS = frozenset({"composition", "group_transform", "background"})


def _operation_runtime_contract_sha256() -> str:
    return hashlib.sha256(Path(__file__).with_name("operations.py").read_bytes()).hexdigest()


def _reject_non_substitutable_issues(issues: Sequence[AECompatibilityIssue]) -> None:
    hard = tuple(issue for issue in issues if issue.semantic_key in _HARD_COMPATIBILITY_KEYS)
    if hard:
        details = "; ".join(f"{issue.semantic_key}: {issue.reason}" for issue in hard)
        raise PlanConflict(f"AE compatibility issues are not substitutable: {details}")


def _preflight_mapping(
    scene: Any,
    capabilities: AECapabilities,
    substitutions: Sequence[AESubstitution],
) -> None:
    try:
        validate_mapping_domains(scene, capabilities, substitutions)
    except AEMappingError as exc:
        raise PlanConflict(f"AE mapping domain validation failed: {exc}") from exc


def current_operation_manifest() -> tuple[dict[str, Any], ...]:
    """Return the deterministic full schema for every accepted operation kind."""
    runtime_contract_sha256 = _operation_runtime_contract_sha256()
    records: list[dict[str, Any]] = []
    for model in _operation_models():
        schema = model.model_json_schema(mode="serialization")
        kinds = get_args(model.model_fields["kind"].annotation)
        for kind in kinds:
            records.append(
                {
                    "kind": kind,
                    "runtime_contract_sha256": runtime_contract_sha256,
                    "schema": schema,
                }
            )
    return tuple(sorted(records, key=lambda item: item["kind"]))


def _effect_manifest(capabilities: AECapabilities) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "match_name": effect.match_name,
            "properties": dict(sorted(effect.properties.items())),
        }
        for effect in capabilities.capabilities.effects
    )


def _outputs(mode: RenderMode, artifact_contract: Mapping[str, Any] | None) -> tuple[str, ...]:
    if artifact_contract is not None:
        raw = artifact_contract.get("outputs")
        if isinstance(raw, (list, tuple)) and all(isinstance(value, str) and value for value in raw):
            return tuple(raw)
    return ("frames", "mp4") if mode == "preview" else ("mp4", "project")


def _validated_issues(value: Sequence[AECompatibilityIssue | Mapping[str, Any]]) -> tuple[AECompatibilityIssue, ...]:
    normalized: list[AECompatibilityIssue] = []
    for item in value:
        if isinstance(item, AECompatibilityIssue):
            normalized.append(item)
            continue
        raw = dict(item)
        if isinstance(raw.get("lost_semantics"), list):
            raw["lost_semantics"] = tuple(raw["lost_semantics"])
        normalized.append(AECompatibilityIssue.model_validate(raw))
    return tuple(normalized)


def _validated_substitution_wire(value: Sequence[AESubstitution | Mapping[str, Any]]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, AESubstitution):
            raw = item.model_dump(mode="json")
        elif isinstance(item, Mapping):
            raw = dict(item)
        else:
            raise PlanConflict("AE substitutions must be objects")
        # The model never supplies acknowledgement.  The explicit request
        # flag below is the only user acknowledgement accepted by this seam.
        raw["acknowledged"] = False
        wire.append(raw)
    return wire

def _indexed_effects(
    effects: Sequence[str | Mapping[str, Any]],
    *,
    offset: int,
) -> tuple[dict[str, Any], ...]:
    indexed: list[dict[str, Any]] = []
    for effect in effects:
        if isinstance(effect, str):
            effect_name = effect
            properties: Mapping[str, Any] = {}
            layer_index = 0
        elif isinstance(effect, Mapping):
            effect_name = effect.get("effect_name")
            properties = effect.get("properties", {})
            layer_index = effect.get("layer_index", 0)
        else:
            raise PlanConflict("AE substitution effect is invalid")
        if (
            not isinstance(effect_name, str)
            or not effect_name
            or not isinstance(properties, Mapping)
            or isinstance(layer_index, bool)
            or not isinstance(layer_index, int)
            or layer_index < 0
        ):
            raise PlanConflict("AE substitution effect is invalid")
        indexed.append(
            {
                "effect_name": effect_name,
                "properties": dict(properties),
                "layer_index": layer_index + offset,
            }
        )
    return tuple(indexed)


def _merge_substitutions(
    substitutions: Sequence[AESubstitution],
) -> tuple[AESubstitution, ...]:
    merged: list[AESubstitution] = []
    positions: dict[tuple[str, str], int] = {}
    layer_counts: dict[tuple[str, str], int] = {}
    for substitution in substitutions:
        key = (substitution.source_element_id, substitution.source_type)
        offset = layer_counts.get(key, 0)
        normalized = substitution.model_copy(
            update={
                "proposed_effects": _indexed_effects(
                    substitution.proposed_effects,
                    offset=offset,
                )
            }
        )
        layer_counts[key] = offset + len(substitution.proposed_layers)
        position = positions.get(key)
        if position is None:
            positions[key] = len(merged)
            merged.append(normalized)
            continue
        previous = merged[position]
        lost_semantics = tuple(
            sorted(set(previous.lost_semantics).union(normalized.lost_semantics))
        )
        merged[position] = previous.model_copy(
            update={
                "proposed_layers": previous.proposed_layers + normalized.proposed_layers,
                "proposed_effects": previous.proposed_effects + normalized.proposed_effects,
                "lost_semantics": lost_semantics,
                "reason": previous.reason or normalized.reason,
            }
        )
    return tuple(merged)


def _validate_predecessor(
    root: Path,
    *,
    predecessor_id: str | None,
    predecessor_digest: str | None,
    predecessor_checkpoint: int | None,
    predecessor_checkpoint_digest: str | None,
    project_id: str,
    scene_id: str,
    version_id: str,
    mode: RenderMode,
) -> RenderPlan | None:
    if (predecessor_id is None) != (predecessor_digest is None):
        raise PlanConflict("predecessor id and digest must be supplied together")
    checkpoint_bound = (
        predecessor_checkpoint is not None
        or predecessor_checkpoint_digest is not None
    )
    if checkpoint_bound and (
        predecessor_checkpoint is None
        or predecessor_checkpoint_digest is None
    ):
        raise PlanConflict("predecessor checkpoint index and digest must be supplied together")
    if mode == "preview" and checkpoint_bound:
        raise PlanConflict("preview plans cannot bind a predecessor checkpoint")
    if mode == "final" and predecessor_id is None:
        raise PlanConflict("final AE plans require a preview predecessor")
    if mode == "final" and not checkpoint_bound:
        raise PlanConflict("final AE plans require a predecessor checkpoint")
    if predecessor_id is None:
        return None
    if not isinstance(predecessor_digest, str) or not _SHA256_RE.fullmatch(predecessor_digest):
        raise PlanConflict("predecessor digest is invalid")
    if (
        predecessor_checkpoint is not None
        and (
            isinstance(predecessor_checkpoint, bool)
            or not isinstance(predecessor_checkpoint, int)
            or predecessor_checkpoint < 0
        )
    ):
        raise PlanConflict("predecessor checkpoint is invalid")
    if checkpoint_bound and (
        not isinstance(predecessor_checkpoint_digest, str)
        or not _SHA256_RE.fullmatch(predecessor_checkpoint_digest)
    ):
        raise PlanConflict("predecessor checkpoint digest is invalid")
    try:
        prior = load_render_plan(root, predecessor_id)
    except PlanConflict as exc:
        raise PlanConflict("predecessor render plan is unavailable") from exc
    if (
        prior.id != predecessor_id
        or prior.digest != predecessor_digest
        or prior.project_id != project_id
        or prior.scene_id != scene_id
        or prior.version_id != version_id
        or prior.backend != "after_effects"
        or (mode == "final" and prior.mode != "preview")
    ):
        raise PlanConflict("predecessor does not match the authoritative AE preview")
    if mode == "final":
        from .coordinator import AECoordinator, CoordinatorConflict

        try:
            session = AECoordinator.cached(root, prior.id).state()
        except CoordinatorConflict as exc:
            raise PlanConflict("predecessor coordinator state is unavailable") from exc
        checkpoint = next(
            (
                item
                for item in session.checkpoints
                if item.index == predecessor_checkpoint
            ),
            None,
        )
        if (
            not session.baseline_complete
            or session.selected_checkpoint != predecessor_checkpoint
            or checkpoint is None
            or not checkpoint.passed
            or not checkpoint.lineage_valid
            or checkpoint.context_digest != predecessor_checkpoint_digest
        ):
            raise PlanConflict(
                "predecessor checkpoint is not the selected passing checkpoint"
            )
    return prior

def prepare_ae_render_plan(
    root: Path,
    *,
    project_id: str,
    scene_id: str,
    version_id: str | None = None,
    mode: RenderMode,
    direction: str | None = None,
    capabilities: AECapabilities,
    client: Any | None = None,
    substitutions: Sequence[AESubstitution | Mapping[str, Any]] | None = None,
    substitutions_acknowledged: bool = False,
    compatibility_issues: Sequence[AECompatibilityIssue | Mapping[str, Any]] | None = None,
    artifact_contract: Mapping[str, Any] | None = None,
    predecessor_id: str | None = None,
    predecessor_digest: str | None = None,
    predecessor_checkpoint: int | None = None,
    predecessor_checkpoint_digest: str | None = None,
) -> RenderPlan | AERenderDraft:
    """Prepare an immutable AE plan or return a frozen compatibility draft.

    The scene, version, locks, operation vocabulary, and capability manifest
    all come from server-owned state.  User/model substitutions are accepted
    only after a fresh compatibility pass and an explicit acknowledgement.
    """
    if not isinstance(capabilities, AECapabilities):
        try:
            capabilities = AECapabilities.model_validate(capabilities)
        except (TypeError, ValueError) as exc:
            raise PlanConflict("AE capabilities are invalid") from exc
    if mode not in ("preview", "final"):
        raise PlanConflict("invalid AE render mode")
    if direction is not None and not isinstance(direction, str):
        raise PlanConflict("direction must be a string or None")
    if substitutions_acknowledged is not False and substitutions_acknowledged is not True:
        raise PlanConflict("substitution acknowledgement is invalid")
    if substitutions is None and substitutions_acknowledged:
        raise PlanConflict("substitution acknowledgement requires substitutions")

    root = Path(root)
    try:
        meta, version, scene_path = _resolve_version(root, project_id, scene_id, version_id)
        scene = load_scene(scene_path)
    except PlanConflict:
        raise
    except Exception as exc:  # noqa: BLE001 - authoritative source boundary
        raise PlanConflict("authoritative AE scene is invalid") from exc
    if scene.id != scene_id:
        raise PlanConflict("scene id does not match the authoritative version")
    if mode == "final":
        _final_gate(meta, scene_id, version.id)
    predecessor = _validate_predecessor(
        root,
        predecessor_id=predecessor_id,
        predecessor_digest=predecessor_digest,
        predecessor_checkpoint=predecessor_checkpoint,
        predecessor_checkpoint_digest=predecessor_checkpoint_digest,
        project_id=project_id,
        scene_id=scene_id,
        version_id=version.id,
        mode=mode,
    )

    current_issues = analyze_ae_compatibility(scene, capabilities)
    _reject_non_substitutable_issues(current_issues)
    if compatibility_issues is not None:
        expected = _validated_issues(compatibility_issues)
        if expected != current_issues:
            raise PlanConflict("AE compatibility issues changed")

    locked_source_ids = tuple(resolve_locked_source_ids(scene))
    capability_manifest = _capability_manifest(capabilities)
    operation_manifest = current_operation_manifest()
    effect_manifest = _effect_manifest(capabilities)
    expected_outputs = _outputs(mode, artifact_contract)

    if current_issues and not substitutions:
        if substitutions_acknowledged:
            raise PlanConflict("substitution acknowledgement requires substitutions")
        if client is None:
            raise PlanConflict("an LLM client is required for AE compatibility proposals")
        proposed = propose_ae_substitutions(client, current_issues, capabilities)
        return AERenderDraft(
            project_id=project_id,
            scene_id=scene_id,
            version_id=version.id,
            mode=mode,
            direction=direction,
            capability_hash=capabilities.capability_hash or "",
            locked_source_ids=locked_source_ids,
            compatibility_issues=current_issues,
            substitutions=proposed,
            expected_outputs=expected_outputs,
        )

    if substitutions is None:
        substitutions = ()
    if current_issues and not substitutions_acknowledged:
        raise PlanConflict("substitution acknowledgement is required")
    if not current_issues and substitutions:
        raise PlanConflict("substitutions do not match current AE compatibility")

    if not current_issues:
        _preflight_mapping(scene, capabilities, ())
        acknowledged: tuple[AESubstitution, ...] = ()
    else:
        wire = _validated_substitution_wire(substitutions)
        try:
            validated = parse_ae_substitutions(wire, current_issues, capabilities)
        except (TypeError, ValueError) as exc:
            raise PlanConflict("AE substitutions do not match current compatibility") from exc
        acknowledged = tuple(
            item.model_copy(update={"acknowledged": True})
            for item in _merge_substitutions(validated)
        )
        _preflight_mapping(scene, capabilities, acknowledged)
    canonical_substitutions = tuple(item.model_dump(mode="json") for item in acknowledged)

    return create_render_plan(
        root,
        project_id=project_id,
        scene_id=scene_id,
        version_id=version.id,
        backend="after_effects",
        mode=mode,
        direction=direction,
        locked_targets=locked_source_ids,
        permitted_operations=operation_manifest,
        effect_schemas=effect_manifest,
        capability_hash=capabilities.capability_hash,
        capability_manifest=capability_manifest,
        substitutions=canonical_substitutions,
        substitutions_acknowledged=bool(acknowledged),
        artifact_contract=artifact_contract,
        predecessor_id=predecessor_id,
        predecessor_digest=predecessor_digest,
        predecessor_checkpoint=predecessor_checkpoint,
        predecessor_checkpoint_digest=predecessor_checkpoint_digest,
        _predecessor_plan=predecessor if mode == "final" else None,
    )


__all__ = ["AERenderDraft", "current_operation_manifest", "prepare_ae_render_plan"]
