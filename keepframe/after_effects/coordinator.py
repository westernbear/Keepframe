from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import secrets
import stat
import struct
import tempfile
import threading
import time
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Literal, Mapping, Protocol, Sequence, cast
from pydantic import ValidationError

from ..render.plan import (
    PlanConflict,
    _atomic_write,
    _canonical_json as _canonical_mapping_json,
    _ensure_project_path,
    _state_lock,
    load_render_plan,
    load_render_plan_state,
    validate_render_plan_execution,
)
from .planning import current_operation_manifest

from .models import (
    AEArtifactReservation,
    AECheckpoint,
    AECheckpointContext,
    AECommand,
    AECommandResult,
    AEPublishedArtifact,
    AESession,
    ArtifactKind,
    CommandKind,
    canonical_json,
)

class CoordinatorConflict(RuntimeError):
    """A stale, illegal, or contradictory coordinator operation."""


class ReadableStream(Protocol):
    def read(self, size: int | None = -1, /) -> bytes: ...

_ACTIVE_STATES = {"baseline", "iterating", "pause_requested", "manual_edit", "finalizing"}
_TERMINAL_STATES = {"done", "failed"}
_PAUSED_PREFIX = "paused:"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,255}$")
_COMMAND_STATES: dict[CommandKind, frozenset[str]] = {
    "heartbeat": frozenset({"baseline", "iterating"}),
    "create_project": frozenset({"baseline"}),
    "open_project": frozenset({"baseline", "iterating", "manual_edit"}),
    "import_asset": frozenset({"baseline"}),
    "apply_batch": frozenset({"baseline", "iterating"}),
    "inspect_layers": frozenset({"baseline", "iterating", "pause_requested", "manual_edit"}),
    "save_checkpoint": frozenset({"baseline", "iterating", "pause_requested"}),
    "render_preview": frozenset({"baseline", "iterating", "pause_requested", "manual_edit"}),
    "render_final": frozenset({"finalizing"}),
    "package_project": frozenset({"finalizing"}),
    "sync_manual": frozenset({"manual_edit"}),
}
_NO_STATE_RESULT_KINDS: frozenset[CommandKind] = frozenset(
    {
        "heartbeat",
        "create_project",
        "open_project",
        "import_asset",
        "inspect_layers",
        "render_preview",
    }
)
_ARTIFACT_KINDS: frozenset[ArtifactKind] = frozenset({"png", "mp4", "aep", "zip"})
_COMMAND_FAILURE_REASONS: dict[CommandKind, str] = {
    "heartbeat": "connector_failed",
    "create_project": "project_failed",
    "open_project": "project_failed",
    "import_asset": "asset_failed",
    "apply_batch": "apply_failed",
    "inspect_layers": "inspection_failed",
    "save_checkpoint": "upload_failed",
    "render_preview": "render_failed",
    "render_final": "render_failed",
    "package_project": "package_failed",
    "sync_manual": "upload_failed",
}
_MIME = {
    "png": "image/png",
    "mp4": "video/mp4",
    "aep": "application/vnd.adobe.after-effects",
    "zip": "application/zip",
}
_MAX_PROTOCOL_BYTES = 1024 * 1024
_MAX_COMMAND_ENVELOPE_BYTES = 64 * 1024
_MAX_COMMAND_PAYLOAD_BYTES = _MAX_PROTOCOL_BYTES - _MAX_COMMAND_ENVELOPE_BYTES
# Checkpoint contexts stay server-side in one compressed, content-addressed
# sidecar per digest. They are not connector protocol data and therefore do
# not inherit the 1 MiB wire ceiling.
# Keep a full hour for the longest MCP operation plus result validation and
# streaming all reserved artifacts before the lease can settle a disconnect.
_MAX_COMMAND_LIFETIME_SECONDS = 24 * 60 * 60.0
_LONG_COMMAND_LEASE_SECONDS = 60 * 60.0

def _now() -> float:
    return time.time()


def _safe_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_COMPONENT.fullmatch(value):
        raise CoordinatorConflict(f"{label} is invalid")
    return value


def _safe_command_id(value: str) -> str:
    if not isinstance(value, str) or not _COMMAND_ID.fullmatch(value):
        raise CoordinatorConflict("command id is invalid")
    return value


def _finite_time(value: float, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CoordinatorConflict(f"{label} is invalid") from exc
    if not math.isfinite(result) or result < 0:
        raise CoordinatorConflict(f"{label} is invalid")
    return result


def _protocol_bytes(
    value: Any,
    label: str,
    *,
    limit: int = _MAX_PROTOCOL_BYTES,
) -> bytes:
    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CoordinatorConflict(f"{label} must be finite canonical JSON") from exc
    if len(encoded) > limit:
        raise CoordinatorConflict(f"{label} exceeds its bounded size")
    return encoded


def _payload_digest(value: Any) -> str:
    return hashlib.sha256(_protocol_bytes(value, "payload")).hexdigest()


def _command_payload_digest(value: Any) -> str:
    return hashlib.sha256(
        _protocol_bytes(value, "command payload", limit=_MAX_COMMAND_PAYLOAD_BYTES)
    ).hexdigest()


def _checkpoint_context_digest(value: Any) -> str:
    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CoordinatorConflict("checkpoint context must be finite canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()
_FORBIDDEN_PAYLOAD_KEYS = {
    "path",
    "url",
    "uri",
    "code",
    "script",
    "expression",
    "filename",
    "filepath",
    "address",
}
_ARTIFACT_PAYLOAD_FIELDS = frozenset(
    {"reservation_id", "kind", "filename", "directory", "length"}
)


def _validate_command_artifacts(value: Any) -> None:
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        raise CoordinatorConflict("command artifacts are invalid")
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _ARTIFACT_PAYLOAD_FIELDS - {"length"} and set(
            item
        ) != _ARTIFACT_PAYLOAD_FIELDS:
            raise CoordinatorConflict("command artifact fields are invalid")
        reservation_id = item["reservation_id"]
        if not isinstance(reservation_id, str) or not _SAFE_COMPONENT.fullmatch(reservation_id):
            raise CoordinatorConflict("command artifact reservation is invalid")
        if item["kind"] not in _ARTIFACT_KINDS:
            raise CoordinatorConflict("command artifact kind is invalid")
        filename = item["filename"]
        if not isinstance(filename, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", filename
        ):
            raise CoordinatorConflict("command artifact filename is invalid")
        if item["directory"] not in {"checkpoints", "renders"}:
            raise CoordinatorConflict("command artifact directory is invalid")
        length = item.get("length")
        if length is not None and (
            not isinstance(length, int) or isinstance(length, bool) or length < 0
        ):
            raise CoordinatorConflict("command artifact length is invalid")


def _validate_command_payload(value: Any, *, allow_artifacts: bool = False) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if allow_artifacts and key == "artifacts":
                _validate_command_artifacts(item)
                continue
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_PAYLOAD_KEYS or any(
                token in key.lower() for token in ("path", "url", "uri", "code", "script", "expression")
            ):
                raise CoordinatorConflict("command payload contains an unsupported field")
            _validate_command_payload(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_command_payload(item)
        return
    if isinstance(value, str):
        if "\x00" in value or "://" in value:
            raise CoordinatorConflict("command payload contains an unsupported value")
        if (
            value.startswith(("/", "\\\\"))
            or re.match(r"^[A-Za-z]:[\\/]", value)
            or any(part == ".." for part in re.split(r"[/\\\\]", value))
        ):
            raise CoordinatorConflict("command payload contains an arbitrary path")





def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise CoordinatorConflict(f"missing persisted file: {path.name}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoordinatorConflict(f"invalid persisted file: {path.name}") from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        if path.is_symlink() or not path.is_dir():
            raise OSError(f"cannot synchronize directory: {path}")
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        st = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & 0x0400)


def _ensure_no_symlink(path: Path, *, kind: str | None = None) -> None:
    if _is_reparse(path):
        raise CoordinatorConflict(f"unsafe {path.name} path")
    if kind == "directory" and path.exists() and not path.is_dir():
        raise CoordinatorConflict(f"{path.name} is not a directory")
    if kind == "file" and path.exists() and not path.is_file():
        raise CoordinatorConflict(f"{path.name} is not a file")
_MAX_ZIP_ENTRIES = 4096
_MAX_ZIP_CENTRAL_SIZE = 16 * 1024 * 1024
_MAX_ZIP_METADATA = 1 * 1024 * 1024
_MAX_PNG_CHUNKS = 1_000_000
_MAX_PNG_WIDTH = 1280
_MAX_PNG_HEIGHT = 720
_MAX_PNG_PIXELS = _MAX_PNG_WIDTH * _MAX_PNG_HEIGHT
_MAX_BOXES = 1_000_000


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    if size < 0:
        raise CoordinatorConflict("artifact container size is invalid")
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(min(1024 * 1024, remaining))
        if not chunk:
            raise CoordinatorConflict("artifact container is truncated")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise CoordinatorConflict("artifact stream returned non-bytes")
        data = bytes(chunk)
        if len(data) > remaining:
            raise CoordinatorConflict("artifact container is malformed")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _discard(stream: BinaryIO, size: int) -> None:
    remaining = size
    while remaining:
        chunk = stream.read(min(1024 * 1024, remaining))
        if not chunk:
            raise CoordinatorConflict("artifact container is truncated")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise CoordinatorConflict("artifact stream returned non-bytes")
        if len(chunk) > remaining:
            raise CoordinatorConflict("artifact container is malformed")
        remaining -= len(chunk)


def _validate_png(path: Path, length: int) -> None:
    if length < 8:
        raise CoordinatorConflict("artifact PNG container is invalid")
    with path.open("rb") as stream:
        if _read_exact(stream, 8) != b"\x89PNG\r\n\x1a\n":
            raise CoordinatorConflict("artifact PNG signature is invalid")
        consumed = 8
        chunks = 0
        seen_ihdr = False
        seen_iend = False
        while consumed < length:
            chunks += 1
            if chunks > _MAX_PNG_CHUNKS:
                raise CoordinatorConflict("artifact PNG has too many chunks")
            header = _read_exact(stream, 8)
            chunk_length = struct.unpack(">I", header[:4])[0]
            chunk_type = header[4:]
            consumed += 8
            if not re.fullmatch(rb"[A-Za-z]{4}", chunk_type):
                raise CoordinatorConflict("artifact PNG chunk type is invalid")
            if seen_iend:
                raise CoordinatorConflict("artifact PNG has data after IEND")
            crc = zlib.crc32(chunk_type)
            ihdr = bytearray()
            remaining = chunk_length
            while remaining:
                part = _read_exact(stream, min(1024 * 1024, remaining))
                if chunk_type == b"IHDR":
                    if len(ihdr) + len(part) > 13:
                        raise CoordinatorConflict("artifact PNG IHDR is invalid")
                    ihdr.extend(part)
                crc = zlib.crc32(part, crc)
                remaining -= len(part)
            actual_crc = struct.unpack(">I", _read_exact(stream, 4))[0]
            consumed += chunk_length + 4
            if actual_crc != (crc & 0xFFFFFFFF):
                raise CoordinatorConflict("artifact PNG CRC is invalid")
            if chunk_type == b"IHDR":
                if seen_ihdr or chunk_length != 13:
                    raise CoordinatorConflict("artifact PNG IHDR is invalid")
                width, height = struct.unpack(">II", ihdr[:8])
                if (
                    width == 0
                    or height == 0
                    or width > _MAX_PNG_WIDTH
                    or height > _MAX_PNG_HEIGHT
                    or width * height > _MAX_PNG_PIXELS
                ):
                    raise CoordinatorConflict("artifact PNG dimensions are invalid")
                seen_ihdr = True
            elif not seen_ihdr:
                raise CoordinatorConflict("artifact PNG is missing IHDR")
            if chunk_type == b"IEND":
                if chunk_length != 0:
                    raise CoordinatorConflict("artifact PNG IEND is invalid")
                seen_iend = True
        if consumed != length or not seen_ihdr or not seen_iend:
            raise CoordinatorConflict("artifact PNG is incomplete")


def _validate_mp4(path: Path, length: int) -> None:
    if length < 16:
        raise CoordinatorConflict("artifact MP4 container is invalid")
    with path.open("rb") as stream:
        offset = 0
        boxes = 0
        seen_ftyp = False
        seen_media = False
        while offset < length:
            boxes += 1
            if boxes > _MAX_BOXES:
                raise CoordinatorConflict("artifact MP4 has too many boxes")
            header = _read_exact(stream, 8)
            size, box_type = struct.unpack(">I4s", header)
            header_size = 8
            if size == 1:
                size = struct.unpack(">Q", _read_exact(stream, 8))[0]
                header_size = 16
            if size < header_size or size > length - offset:
                raise CoordinatorConflict("artifact MP4 box size is invalid")
            if size == 0:
                raise CoordinatorConflict("artifact MP4 box size is invalid")
            if box_type == b"ftyp":
                if size < 16:
                    raise CoordinatorConflict("artifact MP4 ftyp box is invalid")
                seen_ftyp = True
            if box_type in {b"moov", b"moof", b"mdat"}:
                seen_media = True
            _discard(stream, size - header_size)
            offset += size
        if offset != length or not seen_ftyp or not seen_media:
            raise CoordinatorConflict("artifact MP4 container is incomplete")


def _validate_zip(path: Path, length: int) -> None:
    if length < 22:
        raise CoordinatorConflict("artifact ZIP container is invalid")
    tail_length = min(length, 65_557)
    with path.open("rb") as stream:
        stream.seek(length - tail_length)
        tail = _read_exact(stream, tail_length)
        eocd_offset = tail.rfind(b"PK\x05\x06")
        if eocd_offset < 0 or eocd_offset + 22 > tail_length:
            raise CoordinatorConflict("artifact ZIP central directory is missing")
        absolute_eocd = length - tail_length + eocd_offset
        fields = struct.unpack("<4s4H2IH", tail[eocd_offset : eocd_offset + 22])
        _signature, disk, central_disk, entries_disk, entries, central_size, central_offset, comment_size = fields
        if absolute_eocd + 22 + comment_size != length:
            raise CoordinatorConflict("artifact ZIP end record is invalid")
        if disk != 0 or central_disk != 0 or entries_disk != entries:
            raise CoordinatorConflict("artifact ZIP is multi-disk")
        if entries == 0 or entries > _MAX_ZIP_ENTRIES:
            raise CoordinatorConflict("artifact ZIP entry count is invalid")
        if central_size == 0 or central_size > _MAX_ZIP_CENTRAL_SIZE:
            raise CoordinatorConflict("artifact ZIP central directory is invalid")
        if central_offset + central_size != absolute_eocd:
            raise CoordinatorConflict("artifact ZIP central directory bounds are invalid")
        stream.seek(central_offset)
        central = _read_exact(stream, central_size)
        offset = 0
        metadata = 0
        for _ in range(entries):
            if offset + 46 > len(central) or central[offset : offset + 4] != b"PK\x01\x02":
                raise CoordinatorConflict("artifact ZIP central directory is invalid")
            values = struct.unpack("<4s6H3I5H2I", central[offset : offset + 46])
            _signature, _made_by, _needed, _flags, _compression, _mtime, _mdate, _crc, compressed, uncompressed, name_size, extra_size, entry_comment_size, _disk_start, _internal, _external, local_offset = values
            offset += 46
            metadata += name_size + extra_size + entry_comment_size
            if metadata > _MAX_ZIP_METADATA or name_size > 4096:
                raise CoordinatorConflict("artifact ZIP entry metadata is too large")
            end = offset + name_size + extra_size + entry_comment_size
            if end > len(central):
                raise CoordinatorConflict("artifact ZIP central directory is truncated")
            name = central[offset : offset + name_size]
            if b"\x00" in name:
                raise CoordinatorConflict("artifact ZIP entry name is invalid")
            if compressed > length or uncompressed > 4_294_967_296:
                raise CoordinatorConflict("artifact ZIP entry size is invalid")
            if local_offset + 30 > absolute_eocd:
                raise CoordinatorConflict("artifact ZIP local header is invalid")
            stream.seek(local_offset)
            if _read_exact(stream, 4) != b"PK\x03\x04":
                raise CoordinatorConflict("artifact ZIP local header is invalid")
            offset = end
        if offset != len(central):
            raise CoordinatorConflict("artifact ZIP central directory has trailing data")


def _validate_aep(path: Path, length: int) -> None:
    if length < 12:
        raise CoordinatorConflict("artifact AEP container is invalid")
    with path.open("rb") as stream:
        header = _read_exact(stream, 12)
    declared_size = struct.unpack(">I", header[4:8])[0]
    if header[:4] != b"RIFX" or header[8:12] != b"Egg!" or declared_size != length - 8:
        raise CoordinatorConflict("artifact AEP container is invalid")


def _validate_artifact_file(kind: ArtifactKind, path: Path, length: int) -> None:
    _ensure_no_symlink(path, kind="file")
    if not path.is_file() or path.stat().st_size != length:
        raise CoordinatorConflict("artifact length does not match reservation")
    if kind == "png":
        _validate_png(path, length)
    elif kind == "mp4":
        _validate_mp4(path, length)
    elif kind == "zip":
        _validate_zip(path, length)
    elif kind == "aep":
        _validate_aep(path, length)
    else:
        raise CoordinatorConflict("artifact kind is not supported")

def _contains_identifier(value: object, identifier: str) -> bool:
    if isinstance(value, str):
        return value == identifier
    if isinstance(value, Mapping):
        return any(
            _contains_identifier(item, identifier)
            for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_identifier(item, identifier) for item in value)
    return False



def _checkpoint_index(session: AESession) -> int | None:
    if session.open_checkpoint is not None and any(
        item.index == session.open_checkpoint and item.lineage_valid
        for item in session.checkpoints
    ):
        return session.open_checkpoint
    if session.selected_checkpoint is not None and any(
        item.index == session.selected_checkpoint
        and item.lineage_valid
        for item in session.checkpoints
    ):
        return session.selected_checkpoint
    return max(
        (item.index for item in session.checkpoints if item.lineage_valid),
        default=None,
    )


def _manual_reopen_checkpoint(session: AESession) -> int | None:
    """Choose the immutable AE checkpoint manual editing must reopen."""
    if not session.baseline_complete:
        partial = next((item for item in session.checkpoints if item.index == 0), None)
        if partial is not None:
            return 0
    if session.selected_checkpoint is not None and any(
        item.index == session.selected_checkpoint
        and item.passed
        and item.lineage_valid
        for item in session.checkpoints
    ):
        return session.selected_checkpoint
    return max(
        (
            item.index
            for item in session.checkpoints
            if item.passed and item.lineage_valid
        ),
        default=None,
    )


def _paused(status: str) -> bool:
    return isinstance(status, str) and status.startswith(_PAUSED_PREFIX)

def _validate_command_kind_state(kind: CommandKind, expected_state: str) -> None:
    if not isinstance(expected_state, str) or expected_state not in _COMMAND_STATES.get(kind, frozenset()):
        raise CoordinatorConflict("command kind is not valid for the expected state")


def _command_kind_state_matches(command: AECommand) -> bool:
    return command.expected_state in _COMMAND_STATES.get(command.kind, frozenset())


def _checkpoint_from(value: Any) -> AECheckpoint:
    if isinstance(value, AECheckpoint):
        return value
    if not isinstance(value, Mapping):
        raise CoordinatorConflict("checkpoint is required")
    try:
        return AECheckpoint.model_validate_json(canonical_json(value))
    except (ValidationError, TypeError, ValueError) as exc:
        raise CoordinatorConflict("checkpoint is invalid") from exc
def _status_reason(event: str, data: Mapping[str, Any]) -> str:
    reason = data.get("reason")
    if reason is None:
        reason = event
    if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", reason):
        raise CoordinatorConflict("pause reason is invalid")
    return reason



def _json_safe(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_safe(model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _operation_records(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Mapping):
        return ()
    raw = value.get("operations")
    if raw is None and isinstance(value.get("batch"), Mapping):
        raw = value["batch"].get("operations")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _inspection_inventory(value: Any) -> set[str] | None:
    """Extract the source-less IDs from an exact layer inventory."""
    records: set[str] = set()
    saw_inventory = False

    def walk(item: Any) -> None:
        nonlocal saw_inventory
        if isinstance(item, Mapping):
            for key in ("layers", "layer_inventory", "inventory"):
                if key in item:
                    saw_inventory = True
            layer_sources = item.get("layer_sources")
            if isinstance(layer_sources, Mapping):
                saw_inventory = True
                for identifier, source in layer_sources.items():
                    if isinstance(identifier, str) and source is None:
                        records.add(identifier)
            identifier = item.get("layer_instance_id", item.get("instance_id"))
            if isinstance(identifier, str):
                source = item.get("source_element_id", item.get("source_id"))
                if source is None:
                    records.add(identifier)
            for child in item.values():
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)
    if not saw_inventory and not records:
        return None
    return records


def _operation_instance_ids(value: Any) -> set[str]:
    identifiers: set[str] = set()
    for operation in _operation_records(value):
        for key in ("layer_instance_id", "parent_instance_id"):
            identifier = operation.get(key)
            if isinstance(identifier, str):
                identifiers.add(identifier)
    return identifiers


def _validate_apply_payload(payload: Mapping[str, Any], expected_state: str) -> None:
    baseline = payload.get("baseline")
    if type(baseline) is not bool or baseline is not (expected_state == "baseline"):
        raise CoordinatorConflict("apply_batch baseline does not match expected state")


def _command_checkpoint(session: AESession, kind: CommandKind, expected_state: str) -> int | None:
    if kind in {"render_final", "package_project"} or expected_state == "finalizing":
        return session.selected_checkpoint
    if (
        kind not in {"open_project", "save_checkpoint"}
        and expected_state in {"iterating", "manual_edit"}
        and session.selected_checkpoint is not None
        and session.selected_checkpoint != session.open_checkpoint
    ):
        return session.selected_checkpoint
    return _checkpoint_index(session)


def _validate_sync_payload(payload: Mapping[str, Any]) -> None:
    prepare = payload.get("prepare_manual")
    if prepare is not None and type(prepare) is not bool:
        raise CoordinatorConflict("prepare_manual must be a boolean")




class AECoordinator:
    _RECOVERY_GUARD = threading.RLock()
    _LIVE_INSTANCES: dict[str, "AECoordinator"] = {}

    @classmethod
    def cached(cls, root: Path, plan_id: str) -> "AECoordinator":
        key = str((Path(root) / "renders" / plan_id / "ae" / "session.json").resolve(strict=False))
        with cls._RECOVERY_GUARD:
            instance = cls._LIVE_INSTANCES.get(key)
            if instance is None:
                instance = cls(root, plan_id)
                cls._LIVE_INSTANCES[key] = instance
            return instance

    def __init__(self, root: Path, plan_id: str) -> None:
        self.root = Path(root)
        self.plan_id = _safe_component(plan_id, "plan id")
        try:
            self.plan = load_render_plan(self.root, self.plan_id)
        except PlanConflict as exc:
            raise CoordinatorConflict(str(exc)) from exc
        if self.plan.backend != "after_effects":
            raise CoordinatorConflict("coordinator requires an after_effects plan")
        self.project_id = _safe_component(self.plan.project_id, "project id")
        self.plan_dir = self.root / "renders" / self.plan_id
        self.ae_dir = self.plan_dir / "ae"
        self.artifacts_dir = self.ae_dir / "artifacts"
        self._state_path = self.ae_dir / "session.json"
        self._commands_path = self.ae_dir / "commands.json"
        self._reservations_path = self.ae_dir / "reservations.json"
        self._checkpoint_context_dir = self.ae_dir / "checkpoint-contexts"
        self._events_path = self.ae_dir / "events.jsonl"
        try:
            _ensure_project_path(self.root, self.plan_dir, "render plan directory", kind="directory")
            _ensure_project_path(
                self.root,
                self.ae_dir,
                "After Effects directory",
                kind="directory",
                allow_missing=True,
            )
        except PlanConflict as exc:
            raise CoordinatorConflict(str(exc)) from exc
        for path in (self.ae_dir, self.artifacts_dir, self._checkpoint_context_dir):
            _ensure_no_symlink(path, kind="directory")
            path.mkdir(parents=True, exist_ok=True)
        _ensure_no_symlink(self._events_path, kind="file")
        # One lock name below the project serializes all AE plans in this project.
        self._lock_path = self.root / "ae-coordinator"
        self._recover_on_load()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            with _state_lock(self._lock_path):
                yield
        except PlanConflict as exc:
            raise CoordinatorConflict(str(exc)) from exc

    def _hydrate_checkpoint_summary_unlocked(self, summary: AECheckpoint) -> AECheckpoint:
        digest = summary.context_digest
        if digest is None:
            return summary
        checkpoint = self._load_checkpoint_context_unlocked(digest)
        if checkpoint is None:
            raise CoordinatorConflict("checkpoint context is unavailable")
        if (
            checkpoint.index != summary.index
            or checkpoint.provenance != summary.provenance
            or checkpoint.passed != summary.passed
            or checkpoint.lineage_valid != summary.lineage_valid
            or checkpoint.aep_artifact_id != summary.aep_artifact_id
            or checkpoint.preview_artifact_id != summary.preview_artifact_id
            or checkpoint.frame_artifact_ids != summary.frame_artifact_ids
            or checkpoint.failure != summary.failure
        ):
            raise CoordinatorConflict("checkpoint summary does not match context")
        return checkpoint.model_copy(update={"context_digest": None})

    def _load_session_unlocked(self, *, hydrate_checkpoints: bool = True) -> AESession:
        _ensure_no_symlink(self._state_path, kind="file")
        raw = _read_json(self._state_path)
        try:
            session = AESession.model_validate_json(canonical_json(raw))
        except ValidationError as exc:
            raise CoordinatorConflict("persisted AE session is invalid") from exc
        if session.plan_id != self.plan_id or session.project_id != self.project_id:
            raise CoordinatorConflict("persisted AE session ownership mismatch")
        if session.plan_digest != self.plan.digest:
            raise CoordinatorConflict("persisted AE session plan digest mismatch")
        if not hydrate_checkpoints:
            return session
        hydrated = tuple(
            self._hydrate_checkpoint_summary_unlocked(summary)
            for summary in session.checkpoints
        )
        return session.model_copy(update={"checkpoints": hydrated})

    def checkpoint(self, index: int) -> AECheckpoint:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise CoordinatorConflict("checkpoint index is invalid")
        with self._locked():
            session = self._load_session_unlocked(hydrate_checkpoints=False)
            summary = next(
                (item for item in session.checkpoints if item.index == index),
                None,
            )
            if summary is None:
                raise CoordinatorConflict("checkpoint does not exist")
            return self._hydrate_checkpoint_summary_unlocked(summary)

    def _write_session_unlocked(self, session: AESession) -> None:
        compact_checkpoints: list[dict[str, Any]] = []
        for checkpoint in session.checkpoints:
            digest = checkpoint.context_digest
            if digest is None:
                digest = self._write_checkpoint_context_unlocked(checkpoint)
            summary = checkpoint.model_dump(mode="json")
            summary.update(
                {
                    "context_digest": digest,
                    "inspection": {},
                    "operations": [],
                    "verifier_report": {},
                    "model_response": {},
                    "capability_manifest": {},
                    "dependency_manifest": {},
                }
            )
            compact_checkpoints.append(summary)
        payload = session.model_dump(mode="json", exclude={"checkpoints"})
        payload["checkpoints"] = compact_checkpoints
        _atomic_write(self._state_path, _canonical_mapping_json(payload))


    def _load_commands_unlocked(self) -> list[AECommand]:
        _ensure_no_symlink(self._commands_path, kind="file")
        raw = _read_json(self._commands_path, default=[])
        if not isinstance(raw, list):
            raise CoordinatorConflict("persisted command journal is invalid")
        commands: list[AECommand] = []
        try:
            for item in raw:
                commands.append(AECommand.model_validate_json(canonical_json(item)))
        except (ValidationError, TypeError, ValueError) as exc:
            raise CoordinatorConflict("persisted command journal is invalid") from exc
        for command in commands:
            if command.plan_id != self.plan_id or command.project_id != self.project_id:
                raise CoordinatorConflict("persisted command ownership mismatch")
            if not _command_kind_state_matches(command):
                raise CoordinatorConflict("persisted command kind/state matrix is invalid")
        seen_sequences: set[int] = set()
        for command in commands:
            if command.sequence in seen_sequences:
                raise CoordinatorConflict("persisted command sequence is contradictory")
            seen_sequences.add(command.sequence)
            if _payload_digest(command.payload) != command.payload_digest:
                raise CoordinatorConflict("persisted command payload digest mismatch")
            if command.result is not None and _payload_digest(command.result) != command.result_digest:
                raise CoordinatorConflict("persisted command result digest mismatch")
        return commands

    def _write_commands_unlocked(self, commands: list[AECommand]) -> None:
        payload = [item.model_dump(mode="json") for item in sorted(commands, key=lambda item: item.sequence)]
        _atomic_write(self._commands_path, canonical_json(payload) + b"\n")

    def command_history(self) -> tuple[AECommand, ...]:
        """Return the durable command journal without mutating coordinator state."""
        with self._locked():
            return tuple(sorted(self._load_commands_unlocked(), key=lambda item: item.sequence))

    def get_command(self, command_id: str) -> AECommand:
        command_id = _safe_command_id(command_id)
        with self._locked():
            command = next(
                (item for item in self._load_commands_unlocked() if item.id == command_id),
                None,
            )
            if command is None:
                raise CoordinatorConflict("command was not found")
            return command

    def command(self, command_id: str) -> AECommand:
        return self.get_command(command_id)

    def _assert_current_operation_manifest(self) -> None:
        try:
            current = current_operation_manifest()
        except Exception as exc:
            raise CoordinatorConflict("current operation manifest is unavailable") from exc
        if self.plan.permitted_operations != current:
            raise CoordinatorConflict("plan operation manifest does not match current operation manifest")

    @staticmethod
    def _append_tombstones(
        issued: set[str],
        active: set[str],
        tombstones: set[str],
        operations: tuple[Mapping[str, Any], ...],
    ) -> None:
        for operation in operations:
            identifier = operation.get("layer_instance_id")
            if not isinstance(identifier, str):
                continue
            kind = operation.get("kind")
            if kind == "add_layer" and operation.get("source_element_id") is None:
                issued.add(identifier)
                active.add(identifier)
            elif kind == "remove_layer":
                if identifier in issued or identifier in active:
                    tombstones.add(identifier)
                active.discard(identifier)

    def _instance_id_tombstones_unlocked(
        self,
        session: AESession,
        commands: list[AECommand],
        *,
        extra_checkpoints: tuple[AECheckpoint, ...] = (),
    ) -> tuple[str, ...]:
        """Rebuild source-less ID tombstones from one checkpoint lineage.

        A checkpoint may be present in both the completed command result and
        the session record.  Indexing first makes the inspection snapshot
        unique, while the selected passing checkpoint remains authoritative
        over failed candidates that were later rolled back.
        """
        issued = set(session.issued_instance_id_tombstones)
        tombstones = set(session.issued_instance_id_tombstones)
        active: set[str] = set()

        lineage: dict[int, AECheckpoint] = {}

        def add_checkpoint(checkpoint: AECheckpoint) -> None:
            existing = lineage.get(checkpoint.index)
            if existing is not None and existing != checkpoint:
                if (
                    checkpoint.index == 0
                    and existing.provenance == "baseline"
                    and checkpoint.provenance == "baseline"
                    and not existing.passed
                    and checkpoint.passed
                ):
                    lineage[checkpoint.index] = checkpoint
                    return
                raise CoordinatorConflict("checkpoint lineage contains conflicting records")
            lineage[checkpoint.index] = checkpoint

        for checkpoint in session.checkpoints:
            add_checkpoint(checkpoint)
        for command in sorted(commands, key=lambda item: item.sequence):
            result = command.result
            if result is None or not isinstance(result.get("checkpoint"), Mapping):
                continue
            try:
                checkpoint = _checkpoint_from(result["checkpoint"])
            except CoordinatorConflict:
                # Result validation owns malformed checkpoint errors; this
                # helper only reconstructs durable tombstones.
                continue
            add_checkpoint(checkpoint)
        for checkpoint in extra_checkpoints:
            add_checkpoint(checkpoint)

        operation_events: list[tuple[int, int, tuple[Mapping[str, Any], ...]]] = []
        for command in sorted(commands, key=lambda item: item.sequence):
            if (
                command.kind != "apply_batch"
                or command.status != "completed"
                or command.result is None
                or command.result.get("ok") is False
            ):
                continue
            checkpoint = command.expected_checkpoint
            operation_events.append(
                (
                    -1 if checkpoint is None else checkpoint,
                    command.sequence,
                    _operation_records(command.payload),
                )
            )
        operation_events.sort(key=lambda item: (item[0], item[1]))
        event_index = 0
        checkpoint_active: dict[int, set[str]] = {}
        checkpoint_branch_ids: dict[int, set[str]] = {}
        durable_tombstones: set[str] = set()
        rollback_tombstones: set[str] = set()

        def added_instance_ids(operations: tuple[Mapping[str, Any], ...]) -> set[str]:
            identifiers: set[str] = set()
            for operation in operations:
                if operation.get("kind") != "add_layer":
                    continue
                if operation.get("source_element_id") is not None:
                    continue
                identifier = operation.get("layer_instance_id")
                if isinstance(identifier, str):
                    identifiers.add(identifier)
            return identifiers

        def apply_operations(
            operations: tuple[Mapping[str, Any], ...],
            *,
            rollback: bool,
        ) -> None:
            before = set(tombstones)
            self._append_tombstones(issued, active, tombstones, operations)
            added = tombstones - before
            durable_tombstones.update(added)
            if rollback:
                rollback_tombstones.update(added)

        def inspect(checkpoint: AECheckpoint) -> set[str] | None:
            nonlocal active
            inventory = _inspection_inventory(checkpoint.inspection)
            if inventory is None:
                return None
            removed = active - inventory
            tombstones.update(removed)
            if checkpoint.passed:
                durable_tombstones.update(removed)
            issued.update(inventory)
            active = set(inventory)
            return inventory
        for checkpoint in sorted(lineage.values(), key=lambda item: item.index):
            branch_ids = checkpoint_branch_ids.setdefault(checkpoint.index, set())
            while (
                event_index < len(operation_events)
                and operation_events[event_index][0] < checkpoint.index
            ):
                operations = operation_events[event_index][2]
                branch_ids.update(added_instance_ids(operations))
                apply_operations(operations, rollback=not checkpoint.passed)
                event_index += 1
            branch_ids.update(added_instance_ids(checkpoint.operations))
            apply_operations(checkpoint.operations, rollback=not checkpoint.passed)
            inventory = inspect(checkpoint)
            if inventory is not None:
                branch_ids.update(inventory)
            checkpoint_active[checkpoint.index] = set(active)
        while event_index < len(operation_events):
            apply_operations(operation_events[event_index][2], rollback=False)
            event_index += 1

        # ``selected_checkpoint`` is a UI/finalization choice; the active
        # iteration lineage is the latest passing checkpoint.
        passing = [
            item.index
            for item in lineage.values()
            if item.passed and item.lineage_valid
        ]
        selected_index = max(passing) if passing else None
        authoritative = checkpoint_active.get(selected_index) if selected_index is not None else None
        discarded_branch_ids = {
            identifier
            for index, identifiers in checkpoint_branch_ids.items()
            if index != selected_index
            for identifier in identifiers
        }
        if authoritative is None:
            tombstones.update(discarded_branch_ids)
        else:
            authoritative = set(authoritative)
            issued.update(authoritative)
            # IDs observed only on a discarded branch were still issued by
            # the server and must never be recycled.
            tombstones.update(discarded_branch_ids - authoritative)
            clearable = (
                (tombstones - durable_tombstones) | rollback_tombstones
            ) & authoritative
            tombstones.difference_update(clearable)
        return tuple(sorted(tombstones))

    def _checkpoint_context_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CoordinatorConflict("checkpoint context digest is invalid")
        _ensure_no_symlink(self._checkpoint_context_dir, kind="directory")
        return self._checkpoint_context_dir / f"{digest}.json.gz"

    def _load_checkpoint_context_unlocked(self, digest: str) -> AECheckpoint | None:
        path = self._checkpoint_context_path(digest)
        _ensure_no_symlink(path, kind="file")
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rb") as stream:
                encoded = stream.read()
            checkpoint = AECheckpoint.model_validate_json(encoded)
        except (OSError, EOFError, gzip.BadGzipFile, ValidationError, TypeError, ValueError) as exc:
            raise CoordinatorConflict("persisted checkpoint context is invalid") from exc
        if _checkpoint_context_digest(checkpoint.model_dump(mode="json")) != digest:
            raise CoordinatorConflict("checkpoint context digest mismatch")
        return checkpoint.model_copy(update={"context_digest": digest})

    def _write_checkpoint_context_unlocked(self, checkpoint: AECheckpoint) -> str:
        context = checkpoint.model_copy(update={"context_digest": None})
        encoded = canonical_json(context.model_dump(mode="json"))
        digest = hashlib.sha256(encoded).hexdigest()
        path = self._checkpoint_context_path(digest)
        existing = self._load_checkpoint_context_unlocked(digest)
        if existing is not None:
            if existing.model_copy(update={"context_digest": None}) != context:
                raise CoordinatorConflict("checkpoint context digest is already bound")
            return digest
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.",
            suffix=".tmp",
            dir=str(self._checkpoint_context_dir),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                    for offset in range(0, len(encoded), 1024 * 1024):
                        compressed.write(encoded[offset : offset + 1024 * 1024])
                raw.flush()
                os.fsync(raw.fileno())
            _ensure_no_symlink(path, kind="file")
            os.replace(temporary, path)
            temporary = None
            try:
                directory_fd = os.open(self._checkpoint_context_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except OSError as exc:
            raise CoordinatorConflict("could not persist checkpoint context") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return digest

    def _store_checkpoint_context_unlocked(
        self,
        checkpoint: AECheckpoint,
        *,
        command_id: str,
        session_id: str,
    ) -> str:
        del command_id, session_id
        return self._write_checkpoint_context_unlocked(checkpoint)

    def _checkpoint_context_for_command_unlocked(
        self,
        command: AECommand,
    ) -> AECheckpoint:
        payload = command.payload
        digest = payload.get("checkpoint_context_digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CoordinatorConflict("checkpoint context digest is missing")
        checkpoint = self._load_checkpoint_context_unlocked(digest)
        if checkpoint is None:
            raise CoordinatorConflict("checkpoint context is unavailable")
        return checkpoint

    @staticmethod
    def _result_artifact_ids(result: Mapping[str, Any]) -> set[str]:
        identifiers: set[str] = set()
        raw = result.get("artifacts", result.get("artifact_ids"))
        values: list[Any] = []
        if isinstance(raw, Mapping):
            values.extend(raw.values())
        elif isinstance(raw, (list, tuple)):
            values.extend(raw)
        for key, value in result.items():
            if key.endswith("_artifact_id"):
                values.append(value)
        for value in values:
            if isinstance(value, str):
                identifiers.add(value)
            elif isinstance(value, Mapping):
                for key in ("id", "artifact_id", "reservation_id"):
                    item = value.get(key)
                    if isinstance(item, str):
                        identifiers.add(item)
        return identifiers

    def _validate_checkpoint_result_unlocked(
        self,
        command: AECommand,
        result: Mapping[str, Any],
        checkpoint: AECheckpoint,
    ) -> None:
        expected = {
            item.get("reservation_id")
            for item in command.payload.get("artifacts", ())
            if isinstance(item, Mapping)
            and isinstance(item.get("reservation_id"), str)
        }
        returned = self._result_artifact_ids(result)
        if expected and not expected.issubset(returned):
            raise CoordinatorConflict("checkpoint result is missing committed artifacts")

    def _load_reservations_unlocked(self) -> list[AEArtifactReservation]:
        raw = _read_json(self._reservations_path, default=[])
        if not isinstance(raw, list):
            raise CoordinatorConflict("persisted artifact reservations are invalid")
        try:
            reservations = [
                AEArtifactReservation.model_validate_json(canonical_json(item))
                for item in raw
            ]
        except (ValidationError, TypeError, ValueError) as exc:
            raise CoordinatorConflict("persisted artifact reservations are invalid") from exc
        seen_ids: set[str] = set()
        for reservation in reservations:
            if reservation.plan_id != self.plan_id:
                raise CoordinatorConflict("persisted artifact ownership mismatch")
            if reservation.id in seen_ids:
                raise CoordinatorConflict("persisted artifact reservation is contradictory")
            seen_ids.add(reservation.id)
            if reservation.status == "committed" and (
                reservation.sha256 is None or reservation.length is None
            ):
                raise CoordinatorConflict("committed artifact reservation is incomplete")
        reconciled = False
        for index, reservation in enumerate(reservations):
            if reservation.status != "reserved":
                continue
            candidate = self.artifacts_dir / reservation.id
            if not candidate.exists() and not candidate.is_symlink():
                continue
            _ensure_no_symlink(candidate, kind="file")
            if not candidate.is_file() or candidate.stat().st_size > reservation.max_length:
                raise CoordinatorConflict("reserved artifact destination is invalid")
            digest = hashlib.sha256()
            length = 0
            try:
                with candidate.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        length += len(chunk)
                _validate_artifact_file(reservation.kind, candidate, length)
            except CoordinatorConflict:
                raise
            except OSError as exc:
                raise CoordinatorConflict("reserved artifact destination is unreadable") from exc
            reservations[index] = reservation.model_copy(
                update={"status": "committed", "sha256": digest.hexdigest(), "length": length}
            )
            reconciled = True
        if reconciled:
            self._write_reservations_unlocked(reservations)
        return reservations

    def _write_reservations_unlocked(self, reservations: list[AEArtifactReservation]) -> None:
        payload = [item.model_dump(mode="json") for item in reservations]
        _atomic_write(self._reservations_path, canonical_json(payload) + b"\n")

    @staticmethod
    def _serialize_event(
        event: str,
        before: AESession,
        after: AESession,
        data: Mapping[str, Any],
    ) -> bytes:
        safe_data = _json_safe(data)
        if isinstance(safe_data, Mapping) and isinstance(safe_data.get("checkpoint"), Mapping):
            checkpoint = _checkpoint_from(safe_data["checkpoint"])
            digest = _checkpoint_context_digest(checkpoint.model_dump(mode="json"))
            safe_data = dict(safe_data)
            safe_data["checkpoint"] = {
                "index": checkpoint.index,
                "provenance": checkpoint.provenance,
                "passed": checkpoint.passed,
                "context_digest": digest,
            }
        record = {
            "event": event,
            "before_revision": before.revision,
            "after_revision": after.revision,
            "before_status": before.status,
            "after_status": after.status,
            "data": safe_data,
        }
        try:
            return canonical_json(record) + b"\n"
        except (TypeError, ValueError, OverflowError) as exc:
            raise CoordinatorConflict("could not serialize AE transition") from exc

    def _append_event_bytes_unlocked(self, encoded: bytes) -> None:
        fd: int | None = None
        try:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if os.name == "nt":
                # Windows has no reliable O_NOFOLLOW equivalent; reject a
                # reparse point immediately before opening the journal.
                _ensure_no_symlink(self._events_path)
            else:
                flags |= os.O_NOFOLLOW
            fd = os.open(self._events_path, flags, 0o600)
            with os.fdopen(fd, "ab") as stream:
                fd = None
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise CoordinatorConflict("could not persist AE transition") from exc
        finally:
            if fd is not None:
                os.close(fd)


    def _commit_transition_unlocked(
        self,
        before: AESession,
        after: AESession,
        event: str,
        data: Mapping[str, Any],
    ) -> None:
        encoded = self._serialize_event(event, before, after, data)
        self._append_event_bytes_unlocked(encoded)
        self._write_session_unlocked(after)

    def _recover_on_load(self) -> None:
        if not self._state_path.exists():
            return
        with self._RECOVERY_GUARD:
            with self._locked():
                session = self._load_session_unlocked()
                commands = self._load_commands_unlocked()
                session = self._reconcile_completed_unlocked(session, commands)
                needs_restart_pause = session.status in _ACTIVE_STATES or session.status == "pause_requested"
                self._revoke_queued_commands_unlocked(commands)
                if not needs_restart_pause:
                    return
                recovered = session.model_copy(
                    update={
                        "status": "paused:server_restart",
                        "revision": session.revision + 1,
                        "reason": "server_restart",
                        "updated_at": _now(),
                    }
                )
                self._commit_transition_unlocked(session, recovered, "server_restart", {})

    @staticmethod
    def _command_state_matches(session: AESession, command: AECommand) -> bool:
        if not _command_kind_state_matches(command):
            return False
        allowed_states = _COMMAND_STATES[command.kind]
        state_matches = session.status == command.expected_state
        state_matches = state_matches or (
            session.status == "pause_requested"
            and command.expected_state in allowed_states
        )
        state_matches = state_matches or (
            _paused(session.status)
            and session.reason in {
                "user",
                "disconnect",
                "timeout",
                "unpair",
                "server_restart",
                "vision_unsupported",
                "model_paused",
                "capabilities_changed",
            }
            and command.expected_state in allowed_states
        )
        if not state_matches:
            return False
        if command.expected_state == "manual_edit" and command.kind in {
            "inspect_layers",
            "render_preview",
            "sync_manual",
        }:
            workflow = command.payload.get("workflow")
            if not isinstance(workflow, Mapping):
                return session.manual_attempt_id is None
            attempt = workflow.get("manual_attempt_id")
            epoch = workflow.get("manual_epoch")
            if session.manual_attempt_id is not None and (
                attempt != session.manual_attempt_id or epoch != session.manual_epoch
            ):
                return False
        return command.expected_checkpoint == _command_checkpoint(
            session,
            command.kind,
            command.expected_state,
        )

    def _validate_committed_artifact_unlocked(
        self,
        session: AESession,
        reservations: list[AEArtifactReservation],
        artifact_id: str,
        expected_kind: ArtifactKind,
    ) -> None:
        reservation = next((item for item in reservations if item.id == artifact_id), None)
        if reservation is None or reservation.session_id != session.id:
            raise CoordinatorConflict("checkpoint artifact is not owned by this session")
        if reservation.status != "committed" or reservation.kind != expected_kind:
            raise CoordinatorConflict("checkpoint artifact is not committed with the required kind")
        if reservation.length is None or reservation.sha256 is None:
            raise CoordinatorConflict("checkpoint artifact reservation is incomplete")
        path = self.artifacts_dir / reservation.id
        _ensure_no_symlink(path, kind="file")
        if not path.is_file():
            raise CoordinatorConflict("checkpoint artifact is missing")
        digest = hashlib.sha256()
        length = 0
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    length += len(chunk)
        except OSError as exc:
            raise CoordinatorConflict("checkpoint artifact is unreadable") from exc
        if length != reservation.length or digest.hexdigest() != reservation.sha256:
            raise CoordinatorConflict("checkpoint artifact digest does not match reservation")
        _validate_artifact_file(reservation.kind, path, length)

    def _validate_checkpoint_artifacts_unlocked(
        self,
        session: AESession,
        checkpoint: AECheckpoint,
    ) -> None:
        if not checkpoint.frame_artifact_ids:
            raise CoordinatorConflict("checkpoint requires at least one PNG frame")
        reservations = self._load_reservations_unlocked()
        self._validate_committed_artifact_unlocked(
            session,
            reservations,
            checkpoint.aep_artifact_id,
            "aep",
        )
        self._validate_committed_artifact_unlocked(
            session,
            reservations,
            checkpoint.preview_artifact_id,
            "mp4",
        )
        for artifact_id in checkpoint.frame_artifact_ids:
            self._validate_committed_artifact_unlocked(session, reservations, artifact_id, "png")

    def _validate_manual_preparation_artifacts_unlocked(
        self,
        session: AESession,
        command: AECommand,
    ) -> None:
        records = command.payload.get("artifacts")
        if not isinstance(records, list) or len(records) != 1:
            raise CoordinatorConflict("manual preparation requires one AEP artifact")
        record = records[0]
        if not isinstance(record, Mapping) or record.get("kind") != "aep":
            raise CoordinatorConflict("manual preparation artifact must be an AEP")
        artifact_id = record.get("reservation_id", record.get("id"))
        if not isinstance(artifact_id, str):
            raise CoordinatorConflict("manual preparation artifact id is invalid")
        self._validate_committed_artifact_unlocked(
            session,
            self._load_reservations_unlocked(),
            artifact_id,
            "aep",
        )


    @staticmethod
    def _artifact_kind(label: str) -> ArtifactKind | None:
        normalized = label.lower().replace("-", "_")
        if normalized in {"png", "frame", "frames", "frame_artifact", "frame_artifact_id"}:
            return "png"
        if normalized in {"mp4", "video", "preview", "preview_artifact", "preview_artifact_id"}:
            return "mp4"
        if normalized in {"aep", "project", "project_artifact", "project_artifact_id"}:
            return "aep" if normalized == "aep" else "zip"
        if normalized in {"zip", "package", "package_artifact", "package_artifact_id"}:
            return "zip"
        return None

    def _validate_final_artifacts_unlocked(
        self,
        session: AESession,
        result: Mapping[str, Any],
        *,
        required_override: set[ArtifactKind] | None = None,
    ) -> None:
        refs: list[tuple[str, ArtifactKind | None]] = []
        raw = result.get("artifacts")
        if raw is None:
            raw = result.get("artifact_ids")
        if isinstance(raw, Mapping):
            for label, value in raw.items():
                expected = self._artifact_kind(str(label))
                if isinstance(value, Mapping):
                    artifact_id = value.get("id", value.get("artifact_id"))
                    declared_kind = value.get("kind")
                    if isinstance(declared_kind, str) and declared_kind in _ARTIFACT_KINDS:
                        expected = cast(ArtifactKind, declared_kind)
                else:
                    artifact_id = value
                if isinstance(artifact_id, str):
                    refs.append((artifact_id, expected))
        elif isinstance(raw, (list, tuple)):
            for value in raw:
                if isinstance(value, Mapping):
                    artifact_id = value.get("id", value.get("artifact_id"))
                    declared_kind = value.get("kind")
                    expected = (
                        cast(ArtifactKind, declared_kind)
                        if isinstance(declared_kind, str) and declared_kind in _ARTIFACT_KINDS
                        else None
                    )
                else:
                    artifact_id = value
                    expected = None
                if isinstance(artifact_id, str):
                    refs.append((artifact_id, expected))
        for label, value in result.items():
            if label.endswith("_artifact_id") and isinstance(value, str):
                refs.append((value, self._artifact_kind(label)))
        if isinstance(result.get("artifact_id"), str):
            refs.append((result["artifact_id"], None))
        if not refs:
            raise CoordinatorConflict("final result must include committed artifacts")
        reservations = self._load_reservations_unlocked()
        kinds: set[ArtifactKind] = set()
        for artifact_id, expected_kind in refs:
            reservation = next((item for item in reservations if item.id == artifact_id), None)
            if reservation is None:
                raise CoordinatorConflict("final result references an unknown artifact")
            if expected_kind is not None and reservation.kind != expected_kind:
                raise CoordinatorConflict("final result artifact kind is invalid")
            self._validate_committed_artifact_unlocked(
                session,
                reservations,
                artifact_id,
                reservation.kind,
            )
            kinds.add(reservation.kind)
        required: set[ArtifactKind] = set(required_override or ())
        if required_override is None:
            outputs = self.plan.artifact_contract.get("outputs")
            if isinstance(outputs, (list, tuple)):
                for output in outputs:
                    if isinstance(output, str):
                        kind = self._artifact_kind(output)
                        if kind is not None:
                            required.add(kind)
            if self.plan.mode == "final":
                required.update({"aep", "mp4", "zip"})
        if not required.issubset(kinds):
            raise CoordinatorConflict("final result is missing required committed artifacts")

    def _mark_result_applied(self, session: AESession, command: AECommand, **updates: Any) -> AESession:
        if command.sequence <= session.applied_command_sequence:
            return session
        updates.update(
            {
                "applied_command_sequence": command.sequence,
                "revision": session.revision + 1,
                "updated_at": _now(),
            }
        )
        return session.model_copy(update=updates)

    def _reconcile_completed_unlocked(self, session: AESession, commands: list[AECommand]) -> AESession:
        """Apply completed results that were journaled before their state effect."""
        current = session
        for command in sorted(commands, key=lambda item: item.sequence):
            if (
                command.status != "completed"
                or command.result is None
                or command.sequence <= current.applied_command_sequence
                or not self._command_state_matches(current, command)
            ):
                continue
            updated = self._result_state_unlocked(current, command, commands)
            if updated != current:
                self._commit_transition_unlocked(
                    current,
                    updated,
                    "command_reconcile",
                    {"command_id": command.id},
                )
                current = updated
        return current

    @staticmethod
    def _late_pause_updates(session: AESession) -> dict[str, Any] | None:
        if session.status == "pause_requested":
            if session.checkpoint_required:
                return None
            reason = session.reason or "user"
            return {"status": f"paused:{reason}", "reason": reason}
        if _paused(session.status) and session.reason in {
            "user",
            "disconnect",
            "timeout",
            "unpair",
            "server_restart",
            "vision_unsupported",
            "model_paused",
        }:
            return {}
        return None
    def _inspection_capabilities_match_unlocked(self, result: Mapping[str, Any]) -> bool:
        inspection: Mapping[str, Any] | None = None
        if isinstance(result.get("inspection"), Mapping):
            inspection = result["inspection"]
        elif isinstance(result.get("result"), Mapping) and isinstance(
            result["result"].get("inspection"), Mapping
        ):
            inspection = result["result"]["inspection"]
        elif isinstance(result.get("schema_version"), str):
            inspection = result
        if inspection is None:
            return False
        heartbeat = inspection.get("heartbeat")
        if not isinstance(heartbeat, Mapping):
            return False
        if isinstance(heartbeat.get("capabilities"), Mapping):
            return False
        if heartbeat.get("capability_hash") != self.plan.capability_hash:
            return False
        manifest = self.plan.capability_manifest or {}
        for key in ("version", "major", "host"):
            expected = manifest.get(key)
            if expected is not None and heartbeat.get(key) != expected:
                return False
        return True


    def _result_state_unlocked(
        self,
        session: AESession,
        command: AECommand,
        commands: list[AECommand] | None = None,
    ) -> AESession:
        result = command.result or {}
        history = commands or []
        if command.sequence <= session.applied_command_sequence:
            return session
        if not _command_kind_state_matches(command):
            raise CoordinatorConflict("command kind/state matrix is invalid")

        def failed_result() -> AESession:
            workflow = command.payload.get("workflow")
            manual_failure = command.kind == "sync_manual" or (
                isinstance(workflow, Mapping)
                and workflow.get("stage") in {
                    "manual_inspect",
                    "manual_render",
                    "manual_sync",
                    "manual_prepare",
                }
            )
            common: dict[str, Any] = (
                {"manual_attempt_id": None, "manual_sync_requested": False}
                if manual_failure
                else {}
            )
            if session.checkpoint_required and command.kind in {
                "inspect_layers",
                "render_preview",
                "save_checkpoint",
            }:
                reason = _COMMAND_FAILURE_REASONS[command.kind]
                return self._mark_result_applied(
                    session,
                    command,
                    status=f"paused:{reason}",
                    reason=reason,
                    checkpoint_required=False,
                    **common,
                )
            if session.status == "pause_requested":
                reason = session.reason or "command_failed"
                return self._mark_result_applied(
                    session,
                    command,
                    status=f"paused:{reason}",
                    reason=reason,
                    **common,
                )
            if session.status in _ACTIVE_STATES:
                reason = _COMMAND_FAILURE_REASONS[command.kind]
                return self._mark_result_applied(
                    session,
                    command,
                    status=f"paused:{reason}",
                    reason=reason,
                    **common,
                )
            return self._mark_result_applied(session, command, **common)

        if command.kind == "apply_batch":
            _validate_apply_payload(command.payload, command.expected_state)
            # Applying a batch is deliberately a mutation-only command.  A
            # checkpoint must come from a separate inspect/render/save sequence.
            if "checkpoint" in result:
                raise CoordinatorConflict("apply_batch must not return a checkpoint")
            if result.get("ok") is False:
                return failed_result()
            late_updates = self._late_pause_updates(session)
            user_pause = (
                (session.status == "pause_requested" or _paused(session.status))
                and (session.reason or "user") == "user"
            )
            if (
                session.status not in {"baseline", "iterating", "pause_requested"}
                and not user_pause
                and late_updates is None
            ):
                raise CoordinatorConflict("apply_batch is illegal in the current state")
            updates = dict(late_updates or {})
            if user_pause:
                updates.update(
                    {
                        "status": "pause_requested",
                        "reason": "user",
                        "checkpoint_required": True,
                    }
                )
            workflow = self._baseline_workflow(command.payload)
            if command.expected_state == "baseline" and workflow is not None:
                mapping_digest = workflow.get("mapping_digest")
                batch_index = workflow.get("batch_index")
                batch_count = workflow.get("batch_count")
                if (
                    not isinstance(mapping_digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", mapping_digest)
                    or not isinstance(batch_index, int)
                    or isinstance(batch_index, bool)
                    or batch_index < 0
                    or not isinstance(batch_count, int)
                    or isinstance(batch_count, bool)
                    or batch_count < 0
                    or batch_index >= max(batch_count, 1)
                ):
                    raise CoordinatorConflict("baseline progress metadata is invalid")
                if (
                    session.baseline_mapping_digest is not None
                    and session.baseline_mapping_digest != mapping_digest
                ):
                    raise CoordinatorConflict("baseline mapping digest changed")
                if (
                    session.baseline_batch_count is not None
                    and session.baseline_batch_count != batch_count
                ):
                    raise CoordinatorConflict("baseline batch count changed")
                completed = set(session.baseline_completed_batches)
                completed.add(batch_index)
                updates.update(
                    {
                        "baseline_mapping_digest": mapping_digest,
                        "baseline_batch_count": batch_count,
                        "baseline_completed_batches": tuple(sorted(completed)),
                    }
                )
            tombstones = self._instance_id_tombstones_unlocked(session, history)
            if tombstones != session.issued_instance_id_tombstones:
                updates["issued_instance_id_tombstones"] = tombstones
            return self._mark_result_applied(session, command, **updates)

        if command.kind == "sync_manual":
            _validate_sync_payload(command.payload)
            if not self._manual_attempt_matches(session, command):
                raise CoordinatorConflict("manual command attempt is stale")
            if command.payload.get("prepare_manual") is True:
                if "checkpoint" in result:
                    raise CoordinatorConflict("manual preparation must not return a checkpoint")
                if result.get("ok") is False:
                    return failed_result()
                if session.status != "manual_edit":
                    raise CoordinatorConflict("manual preparation requires manual_edit")
                self._validate_manual_preparation_artifacts_unlocked(session, command)
                return self._mark_result_applied(session, command)

        checkpoint_command = command.kind in {"save_checkpoint", "sync_manual"}
        if checkpoint_command and "checkpoint" in result:
            raise CoordinatorConflict("server checkpoint context is not accepted")
        checkpoint: AECheckpoint | None = None
        if checkpoint_command and result.get("ok") is not False:
            checkpoint = self._checkpoint_context_for_command_unlocked(command)
            self._validate_checkpoint_result_unlocked(command, result, checkpoint)
            self._validate_checkpoint_artifacts_unlocked(session, checkpoint)
            if command.kind == "sync_manual" and not session.baseline_complete:
                checkpoint = checkpoint.model_copy(
                    update={"lineage_valid": False}
                )
                checkpoint = checkpoint.model_copy(
                    update={
                        "context_digest": self._write_checkpoint_context_unlocked(checkpoint)
                    }
                )
            existing_baseline = next(
                (item for item in session.checkpoints if item.index == 0),
                None,
            )
            baseline_incomplete = (
                not session.baseline_complete
                or (existing_baseline is not None and not existing_baseline.passed)
            )
            current_checkpoint = _checkpoint_index(session)
            late_updates = self._late_pause_updates(session)

            capture_pause = (
                session.checkpoint_required
                and session.status in {"pause_requested", "paused:user"}
                and (session.reason or "user") == "user"
            )
            if capture_pause:
                replace_incomplete_baseline = (
                    checkpoint.index == 0
                    and checkpoint.provenance == "baseline"
                    and baseline_incomplete
                )
                if current_checkpoint is None:
                    if checkpoint.index != 0:
                        raise CoordinatorConflict("baseline requires a checkpoint 0")
                elif checkpoint.index <= current_checkpoint and not replace_incomplete_baseline:
                    raise CoordinatorConflict("save_checkpoint requires a new checkpoint")
                checkpoints = self._append_checkpoint(
                    session,
                    checkpoint,
                    replace_incomplete_baseline=replace_incomplete_baseline,
                )
                reason = session.reason or "user"
                updates: dict[str, Any] = {
                    "checkpoints": checkpoints,
                    "open_checkpoint": checkpoint.index,
                    "status": f"paused:{reason}",
                    "reason": reason,
                    "checkpoint_required": False,
                }
                if replace_incomplete_baseline and not checkpoint.passed:
                    updates["selected_checkpoint"] = None
                if checkpoint.index == 0:
                    baseline_complete = command.payload.get("baseline_complete")
                    updates["baseline_complete"] = (
                        baseline_complete
                        if isinstance(baseline_complete, bool)
                        else True
                    )
                if checkpoint.passed:
                    updates["selected_checkpoint"] = checkpoint.index
                tombstones = self._instance_id_tombstones_unlocked(
                    session,
                    history,
                    extra_checkpoints=(checkpoint,),
                )
                if tombstones != session.issued_instance_id_tombstones:
                    updates["issued_instance_id_tombstones"] = tombstones
                return self._mark_result_applied(session, command, **updates)

            if command.kind == "save_checkpoint":
                if late_updates is not None:
                    if command.expected_state == "baseline" and checkpoint.index != 0:
                        raise CoordinatorConflict("baseline requires a checkpoint 0")
                    replace_incomplete_baseline = (
                        command.expected_state == "baseline"
                        and checkpoint.index == 0
                        and checkpoint.provenance == "baseline"
                        and baseline_incomplete
                    )
                    if (
                        current_checkpoint is not None
                        and checkpoint.index <= current_checkpoint
                        and not replace_incomplete_baseline
                    ):
                        raise CoordinatorConflict("save_checkpoint requires a new checkpoint")
                    checkpoints = self._append_checkpoint(
                        session,
                        checkpoint,
                        replace_incomplete_baseline=replace_incomplete_baseline,
                    )
                    updates = dict(late_updates)
                    updates["open_checkpoint"] = checkpoint.index
                    if replace_incomplete_baseline and not checkpoint.passed:
                        updates["selected_checkpoint"] = None
                    if checkpoint.index == 0:
                        baseline_complete = command.payload.get("baseline_complete")
                        updates["baseline_complete"] = (
                            baseline_complete
                            if isinstance(baseline_complete, bool)
                            else True
                        )
                    if checkpoint.passed:
                        updates["selected_checkpoint"] = checkpoint.index
                    tombstones = self._instance_id_tombstones_unlocked(
                        session,
                        history,
                        extra_checkpoints=(checkpoint,),
                    )
                    if tombstones != session.issued_instance_id_tombstones:
                        updates["issued_instance_id_tombstones"] = tombstones
                    return self._mark_result_applied(
                        session,
                        command,
                        checkpoints=checkpoints,
                        **updates,
                    )
                if session.status == "baseline":
                    if checkpoint.index != 0:
                        raise CoordinatorConflict("baseline requires a checkpoint 0")
                    replace_incomplete_baseline = (
                        checkpoint.provenance == "baseline"
                        and baseline_incomplete
                    )
                    checkpoints = self._append_checkpoint(
                        session,
                        checkpoint,
                        replace_incomplete_baseline=replace_incomplete_baseline,
                    )
                    baseline_complete = command.payload.get("baseline_complete")
                    baseline_complete = (
                        baseline_complete
                        if isinstance(baseline_complete, bool)
                        else True
                    )
                    updates: dict[str, Any] = {
                        "checkpoints": checkpoints,
                        "open_checkpoint": checkpoint.index,
                        "baseline_complete": baseline_complete,
                        "status": (
                            "iterating"
                            if checkpoint.passed
                            else "paused:verification_failed"
                        ),
                        "reason": None if checkpoint.passed else "verification_failed",
                    }
                    if replace_incomplete_baseline and not checkpoint.passed:
                        updates["selected_checkpoint"] = None
                    elif checkpoint.passed:
                        updates["selected_checkpoint"] = checkpoint.index
                elif session.status == "iterating":
                    if current_checkpoint is None or checkpoint.index <= current_checkpoint:
                        raise CoordinatorConflict("save_checkpoint requires a new checkpoint")
                    checkpoints = self._append_checkpoint(session, checkpoint)
                    updates = {
                        "checkpoints": checkpoints,
                        "open_checkpoint": checkpoint.index,
                    }
                    if checkpoint.passed:
                        updates["selected_checkpoint"] = checkpoint.index
                else:
                    raise CoordinatorConflict("save_checkpoint is illegal in the current state")
                tombstones = self._instance_id_tombstones_unlocked(
                    session,
                    history,
                    extra_checkpoints=(checkpoint,),
                )
                if tombstones != session.issued_instance_id_tombstones:
                    updates["issued_instance_id_tombstones"] = tombstones
                return self._mark_result_applied(session, command, **updates)

            # A regular sync_manual result is the final manual checkpoint.
            if session.status != "manual_edit" and late_updates is None:
                raise CoordinatorConflict("sync_manual requires manual_edit")
            if checkpoint.provenance != "manual":
                raise CoordinatorConflict("manual sync requires a manual checkpoint")
            if current_checkpoint is not None and checkpoint.index <= current_checkpoint:
                raise CoordinatorConflict("manual sync requires a new checkpoint")
            checkpoints = self._append_checkpoint(session, checkpoint)
            updates = {
                "checkpoints": checkpoints,
                "open_checkpoint": checkpoint.index,
                "manual_sync_requested": False,
                "manual_attempt_id": None,
            }
            if late_updates is not None:
                updates.update(late_updates)
            else:
                updates.update(
                    {
                        "status": (
                            "paused:manual_synced"
                            if checkpoint.passed
                            else "paused:verification_failed"
                        ),
                        "reason": (
                            "manual_synced" if checkpoint.passed else "verification_failed"
                        ),
                    }
                )
            if checkpoint.passed:
                updates["selected_checkpoint"] = checkpoint.index
            tombstones = self._instance_id_tombstones_unlocked(
                session,
                history,
                extra_checkpoints=(checkpoint,),
            )
            if tombstones != session.issued_instance_id_tombstones:
                updates["issued_instance_id_tombstones"] = tombstones
            return self._mark_result_applied(session, command, **updates)

        if result.get("ok") is False:
            return failed_result()

        if command.kind == "open_project":
            workflow = command.payload.get("workflow")
            raw_checkpoint = (
                workflow.get("checkpoint_index")
                if isinstance(workflow, Mapping)
                else command.payload.get("checkpoint_index")
            )
            updates = {
                "open_checkpoint": (
                    raw_checkpoint
                    if isinstance(raw_checkpoint, int) and not isinstance(raw_checkpoint, bool)
                    else None
                )
            }
            return self._mark_result_applied(session, command, **updates)

        if (
            command.kind == "inspect_layers"
            and result.get("ok") is not False
            and not self._inspection_capabilities_match_unlocked(result)
        ):
            return self._mark_result_applied(
                session,
                command,
                status="paused:capabilities_changed",
                reason="capabilities_changed",
                checkpoint_required=False,
            )
        if command.kind in _NO_STATE_RESULT_KINDS:
            return self._mark_result_applied(session, command)

        if command.kind == "render_final":
            late_updates = self._late_pause_updates(session)
            if session.status != "finalizing" and late_updates is None:
                raise CoordinatorConflict("render_final requires finalizing")
            self._validate_final_artifacts_unlocked(session, result, required_override={"mp4"})
            return self._mark_result_applied(session, command, **(late_updates or {}))

        if command.kind == "package_project":
            late_updates = self._late_pause_updates(session)
            if session.status != "finalizing" and late_updates is None:
                raise CoordinatorConflict("package_project requires finalizing")
            self._validate_final_artifacts_unlocked(session, result)
            try:
                validate_render_plan_execution(self.root, self.plan_id, session.execution_id)
            except PlanConflict as exc:
                raise CoordinatorConflict(str(exc)) from exc
            if late_updates is None:
                late_updates = {"status": "done", "reason": None}
            return self._mark_result_applied(session, command, **late_updates)

        raise CoordinatorConflict("command result kind has no state effect")
    @staticmethod
    def _append_checkpoint(
        session: AESession,
        checkpoint: AECheckpoint,
        *,
        replace_incomplete_baseline: bool = False,
    ) -> tuple[AECheckpoint, ...]:
        for index, existing in enumerate(session.checkpoints):
            if existing.index != checkpoint.index:
                continue
            if existing == checkpoint:
                return session.checkpoints
            if (
                replace_incomplete_baseline
                and checkpoint.index == 0
                and existing.provenance == "baseline"
                and checkpoint.provenance == "baseline"
            ):
                descendants = tuple(
                    item.model_copy(update={"lineage_valid": False})
                    for item in session.checkpoints[index + 1 :]
                )
                return (
                    *session.checkpoints[:index],
                    checkpoint,
                    *descendants,
                )
            raise CoordinatorConflict("checkpoint index already contains a different record")
        return (*session.checkpoints, checkpoint)

    @staticmethod
    def _revoke_expired_leases(
        commands: list[AECommand],
        now: float,
    ) -> bool:
        revoked = False
        for index, command in enumerate(commands):
            if (
                command.status == "leased"
                and command.lease_expires_at is not None
                and command.lease_expires_at <= now
            ):
                commands[index] = command.model_copy(update={"status": "revoked"})
                revoked = True
        return revoked

    def _revoke_queued_commands_unlocked(self, commands: list[AECommand]) -> None:
        revoked = False
        for index, command in enumerate(commands):
            if command.status == "queued":
                commands[index] = command.model_copy(update={"status": "revoked"})
                revoked = True
        if revoked:
            self._write_commands_unlocked(commands)

    def _revoke_device_commands_unlocked(
        self,
        commands: list[AECommand],
        device_id: str,
        *,
        include_leased: bool,
    ) -> None:
        revoked = False
        statuses = {"queued", "leased"} if include_leased else {"queued"}
        for index, command in enumerate(commands):
            if command.device_id == device_id and command.status in statuses:
                commands[index] = command.model_copy(update={"status": "revoked"})
                revoked = True
        if revoked:
            self._write_commands_unlocked(commands)

    def _settle_pause_requested_unlocked(
        self,
        session: AESession,
        commands: list[AECommand],
        now: float | None = None,
    ) -> AESession:
        if session.status != "pause_requested":
            return session
        if session.checkpoint_required:
            return session
        current_time = _now() if now is None else _finite_time(now, "time")
        # An expired lease is no longer active, but its result remains
        # admissible until a new manual/replacement branch explicitly revokes it.
        leased = any(
            command.status == "leased"
            and command.lease_expires_at is not None
            and command.lease_expires_at > current_time
            for command in commands
        )
        if leased:
            return session
        self._revoke_queued_commands_unlocked(commands)
        reason = session.reason or "user"
        paused = session.model_copy(
            update={
                "status": f"paused:{reason}",
                "reason": reason,
                "revision": session.revision + 1,
                "updated_at": current_time,
            }
        )
        self._commit_transition_unlocked(session, paused, "pause_complete", {})
        return paused

    def start(self, execution_id: str) -> AESession:
        execution_id = _safe_component(execution_id, "execution id")
        with self._locked():
            try:
                render_state = load_render_plan_state(self.root, self.plan_id)
            except PlanConflict as exc:
                raise CoordinatorConflict(str(exc)) from exc
            if render_state.status != "approved" or render_state.execution_id != execution_id:
                raise CoordinatorConflict("render plan approval does not match execution")
            if self._state_path.exists():
                current = self._load_session_unlocked()
                if current.execution_id == execution_id:
                    return current
                raise CoordinatorConflict("AE session already exists for another execution")
            now = _now()
            session = AESession(
                id="ae-" + secrets.token_urlsafe(16),
                project_id=self.project_id,
                plan_id=self.plan_id,
                plan_digest=self.plan.digest,
                execution_id=execution_id,
                created_at=now,
                updated_at=now,
            )
            self._write_session_unlocked(session)
            self._write_commands_unlocked([])
            self._write_reservations_unlocked([])
            return session

    def state(self) -> AESession:
        with self._locked():
            if not self._state_path.exists():
                raise CoordinatorConflict("AE session has not started")
            session = self._load_session_unlocked(hydrate_checkpoints=False)
            commands = self._load_commands_unlocked()
            session = self._reconcile_completed_unlocked(session, commands)
            session = self._settle_pause_requested_unlocked(session, commands)
            compact = []
            for checkpoint in session.checkpoints:
                digest = checkpoint.context_digest
                if digest is None:
                    digest = self._write_checkpoint_context_unlocked(checkpoint)
                compact.append(
                    checkpoint.model_copy(
                        update={
                            "context_digest": digest,
                            "inspection": {},
                            "operations": (),
                            "verifier_report": {},
                            "model_response": {},
                            "capability_manifest": {},
                            "dependency_manifest": {},
                        }
                    )
                )
            return session.model_copy(update={"checkpoints": tuple(compact)})

    @staticmethod
    def _unsaved_apply_requires_checkpoint(
        session: AESession,
        commands: Sequence[AECommand],
    ) -> bool:
        del session
        saved_sequence: dict[int, int] = {}
        for command in commands:
            if command.kind != "save_checkpoint" or command.status != "completed":
                continue
            if not isinstance(command.result, Mapping) or command.result.get("ok") is False:
                continue
            workflow = command.payload.get("workflow")
            index = workflow.get("checkpoint_index") if isinstance(workflow, Mapping) else None
            if index is None:
                index = command.payload.get("index")
            if isinstance(index, int) and not isinstance(index, bool):
                saved_sequence[index] = max(saved_sequence.get(index, 0), command.sequence)
        for command in commands:
            if command.kind != "apply_batch" or command.status != "completed":
                continue
            if not isinstance(command.result, Mapping) or command.result.get("ok") is False:
                continue
            workflow = command.payload.get("workflow")
            if not isinstance(workflow, Mapping):
                return True
            stage = workflow.get("stage")
            if stage == "baseline_apply":
                capture_sequence = saved_sequence.get(0)
            elif stage == "candidate_apply":
                index = workflow.get("checkpoint_index")
                capture_sequence = (
                    saved_sequence.get(index)
                    if isinstance(index, int) and not isinstance(index, bool)
                    else None
                )
            else:
                continue
            if capture_sequence is None or command.sequence > capture_sequence:
                return True
        return False

    @staticmethod
    def _manual_workflow(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        workflow = payload.get("workflow")
        return workflow if isinstance(workflow, Mapping) else None

    @staticmethod
    def _manual_attempt_matches(
        session: AESession,
        command: AECommand,
    ) -> bool:
        workflow = AECoordinator._manual_workflow(command.payload)
        attempt_id = workflow.get("manual_attempt_id") if isinstance(workflow, Mapping) else None
        return (
            session.manual_attempt_id is not None
            and isinstance(workflow, Mapping)
            and attempt_id == session.manual_attempt_id
            and workflow.get("manual_epoch") == session.manual_epoch
        )

    @staticmethod
    def _baseline_workflow(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        workflow = payload.get("workflow")
        if isinstance(workflow, Mapping) and workflow.get("stage") == "baseline_apply":
            return workflow
        return None

    def _transition_unlocked(self, session: AESession, event: str, data: Mapping[str, Any]) -> AESession:
        if not isinstance(event, str) or not event:
            raise CoordinatorConflict("event is required")
        if session.status in _TERMINAL_STATES:
            raise CoordinatorConflict("terminal AE session cannot transition")
        status = session.status
        updated: AESession | None = None
        if event == "device_ready":
            if status != "waiting_for_connector":
                raise CoordinatorConflict("device_ready is illegal in the current state")
            device_id = data.get("device_id")
            if not isinstance(device_id, str):
                raise CoordinatorConflict("device_id is required")
            device_id = _safe_component(device_id, "device id")
            if session.device_id is not None and session.device_id != device_id:
                raise CoordinatorConflict("device is already bound to this session")
            updated = session.model_copy(
                update={
                    "status": "iterating" if session.baseline_complete else "baseline",
                    "device_id": device_id,
                    "revision": session.revision + 1,
                    "reason": None,
                    "updated_at": _now(),
                }
            )
        elif event in {"baseline_complete", "baseline_success"}:
            if status != "baseline":
                raise CoordinatorConflict("baseline completion is illegal in the current state")
            checkpoint = _checkpoint_from(data.get("checkpoint"))
            if checkpoint.index != 0:
                raise CoordinatorConflict("baseline requires a checkpoint 0")
            self._validate_checkpoint_artifacts_unlocked(session, checkpoint)
            tombstones = self._instance_id_tombstones_unlocked(
                session,
                self._load_commands_unlocked(),
                extra_checkpoints=(checkpoint,),
            )
            existing_baseline = next(
                (item for item in session.checkpoints if item.index == 0),
                None,
            )
            replace_incomplete_baseline = (
                existing_baseline is not None
                and existing_baseline.provenance == "baseline"
                and not session.baseline_complete
            )
            checkpoints = self._append_checkpoint(
                session,
                checkpoint,
                replace_incomplete_baseline=replace_incomplete_baseline,
            )
            updated = session.model_copy(
                update={
                    "status": "iterating" if checkpoint.passed else "paused:verification_failed",
                    "checkpoints": checkpoints,
                    "selected_checkpoint": (
                        checkpoint.index
                        if checkpoint.passed
                        else (None if replace_incomplete_baseline else session.selected_checkpoint)
                    ),
                    "open_checkpoint": (
                        checkpoint.index
                        if checkpoint.passed or replace_incomplete_baseline
                        else session.open_checkpoint
                    ),
                    "issued_instance_id_tombstones": tombstones,
                    "baseline_complete": checkpoint.passed,
                    "reason": None if checkpoint.passed else "verification_failed",
                    "revision": session.revision + 1,
                    "updated_at": _now(),
                }
            )
        elif event == "stop":
            if status not in {"baseline", "iterating"}:
                raise CoordinatorConflict("stop is illegal in the current state")
            commands = self._load_commands_unlocked()
            updated = session.model_copy(
                update={
                    "status": "pause_requested",
                    "reason": "user",
                    "checkpoint_required": self._unsaved_apply_requires_checkpoint(
                        session,
                        commands,
                    ),
                    "revision": session.revision + 1,
                    "updated_at": _now(),
                }
            )
        elif event in {"batch_complete", "active_result", "checkpoint_saved", "command_success", "command_result"}:
            if status not in {"baseline", "iterating", "pause_requested"}:
                raise CoordinatorConflict("active result is illegal in the current state")
            checkpoint_raw = data.get("checkpoint")
            checkpoints = session.checkpoints
            checkpoint: AECheckpoint | None = None
            if status == "baseline":
                if checkpoint_raw is None:
                    raise CoordinatorConflict("baseline requires a checkpoint 0")
                checkpoint = _checkpoint_from(checkpoint_raw)
                if checkpoint.index != 0:
                    raise CoordinatorConflict("baseline requires a checkpoint 0")
                self._validate_checkpoint_artifacts_unlocked(session, checkpoint)
                checkpoints = self._append_checkpoint(session, checkpoint)
            elif checkpoint_raw is not None:
                checkpoint = _checkpoint_from(checkpoint_raw)
                current_checkpoint = _checkpoint_index(session)
                if current_checkpoint is not None and checkpoint.index <= current_checkpoint:
                    raise CoordinatorConflict("candidate checkpoint must be new")
                self._validate_checkpoint_artifacts_unlocked(session, checkpoint)
                checkpoints = self._append_checkpoint(session, checkpoint)
            if status == "pause_requested":
                next_status = "paused:user"
                next_reason = "user"
            elif status == "baseline" and checkpoint is not None and not checkpoint.passed:
                next_status = "paused:verification_failed"
                next_reason = "verification_failed"
            else:
                next_status = "iterating"
                next_reason = None
            tombstones = session.issued_instance_id_tombstones
            if checkpoint is not None:
                tombstones = self._instance_id_tombstones_unlocked(
                    session,
                    self._load_commands_unlocked(),
                    extra_checkpoints=(checkpoint,),
                )
            updated = session.model_copy(
                update={
                    "status": next_status,
                    "checkpoints": checkpoints,
                    "selected_checkpoint": (
                        checkpoint.index
                        if checkpoint is not None and checkpoint.passed
                        else session.selected_checkpoint
                    ),
                    "issued_instance_id_tombstones": tombstones,
                    "revision": session.revision + 1,
                    "reason": next_reason,
                    "updated_at": _now(),
                }
            )
        elif event == "no_progress":
            if status != "iterating":
                raise CoordinatorConflict("no-progress pause is illegal in the current state")
            updated = session.model_copy(
                update={
                    "status": "paused:no_progress",
                    "reason": "no_progress",
                    "revision": session.revision + 1,
                    "updated_at": _now(),
                }
            )
        elif event in {"disconnect", "timeout", "unpair", "capabilities_changed", "verification_failed", "upload_failed", "render_failed", "package_failed", "command_failed", "pause_error"}:
            if status not in _ACTIVE_STATES and not _paused(status) and not (
                event == "capabilities_changed" and status == "waiting_for_connector"
            ):
                raise CoordinatorConflict("pause error is illegal in the current state")
            reason = _status_reason(event, data)
            updated = session.model_copy(
                update={
                    "status": f"paused:{reason}",
                    "reason": reason,
                    "checkpoint_required": (
                        False
                        if event in {"disconnect", "timeout", "unpair"}
                        else session.checkpoint_required
                    ),
                    "revision": session.revision + 1,
                    "updated_at": _now(),
                }
            )
        elif event == "continue":
            if not _paused(status):
                raise CoordinatorConflict("continue requires a paused session")
            device_id = session.device_id
            updated = session.model_copy(
                update={
                    "status": "waiting_for_connector",
                    "device_id": device_id,
                    "reason": None,
                    "revision": session.revision + 1,
                    "updated_at": _now(),
                }
            )
        elif event == "begin_manual":
            if not _paused(status):
                raise CoordinatorConflict("manual edit requires a paused session")
            resume_prepared_manual = False
            if status == "paused:server_restart" and session.manual_attempt_id is not None:
                for command in self._load_commands_unlocked():
                    workflow = self._manual_workflow(command.payload)
                    if (
                        command.kind != "sync_manual"
                        or command.status != "completed"
                        or (command.result or {}).get("ok") is False
                        or not isinstance(workflow, Mapping)
                        or workflow.get("manual_attempt_id") != session.manual_attempt_id
                        or workflow.get("manual_epoch") != session.manual_epoch
                        or command.payload.get("prepare_manual") is not True
                    ):
                        continue
                    try:
                        self._validate_manual_preparation_artifacts_unlocked(session, command)
                    except CoordinatorConflict:
                        continue
                    resume_prepared_manual = True
                    break
            updated = session.model_copy(
                update={
                    "status": "manual_edit",
                    "reason": None,
                    "manual_epoch": (
                        session.manual_epoch
                        if resume_prepared_manual
                        else session.manual_epoch + 1
                    ),
                    "manual_sync_requested": False,
                    "manual_attempt_id": (
                        session.manual_attempt_id
                        if resume_prepared_manual
                        else None
                    ),
                    "revision": session.revision + 1,
                    "updated_at": _now(),
                }
            )
        elif event == "request_manual_sync":
            if status != "manual_edit":
                raise CoordinatorConflict("manual sync requires manual_edit")
            if session.manual_sync_requested or session.manual_attempt_id is not None:
                raise CoordinatorConflict("manual sync attempt is already active")
            updated = session.model_copy(
                update={
                    "manual_sync_requested": True,
                    "revision": session.revision + 1,
                    "updated_at": _now(),
                }
            )
        elif event in {"manual_synced", "sync_manual"}:
            raise CoordinatorConflict("manual sync requires a connector result")
        elif event == "finalize":
            if not _paused(status) or self.plan.mode != "final":
                raise CoordinatorConflict("finalization requires a paused final session")
            if not session.baseline_complete:
                raise CoordinatorConflict("finalization requires a complete baseline lineage")
            selected = session.selected_checkpoint
            if selected is None or not any(item.index == selected and item.passed for item in session.checkpoints):
                raise CoordinatorConflict("a passing checkpoint must be selected before finalization")
            try:
                validate_render_plan_execution(self.root, self.plan_id, session.execution_id)
            except PlanConflict as exc:
                raise CoordinatorConflict(str(exc)) from exc
            updated = session.model_copy(
                update={"status": "finalizing", "reason": None, "revision": session.revision + 1, "updated_at": _now()}
            )
        elif event in {"final_complete", "final_success"}:
            raise CoordinatorConflict("final completion requires a package_project result")
        elif event == "fail":
            if status in _TERMINAL_STATES:
                raise CoordinatorConflict("terminal AE session cannot transition")
            updated = session.model_copy(
                update={"status": "failed", "reason": _status_reason(event, data), "revision": session.revision + 1, "updated_at": _now()}
            )
        else:
            raise CoordinatorConflict("unknown AE transition")
        return updated

    def transition(self, event: str, revision: int, **data: Any) -> AESession:
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise CoordinatorConflict("revision is invalid")
        with self._locked():
            session = self._load_session_unlocked()
            if session.revision != revision:
                raise CoordinatorConflict("revision is stale")
            commands = self._load_commands_unlocked()
            session = self._settle_pause_requested_unlocked(session, commands)
            if event == "begin_manual" and self._revoke_expired_leases(commands, _now()):
                self._write_commands_unlocked(commands)
            if session.revision != revision:
                raise CoordinatorConflict("revision is stale")
            if event == "begin_manual":
                if session.device_id is None:
                    raise CoordinatorConflict("manual edit requires a bound connector")
                if any(item.status in {"queued", "leased"} for item in commands):
                    raise CoordinatorConflict("manual edit requires a quiescent connector")
            if event in {"disconnect", "timeout", "unpair"} and session.status in _ACTIVE_STATES:
                now = _now()
                leased = any(
                    command.status == "leased"
                    and command.lease_expires_at is not None
                    and command.lease_expires_at > now
                    for command in commands
                )
                if leased:
                    reason = _status_reason(event, data)
                    updated = session.model_copy(
                        update={
                            "status": "pause_requested",
                            "reason": reason,
                            "checkpoint_required": False,
                            "revision": session.revision + 1,
                            "updated_at": now,
                        }
                    )
                else:
                    self._revoke_queued_commands_unlocked(commands)
                    updated = self._transition_unlocked(session, event, data)
            else:
                updated = self._transition_unlocked(session, event, data)
            self._commit_transition_unlocked(session, updated, event, data)
            return updated

    def select_checkpoint(self, checkpoint: int, revision: int) -> AESession:
        if not isinstance(checkpoint, int) or isinstance(checkpoint, bool) or checkpoint < 0:
            raise CoordinatorConflict("checkpoint is invalid")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise CoordinatorConflict("revision is invalid")
        with self._locked():
            session = self._load_session_unlocked()
            if session.revision != revision:
                raise CoordinatorConflict("revision is stale")
            commands = self._load_commands_unlocked()
            session = self._reconcile_completed_unlocked(session, commands)
            session = self._settle_pause_requested_unlocked(session, commands)
            if session.revision != revision:
                raise CoordinatorConflict("revision is stale")
            if not _paused(session.status):
                raise CoordinatorConflict("checkpoint selection requires a paused session")
            selected = next((item for item in session.checkpoints if item.index == checkpoint), None)
            if selected is None:
                raise CoordinatorConflict("checkpoint does not exist")
            if not session.baseline_complete and checkpoint != 0:
                raise CoordinatorConflict("checkpoint lineage is incomplete until baseline completion")
            if not selected.passed or not selected.lineage_valid:
                raise CoordinatorConflict("checkpoint did not pass current lineage verification")
            selected_session = session.model_copy(
                update={"selected_checkpoint": checkpoint}
            )
            tombstones = self._instance_id_tombstones_unlocked(selected_session, commands)
            updated = selected_session.model_copy(
                update={
                    "issued_instance_id_tombstones": tombstones,
                    "revision": session.revision + 1,
                    "updated_at": _now(),
                }
            )
            self._commit_transition_unlocked(
                session,
                updated,
                "select_checkpoint",
                {"checkpoint": checkpoint},
            )
            return updated

    def enqueue_command(
        self,
        kind: CommandKind,
        payload: Mapping[str, Any] | None = None,
        *,
        expected_state: str,
        expected_checkpoint: int | None,
        lease_seconds: float = 30.0,
        device_id: str | None = None,
        revision: int | None = None,
    ) -> AECommand:
        if kind not in {
            "heartbeat",
            "create_project",
            "open_project",
            "import_asset",
            "apply_batch",
            "inspect_layers",
            "save_checkpoint",
            "render_preview",
            "render_final",
            "package_project",
            "sync_manual",
        }:
            raise CoordinatorConflict("command kind is invalid")
        _validate_command_kind_state(kind, expected_state)
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise CoordinatorConflict("command payload must be an object")
        payload_dict = dict(payload)
        checkpoint_context: AECheckpoint | None = None
        raw_context = payload_dict.pop("checkpoint_context", None)
        if raw_context is not None:
            if kind not in {"save_checkpoint", "sync_manual"}:
                raise CoordinatorConflict("checkpoint context is invalid for this command")
            checkpoint_context = _checkpoint_from(raw_context)
            supplied_digest = payload_dict.get("checkpoint_context_digest")
            context_digest = _checkpoint_context_digest(
                checkpoint_context.model_dump(mode="json")
            )
            if supplied_digest is not None and supplied_digest != context_digest:
                raise CoordinatorConflict("checkpoint context digest does not match")
            payload_dict["checkpoint_context_digest"] = context_digest
        elif "checkpoint_context_digest" in payload_dict:
            raise CoordinatorConflict("checkpoint context must be provided server-side")
        try:
            if kind == "apply_batch":
                _validate_apply_payload(payload_dict, expected_state)
            elif kind == "sync_manual":
                _validate_sync_payload(payload_dict)
            _validate_command_payload(payload_dict, allow_artifacts=True)
        except CoordinatorConflict:
            raise
        except (TypeError, ValueError, OverflowError) as exc:
            raise CoordinatorConflict("command payload must be finite canonical JSON") from exc
        requested_lease = _finite_time(lease_seconds, "lease")
        if kind in {
            "save_checkpoint",
            "sync_manual",
            "render_preview",
            "render_final",
            "package_project",
        } and requested_lease == 30.0:
            requested_lease = _LONG_COMMAND_LEASE_SECONDS
        if requested_lease <= 0:
            raise CoordinatorConflict("lease is invalid")
        with self._locked():
            session = self._load_session_unlocked()
            commands = self._load_commands_unlocked()
            session = self._reconcile_completed_unlocked(session, commands)
            if revision is not None:
                if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
                    raise CoordinatorConflict("revision is invalid")
                if session.revision != revision:
                    raise CoordinatorConflict("revision is stale")
            if session.status in _TERMINAL_STATES:
                raise CoordinatorConflict("terminal AE session cannot enqueue commands")
            if session.status != expected_state:
                raise CoordinatorConflict("expected state is stale")
            if expected_state == "pause_requested" and (
                kind not in {"inspect_layers", "render_preview", "save_checkpoint"}
                or not session.checkpoint_required
                or (session.reason or "user") != "user"
            ):
                raise CoordinatorConflict("checkpoint capture is not required")
            if expected_checkpoint is not None and (
                not isinstance(expected_checkpoint, int)
                or isinstance(expected_checkpoint, bool)
                or expected_checkpoint < 0
            ):
                raise CoordinatorConflict("expected checkpoint is invalid")
            if expected_checkpoint != _command_checkpoint(session, kind, expected_state):
                raise CoordinatorConflict("expected checkpoint is stale")
            if kind == "apply_batch":
                self._assert_current_operation_manifest()
                tombstones = self._instance_id_tombstones_unlocked(session, commands)
                reused = _operation_instance_ids(payload_dict).intersection(tombstones)
                if reused:
                    raise CoordinatorConflict(
                        "apply_batch references a tombstoned layer instance id"
                    )
            if kind == "sync_manual" and expected_state != "manual_edit":
                raise CoordinatorConflict("sync_manual requires manual_edit")
            if any(item.status in {"queued", "leased"} for item in commands):
                raise CoordinatorConflict("a command is already pending")
            command_id = "cmd-" + secrets.token_urlsafe(16)
            manual_prepare = (
                kind == "sync_manual"
                and payload_dict.get("prepare_manual") is True
            )
            manual_sync = kind == "sync_manual"
            manual_attempt_started = False
            manual_workflow = payload_dict.get("workflow")
            if manual_sync:
                if session.status != "manual_edit":
                    raise CoordinatorConflict("manual preparation requires manual_edit")
                if manual_prepare and session.manual_attempt_id is not None:
                    raise CoordinatorConflict("manual sync attempt is already active")
                if session.manual_attempt_id is None:
                    workflow = dict(manual_workflow) if isinstance(manual_workflow, Mapping) else {}
                    workflow.update(
                        {
                            "stage": "manual_prepare" if manual_prepare else "manual_sync",
                            "manual_epoch": session.manual_epoch,
                            "manual_attempt_id": command_id,
                        }
                    )
                    payload_dict["workflow"] = workflow
                    manual_attempt_started = True
                else:
                    if not isinstance(manual_workflow, Mapping):
                        raise CoordinatorConflict("manual command attempt is missing")
                    if (
                        manual_workflow.get("manual_epoch") != session.manual_epoch
                        or manual_workflow.get("manual_attempt_id") != session.manual_attempt_id
                    ):
                        raise CoordinatorConflict("manual command attempt is stale")
            try:
                _validate_command_payload(payload_dict, allow_artifacts=True)
                digest = _command_payload_digest(payload_dict)
            except CoordinatorConflict:
                raise
            except (TypeError, ValueError, OverflowError) as exc:
                raise CoordinatorConflict("command payload must be finite canonical JSON") from exc
            bound_device = session.device_id
            if bound_device is None:
                raise CoordinatorConflict("a connector device is not bound")
            if device_id is not None and device_id != bound_device:
                raise CoordinatorConflict("device is not bound to this session")
            sequence = max((item.sequence for item in commands), default=0) + 1
            now = _now()
            command = AECommand(
                id=command_id,
                nonce="nonce-" + secrets.token_urlsafe(24),
                project_id=self.project_id,
                plan_id=self.plan_id,
                plan_digest=self.plan.digest,
                session_id=session.id,
                device_id=bound_device,
                kind=kind,
                sequence=sequence,
                expected_state=expected_state,
                expected_checkpoint=expected_checkpoint,
                payload=payload_dict,
                payload_digest=digest,
                lease_seconds=float(requested_lease),
                created_at=now,
            )
            if checkpoint_context is not None:
                self._store_checkpoint_context_unlocked(
                    checkpoint_context,
                    command_id=command.id,
                    session_id=session.id,
                )
            commands.append(command)
            self._write_commands_unlocked(commands)
            updated_session = session.model_copy(
                update={
                    "command_sequence": sequence,
                    "last_command_id": command.id,
                    "manual_attempt_id": (
                        command.id if manual_attempt_started else session.manual_attempt_id
                    ),
                    "updated_at": now,
                }
            )
            self._write_session_unlocked(updated_session)
            return command

    def detach_device(
        self,
        device_id: str,
        *,
        reason: Literal["unpair", "replacement"],
        now: float | None = None,
    ) -> AESession:
        device_id = _safe_component(device_id, "device id")
        if reason not in {"unpair", "replacement"}:
            raise CoordinatorConflict("device detach reason is invalid")
        current_time = _now() if now is None else _finite_time(now, "time")
        with self._locked():
            session = self._load_session_unlocked()
            commands = self._load_commands_unlocked()
            session = self._reconcile_completed_unlocked(session, commands)
            if session.device_id != device_id:
                return session
            leased = any(
                command.device_id == device_id
                and command.status == "leased"
                and command.lease_expires_at is not None
                and command.lease_expires_at > current_time
                for command in commands
            )
            if leased:
                self._revoke_device_commands_unlocked(
                    commands,
                    device_id,
                    include_leased=False,
                )
                if (
                    session.status == "pause_requested"
                    and session.reason == reason
                    and not session.checkpoint_required
                ):
                    return session
                updated = session.model_copy(
                    update={
                        "status": "pause_requested",
                        "reason": reason,
                        "checkpoint_required": False,
                        "revision": session.revision + 1,
                        "updated_at": current_time,
                    }
                )
                self._commit_transition_unlocked(
                    session,
                    updated,
                    "device_detach_requested",
                    {"reason": reason},
                )
                return updated
            self._revoke_device_commands_unlocked(
                commands,
                device_id,
                include_leased=True,
            )
            if session.status in _TERMINAL_STATES:
                return session
            if session.status == "waiting_for_connector":
                status = "waiting_for_connector"
                session_reason = None
            else:
                status = f"paused:{reason}"
                session_reason = reason
            updated = session.model_copy(
                update={
                    "status": status,
                    "reason": session_reason,
                    "checkpoint_required": False,
                    "device_id": None,
                    "open_checkpoint": None,
                    "revision": session.revision + 1,
                    "updated_at": current_time,
                }
            )
            self._commit_transition_unlocked(
                session,
                updated,
                "device_detached",
                {"reason": reason},
            )
            return updated

    def next_command(self, device_id: str, now: float | None = None) -> AECommand | None:
        device_id = _safe_component(device_id, "device id")
        current_time = _now() if now is None else _finite_time(now, "time")
        with self._locked():
            session = self._load_session_unlocked()
            if session.device_id != device_id:
                return None
            commands = self._load_commands_unlocked()
            session = self._reconcile_completed_unlocked(session, commands)
            session = self._settle_pause_requested_unlocked(session, commands, current_time)
            if (
                (session.status == "pause_requested" or _paused(session.status))
                and not session.checkpoint_required
            ):
                return None
            stale_revoked = False
            for index, command in enumerate(commands):
                if command.device_id != device_id or command.status in {"completed", "revoked"}:
                    continue
                if not self._command_state_matches(session, command):
                    commands[index] = command.model_copy(update={"status": "revoked"})
                    stale_revoked = True
                    continue
                if command.kind == "apply_batch":
                    self._assert_current_operation_manifest()
                    _validate_apply_payload(command.payload, command.expected_state)
                    tombstones = self._instance_id_tombstones_unlocked(session, commands)
                    if _operation_instance_ids(command.payload).intersection(tombstones):
                        raise CoordinatorConflict(
                            "apply_batch references a tombstoned layer instance id"
                        )
                if (
                    command.status == "leased"
                    and command.lease_expires_at is not None
                    and command.lease_expires_at > current_time
                ):
                    if stale_revoked:
                        self._write_commands_unlocked(commands)
                    return None
                absolute_deadline = command.created_at + _MAX_COMMAND_LIFETIME_SECONDS
                lease_expires_at = min(
                    current_time + command.lease_seconds,
                    absolute_deadline,
                )
                if lease_expires_at <= current_time:
                    commands[index] = command.model_copy(update={"status": "revoked"})
                    stale_revoked = True
                    continue
                leased = command.model_copy(
                    update={
                        "status": "leased",
                        "lease_expires_at": lease_expires_at,
                        "delivered_at": current_time,
                    }
                )
                commands[index] = leased
                self._write_commands_unlocked(commands)
                return leased
            if stale_revoked:
                self._write_commands_unlocked(commands)
            return None

    def renew_command(
        self,
        device_id: str,
        command_id: str,
        *,
        sequence: int,
        nonce: str,
        now: float | None = None,
    ) -> AECommand:
        device_id = _safe_component(device_id, "device id")
        command_id = _safe_command_id(command_id)
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise CoordinatorConflict("command sequence is invalid")
        if not isinstance(nonce, str) or not nonce or len(nonce) > 512:
            raise CoordinatorConflict("command nonce is invalid")
        current_time = _now() if now is None else _finite_time(now, "time")
        with self._locked():
            session = self._load_session_unlocked()
            commands = self._load_commands_unlocked()
            session = self._reconcile_completed_unlocked(session, commands)
            if session.device_id != device_id:
                raise CoordinatorConflict("device is not bound to this session")
            index = next((i for i, item in enumerate(commands) if item.id == command_id), None)
            if index is None:
                raise CoordinatorConflict("command was not found")
            command = commands[index]
            if command.device_id != device_id or command.sequence != sequence:
                raise CoordinatorConflict("command device or sequence does not match")
            if not secrets.compare_digest(command.nonce, nonce):
                raise CoordinatorConflict("command nonce does not match")
            if command.status != "leased":
                raise CoordinatorConflict("command is not leased")
            if command.lease_expires_at is None or command.lease_expires_at <= current_time:
                raise CoordinatorConflict("command lease has expired")
            if session.status == "pause_requested" and session.reason in {"replacement", "unpair"}:
                raise CoordinatorConflict("command lease is draining")
            if not self._command_state_matches(session, command):
                commands[index] = command.model_copy(update={"status": "revoked"})
                self._write_commands_unlocked(commands)
                raise CoordinatorConflict("command state or checkpoint does not match")
            absolute_deadline = command.created_at + _MAX_COMMAND_LIFETIME_SECONDS
            requested_until = max(
                command.lease_expires_at,
                current_time + command.lease_seconds,
            )
            renewed_until = min(requested_until, absolute_deadline)
            if renewed_until <= current_time:
                raise CoordinatorConflict("command lease renewal ceiling reached")
            if renewed_until == command.lease_expires_at:
                return command
            renewed = command.model_copy(update={"lease_expires_at": renewed_until})
            commands[index] = renewed
            self._write_commands_unlocked(commands)
            return renewed

    def accept_result(
        self,
        device_id: str,
        command_id: str,
        *,
        sequence: int,
        result: Mapping[str, Any],
    ) -> AECommandResult:
        device_id = _safe_component(device_id, "device id")
        command_id = _safe_command_id(command_id)
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise CoordinatorConflict("command sequence is invalid")
        if not isinstance(result, Mapping):
            raise CoordinatorConflict("command result must be an object")
        result_dict = dict(result)
        result_digest = _payload_digest(result_dict)
        with self._locked():
            session = self._load_session_unlocked()
            commands = self._load_commands_unlocked()
            session = self._reconcile_completed_unlocked(session, commands)
            if session.device_id != device_id:
                raise CoordinatorConflict("device is not bound to this session")
            index = next((i for i, item in enumerate(commands) if item.id == command_id), None)
            if index is None:
                raise CoordinatorConflict("command was not found")
            command = commands[index]
            if command.device_id != device_id or command.sequence != sequence:
                raise CoordinatorConflict("command device or sequence does not match")
            if command.status == "completed":
                if command.result_digest != result_digest or command.result != result_dict:
                    raise CoordinatorConflict("conflicting completed command result")
                return AECommandResult(
                    command_id=command.id,
                    device_id=device_id,
                    sequence=sequence,
                    result=command.result or {},
                    result_digest=command.result_digest or result_digest,
                    accepted_at=command.delivered_at or command.created_at,
                )
            if not self._command_state_matches(session, command):
                raise CoordinatorConflict("command state or checkpoint does not match the session")
            if command.status != "leased":
                raise CoordinatorConflict("command is not leased")
            completed = command.model_copy(
                update={
                    "status": "completed",
                    "result": result_dict,
                    "result_digest": result_digest,
                }
            )
            # Validate the complete command-specific effect before journaling it.
            commands_for_result = list(commands)
            commands_for_result[index] = completed
            updated_session = self._result_state_unlocked(
                session,
                completed,
                commands_for_result,
            )
            commands[index] = completed
            # The completed result is durable before its state effect.
            self._write_commands_unlocked(commands)
            if updated_session != session:
                self._commit_transition_unlocked(
                    session,
                    updated_session,
                    "command_result",
                    {"command_id": command.id},
                )
            return AECommandResult(
                command_id=command.id,
                device_id=device_id,
                sequence=sequence,
                result=result_dict,
                result_digest=result_digest,
                accepted_at=completed.delivered_at or completed.created_at,
            )

    def reserve_artifact(self, kind: ArtifactKind, max_length: int) -> AEArtifactReservation:
        if not isinstance(kind, str) or kind not in _ARTIFACT_KINDS:
            raise CoordinatorConflict("artifact kind is not supported")
        if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length <= 0:
            raise CoordinatorConflict("artifact max length is invalid")
        if max_length > 4_294_967_296:
            raise CoordinatorConflict("artifact max length is too large")
        with self._locked():
            session = self._load_session_unlocked()
            if session.status in _TERMINAL_STATES:
                raise CoordinatorConflict("terminal AE session cannot reserve artifacts")
            reservations = self._load_reservations_unlocked()
            now = _now()
            reservation = AEArtifactReservation(
                id="art-" + secrets.token_urlsafe(16),
                plan_id=self.plan_id,
                session_id=session.id,
                kind=kind,
                max_length=max_length,
                created_at=now,
            )
            reservations.append(reservation)
            self._write_reservations_unlocked(reservations)
            return reservation
    def _require_live_artifact_command_unlocked(
        self,
        device_id: str,
        reservation_id: str,
    ) -> None:
        now = _now()
        if not any(
            command.status == "leased"
            and command.device_id == device_id
            and command.lease_expires_at is not None
            and command.lease_expires_at > now
            and _contains_identifier(command.payload, reservation_id)
            for command in self._load_commands_unlocked()
        ):
            raise CoordinatorConflict(
                "artifact reservation is not bound to an active command"
            )



    def publish_artifact(
        self,
        device_id: str,
        reservation_id: str,
        stream: ReadableStream,
        *,
        content_length: int,
        require_live_command: bool = False,
    ) -> AEPublishedArtifact:
        device_id = _safe_component(device_id, "device id")
        reservation_id = _safe_command_id(reservation_id)
        if not isinstance(content_length, int) or isinstance(content_length, bool) or content_length < 0:
            raise CoordinatorConflict("artifact content length is invalid")

        with self._locked():
            session = self._load_session_unlocked()
            if session.status in _TERMINAL_STATES:
                raise CoordinatorConflict(
                    "terminal AE session cannot publish artifacts"
                )
            if session.device_id != device_id:
                raise CoordinatorConflict("device is not bound to this session")
            reservations = self._load_reservations_unlocked()
            index = next(
                (
                    i
                    for i, item in enumerate(reservations)
                    if item.id == reservation_id
                ),
                None,
            )
            if index is None:
                raise CoordinatorConflict("artifact reservation was not found")
            reservation = reservations[index]
            if reservation.session_id != session.id:
                raise CoordinatorConflict(
                    "artifact reservation belongs to another session"
                )
            if require_live_command:
                self._require_live_artifact_command_unlocked(
                    device_id,
                    reservation_id,
                )
            if reservation.status not in {"reserved", "committed"}:
                raise CoordinatorConflict("artifact reservation is not available")
            if content_length > reservation.max_length:
                raise CoordinatorConflict("artifact length exceeds reservation")
            _ensure_no_symlink(self.artifacts_dir, kind="directory")
            final_path = self.artifacts_dir / reservation.id
            if reservation.status == "reserved":
                _ensure_no_symlink(final_path)
                if final_path.exists() or final_path.is_symlink() or _is_reparse(final_path):
                    raise CoordinatorConflict("artifact destination already exists")
            else:
                if reservation.length is None or reservation.sha256 is None:
                    raise CoordinatorConflict("committed artifact reservation is incomplete")
            temporary: Path | None = None
            try:
                fd, raw_name = tempfile.mkstemp(
                    prefix=f".{reservation.id}.",
                    suffix=".upload",
                    dir=self.artifacts_dir,
                )
                os.close(fd)
                temporary = Path(raw_name)
                os.chmod(temporary, 0o600)
            except OSError as exc:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                raise CoordinatorConflict("artifact upload could not be staged") from exc
            if temporary is None:
                raise CoordinatorConflict("artifact upload could not be staged")

        digest = hashlib.sha256()
        total = 0
        try:
            with temporary.open("wb") as target:
                while total < content_length:
                    chunk = stream.read(min(1024 * 1024, content_length - total))
                    if not chunk:
                        raise CoordinatorConflict("artifact stream ended before content length")
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise CoordinatorConflict("artifact stream returned non-bytes")
                    data = bytes(chunk)
                    if len(data) > content_length - total:
                        raise CoordinatorConflict("artifact stream exceeds content length")
                    total += len(data)
                    digest.update(data)
                    target.write(data)
                extra = stream.read(1)
                if extra:
                    raise CoordinatorConflict("artifact stream exceeds content length")
                if total != content_length:
                    raise CoordinatorConflict("artifact length does not match content length")
                target.flush()
                os.fsync(target.fileno())
            _validate_artifact_file(reservation.kind, temporary, total)
            sha256 = digest.hexdigest()

            with self._locked():
                session = self._load_session_unlocked()
                if session.status in _TERMINAL_STATES:
                    raise CoordinatorConflict(
                        "terminal AE session cannot publish artifacts"
                    )
                if session.device_id != device_id:
                    raise CoordinatorConflict("device is not bound to this session")
                reservations = self._load_reservations_unlocked()
                index = next(
                    (
                        i
                        for i, item in enumerate(reservations)
                        if item.id == reservation_id
                    ),
                    None,
                )
                if index is None:
                    raise CoordinatorConflict("artifact reservation was not found")
                reservation = reservations[index]
                if reservation.session_id != session.id:
                    raise CoordinatorConflict(
                        "artifact reservation belongs to another session"
                    )
                if require_live_command:
                    self._require_live_artifact_command_unlocked(
                        device_id,
                        reservation_id,
                    )
                if reservation.status not in {"reserved", "committed"}:
                    raise CoordinatorConflict("artifact reservation is not available")
                if content_length > reservation.max_length:
                    raise CoordinatorConflict("artifact length exceeds reservation")
                _ensure_no_symlink(self.artifacts_dir, kind="directory")
                final_path = self.artifacts_dir / reservation.id
                if reservation.status == "reserved":
                    _ensure_no_symlink(final_path)
                    if final_path.exists() or final_path.is_symlink() or _is_reparse(final_path):
                        raise CoordinatorConflict("artifact destination already exists")
                _validate_artifact_file(reservation.kind, temporary, total)
                if require_live_command:
                    self._require_live_artifact_command_unlocked(
                        device_id,
                        reservation_id,
                    )
                if reservation.status == "committed":
                    if reservation.length is None or reservation.sha256 is None:
                        raise CoordinatorConflict("committed artifact reservation is incomplete")
                    if total != reservation.length:
                        raise CoordinatorConflict(
                            "artifact length does not match committed reservation"
                        )
                    if sha256 != reservation.sha256:
                        raise CoordinatorConflict(
                            "artifact digest does not match committed reservation"
                        )
                    _ensure_no_symlink(final_path, kind="file")
                    if not final_path.is_file():
                        raise CoordinatorConflict("committed artifact is missing")
                    existing_digest = hashlib.sha256()
                    existing_length = 0
                    try:
                        with final_path.open("rb") as existing:
                            while chunk := existing.read(1024 * 1024):
                                existing_digest.update(chunk)
                                existing_length += len(chunk)
                    except OSError as exc:
                        raise CoordinatorConflict("committed artifact is unreadable") from exc
                    if existing_length != reservation.length:
                        raise CoordinatorConflict(
                            "committed artifact length does not match reservation"
                        )
                    if existing_digest.hexdigest() != reservation.sha256:
                        raise CoordinatorConflict(
                            "committed artifact digest does not match reservation"
                        )
                    _validate_artifact_file(reservation.kind, final_path, existing_length)
                    if require_live_command:
                        self._require_live_artifact_command_unlocked(
                            device_id,
                            reservation_id,
                        )
                    temporary.unlink()
                    temporary = None
                    return AEPublishedArtifact(
                        id=reservation.id,
                        reservation_id=reservation.id,
                        plan_id=self.plan_id,
                        session_id=reservation.session_id,
                        kind=reservation.kind,
                        length=reservation.length,
                        sha256=reservation.sha256,
                        mime_type=_MIME[reservation.kind],
                    )
                try:
                    os.link(temporary, final_path, follow_symlinks=False)
                    _fsync_directory(self.artifacts_dir)
                    temporary.unlink()
                    temporary = None
                    _fsync_directory(self.artifacts_dir)
                except OSError as exc:
                    raise CoordinatorConflict("artifact publication was not durable") from exc
                published = AEPublishedArtifact(
                    id=reservation.id,
                    reservation_id=reservation.id,
                    plan_id=self.plan_id,
                    session_id=reservation.session_id,
                    kind=reservation.kind,
                    length=total,
                    sha256=sha256,
                    mime_type=_MIME[reservation.kind],
                )
                reservations[index] = reservation.model_copy(
                    update={"status": "committed", "sha256": sha256, "length": total}
                )
                self._write_reservations_unlocked(reservations)
                return published
        except CoordinatorConflict:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        except (OSError, ValueError, ValidationError) as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise CoordinatorConflict("artifact could not be published") from exc

    def artifact_path(self, artifact_id: str) -> tuple[AEArtifactReservation, Path]:
        artifact_id = _safe_command_id(artifact_id)
        with self._locked():
            _ensure_no_symlink(self.artifacts_dir, kind="directory")
            reservations = self._load_reservations_unlocked()
            reservation = next((item for item in reservations if item.id == artifact_id), None)
            if reservation is None or reservation.status != "committed":
                raise CoordinatorConflict("artifact was not found")
            path = self.artifacts_dir / reservation.id
            _ensure_no_symlink(path, kind="file")
            if not path.is_file():
                raise CoordinatorConflict("artifact was not found")
            if reservation.length is None or reservation.sha256 is None:
                raise CoordinatorConflict("artifact reservation is incomplete")
            digest = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
            except OSError as exc:
                raise CoordinatorConflict("artifact was not found") from exc
            if path.stat().st_size != reservation.length or digest.hexdigest() != reservation.sha256:
                raise CoordinatorConflict("artifact digest does not match reservation")
            _validate_artifact_file(reservation.kind, path, reservation.length)
            return reservation, path

    def open_artifact(
        self,
        artifact_id: str,
    ) -> tuple[AEArtifactReservation, BinaryIO]:
        artifact_id = _safe_command_id(artifact_id)
        with self._locked():
            session = self._load_session_unlocked()
            _ensure_no_symlink(self.artifacts_dir, kind="directory")
            reservations = self._load_reservations_unlocked()
            reservation = next(
                (item for item in reservations if item.id == artifact_id),
                None,
            )
            if (
                reservation is None
                or reservation.status != "committed"
                or reservation.session_id != session.id
                or reservation.length is None
                or reservation.sha256 is None
            ):
                raise CoordinatorConflict("artifact was not found")
            path = self.artifacts_dir / reservation.id
            _ensure_no_symlink(path, kind="file")
            flags = os.O_RDONLY
            if os.name != "nt":
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(path, flags)
                file_stat = os.fstat(fd)
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_size != reservation.length
                ):
                    os.close(fd)
                    raise CoordinatorConflict("artifact was not found")
                stream = os.fdopen(fd, "rb")
            except CoordinatorConflict:
                raise
            except OSError as exc:
                raise CoordinatorConflict("artifact was not found") from exc
            try:
                digest = hashlib.sha256()
                length = 0
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    length += len(chunk)
                if (
                    length != reservation.length
                    or digest.hexdigest() != reservation.sha256
                ):
                    raise CoordinatorConflict(
                        "artifact digest does not match reservation"
                    )
                stream.seek(0)
                return reservation, stream
            except Exception:
                stream.close()
                raise



    def open_asset(self, device_id: str, asset_id: str) -> tuple[Any, BinaryIO]:
        device_id = _safe_component(device_id, "device id")
        asset_id = _safe_command_id(asset_id)
        with self._locked():
            session = self._load_session_unlocked()
            if session.device_id != device_id:
                raise CoordinatorConflict("device is not bound to this session")
            asset = next((item for item in self.plan.assets if item.id == asset_id), None)
            if asset is None:
                raise CoordinatorConflict("asset was not found")
            assets_dir = self.plan_dir / "assets"
            _ensure_no_symlink(assets_dir, kind="directory")
            path = assets_dir / asset.id
            _ensure_no_symlink(path, kind="file")
            flags = os.O_RDONLY
            if os.name != "nt":
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(path, flags)
                stream = os.fdopen(fd, "rb")
            except OSError as exc:
                raise CoordinatorConflict("asset was not found") from exc
            try:
                digest = hashlib.sha256()
                length = 0
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    length += len(chunk)
                if length != asset.length or digest.hexdigest() != asset.sha256:
                    raise CoordinatorConflict("asset digest does not match plan")
                stream.seek(0)
                return asset, stream
            except Exception:
                stream.close()
                raise


__all__ = ["AECoordinator", "CoordinatorConflict"]
