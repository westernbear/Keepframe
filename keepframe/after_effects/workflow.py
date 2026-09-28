from __future__ import annotations

"""Durable After Effects workflow orchestration.

The coordinator command journal is the state machine.  This service only derives
one next command from that journal; the only work allowed off the relay thread is
the configured multimodal model call.
"""

import secrets
import threading
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..render.plan import PlanAsset, load_render_plan_scene
from .coordinator import AECoordinator, CoordinatorConflict
from .mapping import BaselineMapping, map_baseline
from .operations import _REQUIRED_EFFECTS, _operation_properties
from .models import AECheckpoint, AESubstitution, canonical_json, json_digest
from .verification import (
    AEInspection,
    chunk_observation_pairs,
    merge_inspection_chunks,
    required_observation_frames,
    validate_manual_inventory,
    verify_inspection,
)
from .vision import (
    VisionFrameError,
    VisionModelError,
    VisionProtocolError,
    VisionStepResult,
    VisionUnsupported,
    run_vision_step,
    select_representative_frames,
)


_MAX_AEP_BYTES = 4_294_967_296
_MAX_MP4_BYTES = 4_294_967_296
_MAX_PNG_BYTES = 128 * 1024 * 1024
_MAX_AGENT_IDS = 1000


class AEWorkflowError(RuntimeError):
    """A server-side workflow derivation failed."""


class AEInspectionBudgetError(AEWorkflowError):
    """The connector cannot fit an inspection response under the wire cap."""


class AEWorkflowService:
    """Advance one durable AE session at a time.

    ``executor`` is deliberately an injected seam.  A production service owns
    one single-worker executor; tests can provide an immediate executor without
    changing the journal protocol.
    """

    def __init__(
        self,
        workspace: Path | str,
        llm_factory: Any,
        executor: Executor | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.llm_factory = llm_factory
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="keepframe-ae-model",
        )
        self._owns_executor = executor is None
        self._futures: dict[str, Future[Any]] = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def executor(self) -> Executor:
        return self._executor

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            futures = tuple(self._futures.values())
            self._futures.clear()
        # A caller-owned executor belongs to the test/application, not this
        # service.  Owned model work is allowed to finish before shutdown so a
        # provider request is never abandoned halfway through its call.
        if self._owns_executor:
            shutdown = getattr(self._executor, "shutdown", None)
            if callable(shutdown):
                shutdown(wait=True)
        else:
            for future in futures:
                if not future.done():
                    future.cancel()

    shutdown = close

    def advance(self, coordinator: AECoordinator) -> Any:
        """Derive and enqueue at most one next command.

        Queued and leased commands always win over derivation.  A completed
        model future is consumed synchronously by this method; only the model
        invocation itself runs in the executor.
        """

        with self._lock:
            if self._closed:
                return None
            state = coordinator.state()
            key = self._key(coordinator, state)
            base_key = self._base_key(coordinator)
            for stale_key, stale_future in tuple(self._futures.items()):
                if stale_key.startswith(f"{base_key}:") and stale_key != key:
                    stale_future.cancel()
                    del self._futures[stale_key]
            history = coordinator.command_history()
            if self._has_pending(history):
                return None
            future = self._futures.get(key)
            if future is not None:
                if not future.done():
                    return None
                del self._futures[key]
                try:
                    step = future.result()
                except VisionUnsupported:
                    self._pause(coordinator, "vision_unsupported")
                    return None
                except VisionProtocolError:
                    self._pause(coordinator, "protocol_error")
                    return None
                except VisionFrameError:
                    self._pause(coordinator, "vision_error")
                    return None
                except VisionModelError:
                    self._pause(coordinator, "model_error")
                    return None
                except Exception:
                    self._pause(coordinator, "model_error")
                    return None
                try:
                    self._handle_model_step(coordinator, step)
                except CoordinatorConflict:
                    return None
                except Exception:
                    self._pause(coordinator, "workflow_error")
                return None

            if state.status == "pause_requested" and state.checkpoint_required:
                capture_stage = next(
                    (
                        self._workflow(command).get("stage")
                        for command in reversed(history)
                        if command.kind == "apply_batch" and self._successful(command)
                    ),
                    None,
                )
                try:
                    if capture_stage == "baseline_apply":
                        return self._advance_baseline(
                            coordinator,
                            state,
                            history,
                            capture_only=True,
                        )
                    return self._advance_iterating(coordinator, state, history)
                except CoordinatorConflict:
                    return None
                except AEInspectionBudgetError:
                    self._pause(coordinator, "inspection_too_large")
                    return None
                except Exception:
                    self._pause(coordinator, "workflow_error")
                    return None
            if state.status == "pause_requested":
                try:
                    coordinator.transition(
                        "pause_error",
                        revision=state.revision,
                        reason=state.reason or "user",
                    )
                except CoordinatorConflict:
                    pass
                return None
            try:
                if state.status in {"waiting_for_connector", "paused:server_restart"}:
                    return None
                if state.status == "baseline":
                    return self._advance_baseline(coordinator, state, history)
                if state.status == "iterating":
                    return self._advance_iterating(coordinator, state, history)
                if state.status == "manual_edit":
                    return self._advance_manual(coordinator, state, history)
                if state.status == "finalizing":
                    return self._advance_finalizing(coordinator, state, history)
                return None
            except CoordinatorConflict:
                return None
            except AEInspectionBudgetError:
                self._pause(coordinator, "inspection_too_large")
                return None
            except Exception:
                self._pause(coordinator, "workflow_error")
                return None

    def manual_sync(self, coordinator: AECoordinator) -> Any:
        """Start a fresh manual attempt from an immutable server checkpoint."""
        with self._lock:
            state = coordinator.state()
            if state.status.startswith("paused:"):
                state = coordinator.transition("begin_manual", revision=state.revision)
            if self._has_pending(coordinator.command_history()):
                raise CoordinatorConflict("manual sync requires no active command")
            if state.status != "manual_edit":
                raise CoordinatorConflict("manual sync requires a paused session")
            if not state.manual_sync_requested:
                state = coordinator.transition("request_manual_sync", revision=state.revision)
            history = coordinator.command_history()
            return self._advance_manual(
                coordinator,
                state,
                history,
                allow_prepare=True,
            )


    @staticmethod
    def _base_key(coordinator: AECoordinator) -> str:
        return f"{coordinator.project_id}:{coordinator.plan_id}"

    @classmethod
    def _key(cls, coordinator: AECoordinator, state: Any | None = None) -> str:
        if state is None:
            return cls._base_key(coordinator)
        checkpoint = cls._checkpoint_index(state)
        return f"{cls._base_key(coordinator)}:{state.status}:{checkpoint}:{state.revision}"

    @staticmethod
    def _has_pending(history: Sequence[Any]) -> bool:
        return any(item.status in {"queued", "leased"} for item in history)

    @staticmethod
    def _pending_command(coordinator: AECoordinator) -> Any:
        pending = [
            item
            for item in coordinator.command_history()
            if item.status in {"queued", "leased"}
        ]
        return pending[-1] if pending else None

    @staticmethod
    def _checkpoint_index(state: Any) -> int | None:
        def valid(item: Any, index: int) -> bool:
            return item.index == index and getattr(item, "lineage_valid", True)

        open_checkpoint = getattr(state, "open_checkpoint", None)
        if isinstance(open_checkpoint, int) and any(
            valid(item, open_checkpoint) for item in state.checkpoints
        ):
            return open_checkpoint
        selected = getattr(state, "selected_checkpoint", None)
        if isinstance(selected, int) and any(
            valid(item, selected) for item in state.checkpoints
        ):
            return selected
        return max(
            (item.index for item in state.checkpoints if getattr(item, "lineage_valid", True)),
            default=None,
        )

    @staticmethod
    def _checkpoint_detail(coordinator: AECoordinator, checkpoint: Any | None) -> Any | None:
        if checkpoint is None:
            return None
        loader = getattr(coordinator, "checkpoint", None)
        if callable(loader):
            return loader(checkpoint.index)
        return checkpoint

    @staticmethod
    def _next_checkpoint_index(state: Any) -> int:
        return max((item.index for item in state.checkpoints), default=-1) + 1

    @staticmethod
    def _successful(command: Any) -> bool:
        return command.status == "completed" and not (
            isinstance(command.result, Mapping) and command.result.get("ok") is False
        )

    @classmethod
    def _completed(
        cls,
        history: Sequence[Any],
        kind: str,
        *,
        stage: str | None = None,
    ) -> list[Any]:
        rows = [item for item in history if item.kind == kind and cls._successful(item)]
        if stage is None:
            return rows
        return [item for item in rows if cls._workflow(item).get("stage") == stage]

    @staticmethod
    def _workflow(command: Any) -> Mapping[str, Any]:
        payload = command.payload if isinstance(command.payload, Mapping) else {}
        value = payload.get("workflow")
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _result_inspection(result: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        if not isinstance(result, Mapping) or result.get("ok") is False:
            return None
        nested = result.get("inspection")
        if isinstance(nested, Mapping):
            return nested
        if isinstance(result.get("result"), Mapping):
            nested = result["result"]
            if isinstance(nested.get("inspection"), Mapping):
                return nested["inspection"]
        if isinstance(result.get("schema_version"), str):
            return result
        return None
 
    @staticmethod
    def _inspection_identity(
        value: Mapping[str, Any] | AEInspection,
    ) -> tuple[dict[str, str | None], dict[str, int]]:
        inspection = (
            value if isinstance(value, AEInspection) else AEInspection.model_validate(value)
        )
        sources = dict(inspection.layer_sources)
        if not sources:
            sources = {
                layer.layer_instance_id: layer.source_element_id
                for layer in inspection.layers
            }
        native_ids = dict(inspection.layer_native_ids)
        if set(sources) != set(native_ids):
            raise AEWorkflowError("inspection native identity inventory is incomplete")
        return sources, native_ids

    @classmethod
    def _validate_inspection_rows(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        expected_sources: Mapping[str, str | None],
        expected_native_ids: Mapping[str, int],
        allow_new_ids: Sequence[str] = (),
    ) -> tuple[dict[str, str | None], dict[str, int]]:
        if not rows:
            raise AEWorkflowError("inspect_layers returned no inspection")
        actual_sources, actual_native_ids = cls._inspection_identity(rows[0])
        expected = dict(expected_sources)
        allowed = set(allow_new_ids)
        for instance_id, source_id in expected.items():
            if instance_id not in actual_sources:
                raise AEWorkflowError(f"inspection is missing layer {instance_id!r}")
            if actual_sources[instance_id] != source_id:
                raise AEWorkflowError(f"inspection source identity drift: {instance_id!r}")
            if instance_id in expected_native_ids and actual_native_ids[instance_id] != expected_native_ids[instance_id]:
                raise AEWorkflowError(f"inspection native identity drift: {instance_id!r}")
        for instance_id, source_id in actual_sources.items():
            if instance_id not in expected:
                if instance_id not in allowed:
                    raise AEWorkflowError(f"inspection returned an unissued layer {instance_id!r}")
                if source_id is not None:
                    raise AEWorkflowError(f"new inspection layer must be source-less: {instance_id!r}")
        for row in rows[1:]:
            sources, native_ids = cls._inspection_identity(row)
            if sources != actual_sources or native_ids != actual_native_ids:
                raise AEWorkflowError("inspection chunks have conflicting layer identities")
        return actual_sources, actual_native_ids

    @staticmethod
    def _result_native_ids(
        result: Mapping[str, Any] | None,
        *,
        expected_keys: Sequence[str] | None = None,
    ) -> dict[str, int]:
        if not isinstance(result, Mapping) or result.get("ok") is False:
            return {}
        nested = result.get("result")
        value = nested if isinstance(nested, Mapping) else result
        raw = value.get("layer_native_ids") if isinstance(value, Mapping) else None
        if not isinstance(raw, Mapping):
            return {}
        source_map = value.get("layer_sources")
        if source_map is not None and (
            not isinstance(source_map, Mapping) or set(source_map) != set(raw)
        ):
            raise AEWorkflowError("apply result layer identity inventory is inconsistent")
        expected = set(expected_keys) if expected_keys is not None else None
        result_ids: dict[str, int] = {}
        seen_native_ids: set[int] = set()
        for instance, native in raw.items():
            if not isinstance(instance, str) or not instance:
                raise AEWorkflowError("apply result native identity key is invalid")
            if expected is not None and instance not in expected:
                raise AEWorkflowError("apply result native identity key is unexpected")
            if (
                not isinstance(native, int)
                or isinstance(native, bool)
                or native < 1
                or native > 1_000_000
            ):
                raise AEWorkflowError("apply result native layer id is invalid")
            if native in seen_native_ids:
                raise AEWorkflowError("apply result native layer ids are duplicated")
            seen_native_ids.add(native)
            result_ids[instance] = native
        return dict(sorted(result_ids.items()))
    @staticmethod
    def _operation_wire(operation: Any) -> dict[str, Any]:
        if isinstance(operation, Mapping):
            return dict(operation)
        dump = getattr(operation, "model_dump", None)
        if not callable(dump):
            raise AEWorkflowError("operation is not serializable")
        return dump(mode="json", by_alias=True, exclude_none=True)

    @classmethod
    def _batch_operations(cls, command: Any) -> list[dict[str, Any]]:
        payload = command.payload if isinstance(command.payload, Mapping) else {}
        batch = payload.get("batch")
        if not isinstance(batch, Mapping):
            return []
        raw = batch.get("operations")
        if not isinstance(raw, list):
            return []
        return [cls._operation_wire(item) for item in raw]

    @classmethod
    def _inventory_from_inspection(
        cls, value: Mapping[str, Any] | AEInspection | None
    ) -> dict[str, str | None]:
        if value is None:
            return {}
        sources, _native_ids = cls._inspection_identity(value)
        return sources

    @classmethod
    def _native_ids_from_inspection(
        cls, value: Mapping[str, Any] | AEInspection | None
    ) -> dict[str, int]:
        if value is None:
            return {}
        _sources, native_ids = cls._inspection_identity(value)
        return native_ids

    @classmethod
    def _inventory_from_checkpoint(cls, checkpoint: Any) -> dict[str, str | None]:
        return cls._inventory_from_inspection(
            checkpoint.inspection if checkpoint is not None else None
        )

    @classmethod
    def _native_ids_from_checkpoint(cls, checkpoint: Any) -> dict[str, int]:
        return cls._native_ids_from_inspection(
            checkpoint.inspection if checkpoint is not None else None
        )

    @staticmethod
    def _simulate_inventory(
        inventory: Mapping[str, str | None],
        operations: Sequence[Mapping[str, Any]],
    ) -> dict[str, str | None]:
        result = dict(inventory)
        for operation in operations:
            kind = operation.get("kind")
            instance = operation.get("layer_instance_id")
            if not isinstance(instance, str):
                continue
            if kind == "add_layer":
                source = operation.get("source_element_id")
                result[instance] = source if source is None else str(source)
            elif kind == "remove_layer":
                result.pop(instance, None)
        return result

    @staticmethod
    def _fresh_agent_ids(
        inventory: Mapping[str, str | None],
        tombstones: Sequence[str],
    ) -> list[str]:
        occupied = set(inventory).union(tombstones)
        result: list[str] = []
        while len(result) < max(0, _MAX_AGENT_IDS - len(inventory)):
            value = "agent-" + secrets.token_hex(16)
            if value in occupied:
                continue
            occupied.add(value)
            result.append(value)
        return result

    def _authoritative(
        self,
        coordinator: AECoordinator,
    ) -> tuple[Any, BaselineMapping, Mapping[str, Any], dict[str, Any]]:
        plan = coordinator.plan
        scene = load_render_plan_scene(coordinator.root, plan.id)
        manifest = dict(plan.capability_manifest or {})
        approved = self._approved_capabilities(plan, manifest)
        substitutions = tuple(
            AESubstitution.model_validate(item) for item in plan.substitutions
        )
        mapping = map_baseline(
            scene,
            plan.assets,
            scene_directory=Path(plan.scene_project_path).parent.as_posix(),
            capabilities=approved,
            substitutions=substitutions,
            locked_source_ids=plan.locked_targets,
        )
        return scene, mapping, manifest, approved

    @staticmethod
    def _approved_capabilities(
        plan: Any,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        catalog = manifest.get("capabilities")
        if not isinstance(catalog, Mapping):
            catalog = manifest
        fonts = catalog.get("font_names", catalog.get("fonts", ()))
        effects = catalog.get("effect_names", catalog.get("effects", ()))
        properties = catalog.get("property_schemas", catalog.get("properties", {}))
        if isinstance(fonts, list) and fonts and isinstance(fonts[0], Mapping):
            fonts = [item.get("match_name") for item in fonts if isinstance(item.get("match_name"), str)]
        if isinstance(effects, list) and effects and isinstance(effects[0], Mapping):
            effects = [item.get("match_name") for item in effects if isinstance(item.get("match_name"), str)]
        return {
            "digest": plan.capability_hash,
            "fonts": list(fonts) if isinstance(fonts, (list, tuple)) else [],
            "effects": list(effects) if isinstance(effects, (list, tuple)) else [],
            "properties": dict(properties) if isinstance(properties, Mapping) else {},
        }

    @staticmethod
    def _approved_capabilities_for_batch(
        approved: Mapping[str, Any],
        batch: Any,
    ) -> dict[str, Any]:
        """Send only catalog entries consumed by this validated operation batch."""
        operations = getattr(batch, "operations", ())
        font_refs: set[str] = set()
        effect_refs: set[str] = set()
        property_refs: set[str] = set()
        for operation in operations:
            font_name = getattr(operation, "font_name", None)
            if isinstance(font_name, str):
                font_refs.add(font_name)
            effect_name = getattr(operation, "effect_name", None)
            if isinstance(effect_name, str):
                effect_refs.add(effect_name)
            properties = _operation_properties(operation)
            property_refs.update(properties)
            effect_refs.update(
                required
                for property_name in properties
                if (required := _REQUIRED_EFFECTS.get(property_name)) is not None
            )
        fonts = tuple(
            name for name in approved.get("fonts", ()) if isinstance(name, str) and name in font_refs
        )
        effects = tuple(
            name for name in approved.get("effects", ()) if isinstance(name, str) and name in effect_refs
        )
        properties = {
            name: schema
            for name, schema in approved.get("properties", {}).items()
            if name in property_refs
        }
        return {
            "digest": approved.get("digest"),
            "fonts": list(fonts),
            "effects": list(effects),
            "properties": properties,
        }

    @staticmethod
    def _plan_assets(plan: Any) -> dict[str, PlanAsset]:
        return {asset.id: asset for asset in plan.assets}

    @staticmethod
    def _asset_record(asset: PlanAsset) -> dict[str, Any]:
        return {"id": asset.id, "sha256": asset.sha256, "length": asset.length}

    def _enqueue(
        self,
        coordinator: AECoordinator,
        state: Any,
        kind: str,
        payload: Mapping[str, Any],
        *,
        expected_checkpoint: int | None = None,
    ) -> Any:
        if expected_checkpoint is None and self._checkpoint_index(state) is not None:
            expected_checkpoint = self._checkpoint_index(state)
        try:
            return coordinator.enqueue_command(
                kind,
                dict(payload),
                expected_state=state.status,
                expected_checkpoint=expected_checkpoint,
                device_id=state.device_id,
                revision=state.revision,
            )
        except CoordinatorConflict as exc:
            if "bounded size" not in str(exc):
                raise
            self._pause(coordinator, "protocol_too_large")
            return None

    @staticmethod
    def _composition_payload(mapping: BaselineMapping) -> dict[str, Any]:
        return mapping.composition.model_dump(mode="json", by_alias=True)
    @staticmethod
    def _baseline_mapping_digest(mapping: BaselineMapping) -> str:
        return json_digest(
            {
                "batches": [batch.to_payload() for batch in mapping.batches],
                "inventory": [
                    row.model_dump(mode="json") for row in mapping.final_inventory
                ],
            }
        )

    @staticmethod
    def _layer_sources(mapping: BaselineMapping) -> dict[str, str | None]:
        return {
            row.instance_id: row.source_element_id for row in mapping.final_inventory
        }

    @staticmethod
    def _observation_chunks(scene: Any) -> list[list[tuple[str, int]]]:
        chunks = chunk_observation_pairs(required_observation_frames(scene), max_pairs=128)
        return chunks or [[]]

    @staticmethod
    def _bound_observation_chunks(
        chunks: Sequence[Sequence[tuple[str, int]]],
        *,
        layer_sources: Mapping[str, str | None],
        capability_manifest: Mapping[str, Any],
        max_new_layers: int = 0,
    ) -> list[list[tuple[str, int]]]:
        """Keep panel inspection responses below their complete JSON budget."""

        max_wire_bytes = 900 * 1024
        contributor_ids = tuple(layer_sources)
        if len(contributor_ids) > 1_000:
            raise AEInspectionBudgetError("inspection layer inventory exceeds the bounded protocol")
        if (
            not isinstance(max_new_layers, int)
            or isinstance(max_new_layers, bool)
            or max_new_layers < 0
        ):
            raise AEInspectionBudgetError("new layer inventory budget is invalid")
        max_new_layers = min(max_new_layers, 1_000 - len(contributor_ids))
        budget_ids = (*contributor_ids, *(f"__new_layer_{index}" for index in range(max_new_layers)))
        rows = [
            {
                # Inventory fields are intentionally the compact authoritative
                # identity used by merge_inspection_chunks.  Display names and
                # optional points are not verification inputs and must not be
                # pessimistically charged at their maximum text size.
                "layer_instance_id": identifier,
                "native_layer_id": 1_000_000,
                "source_element_id": layer_sources.get(identifier),
                "index": index + 1,
                "frame_start": 0,
                "frame_end": 1_000_000,
            }
            for index, identifier in enumerate(budget_ids)
        ]
        layer_sources_wire = {
            identifier: layer_sources[identifier] for identifier in contributor_ids
        }
        layer_native_ids_wire = {identifier: 1_000_000 for identifier in contributor_ids}
        sample = {
            "source_element_id": "x" * 256,
            "frame": 1_000_000,
            "instances": [
                {
                    "layer_instance_id": identifier,
                    "active": True,
                    "sample": {
                        "x": 1_000_000.0,
                        "y": 1_000_000.0,
                        "sx": 1_000_000.0,
                        "sy": 1_000_000.0,
                        "rot": 1_000_000.0,
                        "opacity": 1.0,
                        "xmin": -1_000_000.0,
                        "ymin": -1_000_000.0,
                        "xmax": 1_000_000.0,
                        "ymax": 1_000_000.0,
                    },
                }
                for identifier in budget_ids
            ],
            "missing": False,
        }
        capability_hash = capability_manifest.get("digest") or capability_manifest.get(
            "capability_hash"
        )
        heartbeat = {
            "capability_hash": capability_hash,
            "version": capability_manifest.get("version"),
            "major": capability_manifest.get("major"),
            "host": capability_manifest.get("host"),
        }
        base = {
            "schema_version": "keepframe.ae-inspection/1",
            "frame_count": 1_000_000,
            "fps": 99.0,
            "requested": [],
            "layers": rows,
            "layer_sources": layer_sources_wire,
            "layer_native_ids": layer_native_ids_wire,
            "samples": [],
            "missing_pairs": [],
            "source_aggregated": False,
            "heartbeat": heartbeat,
        }
        base_bytes = len(canonical_json(base))
        per_pair = len(
            canonical_json(
                {
                    "requested": [
                        {"source_element_id": "x" * 256, "frame": 1_000_000}
                    ],
                    "samples": [sample],
                    "missing_pairs": [
                        {"source_element_id": "x" * 256, "frame": 1_000_000}
                    ],
                }
            )
        )
        available = max_wire_bytes - base_bytes
        if available < per_pair:
            raise AEInspectionBudgetError("inspection response cannot fit the bounded protocol")
        max_pairs = max(1, min(128, available // per_pair))
        flattened = [pair for chunk in chunks for pair in chunk]
        if not flattened:
            return [[] for _ in chunks] or [[]]
        return [
            flattened[start : start + max_pairs]
            for start in range(0, len(flattened), max_pairs)
        ]

    @staticmethod
    def _inspection_commands(
        history: Sequence[Any],
        stage: str,
        *,
        after_sequence: int | None = None,
    ) -> list[Any]:
        return sorted(
            [
                item
                for item in AEWorkflowService._completed(history, "inspect_layers", stage=stage)
                if after_sequence is None or item.sequence > after_sequence
            ],
            key=lambda item: int(AEWorkflowService._workflow(item).get("chunk_index", 0)),
        )
    @classmethod
    def _manual_prepare(cls, history: Sequence[Any], state: Any) -> Any | None:
        for command in reversed(history):
            if command.kind != "sync_manual" or not cls._successful(command):
                continue
            payload = command.payload if isinstance(command.payload, Mapping) else {}
            if payload.get("prepare_manual") is not True:
                continue
            workflow = cls._workflow(command)
            if (
                state.manual_attempt_id is None
                or workflow.get("manual_epoch") != state.manual_epoch
                or workflow.get("manual_attempt_id") != state.manual_attempt_id
            ):
                continue
            return command
        return None
    @classmethod
    def _manual_candidate_checkpoint(cls, history: Sequence[Any], state: Any) -> int | None:
        committed = {item.index for item in state.checkpoints}
        for command in reversed(history):
            if command.kind != "sync_manual" or not cls._successful(command):
                continue
            payload = command.payload if isinstance(command.payload, Mapping) else {}
            if payload.get("prepare_manual") is not True:
                continue
            workflow = cls._workflow(command)
            candidate = workflow.get("candidate_checkpoint_index")
            if (
                isinstance(candidate, int)
                and candidate >= 0
                and candidate not in committed
                and (
                    state.manual_attempt_id is None
                    or workflow.get("manual_attempt_id") == state.manual_attempt_id
                )
            ):
                return candidate
        return None

    def _manual_candidate_artifact(
        self,
        coordinator: AECoordinator,
        history: Sequence[Any],
        state: Any,
    ) -> dict[str, Any] | None:
        candidate = self._manual_candidate_checkpoint(history, state)
        if candidate is None:
            return None
        for command in reversed(history):
            if command.kind != "sync_manual" or not self._successful(command):
                continue
            payload = command.payload if isinstance(command.payload, Mapping) else {}
            if payload.get("prepare_manual") is not True:
                continue
            workflow = self._workflow(command)
            if workflow.get("candidate_checkpoint_index") != candidate:
                continue
            records = payload.get("artifacts")
            if not isinstance(records, list) or len(records) != 1:
                continue
            record = records[0]
            if not isinstance(record, Mapping) or record.get("kind") != "aep":
                continue
            artifact_id = record.get("reservation_id", record.get("id"))
            if not isinstance(artifact_id, str):
                continue
            try:
                reservation, _path = coordinator.artifact_path(artifact_id)
            except CoordinatorConflict:
                continue
            if (
                reservation.kind != "aep"
                or reservation.sha256 is None
                or reservation.length is None
            ):
                continue
            return {
                "id": reservation.id,
                "sha256": reservation.sha256,
                "length": reservation.length,
            }
        return None

    @staticmethod
    def _checkpoint_artifact(
        coordinator: AECoordinator,
        state: Any,
        checkpoint_index: int,
    ) -> dict[str, Any] | None:
        checkpoint = next(
            (
                item
                for item in state.checkpoints
                if item.index == checkpoint_index
            ),
            None,
        )
        if checkpoint is None:
            return None
        artifact_id = getattr(checkpoint, "aep_artifact_id", None)
        artifact_path = getattr(coordinator, "artifact_path", None)
        if not isinstance(artifact_id, str) or not callable(artifact_path):
            return None
        try:
            reservation, _path = artifact_path(artifact_id)
        except CoordinatorConflict as exc:
            raise AEWorkflowError("checkpoint artifact is unavailable") from exc
        if (
            reservation.kind != "aep"
            or reservation.sha256 is None
            or reservation.length is None
        ):
            return None
        return {
            "id": reservation.id,
            "sha256": reservation.sha256,
            "length": reservation.length,
        }

    @classmethod
    def _inspection_rows(
        cls,
        history: Sequence[Any],
        stage: str,
        *,
        after_sequence: int | None = None,
    ) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        for command in cls._inspection_commands(
            history,
            stage,
            after_sequence=after_sequence,
        ):
            inspection = cls._result_inspection(command.result)
            if inspection is None:
                raise AEWorkflowError("inspect_layers returned no inspection")
            rows.append(inspection)
        return rows

    @staticmethod
    def _reserve_preview(
        coordinator: AECoordinator,
        checkpoint_index: int,
        representative: Sequence[int],
    ) -> tuple[list[dict[str, Any]], list[str], str]:
        mp4 = coordinator.reserve_artifact("mp4", _MAX_MP4_BYTES).id
        records: list[dict[str, Any]] = [
            {
                "reservation_id": mp4,
                "kind": "mp4",
                "filename": f"checkpoint-{checkpoint_index}.mp4",
                "directory": "renders",
            }
        ]
        frame_ids: list[str] = []
        for frame in representative:
            artifact_id = coordinator.reserve_artifact("png", _MAX_PNG_BYTES).id
            frame_ids.append(artifact_id)
            records.append(
                {
                    "reservation_id": artifact_id,
                    "kind": "png",
                    "filename": f"checkpoint-{checkpoint_index}-{frame:06d}.png",
                    "directory": "renders",
                }
            )
        return records, frame_ids, mp4

    @staticmethod
    def _render_payload(
        scene: Any,
        checkpoint_index: int,
        representative: Sequence[int],
        records: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "checkpoint_index": checkpoint_index,
            "frame_count": int(scene.frames),
            "fps": float(scene.fps),
            "representative_frames": list(representative),
            "artifacts": [dict(item) for item in records],
        }

    @staticmethod
    def _render_command(
        history: Sequence[Any],
        checkpoint_index: int,
        stage: str,
        *,
        manual_marker: Any | None = None,
        after_sequence: int | None = None,
    ) -> Any | None:
        for command in reversed(history):
            if command.kind != "render_preview" or not AEWorkflowService._successful(command):
                continue
            if after_sequence is not None and command.sequence <= after_sequence:
                continue
            workflow = AEWorkflowService._workflow(command)
            if workflow.get("stage") != stage or workflow.get("checkpoint_index") != checkpoint_index:
                continue
            if manual_marker is not None and (
                workflow.get("manual_marker_sequence") != manual_marker.sequence
                or workflow.get("manual_marker_id") != manual_marker.id
            ):
                continue
            return command
        return None

    @staticmethod
    def _render_records(command: Any) -> tuple[list[int], list[str], str]:
        payload = command.payload if isinstance(command.payload, Mapping) else {}
        representative = payload.get("representative_frames")
        records = payload.get("artifacts")
        if not isinstance(representative, list) or not isinstance(records, list):
            raise AEWorkflowError("render_preview payload is incomplete")
        mp4: str | None = None
        frames: dict[int, str] = {}
        checkpoint_index = payload.get("checkpoint_index")
        if not isinstance(checkpoint_index, int):
            raise AEWorkflowError("render checkpoint index is invalid")
        for item in records:
            if not isinstance(item, Mapping):
                raise AEWorkflowError("render artifact record is invalid")
            reservation = item.get("reservation_id")
            kind = item.get("kind")
            filename = item.get("filename")
            if not isinstance(reservation, str) or not isinstance(filename, str):
                raise AEWorkflowError("render artifact reservation is invalid")
            if kind == "mp4":
                mp4 = reservation
            elif kind == "png":
                prefix = f"checkpoint-{checkpoint_index}-"
                if filename.startswith(prefix) and filename.endswith(".png"):
                    try:
                        frame = int(filename[len(prefix) : -4])
                    except ValueError as exc:
                        raise AEWorkflowError("render frame filename is invalid") from exc
                    frames[frame] = reservation
        if mp4 is None or any(frame not in frames for frame in representative):
            raise AEWorkflowError("render artifacts are incomplete")
        return list(representative), [frames[frame] for frame in representative], mp4

    @staticmethod
    def _artifact_record(
        reservation_id: str,
        kind: str,
        filename: str,
        directory: str,
    ) -> dict[str, Any]:
        return {
            "reservation_id": reservation_id,
            "kind": kind,
            "filename": filename,
            "directory": directory,
        }

    @staticmethod
    def _dependency_manifest(plan: Any) -> dict[str, Any]:
        capability_manifest = dict(plan.capability_manifest or {})
        catalog = capability_manifest.get("capabilities", {})
        nonportable: list[dict[str, str]] = []
        if isinstance(catalog, Mapping):
            for kind in ("fonts", "effects"):
                records = catalog.get(kind, [])
                if not isinstance(records, list):
                    continue
                for record in records:
                    if not isinstance(record, Mapping) or any(
                        record.get(field)
                        for field in ("version_or_hash", "version", "sha256")
                    ):
                        continue
                    identity = record.get("match_name", record.get("family"))
                    if isinstance(identity, str):
                        nonportable.append({"kind": kind[:-1], "identity": identity})
            plugins = catalog.get("plugin_versions", {})
            if isinstance(plugins, Mapping):
                for identity, version in plugins.items():
                    if isinstance(identity, str) and not version:
                        nonportable.append({"kind": "plugin", "identity": identity})
        return {
            "capability_manifest": capability_manifest,
            "substitutions": list(plan.substitutions),
            "missing_nonportable_dependencies": {
                "missing": [],
                "nonportable": nonportable,
            },
        }

    @classmethod
    def _checkpoint_context(
        cls,
        *,
        plan: Any,
        index: int,
        provenance: str,
        passed: bool,
        aep: str,
        preview: str,
        frames: Sequence[str],
        inspection: Mapping[str, Any],
        operations: Sequence[Mapping[str, Any]],
        report: Any,
        model_response: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        report_wire = report.model_dump(mode="json") if report is not None else {}
        report_wire.pop("inspection", None)
        violations = report_wire.get("violations")
        failure = None
        if not passed:
            if isinstance(violations, list) and violations:
                failure = "; ".join(str(item) for item in violations)
            else:
                failure = "verification_failed"
        checkpoint = AECheckpoint(
            index=index,
            provenance=provenance,
            passed=bool(passed),
            aep_artifact_id=aep,
            preview_artifact_id=preview,
            frame_artifact_ids=tuple(frames),
            inspection=dict(inspection),
            operations=tuple(dict(item) for item in operations),
            verifier_report=report_wire,
            model_response=dict(model_response or {}),
            capability_manifest=dict(plan.capability_manifest or {}),
            dependency_manifest=cls._dependency_manifest(plan),
            failure=failure,
        )
        return checkpoint.model_dump(mode="json")

    def _pause(self, coordinator: AECoordinator, reason: str) -> None:
        try:
            state = coordinator.state()
            if state.status.startswith("paused:") or state.status in {"done", "failed"}:
                return
            coordinator.transition("pause_error", revision=state.revision, reason=reason)
        except CoordinatorConflict:
            return

    def _advance_baseline(
        self,
        coordinator: AECoordinator,
        state: Any,
        history: Sequence[Any],
        *,
        capture_only: bool = False,
    ) -> Any:
        scene, mapping, manifest, approved = self._authoritative(coordinator)
        plan = coordinator.plan
        mapping_digest = self._baseline_mapping_digest(mapping)
        if not state.baseline_complete and any(
            item.index == 0 for item in state.checkpoints
        ) and state.open_checkpoint != 0:
            manual_attempt_boundary = max(
                (
                    command.sequence
                    for command in history
                    if command.kind == "sync_manual" and self._successful(command)
                ),
                default=None,
            )
            reopened = any(
                command.kind == "open_project"
                and self._successful(command)
                and self._workflow(command).get("stage") == "baseline_reopen"
                and self._workflow(command).get("checkpoint_index") == 0
                and getattr(state, "device_id", None) is not None
                and self._workflow(command).get("device_id") == state.device_id
                and (
                    manual_attempt_boundary is None
                    or command.sequence > manual_attempt_boundary
                )
                for command in history
            )
            if not reopened:
                payload = self._composition_payload(mapping)
                payload["checkpoint_index"] = 0
                checkpoint_artifact = self._checkpoint_artifact(coordinator, state, 0)
                if checkpoint_artifact is not None:
                    payload["checkpoint_artifact"] = checkpoint_artifact
                payload["workflow"] = {
                    "stage": "baseline_reopen",
                    "checkpoint_index": 0,
                    "device_id": getattr(state, "device_id", None),
                }
                return self._enqueue(
                    coordinator,
                    state,
                    "open_project",
                    payload,
                    expected_checkpoint=self._checkpoint_index(state),
                )
        if not self._completed(history, "create_project"):
            payload = self._composition_payload(mapping)
            payload["workflow"] = {"stage": "baseline_create"}
            return self._enqueue(coordinator, state, "create_project", payload, expected_checkpoint=None)

        assets = self._plan_assets(plan)
        imported = {
            item.payload.get("asset_id")
            for item in self._completed(history, "import_asset")
            if isinstance(item.payload, Mapping)
        }
        for asset_id in mapping.imported_asset_ids:
            if asset_id in imported:
                continue
            asset = assets.get(asset_id)
            if asset is None:
                raise AEWorkflowError("baseline asset is missing from the render plan")
            payload = {
                "asset_id": asset.id,
                "assets": [self._asset_record(asset)],
                "workflow": {"stage": "baseline_import", "asset_id": asset.id},
            }
            return self._enqueue(coordinator, state, "import_asset", payload, expected_checkpoint=None)

        baseline_applies = self._completed(history, "apply_batch", stage="baseline_apply")
        applied = {
            self._workflow(item).get("batch_index")
            for item in baseline_applies
        }
        for batch in mapping.batches:
            if batch.index in applied:
                continue
            if capture_only:
                break
            native_ids: dict[str, int] = {}
            for item in baseline_applies:
                workflow = self._workflow(item)
                if workflow.get("batch_index", -1) < batch.index:
                    native_ids.update(self._result_native_ids(item.result))
            missing_native_ids = set(batch.start_layer_sources) - set(native_ids)
            if missing_native_ids:
                raise AEWorkflowError("baseline apply result omitted native layer ids")
            batch_payload = batch.to_payload()
            payload = {
                "batch": batch_payload,
                "approved_capabilities": self._approved_capabilities_for_batch(approved, batch),
                "scene_frame_count": batch.scene_frame_count,
                "duration": batch.duration,
                "layer_count": batch.layer_count,
                "baseline": True,
                "locked_source_ids": list(plan.locked_targets),
                "layer_sources": batch.start_layer_sources,
                "layer_native_ids": {
                    instance_id: native_ids[instance_id]
                    for instance_id in batch.start_layer_sources
                },
                "workflow": {
                    "stage": "baseline_apply",
                    "batch_index": batch.index,
                    "batch_count": len(mapping.batches),
                    "mapping_digest": mapping_digest,
                },
            }
            return self._enqueue(coordinator, state, "apply_batch", payload, expected_checkpoint=None)

        layer_sources = self._layer_sources(mapping)
        checkpoint_operations = [
            self._operation_wire(operation)
            for batch in mapping.batches
            for operation in batch.operations
        ]
        baseline_complete = len(applied) == len(mapping.batches)
        saved_baseline = next(
            (
                item
                for item in state.checkpoints
                if item.index == 0 and item.provenance == "baseline"
            ),
            None,
        )
        saved_baseline = self._checkpoint_detail(coordinator, saved_baseline)
        baseline_checkpoint_complete = bool(
            saved_baseline is not None
            and saved_baseline.passed
            and len(saved_baseline.operations) == len(checkpoint_operations)
        )
        returned_native_ids: dict[str, int] = {}
        if capture_only:
            completed_batches = [batch for batch in mapping.batches if batch.index in applied]
            checkpoint_operations = [
                self._operation_wire(operation)
                for batch in completed_batches
                for operation in batch.operations
            ]
            expected_sources = self._simulate_inventory({}, checkpoint_operations)
            latest_apply = max(
                baseline_applies,
                key=lambda item: self._workflow(item).get("batch_index", -1),
            )
            result = latest_apply.result
            if isinstance(result, Mapping) and isinstance(result.get("result"), Mapping):
                result = result["result"]
            returned_sources = result.get("layer_sources") if isinstance(result, Mapping) else None
            if not isinstance(returned_sources, Mapping) or dict(returned_sources) != expected_sources:
                raise AEWorkflowError("baseline apply returned an invalid layer source inventory")
            layer_sources = expected_sources
            returned_native_ids = self._result_native_ids(
                latest_apply.result,
                expected_keys=tuple(layer_sources),
            )
            if set(returned_native_ids) != set(layer_sources):
                raise AEWorkflowError("baseline apply omitted native layer ids")

        chunks = self._observation_chunks(scene)
        if capture_only:
            chunks = [
                [pair for pair in chunk if pair[0] in layer_sources]
                for chunk in chunks
            ]
        chunks = self._bound_observation_chunks(
            chunks,
            layer_sources=layer_sources,
            capability_manifest=manifest,
        )
        inspection_after_sequence: int | None = None
        if capture_only or not baseline_checkpoint_complete:
            for command in reversed(history):
                if command.kind != "save_checkpoint" or not self._successful(command):
                    continue
                workflow = self._workflow(command)
                if workflow.get("checkpoint_index") == 0:
                    inspection_after_sequence = command.sequence
                    break
        inspections = self._inspection_commands(
            history,
            "baseline_inspect",
            after_sequence=inspection_after_sequence,
        )
        if len(inspections) < len(chunks):
            index = len(inspections)
            layer_native_ids: dict[str, int] = (
                dict(returned_native_ids) if capture_only else {}
            )
            if index:
                previous_rows = self._inspection_rows(
                    history,
                    "baseline_inspect",
                    after_sequence=inspection_after_sequence,
                )
            requested = [
                {"source_element_id": source_id, "frame": frame}
                for source_id, frame in chunks[index]
            ]
            payload = {
                "frame_count": int(scene.frames),
                "fps": float(scene.fps),
                "capability_hash": plan.capability_hash,
                "requested": requested,
                "layer_sources": layer_sources,
                "layer_native_ids": layer_native_ids,
                "workflow": {"stage": "baseline_inspect", "chunk_index": index},
            }
            return self._enqueue(coordinator, state, "inspect_layers", payload, expected_checkpoint=None)

        rows = self._inspection_rows(
            history,
            "baseline_inspect",
            after_sequence=inspection_after_sequence,
        )
        try:
            _sources, layer_native_ids = self._validate_inspection_rows(
                rows,
                expected_sources=layer_sources,
                expected_native_ids=returned_native_ids if capture_only else {},
            )
            merged = merge_inspection_chunks(
                scene,
                rows,
                authoritative_instance_sources=layer_sources,
                authoritative_instance_native_ids=layer_native_ids,
            )
        except Exception:
            self._pause(coordinator, "verification_error")
            return None

        checkpoint_index = 0
        render = self._render_command(
            history,
            checkpoint_index,
            "baseline_render",
            after_sequence=inspection_after_sequence,
        )
        if render is None:
            representative = select_representative_frames(
                scene,
                merged,
                frame_count=scene.frames,
                keep_predicates=scene.constraints,
                cap=12,
            )
            records, _frame_ids, _mp4 = self._reserve_preview(
                coordinator, checkpoint_index, representative
            )
            payload = self._render_payload(scene, checkpoint_index, representative, records)
            payload["workflow"] = {
                "stage": "baseline_render",
                "checkpoint_index": checkpoint_index,
            }
            return self._enqueue(coordinator, state, "render_preview", payload, expected_checkpoint=None)

        if baseline_checkpoint_complete:
            return None
        try:
            report = verify_inspection(
                scene,
                rows,
                authoritative_instance_sources=layer_sources,
                authoritative_instance_native_ids=layer_native_ids,
            )
            representative, frame_ids, preview_id = self._render_records(render)
            aep_id = coordinator.reserve_artifact("aep", _MAX_AEP_BYTES).id
            checkpoint = self._checkpoint_context(
                plan=plan,
                index=0,
                provenance="baseline",
                passed=bool(report.passed and baseline_complete),
                aep=aep_id,
                preview=preview_id,
                frames=frame_ids,
                inspection=merged.model_dump(mode="json"),
                operations=checkpoint_operations,
                report=report,
            )
        except Exception:
            self._pause(coordinator, "verification_error")
            return None
        payload = {
            "index": 0,
            "checkpoint_context": checkpoint,
            "artifacts": [
                self._artifact_record(
                    aep_id,
                    "aep",
                    "checkpoint-0.aep",
                    "checkpoints",
                )
            ],
            "workflow": {
                "stage": "baseline_save",
                "checkpoint_index": 0,
                "baseline_complete": baseline_complete,
                "mapping_digest": mapping_digest,
            },
            "baseline_complete": baseline_complete,
        }
        return self._enqueue(coordinator, state, "save_checkpoint", payload, expected_checkpoint=None)

    def _last_passing(self, state: Any) -> Any | None:
        active_index = self._checkpoint_index(state)
        passing = [
            item
            for item in state.checkpoints
            if item.passed
            and item.lineage_valid
            and (active_index is None or item.index <= active_index)
        ]
        return max(passing, key=lambda item: item.index, default=None)

    def _candidate_apply(self, history: Sequence[Any]) -> Any | None:
        rollback_sequence = max(
            (
                command.sequence
                for command in history
                if command.kind == "open_project"
                and self._successful(command)
                and self._workflow(command).get("stage") == "candidate_rollback"
            ),
            default=None,
        )
        completed_checkpoints = {
            self._workflow(command).get("checkpoint_index")
            for command in history
            if command.kind in {"save_checkpoint", "sync_manual"}
            and self._successful(command)
        }
        for command in reversed(history):
            if command.kind != "apply_batch":
                continue
            workflow = self._workflow(command)
            if (
                workflow.get("stage") == "candidate_apply"
                and workflow.get("checkpoint_index") not in completed_checkpoints
                and (rollback_sequence is None or command.sequence > rollback_sequence)
            ):
                return command
        return None

    def _needs_rollback(self, state: Any, history: Sequence[Any]) -> bool:
        failed = max(
            (item for item in state.checkpoints if not item.passed),
            key=lambda item: item.index,
            default=None,
        )
        if failed is None:
            return False
        passing = self._last_passing(state)
        if passing is None or failed.index <= passing.index:
            return False
        failed_sequence = max(
            (
                command.sequence
                for command in history
                if command.kind in {"save_checkpoint", "sync_manual"}
                and self._successful(command)
                and self._workflow(command).get("checkpoint_index") == failed.index
            ),
            default=None,
        )
        return not any(
            command.kind == "open_project"
            and self._successful(command)
            and self._workflow(command).get("stage") == "candidate_rollback"
            and self._workflow(command).get("checkpoint_index") == passing.index
            and getattr(state, "device_id", None) is not None
            and self._workflow(command).get("device_id") == state.device_id
            and (failed_sequence is None or command.sequence > failed_sequence)
            for command in history
        )

    def _advance_iterating(self, coordinator: AECoordinator, state: Any, history: Sequence[Any]) -> Any:
        scene, mapping, manifest, approved = self._authoritative(coordinator)
        plan = coordinator.plan
        selected_checkpoint = getattr(state, "selected_checkpoint", None)
        if (
            not self._needs_rollback(state, history)
            and isinstance(selected_checkpoint, int)
            and selected_checkpoint != getattr(state, "open_checkpoint", None)
        ):
            reopened = any(
                command.kind == "open_project"
                and self._successful(command)
                and self._workflow(command).get("stage") == "selection_reopen"
                and self._workflow(command).get("checkpoint_index") == selected_checkpoint
                and getattr(state, "device_id", None) is not None
                and self._workflow(command).get("device_id") == state.device_id
                for command in history
            )
            if not reopened:
                payload = self._composition_payload(mapping)
                payload["checkpoint_index"] = selected_checkpoint
                checkpoint_artifact = self._checkpoint_artifact(
                    coordinator,
                    state,
                    selected_checkpoint,
                )
                if checkpoint_artifact is not None:
                    payload["checkpoint_artifact"] = checkpoint_artifact
                payload["workflow"] = {
                    "stage": "selection_reopen",
                    "checkpoint_index": selected_checkpoint,
                    "device_id": getattr(state, "device_id", None),
                }
                return self._enqueue(
                    coordinator,
                    state,
                    "open_project",
                    payload,
                    expected_checkpoint=self._checkpoint_index(state),
                )
        if self._needs_rollback(state, history):
            passing = self._last_passing(state)
            if passing is None:
                self._pause(coordinator, "verification_failed")
                return None
            payload = self._composition_payload(mapping)
            payload.update(
                {
                    "checkpoint_index": passing.index,
                    "workflow": {
                        "stage": "candidate_rollback",
                        "checkpoint_index": passing.index,
                        "device_id": getattr(state, "device_id", None),
                    },
                }
            )
            checkpoint_artifact = self._checkpoint_artifact(
                coordinator,
                state,
                passing.index,
            )
            if checkpoint_artifact is not None:
                payload["checkpoint_artifact"] = checkpoint_artifact
            return self._enqueue(
                coordinator,
                state,
                "open_project",
                payload,
                expected_checkpoint=self._checkpoint_index(state),
            )

        apply = self._candidate_apply(history)
        if apply is not None and self._successful(apply):
            operations = self._batch_operations(apply)
            passing = self._last_passing(state)
            passing = self._checkpoint_detail(coordinator, passing)
            start_inventory = self._inventory_from_checkpoint(passing)
            start_native_ids = self._native_ids_from_checkpoint(passing)
            if not start_inventory:
                start_inventory = dict(apply.payload.get("layer_sources", {}))
            candidate_inventory = self._simulate_inventory(start_inventory, operations)
            try:
                candidate_result_native = self._result_native_ids(
                    apply.result,
                    expected_keys=tuple(candidate_inventory),
                )
                if set(candidate_result_native) != set(candidate_inventory):
                    raise AEWorkflowError("candidate apply omitted native layer ids")
                prior_native_values = set(start_native_ids.values())
                for instance_id, native_id in candidate_result_native.items():
                    prior_native = start_native_ids.get(instance_id)
                    if prior_native is not None and native_id != prior_native:
                        raise AEWorkflowError("candidate apply changed an existing native layer id")
                    if prior_native is None and native_id in prior_native_values:
                        raise AEWorkflowError("candidate apply reused an existing native layer id")
                result_value = apply.result
                if (
                    isinstance(result_value, Mapping)
                    and isinstance(result_value.get("result"), Mapping)
                ):
                    result_value = result_value["result"]
                result_sources = (
                    result_value.get("layer_sources")
                    if isinstance(result_value, Mapping)
                    else None
                )
                if (
                    not isinstance(result_sources, Mapping)
                    or dict(result_sources) != candidate_inventory
                ):
                    raise AEWorkflowError("candidate apply returned an invalid layer source inventory")
            except Exception:
                self._pause(coordinator, "verification_error")
                return None
            candidate_native_ids = {
                instance_id: start_native_ids[instance_id]
                for instance_id in candidate_inventory
                if instance_id in start_native_ids
            }
            added_ids = [
                operation["layer_instance_id"]
                for operation in operations
                if operation.get("kind") == "add_layer"
                and isinstance(operation.get("layer_instance_id"), str)
            ]
            chunks = self._bound_observation_chunks(
                self._observation_chunks(scene),
                layer_sources=candidate_inventory,
                capability_manifest=manifest,
            )
            inspections = self._inspection_commands(
                history,
                "candidate_inspect",
                after_sequence=apply.sequence,
            )
            if len(inspections) < len(chunks):
                index = len(inspections)
                try:
                    prior_rows = self._inspection_rows(
                        history,
                        "candidate_inspect",
                        after_sequence=apply.sequence,
                    )
                    layer_sources = candidate_inventory
                    layer_native_ids = candidate_native_ids
                    if prior_rows:
                        layer_sources, layer_native_ids = self._validate_inspection_rows(
                            prior_rows,
                            expected_sources=candidate_inventory,
                            expected_native_ids=candidate_native_ids,
                            allow_new_ids=added_ids,
                        )
                except Exception:
                    self._pause(coordinator, "verification_error")
                    return None
                requested = [
                    {"source_element_id": source_id, "frame": frame}
                    for source_id, frame in chunks[index]
                ]
                payload = {
                    "frame_count": int(scene.frames),
                    "fps": float(scene.fps),
                    "capability_hash": coordinator.plan.capability_hash,
                    "requested": requested,
                    "layer_sources": layer_sources,
                    "layer_native_ids": layer_native_ids,
                    "workflow": {"stage": "candidate_inspect", "chunk_index": index},
                }
                return self._enqueue(
                    coordinator,
                    state,
                    "inspect_layers",
                    payload,
                    expected_checkpoint=self._checkpoint_index(state),
                )

            rows = self._inspection_rows(
                history,
                "candidate_inspect",
                after_sequence=apply.sequence,
            )
            try:
                layer_sources, layer_native_ids = self._validate_inspection_rows(
                    rows,
                    expected_sources=candidate_inventory,
                    expected_native_ids=candidate_native_ids,
                    allow_new_ids=added_ids,
                )
                merged = merge_inspection_chunks(
                    scene,
                    rows,
                    authoritative_instance_sources=layer_sources,
                    authoritative_instance_native_ids=layer_native_ids,
                )
            except Exception:
                self._pause(coordinator, "verification_error")
                return None
            checkpoint_index = self._workflow(apply).get("checkpoint_index")
            if not isinstance(checkpoint_index, int):
                raise AEWorkflowError("candidate checkpoint index is missing")
            render = self._render_command(history, checkpoint_index, "candidate_render")
            if render is None:
                representative = select_representative_frames(
                    scene,
                    merged,
                    frame_count=scene.frames,
                    keep_predicates=scene.constraints,
                    cap=12,
                )
                records, _frame_ids, _mp4 = self._reserve_preview(
                    coordinator, checkpoint_index, representative
                )
                payload = self._render_payload(scene, checkpoint_index, representative, records)
                payload["workflow"] = {
                    "stage": "candidate_render",
                    "checkpoint_index": checkpoint_index,
                }
                return self._enqueue(
                    coordinator,
                    state,
                    "render_preview",
                    payload,
                    expected_checkpoint=self._checkpoint_index(state),
                )

            saves = [
                command
                for command in self._completed(history, "save_checkpoint")
                if self._workflow(command).get("checkpoint_index") == checkpoint_index
            ]
            if saves:
                return self._schedule_model(coordinator, state, history, scene, approved)
            try:
                report = verify_inspection(
                    scene,
                    rows,
                    authoritative_instance_sources=layer_sources,
                    authoritative_instance_native_ids=layer_native_ids,
                )
                _representative, frame_ids, preview_id = self._render_records(render)
                aep_id = coordinator.reserve_artifact("aep", _MAX_AEP_BYTES).id
                workflow = self._workflow(apply)
                checkpoint = self._checkpoint_context(
                    plan=plan,
                    index=checkpoint_index,
                    provenance="agent",
                    passed=report.passed,
                    aep=aep_id,
                    preview=preview_id,
                    frames=frame_ids,
                    inspection=merged.model_dump(mode="json"),
                    operations=operations,
                    report=report,
                    model_response=workflow.get("model_response")
                    if isinstance(workflow.get("model_response"), Mapping)
                    else {},
                )
            except Exception:
                self._pause(coordinator, "verification_error")
                return None
            payload = {
                "index": checkpoint_index,
                "checkpoint_context": checkpoint,
                "artifacts": [
                    self._artifact_record(
                        aep_id,
                        "aep",
                        f"checkpoint-{checkpoint_index}.aep",
                        "checkpoints",
                    )
                ],
                "workflow": {
                    "stage": "candidate_save",
                    "checkpoint_index": checkpoint_index,
                },
            }
            return self._enqueue(
                coordinator,
                state,
                "save_checkpoint",
                payload,
                expected_checkpoint=self._checkpoint_index(state),
            )

        return self._schedule_model(coordinator, state, history, scene, approved)

    def _schedule_model(
        self,
        coordinator: AECoordinator,
        state: Any,
        history: Sequence[Any],
        scene: Any,
        approved: Mapping[str, Any],
    ) -> None:
        checkpoint = self._last_passing(state)
        checkpoint = self._checkpoint_detail(coordinator, checkpoint)
        if checkpoint is None:
            self._pause(coordinator, "verification_failed")
            return None
        inspection = checkpoint.inspection
        inventory = self._inventory_from_checkpoint(checkpoint)
        representative, frame_ids, _preview_id = self._checkpoint_preview_inputs(
            coordinator,
            checkpoint.index,
            checkpoint.frame_artifact_ids,
            history,
        )
        frames: dict[int, Path] = {}
        for frame, artifact_id in zip(representative, frame_ids):
            _reservation, path = coordinator.artifact_path(artifact_id)
            frames[int(frame)] = path
        previous_checkpoint = max(
            (item for item in state.checkpoints if not item.passed),
            key=lambda item: item.index,
            default=None,
        )
        previous_checkpoint = self._checkpoint_detail(coordinator, previous_checkpoint)
        previous = (
            previous_checkpoint.verifier_report.get("violations", ())
            if previous_checkpoint is not None
            and isinstance(previous_checkpoint.verifier_report, Mapping)
            else ()
        )
        model_inspection = {
            "inspection": inspection,
            "current_layer_sources": inventory,
            "issued_instance_id_tombstones": list(state.issued_instance_id_tombstones),
        }
        args = {
            "frames": frames,
            "direction": coordinator.plan.direction,
            "ir_summary": scene,
            "scene": scene,
            "locked_source_ids": coordinator.plan.locked_targets,
            "inspection": model_inspection,
            "previous_violations": previous if isinstance(previous, Sequence) and not isinstance(previous, (str, bytes)) else (),
            "checkpoint_index": checkpoint.index,
            "approved_capabilities": approved,
            "scene_frame_count": int(scene.frames),
            "duration": float(scene.frames) / float(scene.fps),
            "layer_count": len(inventory),
            "layer_sources": inventory,
            "tombstones": state.issued_instance_id_tombstones,
        }
        key = self._key(coordinator, state)
        self._futures[key] = self._executor.submit(self._run_model, args)
        return None

    def _run_model(self, args: Mapping[str, Any]) -> VisionStepResult:
        factory = self.llm_factory
        client = factory() if callable(factory) else factory
        return run_vision_step(client, **dict(args))

    def _checkpoint_preview_inputs(
        self,
        coordinator: AECoordinator,
        checkpoint_index: int,
        artifact_ids: Sequence[str],
        history: Sequence[Any],
    ) -> tuple[list[int], list[str], str]:
        for command in reversed(history):
            if command.kind != "render_preview" or not self._successful(command):
                continue
            payload = command.payload if isinstance(command.payload, Mapping) else {}
            if payload.get("checkpoint_index") != checkpoint_index:
                continue
            representative, frame_ids, mp4 = self._render_records(command)
            return representative, frame_ids, mp4
        if not artifact_ids:
            raise AEWorkflowError("passing checkpoint has no preview frames")
        # Older durable checkpoints may not retain render command metadata.  In
        # that case retain deterministic ordinal mapping without inventing any
        # image bytes; artifact_path still verifies each committed PNG.
        representative = list(range(len(artifact_ids)))
        for artifact_id in artifact_ids:
            coordinator.artifact_path(artifact_id)
        return representative, list(artifact_ids), ""

    def _handle_model_step(self, coordinator: AECoordinator, step: VisionStepResult) -> None:
        state = coordinator.state()
        history = coordinator.command_history()
        if state.status != "iterating" or self._has_pending(history):
            return
        if step.status == "pause":
            self._pause(coordinator, "model_paused")
            return
        if step.status == "no_progress" or step.batch is None or not step.batch.operations:
            try:
                coordinator.transition("no_progress", revision=state.revision)
            except CoordinatorConflict:
                pass
            return
        digest = step.operation_digest
        previous_digest = None
        for command in reversed(history):
            if command.kind != "apply_batch":
                continue
            workflow = self._workflow(command)
            if workflow.get("stage") == "candidate_apply":
                value = workflow.get("operation_digest")
                if isinstance(value, str):
                    previous_digest = value
                break
        if digest is not None and digest == previous_digest:
            try:
                coordinator.transition("no_progress", revision=state.revision)
            except CoordinatorConflict:
                pass
            return
        _scene, mapping, _manifest, approved = self._authoritative(coordinator)
        checkpoint = self._last_passing(state)
        inventory = self._inventory_from_checkpoint(checkpoint)
        native_ids = self._native_ids_from_checkpoint(checkpoint)
        if not inventory:
            inventory = self._inventory_from_mapping(mapping)
            native_ids = {}
        batch_payload = step.batch.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        payload = {
            "batch": batch_payload,
            "approved_capabilities": self._approved_capabilities_for_batch(approved, step.batch),
            "scene_frame_count": step.batch.scene_frame_count,
            "duration": step.batch.duration,
            "layer_count": step.batch.layer_count,
            "baseline": False,
            "locked_source_ids": list(coordinator.plan.locked_targets),
            "layer_sources": inventory,
            "layer_native_ids": native_ids,
            "workflow": {
                "stage": "candidate_apply",
                "operation_digest": digest,
                "checkpoint_index": self._next_checkpoint_index(state),
                "model_response": step.model_response,
            },
        }
        self._enqueue(
            coordinator,
            state,
            "apply_batch",
            payload,
            expected_checkpoint=self._checkpoint_index(state),
        )

    @staticmethod
    def _inventory_from_mapping(mapping: BaselineMapping) -> dict[str, str | None]:
        return {
            row.instance_id: row.source_element_id for row in mapping.final_inventory
        }

    def _advance_manual(
        self,
        coordinator: AECoordinator,
        state: Any,
        history: Sequence[Any],
        *,
        allow_prepare: bool = False,
    ) -> Any:
        marker = self._manual_prepare(history, state)
        candidate_index = self._manual_candidate_checkpoint(history, state)
        if marker is not None and isinstance(candidate_index, int):
            if candidate_index != state.open_checkpoint:
                _scene, mapping, _manifest, _approved = self._authoritative(coordinator)
                candidate_artifact = self._manual_candidate_artifact(
                    coordinator,
                    history,
                    state,
                )
                if candidate_artifact is None:
                    self._pause(coordinator, "manual_checkpoint_missing")
                    return None
                reopened = any(
                    command.kind == "open_project"
                    and self._successful(command)
                    and self._workflow(command).get("stage") == "manual_candidate_reopen"
                    and self._workflow(command).get("checkpoint_index") == candidate_index
                    and self._workflow(command).get("manual_epoch") == state.manual_epoch
                    and getattr(state, "device_id", None) is not None
                    and self._workflow(command).get("device_id") == state.device_id
                    for command in history
                )
                if not reopened:
                    payload = self._composition_payload(mapping)
                    payload["checkpoint_index"] = candidate_index
                    payload["checkpoint_artifact"] = candidate_artifact
                    payload["workflow"] = {
                        "stage": "manual_candidate_reopen",
                        "checkpoint_index": candidate_index,
                        "manual_epoch": state.manual_epoch,
                        "device_id": getattr(state, "device_id", None),
                    }
                    return self._enqueue(
                        coordinator,
                        state,
                        "open_project",
                        payload,
                        expected_checkpoint=self._checkpoint_index(state),
                    )
        if marker is None:
            scene, mapping, manifest, _approved = self._authoritative(coordinator)
            checkpoint_index = state.open_checkpoint
            if isinstance(state.selected_checkpoint, int) and state.selected_checkpoint != checkpoint_index:
                checkpoint_index = state.selected_checkpoint
            if not isinstance(checkpoint_index, int) and not state.baseline_complete:
                if any(item.index == 0 for item in state.checkpoints):
                    checkpoint_index = 0
            if not isinstance(checkpoint_index, int):
                self._pause(coordinator, "manual_checkpoint_missing")
                return None
            reopened = any(
                command.kind == "open_project"
                and self._successful(command)
                and self._workflow(command).get("stage") == "manual_reopen"
                and self._workflow(command).get("checkpoint_index") == checkpoint_index
                and self._workflow(command).get("manual_epoch") == state.manual_epoch
                and getattr(state, "device_id", None) is not None
                and self._workflow(command).get("device_id") == state.device_id
                for command in history
            )
            if not reopened:
                payload = self._composition_payload(mapping)
                payload["checkpoint_index"] = checkpoint_index
                checkpoint_artifact = self._checkpoint_artifact(
                    coordinator,
                    state,
                    checkpoint_index,
                )
                if checkpoint_artifact is not None:
                    payload["checkpoint_artifact"] = checkpoint_artifact
                payload["workflow"] = {
                    "stage": "manual_reopen",
                    "checkpoint_index": checkpoint_index,
                    "manual_epoch": state.manual_epoch,
                    "device_id": getattr(state, "device_id", None),
                }
                return self._enqueue(
                    coordinator,
                    state,
                    "open_project",
                    payload,
                    expected_checkpoint=self._checkpoint_index(state),
                )
            if not allow_prepare and not state.manual_sync_requested:
                return None
            candidate_index = self._next_checkpoint_index(state)
            reservation = coordinator.reserve_artifact("aep", _MAX_AEP_BYTES)
            payload = {
                "prepare_manual": True,
                "index": candidate_index,
                "artifacts": [
                    self._artifact_record(
                        reservation.id,
                        "aep",
                        f"checkpoint-{candidate_index}.aep",
                        "checkpoints",
                    )
                ],
                "workflow": {
                    "stage": "manual_prepare",
                    "candidate_checkpoint_index": candidate_index,
                },
            }
            return self._enqueue(
                coordinator,
                state,
                "sync_manual",
                payload,
                expected_checkpoint=checkpoint_index,
            )
        scene, _mapping, manifest, _approved = self._authoritative(coordinator)
        plan = coordinator.plan
        base = next(
            (item for item in state.checkpoints if item.index == state.open_checkpoint),
            None,
        )
        if base is None:
            base = next(
                (item for item in state.checkpoints if item.index == state.selected_checkpoint),
                self._last_passing(state),
            )
        base = self._checkpoint_detail(coordinator, base)
        inventory = self._inventory_from_checkpoint(base)
        prior_native_ids = self._native_ids_from_checkpoint(base)
        manual_after = marker.sequence
        inspections = self._inspection_commands(
            history,
            "manual_inspect",
            after_sequence=manual_after,
        )
        budget_sources = inventory
        budget_native_ids = prior_native_ids
        issued_new: Sequence[str] = ()
        if inspections:
            first_payload = inspections[0].payload
            if isinstance(first_payload, Mapping):
                raw_issued = first_payload.get("allow_new_agent_ids")
                if isinstance(raw_issued, list):
                    issued_new = tuple(item for item in raw_issued if isinstance(item, str))
            try:
                first_rows = self._inspection_rows(
                    history,
                    "manual_inspect",
                    after_sequence=manual_after,
                )
                budget_sources, budget_native_ids = validate_manual_inventory(
                    first_rows[0],
                    prior_layer_sources=inventory,
                    prior_layer_native_ids=prior_native_ids,
                    allow_new_agent_ids=issued_new,
                )
            except Exception:
                self._pause(coordinator, "verification_error")
                return None
        chunks = self._bound_observation_chunks(
            self._observation_chunks(scene),
            layer_sources=budget_sources,
            capability_manifest=manifest,
            max_new_layers=0 if inspections else max(0, 1_000 - len(inventory)),
        )
        if len(inspections) < len(chunks):
            index = len(inspections)
            layer_sources = budget_sources
            layer_native_ids = budget_native_ids
            allow_new: list[str] | None = None
            if index == 0:
                allow_new = self._fresh_agent_ids(inventory, state.issued_instance_id_tombstones)
                issued_new = tuple(allow_new)
            else:
                try:
                    rows = self._inspection_rows(
                        history,
                        "manual_inspect",
                        after_sequence=manual_after,
                    )
                    self._validate_inspection_rows(
                        rows,
                        expected_sources=budget_sources,
                        expected_native_ids=budget_native_ids,
                    )
                except Exception:
                    self._pause(coordinator, "verification_error")
                    return None
            payload = {
                "frame_count": int(scene.frames),
                "fps": float(scene.fps),
                "capability_hash": coordinator.plan.capability_hash,
                "requested": [
                    {"source_element_id": source_id, "frame": frame}
                    for source_id, frame in chunks[index]
                ],
                "layer_sources": layer_sources,
                "layer_native_ids": layer_native_ids,
                "workflow": {
                    "stage": "manual_inspect",
                    "chunk_index": index,
                    "manual_marker_sequence": marker.sequence,
                    "manual_marker_id": marker.id,
                    "manual_epoch": state.manual_epoch,
                    "manual_attempt_id": state.manual_attempt_id,
                },
            }
            if allow_new is not None:
                payload["allow_new_agent_ids"] = allow_new
            return self._enqueue(
                coordinator,
                state,
                "inspect_layers",
                payload,
                expected_checkpoint=self._checkpoint_index(state),
            )

        rows = self._inspection_rows(
            history,
            "manual_inspect",
            after_sequence=manual_after,
        )
        try:
            issued_new: Sequence[str] = ()
            if inspections:
                first_payload = inspections[0].payload
                if isinstance(first_payload, Mapping):
                    raw_issued = first_payload.get("allow_new_agent_ids")
                    if isinstance(raw_issued, list):
                        issued_new = tuple(
                            item for item in raw_issued if isinstance(item, str)
                        )
            first_sources, first_native_ids = validate_manual_inventory(
                rows[0],
                prior_layer_sources=inventory,
                prior_layer_native_ids=prior_native_ids,
                allow_new_agent_ids=issued_new,
            )
            authoritative, authoritative_native_ids = self._validate_inspection_rows(
                rows,
                expected_sources=first_sources,
                expected_native_ids=first_native_ids,
            )
            merged = merge_inspection_chunks(
                scene,
                rows,
                authoritative_instance_sources=authoritative,
                authoritative_instance_native_ids=authoritative_native_ids,
            )
        except Exception:
            self._pause(coordinator, "verification_error")
            return None
        checkpoint_index = self._next_checkpoint_index(state)
        render = self._render_command(
            history,
            checkpoint_index,
            "manual_render",
            manual_marker=marker,
        )
        if render is None:
            representative = select_representative_frames(
                scene,
                merged,
                frame_count=scene.frames,
                keep_predicates=scene.constraints,
                cap=12,
            )
            records, _frame_ids, _mp4 = self._reserve_preview(
                coordinator, checkpoint_index, representative
            )
            payload = self._render_payload(scene, checkpoint_index, representative, records)
            payload["workflow"] = {
                "stage": "manual_render",
                "checkpoint_index": checkpoint_index,
                "manual_marker_sequence": marker.sequence,
                "manual_marker_id": marker.id,
                "manual_epoch": state.manual_epoch,
                "manual_attempt_id": state.manual_attempt_id,
            }
            return self._enqueue(
                coordinator,
                state,
                "render_preview",
                payload,
                expected_checkpoint=self._checkpoint_index(state),
            )

        if any(
            self._workflow(item).get("checkpoint_index") == checkpoint_index
            and self._workflow(item).get("stage") == "manual_sync"
            and self._workflow(item).get("manual_marker_sequence") == marker.sequence
            and self._workflow(item).get("manual_marker_id") == marker.id
            for item in self._completed(history, "sync_manual")
        ):
            return None
        try:
            report = verify_inspection(
                scene,
                rows,
                authoritative_instance_sources=authoritative,
                authoritative_instance_native_ids=authoritative_native_ids,
            )
            _representative, frame_ids, preview_id = self._render_records(render)
            aep_id = coordinator.reserve_artifact("aep", _MAX_AEP_BYTES).id
            checkpoint = self._checkpoint_context(
                plan=plan,
                index=checkpoint_index,
                provenance="manual",
                passed=bool(report.passed and state.baseline_complete),
                aep=aep_id,
                preview=preview_id,
                frames=frame_ids,
                inspection=merged.model_dump(mode="json"),
                operations=(),
                report=report,
                model_response={},
            )
        except Exception:
            self._pause(coordinator, "verification_error")
            return None
        payload = {
            "index": checkpoint_index,
            "checkpoint_context": checkpoint,
            "artifacts": [
                self._artifact_record(
                    aep_id,
                    "aep",
                    f"checkpoint-{checkpoint_index}.aep",
                    "checkpoints",
                )
            ],
            "workflow": {
                "stage": "manual_sync",
                "checkpoint_index": checkpoint_index,
                "manual_marker_sequence": marker.sequence,
                "manual_marker_id": marker.id,
                "manual_epoch": state.manual_epoch,
                "manual_attempt_id": state.manual_attempt_id,
            },
        }
        return self._enqueue(
            coordinator,
            state,
            "sync_manual",
            payload,
            expected_checkpoint=self._checkpoint_index(state),
        )

    @classmethod
    def _final_command(
        cls,
        history: Sequence[Any],
        kind: str,
        stage: str,
        checkpoint: int,
        finalization_key: str,
        *,
        device_id: str | None = None,
        successful: bool | None = None,
    ) -> Any | None:
        candidates = []
        for command in history:
            if command.kind != kind:
                continue
            if successful is not None and cls._successful(command) != successful:
                continue
            workflow = cls._workflow(command)
            if (
                workflow.get("stage") != stage
                or workflow.get("checkpoint_index", workflow.get("selected_checkpoint")) != checkpoint
                or workflow.get("finalization_key") != finalization_key
            ):
                continue
            if device_id is not None and workflow.get("device_id") != device_id:
                continue
            candidates.append(command)
        return max(candidates, key=lambda command: command.sequence, default=None)

    @staticmethod
    def _final_asset_records(plan: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        unique: dict[str, Any] = {}
        for asset in plan.assets:
            prior = unique.get(asset.id)
            if prior is not None:
                if (
                    prior.sha256 != asset.sha256
                    or prior.length != asset.length
                    or prior.media_kind != asset.media_kind
                ):
                    raise AEWorkflowError("duplicate package asset metadata conflicts")
                continue
            unique[asset.id] = asset
        assets: list[dict[str, Any]] = []
        package_media: list[dict[str, Any]] = []
        for asset in unique.values():
            assets.append(
                {
                    "asset_id": asset.id,
                    "id": asset.id,
                    "sha256": asset.sha256,
                    "length": asset.length,
                    "media_kind": asset.media_kind,
                    "role": asset.role,
                    "content_filename": asset.id,
                }
            )
            package_media.append(
                {
                    "asset_id": asset.id,
                    "filename": asset.id,
                    "sha256": asset.sha256,
                    "length": asset.length,
                    "media_kind": asset.media_kind,
                }
            )
        return assets, package_media

    def _advance_finalizing(
        self,
        coordinator: AECoordinator,
        state: Any,
        history: Sequence[Any],
    ) -> Any:
        """Reverify the selected checkpoint, then run exactly one final stage."""
        try:
            final_plan, checkpoint, _context_digest, finalization_key = (
                coordinator.validate_finalization()
            )
        except CoordinatorConflict:
            self._pause(coordinator, "verification_failed")
            return None
        selected = checkpoint.index
        checkpoint_artifact = self._checkpoint_artifact(
            coordinator,
            state,
            selected,
        )
        if checkpoint_artifact is None:
            self._pause(coordinator, "verification_failed")
            return None
        scene, _mapping, manifest, _approved = self._authoritative(coordinator)
        device_id = getattr(state, "device_id", None)
        if not isinstance(device_id, str):
            self._pause(coordinator, "connector_failed")
            return None

        reopen = self._final_command(
            history,
            "open_project",
            "final_reopen",
            selected,
            finalization_key,
            device_id=device_id,
            successful=True,
        )
        if reopen is not None and getattr(state, "open_checkpoint", None) != selected:
            reopen = None
        if reopen is None:
            previous = self._final_command(
                history,
                "open_project",
                "final_reopen",
                selected,
                finalization_key,
                device_id=device_id,
                successful=False,
            )
            if previous is not None:
                payload = dict(previous.payload)
            else:
                _scene, mapping, _manifest, _approved = self._authoritative(coordinator)
                payload = self._composition_payload(mapping)
                payload["checkpoint_index"] = selected
                payload["checkpoint_artifact"] = checkpoint_artifact
            payload["checkpoint_index"] = selected
            payload["final_plan_digest"] = final_plan.digest
            payload["finalization_key"] = finalization_key
            payload["workflow"] = {
                "stage": "final_reopen",
                "checkpoint_index": selected,
                "finalization_key": finalization_key,
                "device_id": device_id,
            }
            return self._enqueue(
                coordinator,
                state,
                "open_project",
                payload,
                expected_checkpoint=selected,
            )

        layer_sources = self._inventory_from_checkpoint(checkpoint)
        layer_native_ids = self._native_ids_from_checkpoint(checkpoint)
        if not layer_sources or set(layer_sources) != set(layer_native_ids):
            self._pause(coordinator, "verification_failed")
            return None
        chunks = self._bound_observation_chunks(
            self._observation_chunks(scene),
            layer_sources=layer_sources,
            capability_manifest=manifest,
        )
        inspections = self._inspection_commands(
            history,
            "final_inspect",
            after_sequence=reopen.sequence,
        )
        if len(inspections) < len(chunks):
            index = len(inspections)
            if index:
                try:
                    prior_rows = self._inspection_rows(
                        history,
                        "final_inspect",
                        after_sequence=reopen.sequence,
                    )
                    self._validate_inspection_rows(
                        prior_rows,
                        expected_sources=layer_sources,
                        expected_native_ids=layer_native_ids,
                    )
                except Exception:
                    self._pause(coordinator, "verification_failed")
                    return None
            payload = {
                "frame_count": int(scene.frames),
                "fps": float(scene.fps),
                "capability_hash": final_plan.capability_hash,
                "requested": [
                    {"source_element_id": source_id, "frame": frame}
                    for source_id, frame in chunks[index]
                ],
                "layer_sources": layer_sources,
                "layer_native_ids": layer_native_ids,
                "checkpoint_index": selected,
                "checkpoint_artifact": checkpoint_artifact,
                "final_plan_digest": final_plan.digest,
                "finalization_key": finalization_key,
                "workflow": {
                    "stage": "final_inspect",
                    "chunk_index": index,
                    "checkpoint_index": selected,
                    "finalization_key": finalization_key,
                },
            }
            return self._enqueue(
                coordinator,
                state,
                "inspect_layers",
                payload,
                expected_checkpoint=selected,
            )
        rows = self._inspection_rows(
            history,
            "final_inspect",
            after_sequence=reopen.sequence,
        )
        try:
            authoritative, native_ids = self._validate_inspection_rows(
                rows,
                expected_sources=layer_sources,
                expected_native_ids=layer_native_ids,
            )
            report = verify_inspection(
                scene,
                rows,
                authoritative_instance_sources=authoritative,
                authoritative_instance_native_ids=native_ids,
            )
            if not report.passed:
                self._pause(coordinator, "verification_failed")
                return None
        except Exception:
            self._pause(coordinator, "verification_failed")
            return None

        render = self._final_command(
            history,
            "render_final",
            "final_render",
            selected,
            finalization_key,
            successful=True,
        )
        if render is None:
            previous = self._final_command(
                history,
                "render_final",
                "final_render",
                selected,
                finalization_key,
                successful=False,
            )
            if previous is not None:
                payload = dict(previous.payload)
            else:
                reservation = coordinator.reserve_artifact("mp4", _MAX_MP4_BYTES)
                width, height = (int(scene.size[0]), int(scene.size[1]))
                frame_count = int(scene.frames)
                payload = {
                    "checkpoint_index": selected,
                    "frame_count": frame_count,
                    "fps": float(scene.fps),
                    "width": width,
                    "height": height,
                    "final_plan_digest": final_plan.digest,
                    "finalization_key": finalization_key,
                    "checkpoint_artifact": checkpoint_artifact,
                    "sequence": {
                        "directory": "renders",
                        "pattern": "final-%06d.png",
                        "frame_count": frame_count,
                        "first_frame": 0,
                        "last_frame": max(0, frame_count - 1),
                        "width": width,
                        "height": height,
                        "fps": float(scene.fps),
                    },
                    "artifacts": [
                        self._artifact_record(
                            reservation.id,
                            "mp4",
                            "final.mp4",
                            "renders",
                        )
                    ],
                }
            payload["checkpoint_index"] = selected
            payload["final_plan_digest"] = final_plan.digest
            payload["finalization_key"] = finalization_key
            payload["workflow"] = {
                "stage": "final_render",
                "checkpoint_index": selected,
                "finalization_key": finalization_key,
            }
            return self._enqueue(
                coordinator,
                state,
                "render_final",
                payload,
                expected_checkpoint=selected,
            )

        mp4_id = state.final_mp4_artifact_id
        if not isinstance(mp4_id, str):
            result = render.result if isinstance(render.result, Mapping) else {}
            artifacts = result.get("artifacts")
            if isinstance(artifacts, list):
                mp4_id = next(
                    (
                        item.get("id", item.get("artifact_id"))
                        for item in artifacts
                        if isinstance(item, Mapping) and item.get("kind") == "mp4"
                    ),
                    None,
                )
        if not isinstance(mp4_id, str):
            self._pause(coordinator, "upload_failed")
            return None
        try:
            coordinator.artifact_path(mp4_id)
        except CoordinatorConflict:
            self._pause(coordinator, "upload_failed")
            return None

        package = self._final_command(
            history,
            "package_project",
            "final_package",
            selected,
            finalization_key,
            successful=True,
        )
        if package is not None:
            try:
                coordinator.complete_finalization(state.revision)
            except CoordinatorConflict:
                self._pause(coordinator, "package_failed")
            return None
        previous = self._final_command(
            history,
            "package_project",
            "final_package",
            selected,
            finalization_key,
            successful=False,
        )
        if previous is not None:
            payload = dict(previous.payload)
        else:
            assets, package_media = self._final_asset_records(final_plan)
            aep = coordinator.reserve_artifact("aep", _MAX_AEP_BYTES)
            package_zip = coordinator.reserve_artifact("zip", _MAX_MP4_BYTES)
            payload = {
                "selected_checkpoint": selected,
                "final_plan_digest": final_plan.digest,
                "finalization_key": finalization_key,
                "checkpoint_index": selected,
                "checkpoint_artifact": checkpoint_artifact,
                "assets": assets,
                "package_media": package_media,
                "dependencies": self._dependency_manifest(final_plan),
                "artifacts": [
                    self._artifact_record(aep.id, "aep", "project.aep", "package"),
                    self._artifact_record(package_zip.id, "zip", "project.zip", "checkpoints"),
                ],
            }
        payload["selected_checkpoint"] = selected
        payload["final_plan_digest"] = final_plan.digest
        payload["checkpoint_index"] = selected
        payload["checkpoint_artifact"] = checkpoint_artifact
        payload["finalization_key"] = finalization_key
        payload["workflow"] = {
            "stage": "final_package",
            "checkpoint_index": selected,
            "finalization_key": finalization_key,
        }
        return self._enqueue(
            coordinator,
            state,
            "package_project",
            payload,
            expected_checkpoint=selected,
        )


__all__ = ["AEWorkflowError", "AEWorkflowService"]
