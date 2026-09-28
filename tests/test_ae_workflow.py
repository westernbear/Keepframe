from __future__ import annotations

import io
import json

from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from keepframe.after_effects.coordinator import CoordinatorConflict
from keepframe.after_effects.mapping import BaselineBatch, BaselineMapping, CompositionSettings
from keepframe.after_effects.operations import SetColorOperation
from keepframe.after_effects.planning import current_operation_manifest
from keepframe.after_effects.workflow import AEInspectionBudgetError, AEWorkflowService
from keepframe.ir.store import current_scene, init_project
from keepframe.render.plan import approve_render_plan, create_render_plan
from tests.test_ae_coordinator import (
    _AE_CAPABILITY_HASH,
    _AE_CAPABILITY_MANIFEST,
    _artifact_payload,
    _committed_checkpoint,
    _coordinator,
    _failed_checkpoint,
)
from tests.test_ae_mapping import _scene


class ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # pragma: no cover - exercised by workflow error tests
            future.set_exception(exc)
        return future


class NeverCalledLLM:
    def __call__(self):  # pragma: no cover - baseline must not call the model
        raise AssertionError("baseline must finish before model work")


def _workflow(root):
    scene, _version = current_scene(root, "s1")
    mapping = BaselineMapping(
        composition=CompositionSettings(
            width=scene.size[0],
            height=scene.size[1],
            frame_rate=scene.fps,
            duration=scene.frames / scene.fps,
            background_color="#000000",
        ),
        imported_asset_ids=(),
        batches=(),
        final_inventory=(),
    )
    service = AEWorkflowService(root.parent, NeverCalledLLM(), executor=ImmediateExecutor())
    service._authoritative = lambda _coordinator: (scene, mapping, {}, {})
    return service


def test_advance_starts_durable_baseline_after_device_ready(tmp_path):
    root, plan, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")

    service = _workflow(root)
    service.advance(coordinator)

    history = coordinator.command_history()
    assert [command.kind for command in history] == ["create_project"]
    command = history[0]
    assert command.expected_state == "baseline"
    assert command.payload["width"] == 640
    assert command.payload["height"] == 360
    assert command.payload["frame_rate"] == 30.0
    assert "plan_digest" not in command.payload
    assert "device_token" not in command.payload


def test_advance_does_not_repeat_a_completed_baseline_mutation(tmp_path):
    root, _plan, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    service = _workflow(root)
    service.advance(coordinator)
    command = coordinator.next_command("device-1")
    assert command is not None
    coordinator.accept_result("device-1", command.id, sequence=command.sequence, result={"opened": True})

    service = _workflow(root)
    service.advance(coordinator)
    history = coordinator.command_history()
    assert [item.kind for item in history] == ["create_project", "inspect_layers"]
    assert history[1].payload["layer_native_ids"] == {}
    assert sum(item.kind == "create_project" for item in history) == 1

def test_relay_advance_handoff_is_exposed_as_optional_service(tmp_path):
    # API-shape test: the relay accepts the same service object that the private
    # web listener owns, without requiring a second durable workflow store.
    from keepframe.after_effects.relay import make_relay_server

    server = make_relay_server(tmp_path, deployment_token="deployment-token", workflow=object())
    server.server_close()


def test_completed_candidate_apply_is_not_reused_for_next_checkpoint(tmp_path):
    service = AEWorkflowService(tmp_path, object(), executor=ImmediateExecutor())
    apply = SimpleNamespace(
        kind="apply_batch",
        status="completed",
        result={"ok": True},
        sequence=1,
        payload={
            "workflow": {
                "stage": "candidate_apply",
                "checkpoint_index": 1,
                "operation_digest": "digest-1",
            }
        },
    )
    save = SimpleNamespace(
        kind="save_checkpoint",
        status="completed",
        result={"ok": True, "checkpoint": {"index": 1, "passed": True}},
        sequence=2,
        payload={"workflow": {"stage": "candidate_save", "checkpoint_index": 1}},
    )

    assert service._candidate_apply((apply, save)) is None


def test_manual_checkpoint_consumes_abandoned_candidate_apply(tmp_path):
    service = AEWorkflowService(tmp_path, object(), executor=ImmediateExecutor())
    apply = SimpleNamespace(
        kind="apply_batch",
        status="completed",
        result={"ok": True},
        sequence=1,
        payload={
            "workflow": {
                "stage": "candidate_apply",
                "checkpoint_index": 1,
                "operation_digest": "digest-1",
            }
        },
    )
    manual_sync = SimpleNamespace(
        kind="sync_manual",
        status="completed",
        result={"ok": True, "checkpoint": {"index": 1, "passed": True}},
        sequence=2,
        payload={"workflow": {"stage": "manual_sync", "checkpoint_index": 1}},
    )

    assert service._candidate_apply((apply, manual_sync)) is None


class ApplyClient:
    def __init__(self):
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": f"call-{len(self.calls)}",
                    "type": "function",
                    "function": {
                        "name": "apply_ae_batch",
                        "arguments": json.dumps(
                            {
                                "operations": [
                                    {
                                        "kind": "add_layer",
                                        "layer_instance_id": "new-polish",
                                        "layer_type": "null",
                                        "name": "Polish",
                                    }
                                ]
                            },
                            separators=(",", ":"),
                        ),
                    },
                }
            ],
        }


def _valid_coordinator(tmp_path):
    root = tmp_path / "valid"
    scene = _scene(frames=3, fps=10.0).model_copy(update={"id": "s1"})
    init_project(
        root,
        {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size)},
        scene,
    )
    (root / "meta.json").write_text(
        json.dumps({"id": "valid", "status": "approved", "version": "v1", "scene": "s1"}),
        encoding="utf-8",
    )
    plan = create_render_plan(
        root,
        project_id="valid",
        scene_id="s1",
        version_id="v1",
        backend="after_effects",
        mode="preview",
        direction="Polish without changing the source.",
        permitted_operations=current_operation_manifest(),
        capability_hash=_AE_CAPABILITY_HASH,
        capability_manifest=_AE_CAPABILITY_MANIFEST,
    )
    approval = approve_render_plan(root, plan.id, digest=plan.digest, revision=0)
    from keepframe.after_effects.coordinator import AECoordinator

    coordinator = AECoordinator(root, plan.id)
    session = coordinator.start(approval.execution_id)
    coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    return root, coordinator


def _publish_record(coordinator, record):
    payload = _artifact_payload(record["kind"])
    coordinator.publish_artifact(
        "device-1",
        record["reservation_id"],
        io.BytesIO(payload),
        content_length=len(payload),
        require_live_command=True,
    )


def _complete_next(coordinator, command=None):
    if command is None:
        command = coordinator.next_command("device-1")
    assert command is not None
    if command.kind == "inspect_layers":
        sources = command.payload["layer_sources"]
        native_ids = dict(command.payload.get("layer_native_ids", {}))
        next_native_id = max(native_ids.values(), default=100) + 1
        for instance_id in sources:
            if instance_id not in native_ids:
                native_ids[instance_id] = next_native_id
                next_native_id += 1
        result = {
            "schema_version": "keepframe.ae-inspection/1",
            "frame_count": command.payload["frame_count"],
            "fps": command.payload["fps"],
            "requested": command.payload["requested"],
            "layers": [
                {
                    "layer_instance_id": instance_id,
                    "native_layer_id": native_ids[instance_id],
                    "source_element_id": source_id,
                    "kind": "null",
                    "index": index,
                    "frame_start": 0,
                    "frame_end": command.payload["frame_count"],
                }
                for index, (instance_id, source_id) in enumerate(sources.items(), start=1)
            ],
            "layer_sources": sources,
            "layer_native_ids": native_ids,
            "samples": [],
            "missing_pairs": [],
            "source_aggregated": False,
            "heartbeat": {
                "capability_hash": _AE_CAPABILITY_HASH,
                "version": _AE_CAPABILITY_MANIFEST["version"],
                "major": _AE_CAPABILITY_MANIFEST["major"],
                "host": _AE_CAPABILITY_MANIFEST["host"],
            },
        }
    else:
        for record in command.payload.get("artifacts", ()):
            _publish_record(coordinator, record)
        result = {"ok": True}
        if command.kind == "apply_batch":
            source_map = dict(command.payload.get("layer_sources", {}))
            native_map = dict(command.payload.get("layer_native_ids", {}))
            next_native_id = max(native_map.values(), default=100) + 1
            for operation in command.payload.get("batch", {}).get("operations", ()):
                instance_id = operation.get("layer_instance_id")
                if not isinstance(instance_id, str):
                    continue
                if operation.get("kind") == "add_layer":
                    source_map[instance_id] = operation.get("source_element_id")
                    native_map.setdefault(instance_id, next_native_id)
                    next_native_id += 1
                elif operation.get("kind") == "remove_layer":
                    source_map.pop(instance_id, None)
                    native_map.pop(instance_id, None)
            result["layer_sources"] = source_map
            result["layer_native_ids"] = native_map
        if command.kind == "sync_manual" and command.payload.get("prepare_manual") is True:
            pass
        elif command.kind in {"save_checkpoint", "sync_manual"}:
            result["artifacts"] = [
                record["reservation_id"]
                for record in command.payload.get("artifacts", ())
                if isinstance(record, dict) and isinstance(record.get("reservation_id"), str)
            ]
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result=result,
    )
    return command


def _drive_checkpoint(service, coordinator, target_count):
    for _ in range(32):
        if len(coordinator.state().checkpoints) >= target_count:
            return
        service.advance(coordinator)
        _complete_next(coordinator)
    raise AssertionError("workflow did not reach checkpoint")

def test_stop_during_first_baseline_batch_captures_partial_state(tmp_path):
    root, _plan, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    service = _workflow(root)
    scene, mapping, manifest, approved = service._authoritative(coordinator)
    add_first = {
        "kind": "add_layer",
        "layer_instance_id": "first",
        "layer_type": "null",
        "name": "First",
    }
    add_second = {
        "kind": "add_layer",
        "layer_instance_id": "second",
        "layer_type": "null",
        "name": "Second",
    }
    batches = (
        BaselineBatch(
            index=0,
            capability_digest=_AE_CAPABILITY_HASH,
            scene_frame_count=scene.frames,
            duration=scene.frames / scene.fps,
            operations=(add_first,),
            layer_sources={},
            layer_count=0,
        ),
        BaselineBatch(
            index=1,
            capability_digest=_AE_CAPABILITY_HASH,
            scene_frame_count=scene.frames,
            duration=scene.frames / scene.fps,
            operations=(add_second,),
            layer_sources={"first": None},
            layer_count=1,
        ),
    )
    mapping = mapping.model_copy(update={"batches": batches})
    service._authoritative = lambda _coordinator: (scene, mapping, manifest, approved)

    service.advance(coordinator)
    _complete_next(coordinator)
    service.advance(coordinator)
    apply = coordinator.next_command("device-1")
    assert apply is not None and apply.kind == "apply_batch"
    session = coordinator.transition("stop", revision=coordinator.state().revision)
    coordinator.accept_result(
        "device-1",
        apply.id,
        sequence=apply.sequence,
        result={
            "ok": True,
            "layer_sources": {"first": None},
            "layer_native_ids": {"first": 101},
        },
    )

    for _ in range(8):
        if coordinator.state().status.startswith("paused:"):
            break
        service.advance(coordinator)
        _complete_next(coordinator)

    state = coordinator.state()
    assert state.status == "paused:user"
    assert len(state.checkpoints) == 1
    assert coordinator.checkpoint(0).operations == (add_first,)
    assert not any(
        command.kind == "apply_batch"
        and command.payload.get("workflow", {}).get("batch_index") == 1
        for command in coordinator.command_history()
    )
    state = coordinator.transition("continue", revision=state.revision)
    state = coordinator.transition(
        "device_ready",
        revision=state.revision,
        device_id="device-1",
    )
    service.advance(coordinator)
    remaining = coordinator.next_command("device-1")
    assert remaining is not None and remaining.kind == "apply_batch"
    assert remaining.payload["workflow"]["batch_index"] == 1
    _complete_next(coordinator, remaining)
    state = coordinator.transition("stop", revision=coordinator.state().revision)
    assert state.status == "pause_requested"
    assert state.checkpoint_required is True
    for _ in range(16):
        if (
            coordinator.state().checkpoints
            and len(coordinator.checkpoint(0).operations) == 2
        ):
            break
        service.advance(coordinator)
        _complete_next(coordinator)
    state = coordinator.state()
    assert state.baseline_complete is True
    assert coordinator.checkpoint(0).operations == (add_first, add_second)


def test_full_baseline_candidate_and_repeated_plan_pause(tmp_path):
    root, coordinator = _valid_coordinator(tmp_path)
    client = ApplyClient()
    service = AEWorkflowService(root.parent, lambda: client, executor=ImmediateExecutor())

    _drive_checkpoint(service, coordinator, 1)
    baseline = coordinator.state().checkpoints[0]
    assert baseline.index == 0
    assert baseline.provenance == "baseline"
    assert baseline.passed is True

    service.advance(coordinator)
    assert coordinator.next_command("device-1") is None
    service.advance(coordinator)
    apply = coordinator.next_command("device-1")
    assert apply is not None and apply.kind == "apply_batch"
    assert apply.payload["workflow"]["checkpoint_index"] == 1
    new_id = apply.payload["batch"]["operations"][0]["layer_instance_id"]
    coordinator.accept_result(
        "device-1",
        apply.id,
        sequence=apply.sequence,
        result={
            "ok": True,
            "layer_sources": {new_id: None},
            "layer_native_ids": {new_id: 101},
        },
    )
    _drive_checkpoint(service, coordinator, 2)
    candidate = coordinator.state().checkpoints[1]
    assert candidate.provenance == "agent"
    assert candidate.passed is True
    assert coordinator.checkpoint(1).operations[0]["kind"] == "add_layer"

    service.advance(coordinator)
    service.advance(coordinator)
    assert coordinator.state().status == "paused:no_progress"
    assert len(client.calls) == 2
    assert [
        tool["function"]["name"] for tool in client.calls[0][1]
    ] == ["apply_ae_batch", "pause_ae"]


class NoopClient:
    def complete(self, messages, tools):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-noop",
                    "type": "function",
                    "function": {
                        "name": "apply_ae_batch",
                        "arguments": '{"operations":[]}',
                    },
                }
            ],
        }


class UnsupportedClient:
    def complete(self, messages, tools):
        raise RuntimeError("multimodal image input is unsupported")


def test_noop_and_unsupported_vision_pause_with_baseline_preserved(tmp_path):
    root, coordinator = _valid_coordinator(tmp_path)
    noop = AEWorkflowService(root.parent, lambda: NoopClient(), executor=ImmediateExecutor())
    _drive_checkpoint(noop, coordinator, 1)
    noop.advance(coordinator)
    noop.advance(coordinator)
    state = coordinator.state()
    assert state.status == "paused:no_progress"
    assert state.selected_checkpoint == 0
    assert [item.index for item in state.checkpoints] == [0]

    second_root, second = _valid_coordinator(tmp_path / "second")
    unsupported = AEWorkflowService(
        second_root.parent,
        lambda: UnsupportedClient(),
        executor=ImmediateExecutor(),
    )
    _drive_checkpoint(unsupported, second, 1)
    unsupported.advance(second)
    unsupported.advance(second)
    state = second.state()
    assert state.status == "paused:vision_unsupported"
    assert state.selected_checkpoint == 0


def test_manual_sync_runs_inspect_render_save_and_stays_one_way(tmp_path):
    root, coordinator = _valid_coordinator(tmp_path)
    service = AEWorkflowService(root.parent, lambda: NoopClient(), executor=ImmediateExecutor())
    _drive_checkpoint(service, coordinator, 1)
    state = coordinator.transition("no_progress", revision=coordinator.state().revision)
    coordinator.transition("begin_manual", revision=state.revision)
    service.manual_sync(coordinator)
    reopen = coordinator.command_history()[-1]
    assert reopen.kind == "open_project"
    assert reopen.payload["checkpoint_index"] == 0
    _complete_next(coordinator)
    service.advance(coordinator)
    prepare = coordinator.command_history()[-1]
    assert prepare.kind == "sync_manual"
    assert prepare.payload["prepare_manual"] is True

    _drive_checkpoint(service, coordinator, 2)

    state = coordinator.state()
    assert state.status == "paused:manual_synced"
    assert coordinator.checkpoint(state.checkpoints[-1].index).provenance == "manual"
    assert coordinator.checkpoint(state.checkpoints[-1].index).passed is True
    first_inspection = next(
        command
        for command in coordinator.command_history()
        if (
            command.kind == "inspect_layers"
            and command.payload.get("workflow", {}).get("stage") == "manual_inspect"
        )
    )
    assert first_inspection.payload["workflow"]["manual_marker_sequence"] == prepare.sequence
    assert first_inspection.payload["workflow"]["manual_marker_id"] == prepare.id
    first_inspections = sum(
        command.kind == "inspect_layers" for command in coordinator.command_history()
    )
    service.manual_sync(coordinator)
    second_reopen = coordinator.command_history()[-1]
    assert second_reopen.kind == "open_project"
    _complete_next(coordinator)
    service.advance(coordinator)
    second_prepare = coordinator.command_history()[-1]
    _drive_checkpoint(service, coordinator, 3)
    assert sum(
        command.kind == "inspect_layers" for command in coordinator.command_history()
    ) == first_inspections + 1
    last_inspection = [
        command for command in coordinator.command_history() if command.kind == "inspect_layers"
    ][-1]
    assert last_inspection.payload["workflow"]["manual_marker_sequence"] == second_prepare.sequence
    assert last_inspection.payload["workflow"]["manual_marker_id"] == second_prepare.id
    assert second_prepare.sequence != prepare.sequence
    state = coordinator.state()
    assert coordinator.checkpoint(state.checkpoints[-1].index).index == 2
    assert state.status == "paused:manual_synced"
    assert current_scene(root, "s1")[0] == _scene(frames=3, fps=10.0).model_copy(
        update={"id": "s1"}
    )


def test_manual_sync_does_not_reuse_failed_epoch_inspections(tmp_path):
    root, coordinator = _valid_coordinator(tmp_path)
    service = AEWorkflowService(
        root.parent,
        lambda: NoopClient(),
        executor=ImmediateExecutor(),
    )
    _drive_checkpoint(service, coordinator, 1)
    state = coordinator.transition("no_progress", revision=coordinator.state().revision)
    coordinator.transition("begin_manual", revision=state.revision)
    service.manual_sync(coordinator)
    first_reopen = coordinator.command_history()[-1]
    assert first_reopen.kind == "open_project"
    _complete_next(coordinator)
    service.advance(coordinator)
    first_prepare = coordinator.command_history()[-1]

    service._observation_chunks = lambda _scene: [[], []]
    service.advance(coordinator)
    prepared = coordinator.next_command("device-1")
    assert prepared is not None and prepared.kind == "sync_manual"
    _complete_next(coordinator, prepared)
    service.advance(coordinator)
    candidate_reopen = coordinator.next_command("device-1")
    assert candidate_reopen is not None and candidate_reopen.kind == "open_project"
    assert set(candidate_reopen.payload["checkpoint_artifact"]) == {
        "id",
        "sha256",
        "length",
    }
    _complete_next(coordinator, candidate_reopen)
    service.advance(coordinator)
    first_inspect = coordinator.next_command("device-1")
    assert first_inspect is not None and first_inspect.kind == "inspect_layers"
    _complete_next(coordinator, first_inspect)
    service.advance(coordinator)
    second_inspect = coordinator.next_command("device-1")
    assert second_inspect is not None and second_inspect.kind == "inspect_layers"
    coordinator.accept_result(
        "device-1",
        second_inspect.id,
        sequence=second_inspect.sequence,
        result={"ok": False},
    )
    assert coordinator.state().status == "paused:inspection_failed"

    state = coordinator.transition("continue", revision=coordinator.state().revision)
    state = coordinator.transition(
        "device_ready",
        revision=state.revision,
        device_id="device-1",
    )
    state = coordinator.transition("no_progress", revision=state.revision)
    coordinator.transition("begin_manual", revision=state.revision)
    service.advance(coordinator)
    second_reopen = coordinator.command_history()[-1]
    assert second_reopen.kind == "open_project"
    _complete_next(coordinator)
    service.advance(coordinator)
    second_prepare = coordinator.command_history()[-1]
    assert second_prepare.id != first_prepare.id
    assert (
        second_prepare.payload["workflow"]["manual_epoch"]
        > first_prepare.payload["workflow"]["manual_epoch"]
    )

def test_failed_candidate_is_preserved_then_rolls_back_last_passing(tmp_path, monkeypatch):
    import keepframe.after_effects.workflow as workflow_module

    root, coordinator = _valid_coordinator(tmp_path)
    client = ApplyClient()
    service = AEWorkflowService(root.parent, lambda: client, executor=ImmediateExecutor())
    original_verify = workflow_module.verify_inspection
    calls = 0

    def fail_candidate(*args, **kwargs):
        nonlocal calls
        calls += 1
        report = original_verify(*args, **kwargs)
        if calls == 1:
            return report
        return report.model_copy(
            update={"passed": False, "violations": ("keep predicate failed",)}
        )

    monkeypatch.setattr(workflow_module, "verify_inspection", fail_candidate)
    _drive_checkpoint(service, coordinator, 1)
    service.advance(coordinator)
    service.advance(coordinator)
    apply = _complete_next(coordinator)
    assert apply.kind == "apply_batch"
    _drive_checkpoint(service, coordinator, 2)
    state = coordinator.state()
    assert state.checkpoints[-1].passed is False
    assert state.selected_checkpoint == 0

    service.advance(coordinator)
    rollback = coordinator.next_command("device-1")
    assert rollback is not None and rollback.kind == "open_project"
    assert rollback.payload["checkpoint_index"] == 0
    coordinator.accept_result(
        "device-1",
        rollback.id,
        sequence=rollback.sequence,
        result={"ok": True},
    )
    service.advance(coordinator)
    assert len(client.calls) == 2


def test_manual_sync_rejects_active_automatic_workflow(tmp_path):
    root, coordinator = _valid_coordinator(tmp_path)
    service = AEWorkflowService(root.parent, lambda: NoopClient(), executor=ImmediateExecutor())


    with pytest.raises(CoordinatorConflict, match="paused"):
        service.manual_sync(coordinator)
    assert coordinator.command_history() == ()


def test_model_future_from_old_revision_is_discarded(tmp_path):
    root, coordinator = _valid_coordinator(tmp_path)
    service = AEWorkflowService(root.parent, lambda: NoopClient(), executor=ImmediateExecutor())
    _drive_checkpoint(service, coordinator, 1)
    state = coordinator.state()
    stale = Future()
    stale.set_result(SimpleNamespace())
    stale_key = service._key(coordinator, state)
    service._futures[stale_key] = stale

    state = coordinator.transition("stop", revision=state.revision)
    state = coordinator.state()
    state = coordinator.transition("continue", revision=state.revision)
    coordinator.transition("device_ready", revision=state.revision, device_id="device-1")
    service._advance_iterating = lambda *_args: None
    service.advance(coordinator)

    assert stale_key not in service._futures
    assert not any(item.kind == "apply_batch" for item in coordinator.command_history())


def test_inspection_chunk_budget_accounts_for_full_layer_inventory(tmp_path):
    service = AEWorkflowService(tmp_path, lambda: NoopClient(), executor=ImmediateExecutor())
    layer_sources = {f"instance-{index}": None for index in range(40)}
    pairs = [(f"source-{index}", index) for index in range(128)]
    chunks = service._bound_observation_chunks(
        [pairs],
        layer_sources=layer_sources,
        capability_manifest={"capabilities": {"font_names": (), "effect_names": (), "property_schemas": {}}},
    )

    assert [pair for chunk in chunks for pair in chunk] == pairs
    assert max(len(chunk) for chunk in chunks) < len(pairs)


def test_inspection_chunk_budget_accepts_maximum_layer_inventory(tmp_path):
    service = AEWorkflowService(tmp_path, lambda: NoopClient(), executor=ImmediateExecutor())
    layer_sources = {f"instance-{index}": None for index in range(1_000)}

    chunks = service._bound_observation_chunks(
        [[("source-0", 0)]],
        layer_sources=layer_sources,
        capability_manifest={"capabilities": {"font_names": (), "effect_names": (), "property_schemas": {}}},
    )

    assert [pair for chunk in chunks for pair in chunk] == [("source-0", 0)]


def test_baseline_reopen_is_scoped_after_latest_manual_attempt(tmp_path):
    service = AEWorkflowService(tmp_path, lambda: NoopClient(), executor=ImmediateExecutor())
    mapping = SimpleNamespace(
        batches=(),
        final_inventory=(),
        composition=SimpleNamespace(model_dump=lambda mode="json", by_alias=False: {}),
    )
    state = SimpleNamespace(
        baseline_complete=False,
        checkpoints=(SimpleNamespace(index=0),),
        open_checkpoint=1,
    )
    old_reopen = SimpleNamespace(
        kind="open_project",
        status="completed",
        result={"ok": True},
        sequence=1,
        payload={"workflow": {"stage": "baseline_reopen", "checkpoint_index": 0}},
    )
    manual_attempt = SimpleNamespace(
        kind="sync_manual",
        status="completed",
        result={"ok": True, "checkpoint": {"index": 1, "passed": True}},
        sequence=2,
        payload={"workflow": {"stage": "manual_sync", "checkpoint_index": 1}},
    )
    service._authoritative = lambda _coordinator: (
        None,
        mapping,
        {},
        SimpleNamespace(capability_hash="a" * 64),
    )
    service._enqueue = lambda _coordinator, _state, kind, payload, **_kwargs: (kind, payload)

    command, _payload = service._advance_baseline(
        SimpleNamespace(plan=SimpleNamespace(capability_hash="a" * 64)),
        state,
        (old_reopen, manual_attempt),
    )

    assert command == "open_project"


def test_impossible_inspection_budget_pauses_with_visible_reason(tmp_path):
    root, coordinator = _valid_coordinator(tmp_path)
    service = AEWorkflowService(root.parent, lambda: NoopClient(), executor=ImmediateExecutor())
    service._bound_observation_chunks = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AEInspectionBudgetError("inspection response cannot fit the bounded protocol")
    )

    service.advance(coordinator)
    _complete_next(coordinator)
    service.advance(coordinator)

    assert coordinator.state().status == "paused:inspection_too_large"


def test_apply_batch_capabilities_are_compacted_to_referenced_entries(tmp_path):
    service = AEWorkflowService(tmp_path, lambda: NoopClient(), executor=ImmediateExecutor())
    batch = BaselineBatch(
        index=0,
        capability_digest="a" * 64,
        scene_frame_count=3,
        duration=0.3,
        operations=(
            {
                "kind": "add_layer",
                "layer_instance_id": "agent-layer",
                "layer_type": "null",
                "name": "Agent",
            },
        ),
        layer_sources={},
        layer_count=0,
    )
    approved = {
        "digest": "a" * 64,
        "fonts": [f"unused-font-{index}" + "x" * 4096 for index in range(100)],
        "effects": [f"unused-effect-{index}" + "x" * 4096 for index in range(100)],
        "properties": {f"unused-property-{index}": "number" for index in range(1000)},
    }

    compact = service._approved_capabilities_for_batch(approved, batch)

    assert compact == {"digest": "a" * 64, "fonts": [], "effects": [], "properties": {}}


def test_capability_compaction_includes_required_effect_for_color_property(tmp_path):
    service = AEWorkflowService(tmp_path, lambda: NoopClient(), executor=ImmediateExecutor())
    batch = BaselineBatch(
        index=0,
        capability_digest="a" * 64,
        scene_frame_count=3,
        duration=0.3,
        operations=(
            SetColorOperation(layer_instance_id="layer-1", color=[1.0, 0.0, 0.0]),
        ),
        layer_sources={"layer-1": None},
        layer_count=1,
    )
    approved = {
        "digest": "a" * 64,
        "effects": ["ADBE Fill", "ADBE Gaussian Blur 2"],
        "properties": {"ADBE Fill Color": "color"},
    }

    compact = service._approved_capabilities_for_batch(approved, batch)

    assert compact["effects"] == ["ADBE Fill"]
    assert compact["properties"] == {"ADBE Fill Color": "color"}


def test_manual_edit_reopens_partial_baseline_before_continue(tmp_path):
    root, coordinator = _valid_coordinator(tmp_path)
    session = coordinator.state()
    partial = _failed_checkpoint(coordinator, 0, provenance="baseline")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=partial.model_dump(mode="json"),
    )
    assert session.status == "paused:verification_failed"
    assert session.baseline_complete is False

    service = AEWorkflowService(root.parent, lambda: NoopClient(), executor=ImmediateExecutor())
    session = coordinator.transition("continue", revision=session.revision)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition("pause_error", revision=session.revision, reason="user")
    session = coordinator.transition("begin_manual", revision=session.revision)
    service.manual_sync(coordinator)
    reopen = coordinator.command_history()[-1]
    assert reopen.kind == "open_project"
    assert reopen.payload["checkpoint_index"] == 0
    _complete_next(coordinator)
    service.advance(coordinator)
    _complete_next(coordinator)
    _drive_checkpoint(service, coordinator, 2)

    state = coordinator.state()
    assert state.checkpoints[-1].index == 1
    assert state.checkpoints[-1].provenance == "manual"
    state = coordinator.transition("continue", revision=state.revision)
    state = coordinator.transition("device_ready", revision=state.revision, device_id="device-1")
    service.advance(coordinator)
    baseline_reopen = coordinator.command_history()[-1]
    assert baseline_reopen.kind == "open_project"
    assert baseline_reopen.payload["checkpoint_index"] == 0



def test_replacement_device_reopens_selected_checkpoint_before_iterating(tmp_path):
    root, _plan, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    baseline = _committed_checkpoint(coordinator, 0, provenance="baseline")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=baseline.model_dump(mode="json"),
    )
    session = coordinator.detach_device("device-1", reason="replacement")
    assert session.device_id is None
    assert session.open_checkpoint is None
    session = coordinator.transition("continue", revision=session.revision)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-2")

    service = _workflow(root)
    command = service.advance(coordinator)

    assert command is not None
    assert command.kind == "open_project"
    assert command.payload["checkpoint_index"] == 0
    assert command.payload["checkpoint_artifact"]["id"] == baseline.aep_artifact_id
    assert command.payload["workflow"]["device_id"] == "device-2"


def test_restart_resumes_committed_manual_prepare_candidate(tmp_path):
    from keepframe.after_effects.coordinator import AECoordinator

    root, plan, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    baseline = _committed_checkpoint(coordinator, 0, provenance="baseline")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=baseline.model_dump(mode="json"),
    )
    session = coordinator.transition("no_progress", revision=session.revision)
    session = coordinator.transition("begin_manual", revision=session.revision)
    data = _artifact_payload("aep")
    reservation = coordinator.reserve_artifact("aep", len(data))
    preparation = coordinator.enqueue_command(
        "sync_manual",
        {
            "prepare_manual": True,
            "workflow": {"candidate_checkpoint_index": 1},
            "artifacts": [
                {
                    "reservation_id": reservation.id,
                    "kind": "aep",
                    "filename": "checkpoint-1.aep",
                    "directory": "checkpoints",
                }
            ],
        },
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.publish_artifact(
        "device-1",
        reservation.id,
        io.BytesIO(data),
        content_length=len(data),
        require_live_command=True,
    )
    coordinator.accept_result(
        "device-1",
        preparation.id,
        sequence=preparation.sequence,
        result={"ok": True},
    )

    recovered = AECoordinator(root, plan.id)
    state = recovered.state()
    assert state.status == "paused:server_restart"
    state = recovered.transition("begin_manual", revision=state.revision)
    assert state.manual_attempt_id == preparation.id
    assert state.manual_epoch == session.manual_epoch

    service = _workflow(root)
    command = service.advance(recovered)
    assert command is not None
    assert command.kind == "open_project"
    assert command.payload["checkpoint_index"] == 1
    assert command.payload["checkpoint_artifact"]["id"] == reservation.id

def test_begin_manual_poll_stays_idle_until_explicit_sync(tmp_path):
    root, coordinator = _valid_coordinator(tmp_path)
    service = AEWorkflowService(root.parent, lambda: NoopClient(), executor=ImmediateExecutor())
    _drive_checkpoint(service, coordinator, 1)
    state = coordinator.transition("no_progress", revision=coordinator.state().revision)
    coordinator.transition("begin_manual", revision=state.revision)

    service.advance(coordinator)
    reopen = coordinator.next_command("device-1")
    assert reopen is not None and reopen.kind == "open_project"
    _complete_next(coordinator, reopen)
    service.advance(coordinator)

    assert not any(
        command.kind == "sync_manual" and command.payload.get("prepare_manual") is True
        for command in coordinator.command_history()
    )


def test_manual_sync_reopens_after_expired_paused_lease(tmp_path, monkeypatch):
    root, coordinator = _valid_coordinator(tmp_path)
    service = AEWorkflowService(root.parent, lambda: NoopClient(), executor=ImmediateExecutor())
    _drive_checkpoint(service, coordinator, 1)
    state = coordinator.transition("no_progress", revision=coordinator.state().revision)
    state = coordinator.transition("begin_manual", revision=state.revision)
    monkeypatch.setattr("keepframe.after_effects.coordinator._now", lambda: 100.0)
    preparation = coordinator.enqueue_command(
        "sync_manual",
        {"prepare_manual": True, "workflow": {"stage": "manual_prepare"}},
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    state = coordinator.transition("timeout", revision=state.revision)
    monkeypatch.setattr("keepframe.after_effects.coordinator._now", lambda: 3701.0)

    service.manual_sync(coordinator)

    assert coordinator.get_command(preparation.id).status == "revoked"
