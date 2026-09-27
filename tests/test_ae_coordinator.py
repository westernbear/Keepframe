import io
import json
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from keepframe.after_effects.coordinator import AECoordinator, CoordinatorConflict
from keepframe.after_effects.models import AECheckpoint
from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.render.plan import RenderMode, approve_render_plan, create_render_plan


def _coordinator(tmp_path: Path, *, mode: RenderMode = "preview"):
    root = tmp_path / "p1"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=12, with_text=False, frames=12)
    scene = scene.model_copy(update={"id": "s1"})
    init_project(root, {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    (root / "meta.json").write_text(
        json.dumps({"id": "p1", "status": "approved", "version": "v1", "scene": "s1"}),
        encoding="utf-8",
    )
    plan = create_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        backend="after_effects",
        mode=mode,
        capability_hash="a" * 64,
        permitted_operations=({"kind": "create_project"}, {"kind": "apply_batch"}),
    )
    approval = approve_render_plan(root, plan.id, digest=plan.digest, revision=0)
    coordinator = AECoordinator(root, plan.id)
    session = coordinator.start(approval.execution_id)
    return root, plan, coordinator, session




def _artifact_payload(kind: str) -> bytes:
    if kind == "png":
        def chunk(name: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(data, zlib.crc32(name)))

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff"))
            + chunk(b"IEND", b"")
        )
    if kind == "mp4":
        return struct.pack(">I4s", 16, b"ftyp") + b"isom\x00\x00\x00\x00" + struct.pack(">I4s", 8, b"mdat")
    if kind == "aep":
        return b"RIFX" + struct.pack(">I", 4) + b"Egg!"
    if kind == "zip":
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("project.aep", _artifact_payload("aep"))
        return stream.getvalue()
    raise AssertionError(kind)


def _publish(coordinator: AECoordinator, kind: str) -> str:
    payload = _artifact_payload(kind)
    reservation = coordinator.reserve_artifact(kind, max_length=len(payload))
    return coordinator.publish_artifact(
        reservation.id,
        io.BytesIO(payload),
        content_length=len(payload),
    ).id


def _committed_checkpoint(
    coordinator: AECoordinator,
    index: int,
    *,
    provenance: str = "agent",
) -> AECheckpoint:
    return AECheckpoint(
        index=index,
        provenance=provenance,
        passed=True,
        aep_artifact_id=_publish(coordinator, "aep"),
        preview_artifact_id=_publish(coordinator, "mp4"),
        frame_artifact_ids=(_publish(coordinator, "png"),),
    )


def test_transition_table_enforces_revisions_and_pause_flow(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    assert session.status == "waiting_for_connector"
    with pytest.raises(CoordinatorConflict, match="revision"):
        coordinator.transition("device_ready", revision=9, device_id="device-1")

    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    assert session.status == "baseline"
    with pytest.raises(CoordinatorConflict, match="checkpoint 0"):
        coordinator.transition("batch_complete", revision=session.revision)
    incomplete = AECheckpoint(
        index=0,
        provenance="baseline",
        passed=True,
        aep_artifact_id=_publish(coordinator, "aep"),
        preview_artifact_id=_publish(coordinator, "mp4"),
        frame_artifact_ids=(),
    )
    with pytest.raises(CoordinatorConflict, match="PNG frame"):
        coordinator.transition(
            "baseline_complete",
            revision=session.revision,
            checkpoint=incomplete.model_dump(mode="json"),
        )
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    assert session.status == "iterating"
    session = coordinator.transition("stop", revision=session.revision)
    assert session.status == "pause_requested"
    session = coordinator.state()
    assert session.status == "paused:user"
    assert [checkpoint.index for checkpoint in session.checkpoints] == [0]

    session = coordinator.transition("begin_manual", revision=session.revision)
    assert session.status == "manual_edit"
    checkpoint = _committed_checkpoint(coordinator, 1, provenance="manual")
    command = coordinator.enqueue_command(
        "sync_manual",
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    leased = coordinator.next_command("device-1")
    assert leased is not None and leased.id == command.id
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result={"ok": True, "checkpoint": checkpoint.model_dump(mode="json")},
    )
    session = coordinator.state()
    assert session.status == "paused:manual_synced"
    session = coordinator.transition("continue", revision=session.revision)
    assert session.status == "waiting_for_connector"
    with pytest.raises(CoordinatorConflict, match="bound"):
        coordinator.transition("device_ready", revision=session.revision, device_id="device-2")


def test_restart_recovers_every_active_state_to_paused(tmp_path):
    for offset, target in enumerate(("baseline", "iterating", "pause_requested", "manual_edit", "finalizing")):
        _, _, coordinator, session = _coordinator(tmp_path / str(offset), mode="final")
        session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
        if target != "baseline":
            session = coordinator.transition(
                "baseline_complete",
                revision=session.revision,
                checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
            )
        if target == "pause_requested":
            session = coordinator.transition("stop", revision=session.revision)
        elif target in {"manual_edit", "finalizing"}:
            session = coordinator.transition("no_progress", revision=session.revision)
            if target == "manual_edit":
                session = coordinator.transition("begin_manual", revision=session.revision)
            else:
                session = coordinator.select_checkpoint(0, revision=session.revision)
                session = coordinator.transition("finalize", revision=session.revision)
        assert session.status == target
        recovered = AECoordinator(coordinator.root, coordinator.plan_id).state()
        assert recovered.status == "paused:server_restart"


def test_command_leases_redeliver_without_reapplying_and_results_are_idempotent(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    command = coordinator.enqueue_command(
        "heartbeat",
        {"probe": True},
        expected_state="baseline",
        expected_checkpoint=None,
        lease_seconds=10,
    )

    leased = coordinator.next_command("device-1", now=100)
    assert leased is not None and leased.id == command.id and leased.sequence == 1
    assert coordinator.next_command("device-1", now=105) is None
    redelivered = coordinator.next_command("device-1", now=111)
    assert redelivered is not None and redelivered.id == command.id

    result = coordinator.accept_result(
        "device-1", command.id, sequence=1, result={"ok": True, "checkpoint": 0}
    )
    retry = coordinator.accept_result(
        "device-1", command.id, sequence=1, result={"ok": True, "checkpoint": 0}
    )
    assert retry == result
    revision = coordinator.state().revision
    assert coordinator.state().revision == revision
    state = coordinator.state()
    assert state.status == "baseline"
    assert state.applied_command_sequence == 1
    with pytest.raises(CoordinatorConflict, match="checkpoint"):
        coordinator.enqueue_command(
            "heartbeat",
            expected_state="baseline",
            expected_checkpoint=0,
        )
    with pytest.raises(CoordinatorConflict, match="conflicting"):
        coordinator.accept_result("device-1", command.id, sequence=1, result={"ok": False})


def test_disconnect_settles_after_active_command_lease_expires(tmp_path, monkeypatch):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    monkeypatch.setattr("keepframe.after_effects.coordinator._now", lambda: 100.0)
    command = coordinator.enqueue_command(
        "heartbeat",
        expected_state="baseline",
        expected_checkpoint=None,
        lease_seconds=10,
    )
    assert coordinator.next_command("device-1") is not None
    session = coordinator.transition("disconnect", revision=session.revision)
    assert session.status == "pause_requested"

    monkeypatch.setattr("keepframe.after_effects.coordinator._now", lambda: 111.0)
    session = coordinator.state()
    assert session.status == "paused:disconnect"
    assert coordinator.next_command("device-1") is None
    accepted = coordinator.accept_result(
        "device-1", command.id, sequence=command.sequence, result={"ok": True}
    )
    assert accepted.command_id == command.id
    assert coordinator.state().status == "paused:disconnect"


def test_stop_waits_for_active_batch_checkpoint(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    command = coordinator.enqueue_command(
        "apply_batch",
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    session = coordinator.transition("stop", revision=session.revision)
    assert session.status == "pause_requested"

    checkpoint = _committed_checkpoint(coordinator, 1)
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result={"ok": True, "checkpoint": checkpoint.model_dump(mode="json")},
    )
    session = coordinator.state()
    assert session.status == "paused:user"
    assert [item.index for item in session.checkpoints] == [0, 1]


def test_late_baseline_checkpoint_must_still_be_zero(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    command = coordinator.enqueue_command(
        "save_checkpoint",
        expected_state="baseline",
        expected_checkpoint=None,
    )
    assert coordinator.next_command("device-1") is not None
    session = coordinator.transition("disconnect", revision=session.revision)
    assert session.status == "pause_requested"
    checkpoint = _committed_checkpoint(coordinator, 1, provenance="baseline")

    with pytest.raises(CoordinatorConflict, match="checkpoint 0"):
        coordinator.accept_result(
            "device-1",
            command.id,
            sequence=command.sequence,
            result={"ok": True, "checkpoint": checkpoint.model_dump(mode="json")},
        )


def test_command_kind_state_matrix_rejects_unsafe_work(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    with pytest.raises(CoordinatorConflict, match="state"):
        coordinator.enqueue_command(
            "package_project",
            expected_state="baseline",
            expected_checkpoint=None,
        )


def test_event_journal_does_not_follow_symlinks(tmp_path):
    root, plan, coordinator, session = _coordinator(tmp_path)
    outside = tmp_path / "outside.log"
    outside.write_text("unchanged", encoding="utf-8")
    events = root / "renders" / plan.id / "ae" / "events.jsonl"
    try:
        events.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlink unavailable: {exc}")

    with pytest.raises(CoordinatorConflict):
        coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    assert outside.read_text(encoding="utf-8") == "unchanged"
    assert coordinator.state().status == "waiting_for_connector"



def test_inflight_manual_result_survives_disconnect(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    session = coordinator.transition("no_progress", revision=session.revision)
    session = coordinator.transition("begin_manual", revision=session.revision)
    command = coordinator.enqueue_command(
        "sync_manual",
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    session = coordinator.transition("disconnect", revision=session.revision)
    assert session.status == "pause_requested"

    checkpoint = _committed_checkpoint(coordinator, 1, provenance="manual")
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result={"ok": True, "checkpoint": checkpoint.model_dump(mode="json")},
    )
    session = coordinator.state()
    assert session.status == "paused:disconnect"
    assert [item.index for item in session.checkpoints] == [0, 1]

def test_no_progress_disconnect_and_finalization_edges(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path, mode="final")
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    session = coordinator.transition("no_progress", revision=session.revision)
    assert session.status == "paused:no_progress"
    session = coordinator.select_checkpoint(0, revision=session.revision)
    session = coordinator.transition("finalize", revision=session.revision)
    assert session.status == "finalizing"
    with pytest.raises(CoordinatorConflict, match="package_project"):
        coordinator.transition("final_complete", revision=session.revision)

    mp4_id = _publish(coordinator, "mp4")
    render = coordinator.enqueue_command(
        "render_final",
        expected_state="finalizing",
        expected_checkpoint=0,
    )
    leased = coordinator.next_command("device-1")
    assert leased is not None and leased.id == render.id
    coordinator.accept_result(
        "device-1",
        render.id,
        sequence=render.sequence,
        result={"ok": True, "artifacts": [{"id": mp4_id, "kind": "mp4"}]},
    )

    package = coordinator.enqueue_command(
        "package_project",
        expected_state="finalizing",
        expected_checkpoint=0,
    )
    leased = coordinator.next_command("device-1")
    assert leased is not None and leased.id == package.id
    coordinator.accept_result(
        "device-1",
        package.id,
        sequence=package.sequence,
        result={
            "ok": True,
            "artifacts": [
                {"id": _publish(coordinator, "aep"), "kind": "aep"},
                {"id": mp4_id, "kind": "mp4"},
                {"id": _publish(coordinator, "zip"), "kind": "zip"},
            ],
        },
    )
    session = coordinator.state()
    assert session.status == "done"
    with pytest.raises(CoordinatorConflict, match="terminal"):
        coordinator.transition("continue", revision=session.revision)


def test_finalization_revalidates_authoritative_approval(tmp_path):
    root, _, coordinator, session = _coordinator(tmp_path, mode="final")
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    session = coordinator.transition("no_progress", revision=session.revision)
    session = coordinator.select_checkpoint(0, revision=session.revision)
    meta_path = root / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = "review"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(CoordinatorConflict, match="approved"):
        coordinator.transition("finalize", revision=session.revision)
    assert coordinator.state().status == "paused:no_progress"


def test_artifact_reservations_stream_validate_and_publish_once(tmp_path):
    root, plan, coordinator, _ = _coordinator(tmp_path)
    payload = _artifact_payload("png")
    reservation = coordinator.reserve_artifact("png", max_length=len(payload))

    artifact = coordinator.publish_artifact(
        reservation.id,
        io.BytesIO(payload),
        content_length=len(payload),
    )

    path = root / "renders" / plan.id / "ae" / "artifacts" / reservation.id
    assert artifact.sha256
    assert path.read_bytes() == payload
    with pytest.raises(CoordinatorConflict, match="committed"):
        coordinator.publish_artifact(reservation.id, io.BytesIO(payload), content_length=len(payload))

    too_small = coordinator.reserve_artifact("png", max_length=4)
    with pytest.raises(CoordinatorConflict, match="length"):
        coordinator.publish_artifact(too_small.id, io.BytesIO(payload), content_length=len(payload))

    invalid = coordinator.reserve_artifact("png", max_length=32)
    fake_png = b"\x89PNG\r\n\x1a\nframe"
    with pytest.raises(CoordinatorConflict, match="container|PNG"):
        coordinator.publish_artifact(invalid.id, io.BytesIO(fake_png), content_length=len(fake_png))

    occupied = coordinator.reserve_artifact("png", max_length=len(payload))
    occupied_path = root / "renders" / plan.id / "ae" / "artifacts" / occupied.id
    occupied_path.write_bytes(b"sentinel")
    with pytest.raises(CoordinatorConflict):
        coordinator.publish_artifact(occupied.id, io.BytesIO(payload), content_length=len(payload))
    assert occupied_path.read_bytes() == b"sentinel"
