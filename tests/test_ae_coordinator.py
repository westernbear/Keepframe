import base64
import hashlib
import io
import json
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from keepframe.after_effects.coordinator import AECoordinator, CoordinatorConflict
from keepframe.after_effects.models import (
    AECheckpoint,
    canonical_json,
    compute_finalization_key,
    json_digest,
)
from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.after_effects.planning import current_operation_manifest
from keepframe.render.plan import RenderMode, approve_render_plan, create_render_plan

_AE_CAPABILITY_MANIFEST = {
    "version": "24.1.0",
    "major": 24,
    "host": "after-effects",
    "ready": True,
    "capabilities": {
        "font_names": ["Arial"],
        "fonts": [
            {
                "match_name": "Arial",
                "family": "Arial",
                "style": "Regular",
                "version": "1",
                "version_or_hash": "1",
                "sha256": None,
            }
        ],
        "effect_names": ["ADBE Fill"],
        "effects": [
            {
                "match_name": "ADBE Fill",
                "display_name": "Fill",
                "version": "1",
                "version_or_hash": "1",
                "properties": {"ADBE Fill-0002": "color"},
            }
        ],
        "property_schemas": {"ADBE Opacity": "number"},
        "properties": {"ADBE Opacity": "number"},
        "plugin_versions": {"ADBE Fill": "1"},
    },
}
_AE_CAPABILITY_HASH = hashlib.sha256(
    json.dumps(
        _AE_CAPABILITY_MANIFEST,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()
_FINAL_RENDER_FIELDS = {
    "width": 640,
    "height": 360,
    "fps": 30.0,
    "frame_count": 12,
}


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
        mode="preview" if mode == "final" else mode,
        permitted_operations=current_operation_manifest(),
        capability_hash=_AE_CAPABILITY_HASH,
        capability_manifest=_AE_CAPABILITY_MANIFEST,
    )
    approval = approve_render_plan(root, plan.id, digest=plan.digest, revision=0)
    coordinator = AECoordinator(root, plan.id)
    session = coordinator.start(approval.execution_id)
    return root, plan, coordinator, session

def _final_successor(
    root: Path,
    preview_plan,
    coordinator: AECoordinator,
    checkpoint: int = 0,
):
    record = coordinator.checkpoint(checkpoint)
    checkpoint_digest = json_digest(record.model_dump(mode="json"))
    final = create_render_plan(
        root,
        project_id=preview_plan.project_id,
        scene_id=preview_plan.scene_id,
        version_id=preview_plan.version_id,
        backend="after_effects",
        mode="final",
        direction=preview_plan.direction,
        locked_targets=preview_plan.locked_targets,
        permitted_operations=preview_plan.permitted_operations,
        effect_schemas=preview_plan.effect_schemas,
        capability_hash=preview_plan.capability_hash,
        capability_manifest=preview_plan.capability_manifest,
        substitutions=preview_plan.substitutions,
        substitutions_acknowledged=preview_plan.substitutions_acknowledged,
        predecessor_id=preview_plan.id,
        predecessor_digest=preview_plan.digest,
        predecessor_checkpoint=checkpoint,
        predecessor_checkpoint_digest=checkpoint_digest,
        _predecessor_plan=preview_plan,
    )
    approval = approve_render_plan(root, final.id, digest=final.digest, revision=0)
    return final, approval.execution_id

def _finalize(root: Path, preview_plan, coordinator: AECoordinator, session):
    final, execution_id = _final_successor(root, preview_plan, coordinator)
    updated = coordinator.transition(
        "finalize",
        revision=session.revision,
        final_plan_id=final.id,
        execution_id=execution_id,
    )
    return updated, final, execution_id




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
        return base64.b64decode(
            "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAAIZnJlZQAAA/VtZGF0AAACrgYF//+q3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NSByMzIyMiBiMzU2MDVhIC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyNSAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTEgcmVmPTMgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4MzoweDExMyBtZT1oZXggc3VibWU9NyBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0xIG1lX3JhbmdlPTE2IGNocm9tYV9tZT0xIHRyZWxsaXM9MSA4eDhkY3Q9MSBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0tMiB0aHJlYWRzPTYgbG9va2FoZWFkX3RocmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0zIGJfcHlyYW1pZD0yIGJfYWRhcHQ9MSBiX2JpYXM9MCBkaXJlY3Q9MSB3ZWlnaHRiPTEgb3Blbl9nb3A9MCB3ZWlnaHRwPTIga2V5aW50PTI1MCBrZXlpbnRfbWluPTI1IHNjZW5lY3V0PTQwIGludHJhX3JlZnJlc2g9MCByY19sb29rYWhlYWQ9NDAgcmM9Y3JmIG1idHJlZT0xIGNyZj0yMy4wIHFjb21wPTAuNjAgcXBtaW49MCBxcG1heD02OSBxcHN0ZXA9NCBpcF9yYXRpbz0xLjQwIGFxPTE6MS4wMACAAAAAWmWIhAA3//728P4FNlYEUJcRzeidMx+/Fbi6NDe9zgAAAwAAAwAAAwG5pYX/dnfziCAAAAMAdsAVAIAEbDZC5DVDwFDHWKcQAgQAAAMAAAMAAAMAAAMAAAMC3wAAABBBmiRsQz/+nhAAAAMAAAb0AAAADkGeQniFfwAAAwAAAwF3AAAADgGeYXRCfwAAAwAAAwHdAAAADgGeY2pCfwAAAwAAAwHdAAAAFkGaaEmoQWiZTAhf//6MsAAAAwAABv0AAAAQQZ6GRREsK/8AAAMAAAMBdwAAAA4BnqV0Qn8AAAMAAAMB3QAAAA4BnqdqQn8AAAMAAAMB3QAAABdBmqtJqEFsmUwIT//98QAAAwAAAwBBwAAAABBBnslFFSwr/wAAAwAAAwF3AAAADgGe6mpCfwAAAwAAAwHdAAADw21vb3YAAABsbXZoZAAAAAAAAAAAAAAAAAAAA+gAAAGQAAEAAAEAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAALudHJhawAAAFx0a2hkAAAAAwAAAAAAAAAAAAAAAQAAAAAAAAGQAAAAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAKAAAABaAAAAAAAJGVkdHMAAAAcZWxzdAAAAAAAAAABAAABkAAABAAAAQAAAAACZm1kaWEAAAAgbWRoZAAAAAAAAAAAAAAAAAAAPAAAABgAVcQAAAAAAC1oZGxyAAAAAAAAAAB2aWRlAAAAAAAAAAAAAAAAVmlkZW9IYW5kbGVyAAAAAhFtaW5mAAAAFHZtaGQAAAABAAAAAAAAAAAAAAAkZGluZgAAABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAAHRc3RibAAAAMFzdHNkAAAAAAAAAAEAAACxYXZjMQAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAKAAWgASAAAAEgAAAAAAAAAARVMYXZjNjIuMTEuMTAwIGxpYngyNjQAAAAAAAAAAAAAABj//wAAADdhdmNDAWQAHv/hABpnZAAerNlAoC/5cBEAAAMAAQAAAwA8DxYtlgEABmjr48siwP34+AAAAAAQcGFzcAAAAAEAAAABAAAAFGJ0cnQAAAAAAABOhAAAAAAAAAAYc3R0cwAAAAAAAAABAAAADAAAAgAAAAAUc3RzcwAAAAAAAAABAAAAAQAAAGhjdHRzAAAAAAAAAAsAAAABAAAEAAAAAAEAAAoAAAAAAQAABAAAAAABAAAAAAAAAAEAAAIAAAAAAQAACgAAAAABAAAEAAAAAAEAAAAAAAAAAQAAAgAAAAABAAAIAAAAAAIAAAIAAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAAMAAAAAQAAAERzdHN6AAAAAAAAAAAAAAAMAAADEAAAABQAAAASAAAAEgAAABIAAAAaAAAAFAAAABIAAAASAAAAGwAAABQAAAASAAAAFHN0Y28AAAAAAAAAAQAAADAAAABhdWR0YQAAAFltZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAACxpbHN0AAAAJKl0b28AAAAcZGF0YQAAAAEAAAAATGF2ZjYyLjMuMTAw"
        )
    if kind == "aep":
        return (
            b"RIFX"
            + struct.pack(">I", 28)
            + b"Egg!LIST"
            + struct.pack(">I", 16)
            + b"Foldtdsn"
            + struct.pack(">I", 4)
            + b"\x00\x00\x00\x01"
        )
    if kind == "zip":
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("project.aep", _artifact_payload("aep"))
        return stream.getvalue()
    raise AssertionError(kind)


def _publish(coordinator: AECoordinator, kind: str) -> str:
    payload = _artifact_payload(kind)
    reservation = coordinator.reserve_artifact(kind, max_length=len(payload))
    device_id = coordinator.state().device_id
    assert device_id is not None
    return coordinator.publish_artifact(
        device_id,
        reservation.id,
        io.BytesIO(payload),
        content_length=len(payload),
    ).id

def _publish_reserved(coordinator: AECoordinator, reservation_id: str, payload: bytes) -> str:
    device_id = coordinator.state().device_id
    assert device_id is not None
    return coordinator.publish_artifact(
        device_id,
        reservation_id,
        io.BytesIO(payload),
        content_length=len(payload),
    ).id


def _final_package(
    coordinator: AECoordinator,
    final,
    finalization_key: str,
    checkpoint: int = 0,
) -> tuple[dict[str, object], dict[str, object]]:
    aep_bytes = _artifact_payload("aep")
    aep = coordinator.reserve_artifact("aep", max_length=len(aep_bytes))
    _publish_reserved(coordinator, aep.id, aep_bytes)
    assets = [
        {
            "asset_id": asset.id,
            "id": asset.id,
            "sha256": asset.sha256,
            "length": asset.length,
            "media_kind": asset.media_kind,
            "role": asset.role,
            "content_filename": asset.id,
        }
        for asset in final.assets
    ]
    package_media = [
        {
            "asset_id": asset.id,
            "filename": asset.id,
            "sha256": asset.sha256,
            "length": asset.length,
            "media_kind": asset.media_kind,
        }
        for asset in final.assets
    ]
    approved = dict(final.capability_manifest or {})
    catalog = approved["capabilities"]
    fonts = list(catalog["fonts"])
    effects = list(catalog["effects"])
    plugins = dict(catalog["plugin_versions"])
    ae = {
        "version": approved["version"],
        "major": approved["major"],
        "host": approved["host"],
        "capability_hash": final.capability_hash,
    }
    dependencies = {
        "capability_manifest": approved,
        "substitutions": list(final.substitutions),
        "missing_nonportable_dependencies": [],
    }
    media_manifest = sorted(
        (
            {
                **record,
                "role": next(
                    asset.role
                    for asset in final.assets
                    if asset.id == record["asset_id"]
                ),
            }
            for record in package_media
        ),
        key=lambda item: (str(item["filename"]), str(item["asset_id"])),
    )
    manifest = {
        "schema_version": "keepframe.dependencies/1",
        "after_effects": ae,
        "ae": ae,
        "os": {
            "system": "Windows",
            "release": "11",
            "version": "test",
            "machine": "AMD64",
        },
        "capability_manifest": {
            "approved": approved,
            "live": {
                "capability_hash": final.capability_hash,
                "fonts": fonts,
                "effects": effects,
                "plugins": plugins,
            },
        },
        "fonts": fonts,
        "effects": effects,
        "plugins": plugins,
        "media": media_manifest,
        "substitutions": list(final.substitutions),
        "selected_checkpoint": checkpoint,
        "plan_digest": final.digest,
        "final_plan_digest": final.digest,
        "finalization_key": finalization_key,
        "missing_dependencies": [],
        "nonportable_dependencies": [],
        "missing_nonportable_dependencies": [],
    }
    archive_stream = io.BytesIO()
    with zipfile.ZipFile(
        archive_stream,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("project.aep", aep_bytes)
        archive.writestr("dependencies.json", canonical_json(manifest))
        for asset in final.assets:
            archive.writestr(
                f"collected_media/{asset.id}",
                coordinator.root.joinpath(*asset.project_path.split("/")).read_bytes(),
            )
    archive_bytes = archive_stream.getvalue()
    package_zip = coordinator.reserve_artifact("zip", max_length=len(archive_bytes))
    _publish_reserved(coordinator, package_zip.id, archive_bytes)
    payload: dict[str, object] = {
        "selected_checkpoint": checkpoint,
        "final_plan_digest": final.digest,
        "finalization_key": finalization_key,
        "assets": assets,
        "package_media": package_media,
        "dependencies": dependencies,
        "artifacts": [
            {
                "reservation_id": aep.id,
                "kind": "aep",
                "filename": "project.aep",
                "directory": "package",
            },
            {
                "reservation_id": package_zip.id,
                "kind": "zip",
                "filename": "project.zip",
                "directory": "checkpoints",
            },
        ],
    }
    result: dict[str, object] = {
        "ok": True,
        "artifacts": [
            {"id": aep.id, "kind": "aep"},
            {"id": package_zip.id, "kind": "zip"},
        ],
    }
    return payload, result


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



def _checkpoint_artifacts(checkpoint: AECheckpoint) -> tuple[str, ...]:
    return (
        checkpoint.aep_artifact_id,
        checkpoint.preview_artifact_id,
        *checkpoint.frame_artifact_ids,
    )


def _checkpoint_payload(checkpoint: AECheckpoint) -> dict[str, object]:
    return {
        "checkpoint_context": checkpoint.model_dump(mode="json"),
        "artifacts": [
            {
                "reservation_id": artifact_id,
                "kind": (
                    "aep"
                    if index == 0
                    else "mp4"
                    if index == 1
                    else "png"
                ),
                "filename": (
                    "checkpoint.aep"
                    if index == 0
                    else "preview.mp4"
                    if index == 1
                    else f"frame-{index - 2}.png"
                ),
                "directory": "checkpoints",
            }
            for index, artifact_id in enumerate(_checkpoint_artifacts(checkpoint))
        ],
    }


def _checkpoint_result(checkpoint: AECheckpoint) -> dict[str, object]:
    return {"ok": True, "artifacts": list(_checkpoint_artifacts(checkpoint))}


def _enqueue_checkpoint(
    coordinator: AECoordinator,
    kind: str,
    checkpoint: AECheckpoint,
    *,
    expected_state: str,
    expected_checkpoint: int | None,
):
    return coordinator.enqueue_command(
        kind,
        _checkpoint_payload(checkpoint),
        expected_state=expected_state,
        expected_checkpoint=expected_checkpoint,
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
    command = _enqueue_checkpoint(
        coordinator,
        "sync_manual",
        checkpoint,
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    leased = coordinator.next_command("device-1")
    assert leased is not None and leased.id == command.id
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result=_checkpoint_result(checkpoint),
    )
    session = coordinator.state()
    assert session.status == "paused:manual_synced"
    session = coordinator.transition("continue", revision=session.revision)
    assert session.status == "waiting_for_connector"
    with pytest.raises(CoordinatorConflict, match="bound"):
        coordinator.transition("device_ready", revision=session.revision, device_id="device-2")



def test_full_baseline_replaces_partial_checkpoint_and_resets_lineage(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    partial = _failed_checkpoint(coordinator, 0, provenance="baseline")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=partial.model_dump(mode="json"),
    )
    session = coordinator.transition("begin_manual", revision=session.revision)
    manual = _committed_checkpoint(coordinator, 1, provenance="manual")
    command = _enqueue_checkpoint(
        coordinator,
        "sync_manual",
        manual,
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    coordinator.next_command("device-1")
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result=_checkpoint_result(manual),
    )
    session = coordinator.transition("continue", revision=coordinator.state().revision)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    full = _committed_checkpoint(coordinator, 0, provenance="baseline")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=full.model_dump(mode="json"),
    )

    assert [checkpoint.index for checkpoint in session.checkpoints] == [0, 1]
    assert session.checkpoints[0] == full
    assert session.checkpoints[1] == manual.model_copy(update={"lineage_valid": False})
    assert session.open_checkpoint == 0
    assert session.selected_checkpoint == 0
    assert session.baseline_complete is True



def test_incomplete_baseline_blocks_manual_selection_and_finalization(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path, mode="final")
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    partial = _failed_checkpoint(coordinator, 0, provenance="baseline")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=partial.model_dump(mode="json"),
    )
    session = coordinator.transition("pause_error", revision=session.revision, reason="user")
    session = coordinator.transition("begin_manual", revision=session.revision)
    manual = _committed_checkpoint(coordinator, 1, provenance="manual")
    command = _enqueue_checkpoint(
        coordinator,
        "sync_manual",
        manual,
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result=_checkpoint_result(manual),
    )
    state = coordinator.state()
    assert state.checkpoints[-1].lineage_valid is False
    with pytest.raises(CoordinatorConflict, match="lineage"):
        coordinator.select_checkpoint(1, revision=state.revision)
    with pytest.raises(CoordinatorConflict):
        coordinator.transition("finalize", revision=state.revision, final_plan_id="missing-final", execution_id="missing-execution")
def test_restart_recovers_every_active_state_to_paused(tmp_path):
    for offset, target in enumerate(("baseline", "iterating", "pause_requested", "manual_edit", "finalizing")):
        _, plan, coordinator, session = _coordinator(tmp_path / str(offset), mode="final")
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
                session, _final, _execution = _finalize(
                    coordinator.root,
                    plan,
                    coordinator,
                    session,
                )
        assert session.status == target
        recovered = AECoordinator(coordinator.root, coordinator.plan_id).state()
        assert recovered.status == "paused:server_restart"



def test_restart_reconciles_completed_final_result_before_server_pause(tmp_path, monkeypatch):
    root, plan, coordinator, session = _coordinator(tmp_path, mode="final")
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    session = coordinator.transition("no_progress", revision=session.revision)
    session = coordinator.select_checkpoint(0, revision=session.revision)
    session, final, _execution = _finalize(root, plan, coordinator, session)

    render = coordinator.enqueue_command(
        "render_final",
        {
            "checkpoint_index": 0,
            "final_plan_digest": final.digest,
            "finalization_key": session.finalization_key,
            **_FINAL_RENDER_FIELDS,
        },
        expected_state="finalizing",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    mp4_id = _publish(coordinator, "mp4")
    coordinator.accept_result(
        "device-1",
        render.id,
        sequence=render.sequence,
        result={"ok": True, "artifacts": [{"id": mp4_id, "kind": "mp4"}]},
    )
    assert session.finalization_key
    package_payload, package_result = _final_package(
        coordinator,
        final,
        session.finalization_key,
    )
    package = coordinator.enqueue_command(
        "package_project",
        package_payload,
        expected_state="finalizing",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    original_commit = coordinator._commit_transition_unlocked

    def crash_after_durable_result(before, after, event, data):
        if event == "command_result" and after.status == "done":
            raise RuntimeError("simulated restart")
        return original_commit(before, after, event, data)

    monkeypatch.setattr(coordinator, "_commit_transition_unlocked", crash_after_durable_result)
    with pytest.raises(RuntimeError, match="simulated restart"):
        coordinator.accept_result(
            "device-1",
            package.id,
            sequence=package.sequence,
            result=package_result,
        )

    recovered = AECoordinator(root, plan.id).state()
    assert recovered.status == "done"


def test_agent_id_added_at_passing_checkpoint_remains_targetable(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    baseline = _committed_checkpoint(coordinator, 0, provenance="baseline").model_copy(
        update={"inspection": {"layers": []}}
    )
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=baseline.model_dump(mode="json"),
    )
    candidate = _committed_checkpoint(coordinator, 1).model_copy(
        update={
            "inspection": {
                "layers": [
                    {"layer_instance_id": "agent-added", "source_element_id": None}
                ]
            },
            "operations": (
                {
                    "kind": "add_layer",
                    "layer_instance_id": "agent-added",
                    "layer_type": "null",
                    "name": "added",
                },
            ),
        }
    )
    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        candidate,
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(candidate),
    )
    assert "agent-added" not in coordinator.state().issued_instance_id_tombstones

    target = coordinator.enqueue_command(
        "apply_batch",
        {
            "baseline": False,
            "operations": [
                {"kind": "set_transform", "layer_instance_id": "agent-added"}
            ],
        },
        expected_state="iterating",
        expected_checkpoint=1,
    )
    assert target.kind == "apply_batch"


def test_selecting_older_passing_checkpoint_does_not_tombstone_live_agent_id(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    baseline = _committed_checkpoint(coordinator, 0, provenance="baseline").model_copy(
        update={
            "inspection": {
                "layers": [
                    {"layer_instance_id": "agent-live", "source_element_id": None}
                ]
            }
        }
    )
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=baseline.model_dump(mode="json"),
    )
    candidate = _committed_checkpoint(coordinator, 1).model_copy(
        update={
            "inspection": {
                "layers": [
                    {"layer_instance_id": "agent-live", "source_element_id": None},
                    {"layer_instance_id": "agent-new", "source_element_id": None},
                ]
            },
            "operations": (
                {
                    "kind": "add_layer",
                    "layer_instance_id": "agent-new",
                    "layer_type": "null",
                    "name": "new",
                },
            ),
        }
    )
    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        candidate,
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(candidate),
    )
    state = coordinator.transition("no_progress", revision=coordinator.state().revision)
    state = coordinator.select_checkpoint(0, revision=state.revision)
    assert "agent-new" not in state.issued_instance_id_tombstones
    state = coordinator.transition("continue", revision=state.revision)
    state = coordinator.transition("device_ready", revision=state.revision, device_id="device-1")
    target = coordinator.enqueue_command(
        "apply_batch",
        {
            "baseline": False,
            "operations": [
                {"kind": "set_transform", "layer_instance_id": "agent-new"}
            ],
        },
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert target.kind == "apply_batch"


def test_failed_candidate_removal_rollback_restores_live_agent_id(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    baseline = _committed_checkpoint(coordinator, 0, provenance="baseline").model_copy(
        update={
            "inspection": {
                "layers": [
                    {"layer_instance_id": "agent-live", "source_element_id": None}
                ]
            }
        }
    )
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=baseline.model_dump(mode="json"),
    )
    failed = _failed_checkpoint(coordinator, 1).model_copy(
        update={"inspection": {"layers": []}}
    )
    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        failed,
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(failed),
    )
    rollback = coordinator.enqueue_command(
        "open_project",
        {"checkpoint_index": 0},
        expected_state="iterating",
        expected_checkpoint=1,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        rollback.id,
        sequence=rollback.sequence,
        result={"ok": True},
    )
    assert "agent-live" not in coordinator.state().issued_instance_id_tombstones
    target = coordinator.enqueue_command(
        "apply_batch",
        {
            "baseline": False,
            "operations": [
                {"kind": "set_transform", "layer_instance_id": "agent-live"}
            ],
        },
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert target.kind == "apply_batch"


def test_failed_candidate_agent_id_is_tombstoned_when_rollback_discards_branch(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    baseline = _committed_checkpoint(coordinator, 0, provenance="baseline").model_copy(
        update={
            "inspection": {
                "layers": [
                    {"layer_instance_id": "agent-live", "source_element_id": None}
                ]
            }
        }
    )
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=baseline.model_dump(mode="json"),
    )
    failed = _failed_checkpoint(coordinator, 1).model_copy(
        update={
            "inspection": {
                "layers": [
                    {"layer_instance_id": "agent-live", "source_element_id": None},
                    {"layer_instance_id": "agent-discarded", "source_element_id": None},
                ]
            },
            "operations": (
                {
                    "kind": "add_layer",
                    "layer_instance_id": "agent-discarded",
                    "layer_type": "null",
                    "name": "discarded",
                },
            ),
        }
    )
    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        failed,
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(failed),
    )
    assert "agent-live" not in coordinator.state().issued_instance_id_tombstones
    assert "agent-discarded" in coordinator.state().issued_instance_id_tombstones

    rollback = coordinator.enqueue_command(
        "open_project",
        {"checkpoint_index": 0},
        expected_state="iterating",
        expected_checkpoint=1,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        rollback.id,
        sequence=rollback.sequence,
        result={"ok": True},
    )
    with pytest.raises(CoordinatorConflict, match="tombstone"):
        coordinator.enqueue_command(
            "apply_batch",
            {
                "baseline": False,
                "operations": [
                    {"kind": "set_transform", "layer_instance_id": "agent-discarded"}
                ],
            },
            expected_state="iterating",
            expected_checkpoint=0,
        )


def test_truly_removed_source_less_id_remains_tombstoned_after_restart(tmp_path):
    root, plan, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    baseline = _committed_checkpoint(coordinator, 0, provenance="baseline").model_copy(
        update={
            "inspection": {
                "layers": [
                    {"layer_instance_id": "agent-removed", "source_element_id": None}
                ]
            }
        }
    )
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=baseline.model_dump(mode="json"),
    )
    removed = _committed_checkpoint(coordinator, 1).model_copy(
        update={"inspection": {"layers": []}}
    )
    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        removed,
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(removed),
    )
    assert "agent-removed" in coordinator.state().issued_instance_id_tombstones

    recovered = AECoordinator(root, plan.id).state()
    assert "agent-removed" in recovered.issued_instance_id_tombstones

def test_command_leases_redeliver_without_reapplying_and_results_are_idempotent(tmp_path):
    _, plan, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    command = coordinator.enqueue_command(
        "heartbeat",
        {"probe": True},
        expected_state="baseline",
        expected_checkpoint=None,
        lease_seconds=10,
    )
    assert command.plan_digest == plan.digest

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



def test_checkpoint_and_package_commands_use_long_leases(tmp_path, monkeypatch):
    monkeypatch.setattr("keepframe.after_effects.coordinator._now", lambda: 100.0)

    def assert_long_lease(owner, command):
        assert command.lease_seconds > 30
        assert owner.next_command("device-1", now=100) is not None
        assert owner.next_command("device-1", now=131) is None
        redelivered = owner.next_command("device-1", now=3701)

    _, _, baseline, session = _coordinator(tmp_path / "save")
    session = baseline.transition("device_ready", revision=session.revision, device_id="device-1")
    save = baseline.enqueue_command(
        "save_checkpoint",
        {},
        expected_state="baseline",
        expected_checkpoint=None,
    )
    assert_long_lease(baseline, save)

    _, _, manual, session = _coordinator(tmp_path / "manual")
    session = _ready_iterating(manual, session)
    session = manual.transition("no_progress", revision=session.revision)
    session = manual.transition("begin_manual", revision=session.revision)
    sync = manual.enqueue_command(
        "sync_manual",
        {"prepare": True},
        expected_state="manual_edit",
        expected_checkpoint=session.selected_checkpoint,
    )
    assert_long_lease(manual, sync)


    root, preview_plan, package, session = _coordinator(tmp_path / "package", mode="final")
    session = _ready_iterating(package, session)
    session = package.transition("no_progress", revision=session.revision)
    session = package.select_checkpoint(0, revision=session.revision)
    session, final, _execution = _finalize(root, preview_plan, package, session)
    command = package.enqueue_command(
        "package_project",
        {
            "selected_checkpoint": session.selected_checkpoint,
            "final_plan_digest": final.digest,
            "finalization_key": session.finalization_key,
        },
        expected_state="finalizing",
        expected_checkpoint=session.selected_checkpoint,
    )
    assert_long_lease(package, command)

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


def test_leased_command_can_renew_only_with_bound_nonce_and_live_state(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    command = coordinator.enqueue_command(
        "heartbeat",
        {},
        expected_state="baseline",
        expected_checkpoint=None,
        lease_seconds=10,
    )
    leased = coordinator.next_command("device-1", now=100)
    assert leased is not None

    renewed = coordinator.renew_command(
        "device-1",
        command.id,
        sequence=command.sequence,
        nonce=command.nonce,
        now=105,
    )
    assert renewed.lease_expires_at is not None
    assert renewed.lease_expires_at > leased.lease_expires_at

    with pytest.raises(CoordinatorConflict, match="nonce"):
        coordinator.renew_command(
            "device-1",
            command.id,
            sequence=command.sequence,
            nonce="nonce-invalid",
            now=106,
        )

    coordinator.detach_device("device-1", reason="unpair")
    with pytest.raises(CoordinatorConflict, match="bound"):
        coordinator.renew_command(
            "device-1",
            command.id,
            sequence=command.sequence,
            nonce=command.nonce,
            now=107,
        )


def test_command_renewal_is_wall_clock_bounded_and_idempotent(tmp_path, monkeypatch):
    _, _, coordinator, session = _coordinator(tmp_path)
    monkeypatch.setattr("keepframe.after_effects.coordinator._now", lambda: 100.0)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    command = coordinator.enqueue_command(
        "heartbeat",
        {},
        expected_state="baseline",
        expected_checkpoint=None,
        lease_seconds=30,
    )
    leased = coordinator.next_command("device-1", now=100)
    assert leased is not None and leased.lease_expires_at == 130

    for _ in range(20):
        assert coordinator.renew_command(
            "device-1",
            command.id,
            sequence=command.sequence,
            nonce=command.nonce,
            now=100,
        ).lease_expires_at == 130

    cadence = coordinator.renew_command(
        "device-1",
        command.id,
        sequence=command.sequence,
        nonce=command.nonce,
        now=110,
    )
    assert cadence.lease_expires_at == 140

    assert coordinator.renew_command(
        "device-1",
        command.id,
        sequence=command.sequence,
        nonce=command.nonce,
        now=120,
    ).lease_expires_at == 150
    assert coordinator.renew_command(
        "device-1",
        command.id,
        sequence=command.sequence,
        nonce=command.nonce,
        now=140,
    ).lease_expires_at == 170
    assert coordinator.renew_command(
        "device-1",
        command.id,
        sequence=command.sequence,
        nonce=command.nonce,
        now=160,
    ).lease_expires_at == 190
    monkeypatch.setattr("keepframe.after_effects.coordinator._MAX_COMMAND_LIFETIME_SECONDS", 100.0)
    capped = coordinator.renew_command(
        "device-1",
        command.id,
        sequence=command.sequence,
        nonce=command.nonce,
        now=180,
    )
    assert capped.lease_expires_at == 200
    assert coordinator.renew_command(
        "device-1",
        command.id,
        sequence=command.sequence,
        nonce=command.nonce,
        now=181,
    ).lease_expires_at == 200
    with pytest.raises(CoordinatorConflict, match="expired"):
        coordinator.renew_command(
            "device-1",
            command.id,
            sequence=command.sequence,
            nonce=command.nonce,
            now=200,
        )


@pytest.mark.parametrize("reason", ["replacement", "timeout"])
def test_detach_draining_command_cannot_extend_lease(tmp_path, reason):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    command = coordinator.enqueue_command(
        "heartbeat",
        {},
        expected_state="baseline",
        expected_checkpoint=None,
        lease_seconds=30,
    )
    leased = coordinator.next_command("device-1", now=100)
    assert leased is not None
    draining = coordinator.detach_device("device-1", reason=reason, now=105)
    assert draining.status == "pause_requested"
    with pytest.raises(CoordinatorConflict, match="draining"):
        coordinator.renew_command(
            "device-1",
            command.id,
            sequence=command.sequence,
            nonce=command.nonce,
            now=106,
        )
    settled = coordinator.detach_device("device-1", reason=reason, now=131)
    assert settled.device_id is None


def test_expired_manual_preparation_is_revoked_before_reopen(tmp_path, monkeypatch):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = _ready_iterating(coordinator, session)
    session = coordinator.transition("no_progress", revision=session.revision)
    session = coordinator.transition("begin_manual", revision=session.revision)
    monkeypatch.setattr("keepframe.after_effects.coordinator._now", lambda: 100.0)
    preparation = coordinator.enqueue_command(
        "sync_manual",
        {"prepare_manual": True, "workflow": {"stage": "manual_prepare"}},
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    session = coordinator.transition("timeout", revision=session.revision)
    monkeypatch.setattr("keepframe.after_effects.coordinator._now", lambda: 3701.0)
    session = coordinator.state()
    assert session.status == "paused:timeout"
    session = coordinator.transition("begin_manual", revision=session.revision)
    assert next(
        item for item in coordinator.command_history() if item.id == preparation.id
    ).status == "revoked"
    with pytest.raises(CoordinatorConflict, match="leased"):
        coordinator.accept_result(
            "device-1",
            preparation.id,
            sequence=preparation.sequence,
            result={"ok": True},
        )

    fresh = coordinator.enqueue_command(
        "sync_manual",
        {"prepare_manual": True, "workflow": {"stage": "manual_prepare"}},
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    assert fresh.id != preparation.id


def test_stop_waits_for_active_batch_checkpoint(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    checkpoint = _committed_checkpoint(coordinator, 1)
    command = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        checkpoint,
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    session = coordinator.transition("stop", revision=session.revision)
    assert session.status == "pause_requested"

    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result=_checkpoint_result(checkpoint),
    )
    session = coordinator.state()
    assert session.status == "paused:user"
    assert [item.index for item in session.checkpoints] == [0, 1]


def test_late_baseline_checkpoint_must_still_be_zero(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    checkpoint = _committed_checkpoint(coordinator, 1, provenance="baseline")
    command = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        checkpoint,
        expected_state="baseline",
        expected_checkpoint=None,
    )
    assert coordinator.next_command("device-1") is not None
    session = coordinator.transition("disconnect", revision=session.revision)
    assert session.status == "pause_requested"

    with pytest.raises(CoordinatorConflict, match="checkpoint 0"):
        coordinator.accept_result(
            "device-1",
            command.id,
            sequence=command.sequence,
            result=_checkpoint_result(checkpoint),
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
    checkpoint = _committed_checkpoint(coordinator, 1, provenance="manual")
    command = _enqueue_checkpoint(
        coordinator,
        "sync_manual",
        checkpoint,
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    session = coordinator.transition("disconnect", revision=session.revision)
    assert session.status == "pause_requested"

    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result=_checkpoint_result(checkpoint),
    )
    session = coordinator.state()
    assert session.status == "paused:disconnect"
    assert session.reason == "disconnect"
    assert [item.index for item in session.checkpoints] == [0, 1]
def test_failed_render_command_surfaces_render_specific_pause(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    command = coordinator.enqueue_command(
        "render_preview",
        expected_state="baseline",
        expected_checkpoint=None,
    )
    assert coordinator.next_command("device-1") is not None

    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result={"ok": False, "error": "connector preview processing failed"},
    )

    assert coordinator.state().status == "paused:render_failed"



def test_no_progress_disconnect_and_finalization_edges(tmp_path):
    root, preview_plan, coordinator, session = _coordinator(tmp_path, mode="final")
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    session = coordinator.transition("no_progress", revision=session.revision)
    assert session.status == "paused:no_progress"
    session = coordinator.select_checkpoint(0, revision=session.revision)
    session, final, _execution = _finalize(root, preview_plan, coordinator, session)
    assert session.status == "finalizing"
    with pytest.raises(CoordinatorConflict, match="package_project"):
        coordinator.transition("final_complete", revision=session.revision)

    mp4_id = _publish(coordinator, "mp4")
    render = coordinator.enqueue_command(
        "render_final",
        {
            "checkpoint_index": 0,
            "final_plan_digest": final.digest,
            "finalization_key": session.finalization_key,
            **_FINAL_RENDER_FIELDS,
        },
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

    assert session.finalization_key
    package_payload, package_result = _final_package(
        coordinator,
        final,
        session.finalization_key,
    )
    package = coordinator.enqueue_command(
        "package_project",
        package_payload,
        expected_state="finalizing",
        expected_checkpoint=0,
    )
    leased = coordinator.next_command("device-1")
    assert leased is not None and leased.id == package.id
    coordinator.accept_result(
        "device-1",
        package.id,
        sequence=package.sequence,
        result=package_result,
    )
    session = coordinator.state()
    assert session.status == "done"
    with pytest.raises(CoordinatorConflict, match="terminal"):
        coordinator.transition("continue", revision=session.revision)


def test_finalization_revalidates_authoritative_approval(tmp_path):
    root, preview_plan, coordinator, session = _coordinator(tmp_path, mode="final")
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    session = coordinator.transition("no_progress", revision=session.revision)
    session = coordinator.select_checkpoint(0, revision=session.revision)
    final, execution_id = _final_successor(root, preview_plan, coordinator)
    meta_path = root / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = "review"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(CoordinatorConflict, match="approved"):
        coordinator.transition(
            "finalize",
            revision=session.revision,
            final_plan_id=final.id,
            execution_id=execution_id,
        )
    assert coordinator.state().status == "paused:no_progress"


def test_artifact_reservations_stream_validate_and_publish_once(tmp_path):
    root, plan, coordinator, session = _coordinator(tmp_path)
    coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    payload = _artifact_payload("png")
    reservation = coordinator.reserve_artifact("png", max_length=len(payload))

    with pytest.raises(CoordinatorConflict, match="bound"):
        coordinator.publish_artifact(
            "device-2", reservation.id, io.BytesIO(payload), content_length=len(payload)
        )
    artifact = coordinator.publish_artifact(
        "device-1",
        reservation.id,
        io.BytesIO(payload),
        content_length=len(payload),
    )

    path = root / "renders" / plan.id / "ae" / "artifacts" / reservation.id
    assert artifact.sha256
    reservations_path = root / "renders" / plan.id / "ae" / "reservations.json"
    committed_state = reservations_path.read_bytes()
    assert path.read_bytes() == payload
    retry = coordinator.publish_artifact(
        "device-1",
        reservation.id,
        io.BytesIO(payload),
        content_length=len(payload),
    )
    assert retry == artifact
    assert path.read_bytes() == payload
    assert reservations_path.read_bytes() == committed_state
    different = bytearray(payload)
    different[19] = 2
    different[29:33] = struct.pack(
        ">I",
        zlib.crc32(different[12:29]) & 0xFFFFFFFF,
    )
    with pytest.raises(CoordinatorConflict, match="digest"):
        coordinator.publish_artifact(
            "device-1",
            reservation.id,
            io.BytesIO(different),
            content_length=len(different),
        )
    with pytest.raises(CoordinatorConflict):
        coordinator.publish_artifact(
            "device-1",
            reservation.id,
            io.BytesIO(payload[:-1]),
            content_length=len(payload) - 1,
        )
    too_small = coordinator.reserve_artifact("png", max_length=4)
    with pytest.raises(CoordinatorConflict, match="length"):
        coordinator.publish_artifact(
            "device-1", too_small.id, io.BytesIO(payload), content_length=len(payload)
        )

    invalid = coordinator.reserve_artifact("png", max_length=32)
    fake_png = b"\x89PNG\r\n\x1a\nframe"
    with pytest.raises(CoordinatorConflict, match="container|PNG"):
        coordinator.publish_artifact(
            "device-1", invalid.id, io.BytesIO(fake_png), content_length=len(fake_png)
        )
    fake_aep = b"RIFX" + struct.pack(">I", 4) + b"Egg!"
    invalid_aep = coordinator.reserve_artifact("aep", max_length=len(fake_aep))
    with pytest.raises(CoordinatorConflict, match="AEP"):
        coordinator.publish_artifact(
            "device-1",
            invalid_aep.id,
            io.BytesIO(fake_aep),
            content_length=len(fake_aep),
        )
    malformed_nested_aep = (
        b"RIFX"
        + struct.pack(">I", 28)
        + b"Egg!LIST"
        + struct.pack(">I", 16)
        + b"Foldtdsn"
        + struct.pack(">I", 8)
        + b"\x00\x00\x00\x01"
    )
    malformed_aep = coordinator.reserve_artifact(
        "aep",
        max_length=len(malformed_nested_aep),
    )
    with pytest.raises(CoordinatorConflict, match="truncated"):
        coordinator.publish_artifact(
            "device-1",
            malformed_aep.id,
            io.BytesIO(malformed_nested_aep),
            content_length=len(malformed_nested_aep),
        )
    oversized = bytearray(payload)
    oversized[16:20] = struct.pack(">I", 0xFFFFFFFF)
    oversized[29:33] = struct.pack(
        ">I",
        zlib.crc32(oversized[12:29]) & 0xFFFFFFFF,
    )
    oversized_reservation = coordinator.reserve_artifact(
        "png", max_length=len(oversized)
    )
    oversized_path = (
        root
        / "renders"
        / plan.id
        / "ae"
        / "artifacts"
        / oversized_reservation.id
    )
    with pytest.raises(CoordinatorConflict, match="dimensions"):
        coordinator.publish_artifact(
            "device-1",
            oversized_reservation.id,
            io.BytesIO(oversized),
            content_length=len(oversized),
        )
    assert not oversized_path.exists()


    occupied = coordinator.reserve_artifact("png", max_length=len(payload))
    occupied_path = root / "renders" / plan.id / "ae" / "artifacts" / occupied.id
    occupied_path.write_bytes(b"sentinel")
    with pytest.raises(CoordinatorConflict):
        coordinator.publish_artifact(
            "device-1", occupied.id, io.BytesIO(payload), content_length=len(payload)
        )
    assert occupied_path.read_bytes() == b"sentinel"


def test_connector_artifact_requires_live_command_reference(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    coordinator.transition(
        "device_ready",
        revision=session.revision,
        device_id="device-1",
    )
    payload = _artifact_payload("png")
    allowed = coordinator.reserve_artifact("png", len(payload))
    unrelated = coordinator.reserve_artifact("png", len(payload))
    command = coordinator.enqueue_command(
        "heartbeat",
        {"artifact": {"reservation_id": allowed.id}},
        expected_state="baseline",
        expected_checkpoint=None,
        lease_seconds=60,
    )
    leased = coordinator.next_command("device-1")
    assert leased is not None and leased.id == command.id

    with pytest.raises(CoordinatorConflict, match="active command"):
        coordinator.publish_artifact(
            "device-1",
            unrelated.id,
            io.BytesIO(payload),
            content_length=len(payload),
            require_live_command=True,
        )
    published = coordinator.publish_artifact(
        "device-1",
        allowed.id,
        io.BytesIO(payload),
        content_length=len(payload),
        require_live_command=True,
    )
    assert published.reservation_id == allowed.id


def test_connector_artifact_lease_must_survive_final_validation(
    tmp_path,
    monkeypatch,
):
    root, plan, coordinator, session = _coordinator(tmp_path)
    coordinator.transition(
        "device_ready",
        revision=session.revision,
        device_id="device-1",
    )
    payload = _artifact_payload("png")
    reservation = coordinator.reserve_artifact("png", len(payload))
    clock = [100.0]
    monkeypatch.setattr(
        "keepframe.after_effects.coordinator._now",
        lambda: clock[0],
    )
    coordinator.enqueue_command(
        "heartbeat",
        {"artifact_id": reservation.id},
        expected_state="baseline",
        expected_checkpoint=None,
        lease_seconds=10,
    )
    assert coordinator.next_command("device-1") is not None

    from keepframe.after_effects import coordinator as coordinator_module

    validate = coordinator_module._validate_artifact_file
    validations = 0

    def expire_during_final_validation(kind, path, length):
        nonlocal validations
        validate(kind, path, length)
        validations += 1
        if validations == 2:
            clock[0] = 111.0

    monkeypatch.setattr(
        coordinator_module,
        "_validate_artifact_file",
        expire_during_final_validation,
    )
    with pytest.raises(CoordinatorConflict, match="active command"):
        coordinator.publish_artifact(
            "device-1",
            reservation.id,
            io.BytesIO(payload),
            content_length=len(payload),
            require_live_command=True,
        )
    assert not (
        root
        / "renders"
        / plan.id
        / "ae"
        / "artifacts"
        / reservation.id
    ).exists()


def _failed_checkpoint(
    coordinator: AECoordinator,
    index: int,
    *,
    provenance: str = "agent",
    failure: str = "keep predicate failed",
) -> AECheckpoint:
    return AECheckpoint(
        index=index,
        provenance=provenance,
        passed=False,
        aep_artifact_id=_publish(coordinator, "aep"),
        preview_artifact_id=_publish(coordinator, "mp4"),
        frame_artifact_ids=(_publish(coordinator, "png"),),
        failure=failure,
    )


def _ready_iterating(coordinator: AECoordinator, session):
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    return session


def test_apply_batch_is_mutation_only_and_allowed_for_baseline_and_iteration(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    baseline = coordinator.enqueue_command(
        "apply_batch",
        {"baseline": True, "operations": []},
        expected_state="baseline",
        expected_checkpoint=None,
    )
    leased = coordinator.next_command("device-1")
    assert leased is not None and leased.id == baseline.id
    coordinator.accept_result(
        "device-1",
        baseline.id,
        sequence=baseline.sequence,
        result={"ok": True},
    )
    assert coordinator.state().status == "baseline"
    assert coordinator.state().applied_command_sequence == baseline.sequence

    with pytest.raises(CoordinatorConflict, match="baseline"):
        coordinator.enqueue_command(
            "apply_batch",
            {"baseline": False, "operations": []},
            expected_state="baseline",
            expected_checkpoint=None,
        )
    session = coordinator.transition(
        "baseline_complete",
        revision=coordinator.state().revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    iteration = coordinator.enqueue_command(
        "apply_batch",
        {"baseline": False, "operations": []},
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        iteration.id,
        sequence=iteration.sequence,
        result={"ok": True},
    )
    assert coordinator.state().status == "iterating"
    assert [item.index for item in coordinator.state().checkpoints] == [0]


def test_open_project_allows_iteration_rollback_to_last_passing_checkpoint(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    _ready_iterating(coordinator, session)
    command = coordinator.enqueue_command(
        "open_project",
        {"checkpoint_index": 0},
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None

    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result={"ok": True},
    )

    assert coordinator.state().status == "iterating"


def test_render_artifacts_allow_only_safe_server_local_names(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    reservation = coordinator.reserve_artifact("mp4", max_length=1024)
    coordinator.enqueue_command(
        "render_preview",
        {
            "artifacts": [
                {
                    "reservation_id": reservation.id,
                    "kind": "mp4",
                    "filename": "checkpoint-0.mp4",
                    "directory": "renders",
                }
            ]
        },
        expected_state="baseline",
        expected_checkpoint=None,
    )

    with pytest.raises(CoordinatorConflict, match="artifact filename"):
        coordinator.enqueue_command(
            "render_preview",
            {
                "artifacts": [
                    {
                        "reservation_id": reservation.id,
                        "kind": "mp4",
                        "filename": "../checkpoint-0.mp4",
                        "directory": "renders",
                    }
                ]
            },
            expected_state="baseline",
            expected_checkpoint=None,
        )


def test_apply_batch_late_stop_requires_checkpoint_capture(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = _ready_iterating(coordinator, session)
    command = coordinator.enqueue_command(
        "apply_batch",
        {"baseline": False, "operations": []},
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    session = coordinator.transition("stop", revision=session.revision)
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result={"ok": True},
    )

    state = coordinator.state()
    assert state.status == "pause_requested"
    assert state.checkpoint_required is True

    inspect = coordinator.enqueue_command(
        "inspect_layers",
        expected_state="pause_requested",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1").id == inspect.id
    coordinator.accept_result(
        "device-1",
        inspect.id,
        sequence=inspect.sequence,
        result={
            "ok": True,
            "schema_version": "keepframe.ae-inspection/1",
            "layers": [],
            "heartbeat": {
                "capability_hash": _AE_CAPABILITY_HASH,
                "version": _AE_CAPABILITY_MANIFEST["version"],
                "major": _AE_CAPABILITY_MANIFEST["major"],
                "host": _AE_CAPABILITY_MANIFEST["host"],
            },
        },
    )
    state = coordinator.state()
    assert state.status == "pause_requested"
    assert state.checkpoint_required is True

    render = coordinator.enqueue_command(
        "render_preview",
        expected_state="pause_requested",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1").id == render.id
    coordinator.accept_result(
        "device-1",
        render.id,
        sequence=render.sequence,
        result={"ok": True},
    )
    state = coordinator.state()
    assert state.status == "pause_requested"
    assert state.checkpoint_required is True

    checkpoint = _committed_checkpoint(coordinator, 1)
    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        checkpoint,
        expected_state="pause_requested",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1").id == save.id
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(checkpoint),
    )

    state = coordinator.state()
    assert state.status == "paused:user"
    assert state.checkpoint_required is False
    assert [item.index for item in state.checkpoints] == [0, 1]
    assert coordinator.checkpoint(1) == checkpoint
    assert state.selected_checkpoint == 1
    assert state.applied_command_sequence == save.sequence
def test_stop_after_completed_apply_requires_checkpoint_capture(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = _ready_iterating(coordinator, session)
    command = coordinator.enqueue_command(
        "apply_batch",
        {"baseline": False, "operations": []},
        expected_state="iterating",
        expected_checkpoint=0,
    )
    leased = coordinator.next_command("device-1")
    assert leased is not None and leased.id == command.id
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result={"ok": True},
    )
    state = coordinator.state()
    assert state.status == "iterating"
    state = coordinator.transition("stop", revision=state.revision)
    assert state.status == "pause_requested"
    assert state.checkpoint_required is True



def test_late_stop_capture_saves_baseline_checkpoint_zero(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    command = coordinator.enqueue_command(
        "apply_batch",
        {"baseline": True, "operations": []},
        expected_state="baseline",
        expected_checkpoint=None,
    )
    assert coordinator.next_command("device-1").id == command.id
    session = coordinator.transition("stop", revision=session.revision)
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result={"ok": True},
    )

    state = coordinator.state()
    assert state.status == "pause_requested"
    assert state.checkpoint_required is True

    checkpoint = _committed_checkpoint(coordinator, 0, provenance="baseline")
    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        checkpoint,
        expected_state="pause_requested",
        expected_checkpoint=None,
    )
    assert coordinator.next_command("device-1").id == save.id
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(checkpoint),
    )

    state = coordinator.state()
    assert state.status == "paused:user"
    assert state.checkpoint_required is False
    assert [item.index for item in state.checkpoints] == [0]
    assert coordinator.checkpoint(0) == checkpoint
    assert state.selected_checkpoint == 0


@pytest.mark.parametrize("reason", ["disconnect", "timeout", "unpair"])
def test_late_apply_disconnect_reasons_do_not_require_capture(tmp_path, reason):
    _, _, coordinator, session = _coordinator(tmp_path / reason)
    session = _ready_iterating(coordinator, session)
    apply = coordinator.enqueue_command(
        "apply_batch",
        {"baseline": False, "operations": []},
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1").id == apply.id
    session = coordinator.transition(reason, revision=session.revision)
    assert session.status == "pause_requested"
    coordinator.accept_result(
        "device-1",
        apply.id,
        sequence=apply.sequence,
        result={"ok": True},
    )

    state = coordinator.state()
    assert state.status == f"paused:{reason}"
    assert state.reason == reason
    assert state.checkpoint_required is False


@pytest.mark.parametrize("reason", ["disconnect", "timeout", "unpair"])
def test_connector_loss_cancels_pending_user_capture(tmp_path, reason):
    _, _, coordinator, session = _coordinator(tmp_path / reason)
    session = _ready_iterating(coordinator, session)
    apply = coordinator.enqueue_command(
        "apply_batch",
        {"baseline": False, "operations": []},
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1").id == apply.id
    session = coordinator.transition("stop", revision=session.revision)
    coordinator.accept_result(
        "device-1",
        apply.id,
        sequence=apply.sequence,
        result={"ok": True},
    )
    state = coordinator.state()
    assert state.status == "pause_requested"
    assert state.checkpoint_required is True

    state = coordinator.transition(reason, revision=state.revision)
    assert state.status == f"paused:{reason}"
    assert state.checkpoint_required is False


def test_failed_late_stop_capture_clears_requirement_with_specific_reason(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = _ready_iterating(coordinator, session)
    apply = coordinator.enqueue_command(
        "apply_batch",
        {"baseline": False, "operations": []},
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1").id == apply.id
    session = coordinator.transition("stop", revision=session.revision)
    coordinator.accept_result(
        "device-1",
        apply.id,
        sequence=apply.sequence,
        result={"ok": True},
    )
    assert coordinator.state().checkpoint_required is True

    render = coordinator.enqueue_command(
        "render_preview",
        expected_state="pause_requested",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1").id == render.id
    coordinator.accept_result(
        "device-1",
        render.id,
        sequence=render.sequence,
        result={"ok": False},
    )

    state = coordinator.state()
    assert state.status == "paused:render_failed"
    assert state.reason == "render_failed"
    assert state.checkpoint_required is False


def test_failed_checkpoint_is_preserved_and_last_passing_checkpoint_selected(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path, mode="final")
    session = _ready_iterating(coordinator, session)
    failed = _failed_checkpoint(coordinator, 1)
    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        failed,
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(failed),
    )
    state = coordinator.state()
    assert state.status == "iterating"
    assert [item.index for item in state.checkpoints] == [0, 1]
    assert state.selected_checkpoint == 0
    state = coordinator.transition("no_progress", revision=state.revision)
    with pytest.raises(CoordinatorConflict, match="verification"):
        coordinator.select_checkpoint(1, revision=state.revision)
    state = coordinator.transition("continue", revision=state.revision)
    state = coordinator.transition("device_ready", revision=state.revision, device_id="device-1")

    passed = _committed_checkpoint(coordinator, 2)
    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        passed,
        expected_state="iterating",
        expected_checkpoint=1,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(passed),
    )
    state = coordinator.state()
    assert state.status == "iterating"
    assert state.selected_checkpoint == 2
    assert [item.index for item in state.checkpoints] == [0, 1, 2]


def test_server_checkpoint_context_cannot_be_replaced_by_connector(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    expected = _committed_checkpoint(coordinator, 0, provenance="baseline")
    save = coordinator.enqueue_command(
        "save_checkpoint",
        {"checkpoint_context": expected.model_dump(mode="json")},
        expected_state="baseline",
        expected_checkpoint=None,
    )
    assert coordinator.next_command("device-1") is not None
    forged = expected.model_copy(update={"model_response": {"forged": True}})

    with pytest.raises(CoordinatorConflict, match="server checkpoint context"):
        coordinator.accept_result(
            "device-1",
            save.id,
            sequence=save.sequence,
            result={"ok": True, "checkpoint": forged.model_dump(mode="json")},
        )


def test_server_checkpoint_context_storage_allows_over_wire_limit(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    checkpoint = _committed_checkpoint(coordinator, 0, provenance="baseline").model_copy(
        update={"inspection": {"blob": "x" * (1024 * 1024 + 1)}}
    )

    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        checkpoint,
        expected_state="baseline",
        expected_checkpoint=None,
    )

    assert "checkpoint_context" not in save.payload
    assert isinstance(save.payload.get("checkpoint_context_digest"), str)
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(checkpoint),
    )
    assert coordinator.checkpoint(0).inspection["blob"].startswith("x")



def test_server_checkpoint_context_storage_allows_large_full_frame_context(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    checkpoint = _committed_checkpoint(coordinator, 0, provenance="baseline").model_copy(
        update={"inspection": {"samples": "x" * (64 * 1024 * 1024 + 1)}}
    )

    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        checkpoint,
        expected_state="baseline",
        expected_checkpoint=None,
    )

    assert isinstance(save.payload.get("checkpoint_context_digest"), str)
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(checkpoint),
    )
    persisted = json.loads((coordinator.ae_dir / "session.json").read_text(encoding="utf-8"))
    assert persisted["checkpoints"][0]["inspection"] == {}
    assert list((coordinator.ae_dir / "checkpoint-contexts").glob("*.json.gz"))
    assert coordinator.checkpoint(0).inspection["samples"].startswith("x")

def test_baseline_failure_preserves_checkpoint_zero_and_pauses_verification(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    failed = _failed_checkpoint(coordinator, 0, provenance="baseline")
    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        failed,
        expected_state="baseline",
        expected_checkpoint=None,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(failed),
    )
    state = coordinator.state()
    assert state.status == "paused:verification_failed"
    assert [item.index for item in state.checkpoints] == [0]
    assert state.checkpoints[0].passed is False
    assert state.selected_checkpoint is None


def test_manual_sync_preserves_failed_candidate_and_manual_passes_pause_synced(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = _ready_iterating(coordinator, session)
    session = coordinator.transition("no_progress", revision=session.revision)
    session = coordinator.transition("begin_manual", revision=session.revision)
    failed = _failed_checkpoint(coordinator, 1, provenance="manual")
    command = _enqueue_checkpoint(
        coordinator,
        "sync_manual",
        failed,
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result=_checkpoint_result(failed),
    )
    state = coordinator.state()
    assert state.status == "paused:verification_failed"
    assert state.checkpoints[-1].provenance == "manual"
    assert state.checkpoints[-1].passed is False

    state = coordinator.transition("continue", revision=state.revision)
    state = coordinator.transition("device_ready", revision=state.revision, device_id="device-1")
    state = coordinator.transition("no_progress", revision=state.revision)
    state = coordinator.transition("begin_manual", revision=state.revision)
    passed = _committed_checkpoint(coordinator, 2, provenance="manual")
    command = _enqueue_checkpoint(
        coordinator,
        "sync_manual",
        passed,
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result=_checkpoint_result(passed),
    )
    state = coordinator.state()
    assert state.status == "paused:manual_synced"
    assert state.selected_checkpoint == 2


def test_manual_prepare_sync_is_mutation_only_and_manual_inspect_render_are_allowed(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = _ready_iterating(coordinator, session)
    session = coordinator.transition("no_progress", revision=session.revision)
    session = coordinator.transition("begin_manual", revision=session.revision)
    artifact = coordinator.reserve_artifact("aep", len(_artifact_payload("aep")))
    prepare = coordinator.enqueue_command(
        "sync_manual",
        {
            "prepare_manual": True,
            "artifacts": [
                {
                    "reservation_id": artifact.id,
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
        artifact.id,
        io.BytesIO(_artifact_payload("aep")),
        content_length=len(_artifact_payload("aep")),
        require_live_command=True,
    )
    coordinator.accept_result(
        "device-1",
        prepare.id,
        sequence=prepare.sequence,
        result={"ok": True},
    )
    assert coordinator.state().status == "manual_edit"

    manual_state = coordinator.state()
    inspect = coordinator.enqueue_command(
        "inspect_layers",
        {
            "workflow": {
                "stage": "manual_inspect",
                "manual_epoch": manual_state.manual_epoch,
                "manual_attempt_id": manual_state.manual_attempt_id,
            }
        },
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        inspect.id,
        sequence=inspect.sequence,
        result={
            "ok": True,
            "schema_version": "keepframe.ae-inspection/1",
            "heartbeat": {
                "capability_hash": _AE_CAPABILITY_HASH,
                "version": _AE_CAPABILITY_MANIFEST["version"],
                "major": _AE_CAPABILITY_MANIFEST["major"],
                "host": _AE_CAPABILITY_MANIFEST["host"],
            },
        },
    )
    render = coordinator.enqueue_command(
        "render_preview",
        {
            "workflow": {
                "stage": "manual_render",
                "manual_epoch": manual_state.manual_epoch,
                "manual_attempt_id": manual_state.manual_attempt_id,
            }
        },
        expected_state="manual_edit",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        render.id,
        sequence=render.sequence,
        result={"ok": True},
    )
    assert coordinator.state().status == "manual_edit"


def test_apply_batch_rejects_manifest_drift_and_cross_batch_tombstone_reuse(tmp_path, monkeypatch):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    manifest = current_operation_manifest()
    monkeypatch.setattr(
        "keepframe.after_effects.coordinator.current_operation_manifest",
        lambda: tuple((*manifest[:-1], {**manifest[-1], "kind": "drift"})),
    )
    with pytest.raises(CoordinatorConflict, match="operation manifest"):
        coordinator.enqueue_command(
            "apply_batch",
            {"baseline": True, "operations": []},
            expected_state="baseline",
            expected_checkpoint=None,
        )
    monkeypatch.setattr(
        "keepframe.after_effects.coordinator.current_operation_manifest",
        lambda: manifest,
    )

    add = coordinator.enqueue_command(
        "apply_batch",
        {
            "baseline": True,
            "operations": [
                {
                    "kind": "add_layer",
                    "layer_instance_id": "agent-temp",
                    "layer_type": "null",
                    "name": "temp",
                }
            ],
        },
        expected_state="baseline",
        expected_checkpoint=None,
    )
    monkeypatch.setattr(
        "keepframe.after_effects.coordinator.current_operation_manifest",
        lambda: tuple((*manifest[:-1], {**manifest[-1], "kind": "drift"})),
    )
    with pytest.raises(CoordinatorConflict, match="operation manifest"):
        coordinator.next_command("device-1")
    monkeypatch.setattr(
        "keepframe.after_effects.coordinator.current_operation_manifest",
        lambda: manifest,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result("device-1", add.id, sequence=add.sequence, result={"ok": True})
    remove = coordinator.enqueue_command(
        "apply_batch",
        {
            "baseline": True,
            "operations": [{"kind": "remove_layer", "layer_instance_id": "agent-temp"}],
        },
        expected_state="baseline",
        expected_checkpoint=None,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result("device-1", remove.id, sequence=remove.sequence, result={"ok": True})
    assert "agent-temp" in coordinator.state().issued_instance_id_tombstones
    with pytest.raises(CoordinatorConflict, match="tombstone"):
        coordinator.enqueue_command(
            "apply_batch",
            {
                "baseline": True,
                "operations": [
                    {
                        "kind": "add_layer",
                        "layer_instance_id": "agent-temp",
                        "layer_type": "null",
                        "name": "reused",
                    }
                ],
            },
            expected_state="baseline",
            expected_checkpoint=None,
        )



def test_checkpoint_inspection_removal_tombstones_source_less_id(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    baseline = _committed_checkpoint(coordinator, 0, provenance="baseline")
    baseline = baseline.model_copy(
        update={
            "inspection": {
                "layers": [
                    {"layer_instance_id": "agent-inspect", "source_element_id": None}
                ]
            }
        }
    )
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=baseline.model_dump(mode="json"),
    )
    candidate = _committed_checkpoint(coordinator, 1)
    candidate = candidate.model_copy(update={"inspection": {"layers": []}})
    command = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        candidate,
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result=_checkpoint_result(candidate),
    )
    assert "agent-inspect" in coordinator.state().issued_instance_id_tombstones
    with pytest.raises(CoordinatorConflict, match="tombstone"):
        coordinator.enqueue_command(
            "apply_batch",
            {
                "baseline": False,
                "operations": [
                    {
                        "kind": "add_layer",
                        "layer_instance_id": "agent-inspect",
                        "layer_type": "null",
                        "name": "reused",
                    }
                ],
            },
            expected_state="iterating",
            expected_checkpoint=1,
        )




def test_finalization_binds_commands_to_selected_passing_checkpoint(tmp_path):
    root, preview_plan, coordinator, session = _coordinator(tmp_path, mode="final")
    session = _ready_iterating(coordinator, session)
    failed = _failed_checkpoint(coordinator, 1)
    save = _enqueue_checkpoint(
        coordinator,
        "save_checkpoint",
        failed,
        expected_state="iterating",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None
    coordinator.accept_result(
        "device-1",
        save.id,
        sequence=save.sequence,
        result=_checkpoint_result(failed),
    )
    state = coordinator.transition("no_progress", revision=coordinator.state().revision)
    state = coordinator.select_checkpoint(0, revision=state.revision)
    state, final, _execution = _finalize(root, preview_plan, coordinator, state)
    command = coordinator.enqueue_command(
        "render_final",
        {
            "checkpoint_index": 0,
            "final_plan_digest": final.digest,
            "finalization_key": state.finalization_key,
            **_FINAL_RENDER_FIELDS,
        },
        expected_state="finalizing",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1").id == command.id
    fake_mp4 = (
        struct.pack(">I4s", 16, b"ftyp")
        + b"isom\x00\x00\x00\x00"
        + struct.pack(">I4s", 8, b"mdat")
    )
    reservation = coordinator.reserve_artifact("mp4", max_length=len(fake_mp4))
    artifact_id = _publish_reserved(coordinator, reservation.id, fake_mp4)
    with pytest.raises(CoordinatorConflict, match="probe failed"):
        coordinator.accept_result(
            "device-1",
            command.id,
            sequence=command.sequence,
            result={
                "ok": True,
                "artifacts": [{"id": artifact_id, "kind": "mp4"}],
            },
        )


def test_paused_finalization_can_retry_or_replace_its_successor(tmp_path):
    root, preview, coordinator, session = _coordinator(tmp_path, mode="final")
    session = _ready_iterating(coordinator, session)
    session = coordinator.transition("no_progress", revision=session.revision)
    session = coordinator.select_checkpoint(0, revision=session.revision)
    session, final, execution = _finalize(root, preview, coordinator, session)
    command = coordinator.enqueue_command(
        "render_final",
        {
            "checkpoint_index": 0,
            "final_plan_digest": final.digest,
            "finalization_key": session.finalization_key,
            **_FINAL_RENDER_FIELDS,
        },
        expected_state="finalizing",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1").id == command.id
    mp4_id = _publish(coordinator, "mp4")
    coordinator.accept_result(
        "device-1",
        command.id,
        sequence=command.sequence,
        result={"ok": True, "artifacts": [{"id": mp4_id, "kind": "mp4"}]},
    )
    session = coordinator.state()
    session = coordinator.transition("package_failed", revision=session.revision)
    session = coordinator.transition(
        "finalize",
        revision=session.revision,
        final_plan_id=final.id,
        execution_id=execution,
    )
    assert session.final_mp4_artifact_id == mp4_id

    session = coordinator.transition("package_failed", revision=session.revision)
    replacement, replacement_execution = _final_successor(
        root,
        preview,
        coordinator,
    )
    session = coordinator.transition(
        "finalize",
        revision=session.revision,
        final_plan_id=replacement.id,
        execution_id=replacement_execution,
    )
    assert session.final_plan_id == replacement.id
    assert session.final_mp4_artifact_id is None
    assert session.final_artifact_ids == {}


def test_command_history_and_exact_lookup_are_immutable_reads(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    command = coordinator.enqueue_command(
        "heartbeat",
        expected_state="baseline",
        expected_checkpoint=None,
    )
    history = coordinator.command_history()
    assert isinstance(history, tuple)
    assert history[-1] == command
    assert coordinator.get_command(command.id) == command
    with pytest.raises(CoordinatorConflict, match="command"):
        coordinator.get_command("cmd-missing")
    history[0].payload["changed"] = True
    assert coordinator.command_history()[-1].payload == {}
    assert coordinator.get_command(command.id).payload == {}


def test_restart_recovers_pause_requested_to_server_restart(tmp_path):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    command = coordinator.enqueue_command(
        "apply_batch",
        {"baseline": True, "operations": []},
        expected_state="baseline",
        expected_checkpoint=None,
    )
    assert coordinator.next_command("device-1") is not None
    session = coordinator.transition("stop", revision=session.revision)
    assert session.status == "pause_requested"
    recovered = AECoordinator(coordinator.root, coordinator.plan_id).state()
    assert recovered.status == "paused:server_restart"




def test_restart_revokes_leased_command_before_continue_can_redeliver(tmp_path):
    root, preview_plan, coordinator, session = _coordinator(tmp_path, mode="final")
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    session = coordinator.transition("no_progress", revision=session.revision)
    session = coordinator.select_checkpoint(0, revision=session.revision)
    session, final, _execution = _finalize(root, preview_plan, coordinator, session)
    command = coordinator.enqueue_command(
        "render_final",
        {
            "checkpoint_index": 0,
            "final_plan_digest": final.digest,
            "finalization_key": session.finalization_key,
            **_FINAL_RENDER_FIELDS,
        },
        expected_state="finalizing",
        expected_checkpoint=0,
    )
    assert coordinator.next_command("device-1") is not None

    recovered = AECoordinator(coordinator.root, coordinator.plan_id)
    state = recovered.state()
    assert state.status == "paused:server_restart"
    state = recovered.transition("continue", revision=state.revision)
    recovered.transition("device_ready", revision=state.revision, device_id="device-1")

    assert recovered.next_command("device-1") is None
    assert recovered.get_command(command.id).status == "revoked"
@pytest.mark.parametrize("reason", ["vision_unsupported", "model_paused"])
def test_pause_error_accepts_vision_loop_reasons(tmp_path, reason):
    _, _, coordinator, session = _coordinator(tmp_path)
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    paused = coordinator.transition(
        "pause_error",
        revision=session.revision,
        reason=reason,
    )
    assert paused.status == f"paused:{reason}"

def test_finalization_persists_approved_binding_and_idempotency_key(tmp_path):
    root, preview_plan, coordinator, session = _coordinator(tmp_path, mode="final")
    session = coordinator.transition("device_ready", revision=session.revision, device_id="device-1")
    session = coordinator.transition(
        "baseline_complete",
        revision=session.revision,
        checkpoint=_committed_checkpoint(coordinator, 0, provenance="baseline").model_dump(mode="json"),
    )
    session = coordinator.transition("no_progress", revision=session.revision)
    session = coordinator.select_checkpoint(0, revision=session.revision)
    session, final, execution_id = _finalize(root, preview_plan, coordinator, session)

    assert session.final_plan_id == final.id
    assert session.final_plan_digest == final.digest
    assert session.final_execution_id == execution_id
    assert session.finalization_key == compute_finalization_key(final.digest, 0)
