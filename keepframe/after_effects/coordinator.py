from __future__ import annotations

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
from typing import Any, BinaryIO, Iterator, Literal, Mapping, Protocol, cast
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

from .models import (
    AEArtifactReservation,
    AECheckpoint,
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
    "open_project": frozenset({"baseline"}),
    "import_asset": frozenset({"baseline"}),
    "apply_batch": frozenset({"iterating"}),
    "inspect_layers": frozenset({"baseline", "iterating"}),
    "save_checkpoint": frozenset({"baseline", "iterating"}),
    "render_preview": frozenset({"baseline", "iterating"}),
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
_MIME = {
    "png": "image/png",
    "mp4": "video/mp4",
    "aep": "application/vnd.adobe.after-effects",
    "zip": "application/zip",
}


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


def _payload_digest(value: Any) -> str:
    try:
        return hashlib.sha256(canonical_json(value)).hexdigest()
    except (TypeError, ValueError, OverflowError) as exc:
        raise CoordinatorConflict("payload must be finite canonical JSON") from exc

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


def _validate_command_payload(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
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
                if width == 0 or height == 0:
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
    if not session.checkpoints:
        return None
    return max(item.index for item in session.checkpoints)


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
        for path in (self.ae_dir, self.artifacts_dir):
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

    def _load_session_unlocked(self) -> AESession:
        raw = _read_json(self._state_path)
        try:
            session = AESession.model_validate_json(canonical_json(raw))
        except ValidationError as exc:
            raise CoordinatorConflict("persisted AE session is invalid") from exc
        if session.plan_id != self.plan_id or session.project_id != self.project_id:
            raise CoordinatorConflict("persisted AE session ownership mismatch")
        if session.plan_digest != self.plan.digest:
            raise CoordinatorConflict("persisted AE session plan digest mismatch")
        return session

    def _write_session_unlocked(self, session: AESession) -> None:
        _atomic_write(self._state_path, _canonical_mapping_json(session.model_dump(mode="json")))

    def _load_commands_unlocked(self) -> list[AECommand]:
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
        record = {
            "event": event,
            "before_revision": before.revision,
            "after_revision": after.revision,
            "before_status": before.status,
            "after_status": after.status,
            "data": _json_safe(data),
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
                self._revoke_queued_commands_unlocked(commands)
                if session.status not in _ACTIVE_STATES:
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
            and session.reason in {"user", "disconnect", "timeout", "unpair", "server_restart"}
            and command.expected_state in allowed_states
        )
        if not state_matches:
            return False
        return command.expected_checkpoint == _checkpoint_index(session)

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
            updated = self._result_state_unlocked(current, command)
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
            reason = session.reason or "user"
            return {"status": f"paused:{reason}", "reason": reason}
        if _paused(session.status) and session.reason in {
            "user",
            "disconnect",
            "timeout",
            "unpair",
            "server_restart",
        }:
            return {}
        return None

    def _result_state_unlocked(self, session: AESession, command: AECommand) -> AESession:
        result = command.result or {}
        if command.sequence <= session.applied_command_sequence:
            return session
        if not _command_kind_state_matches(command):
            raise CoordinatorConflict("command kind/state matrix is invalid")
        if result.get("ok") is False:
            if session.status == "pause_requested":
                reason = session.reason or "command_failed"
                return self._mark_result_applied(
                    session,
                    command,
                    status=f"paused:{reason}",
                    reason=reason,
                )
            if session.status in _ACTIVE_STATES:
                return self._mark_result_applied(
                    session,
                    command,
                    status="paused:command_failed",
                    reason="command_failed",
                )
            return self._mark_result_applied(session, command)

        if command.kind in _NO_STATE_RESULT_KINDS:
            return self._mark_result_applied(session, command)

        if command.kind == "apply_batch":
            checkpoint = _checkpoint_from(result.get("checkpoint"))
            if not checkpoint.passed or not checkpoint.frame_artifact_ids:
                raise CoordinatorConflict("apply_batch requires a passing checkpoint with PNG frames")
            self._validate_checkpoint_artifacts_unlocked(session, checkpoint)
            current_checkpoint = _checkpoint_index(session)
            if current_checkpoint is None or checkpoint.index <= current_checkpoint:
                raise CoordinatorConflict("apply_batch requires a new checkpoint")
            late_updates = self._late_pause_updates(session)
            if session.status == "iterating":
                return self._mark_result_applied(
                    session,
                    command,
                    checkpoints=self._append_checkpoint(session, checkpoint),
                )
            if late_updates is not None:
                return self._mark_result_applied(
                    session,
                    command,
                    **late_updates,
                    checkpoints=self._append_checkpoint(session, checkpoint),
                )
            raise CoordinatorConflict("apply_batch is illegal in the current state")

        if command.kind == "save_checkpoint":
            checkpoint = _checkpoint_from(result.get("checkpoint"))
            if not checkpoint.passed:
                raise CoordinatorConflict("save_checkpoint requires a passing checkpoint")
            self._validate_checkpoint_artifacts_unlocked(session, checkpoint)
            current_checkpoint = _checkpoint_index(session)
            if session.status == "baseline":
                if checkpoint.index != 0:
                    raise CoordinatorConflict("baseline requires a checkpoint 0")
                return self._mark_result_applied(
                    session,
                    command,
                    status="iterating",
                    checkpoints=self._append_checkpoint(session, checkpoint),
                    reason=None,
                )
            if session.status == "iterating":
                if current_checkpoint is None or checkpoint.index <= current_checkpoint:
                    raise CoordinatorConflict("iterating requires a new passing checkpoint")
                return self._mark_result_applied(
                    session,
                    command,
                    checkpoints=self._append_checkpoint(session, checkpoint),
                )
            late_updates = self._late_pause_updates(session)
            if late_updates is not None:
                if command.expected_state == "baseline" and checkpoint.index != 0:
                    raise CoordinatorConflict("baseline requires a checkpoint 0")
                if current_checkpoint is not None and checkpoint.index <= current_checkpoint:
                    raise CoordinatorConflict("save_checkpoint requires a new checkpoint")
                return self._mark_result_applied(
                    session,
                    command,
                    **late_updates,
                    checkpoints=self._append_checkpoint(session, checkpoint),
                )
            raise CoordinatorConflict("save_checkpoint is illegal in the current state")

        if command.kind == "sync_manual":
            late_updates = self._late_pause_updates(session)
            if session.status != "manual_edit" and late_updates is None:
                raise CoordinatorConflict("sync_manual requires manual_edit")
            checkpoint = _checkpoint_from(result.get("checkpoint"))
            if checkpoint.provenance != "manual" or not checkpoint.passed:
                raise CoordinatorConflict("manual sync requires a passing manual checkpoint")
            current_checkpoint = _checkpoint_index(session)
            if current_checkpoint is not None and checkpoint.index <= current_checkpoint:
                raise CoordinatorConflict("manual sync requires a new checkpoint")
            self._validate_checkpoint_artifacts_unlocked(session, checkpoint)
            if late_updates is None:
                late_updates = {"status": "paused:manual_synced", "reason": "manual_synced"}
            return self._mark_result_applied(
                session,
                command,
                **late_updates,
                checkpoints=self._append_checkpoint(session, checkpoint),
            )

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
    def _append_checkpoint(session: AESession, checkpoint: AECheckpoint) -> tuple[AECheckpoint, ...]:
        if any(item.index == checkpoint.index for item in session.checkpoints):
            existing = next(item for item in session.checkpoints if item.index == checkpoint.index)
            if existing != checkpoint:
                raise CoordinatorConflict("checkpoint index already contains a different record")
            return session.checkpoints
        return (*session.checkpoints, checkpoint)

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
        current_time = _now() if now is None else _finite_time(now, "time")
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
            session = self._load_session_unlocked()
            commands = self._load_commands_unlocked()
            session = self._reconcile_completed_unlocked(session, commands)
            return self._settle_pause_requested_unlocked(session, commands)

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
                    "status": "baseline"
                    if not any(item.index == 0 and item.passed for item in session.checkpoints)
                    else "iterating",
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
            if checkpoint.index != 0 or not checkpoint.passed:
                raise CoordinatorConflict("baseline requires a passing checkpoint 0")
            self._validate_checkpoint_artifacts_unlocked(session, checkpoint)
            updated = session.model_copy(
                update={
                    "status": "iterating",
                    "checkpoints": self._append_checkpoint(session, checkpoint),
                    "revision": session.revision + 1,
                    "reason": None,
                    "updated_at": _now(),
                }
            )
        elif event == "stop":
            if status not in {"baseline", "iterating"}:
                raise CoordinatorConflict("stop is illegal in the current state")
            updated = session.model_copy(
                update={"status": "pause_requested", "revision": session.revision + 1, "updated_at": _now()}
            )
        elif event in {"batch_complete", "active_result", "checkpoint_saved", "command_success", "command_result"}:
            if status not in {"baseline", "iterating", "pause_requested"}:
                raise CoordinatorConflict("active result is illegal in the current state")
            checkpoint_raw = data.get("checkpoint")
            checkpoints = session.checkpoints
            if status == "baseline":
                if checkpoint_raw is None:
                    raise CoordinatorConflict("baseline requires a passing checkpoint 0")
                checkpoint = _checkpoint_from(checkpoint_raw)
                if checkpoint.index != 0 or not checkpoint.passed:
                    raise CoordinatorConflict("baseline requires a passing checkpoint 0")
                self._validate_checkpoint_artifacts_unlocked(session, checkpoint)
                checkpoints = self._append_checkpoint(session, checkpoint)
            elif checkpoint_raw is not None:
                checkpoint = _checkpoint_from(checkpoint_raw)
                if not checkpoint.passed:
                    raise CoordinatorConflict("candidate checkpoint did not pass verification")
                self._validate_checkpoint_artifacts_unlocked(session, checkpoint)
                checkpoints = self._append_checkpoint(session, checkpoint)
            next_status = "paused:user" if status == "pause_requested" else "iterating"
            updated = session.model_copy(
                update={
                    "status": next_status,
                    "checkpoints": checkpoints,
                    "revision": session.revision + 1,
                    "reason": "user" if next_status == "paused:user" else None,
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
            if status not in _ACTIVE_STATES and not _paused(status):
                raise CoordinatorConflict("pause error is illegal in the current state")
            reason = _status_reason(event, data)
            updated = session.model_copy(
                update={
                    "status": f"paused:{reason}",
                    "reason": reason,
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
            updated = session.model_copy(
                update={"status": "manual_edit", "reason": None, "revision": session.revision + 1, "updated_at": _now()}
            )
        elif event in {"manual_synced", "sync_manual"}:
            raise CoordinatorConflict("manual sync requires a connector result")
        elif event == "finalize":
            if not _paused(status) or self.plan.mode != "final":
                raise CoordinatorConflict("finalization requires a paused final session")
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
            session = self._reconcile_completed_unlocked(session, commands)
            session = self._settle_pause_requested_unlocked(session, commands)
            if session.revision != revision:
                raise CoordinatorConflict("revision is stale")
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
            if not selected.passed:
                raise CoordinatorConflict("checkpoint did not pass verification")
            updated = session.model_copy(
                update={"selected_checkpoint": checkpoint, "revision": session.revision + 1, "updated_at": _now()}
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
        try:
            payload_dict = dict(payload)
            _validate_command_payload(payload_dict)
            digest = _payload_digest(payload_dict)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CoordinatorConflict("command payload must be finite canonical JSON") from exc
        _finite_time(lease_seconds, "lease")
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
            if expected_checkpoint is not None and (
                not isinstance(expected_checkpoint, int) or isinstance(expected_checkpoint, bool) or expected_checkpoint < 0
            ):
                raise CoordinatorConflict("expected checkpoint is invalid")
            if expected_checkpoint != _checkpoint_index(session):
                raise CoordinatorConflict("expected checkpoint is stale")
            if kind == "sync_manual" and expected_state != "manual_edit":
                raise CoordinatorConflict("sync_manual requires manual_edit")
            bound_device = session.device_id
            if bound_device is None:
                raise CoordinatorConflict("a connector device is not bound")
            if device_id is not None and device_id != bound_device:
                raise CoordinatorConflict("device is not bound to this session")
            if any(item.status in {"queued", "leased"} for item in commands):
                raise CoordinatorConflict("a command is already pending")
            sequence = max((item.sequence for item in commands), default=0) + 1
            now = _now()
            command = AECommand(
                id="cmd-" + secrets.token_urlsafe(16),
                nonce="nonce-" + secrets.token_urlsafe(24),
                project_id=self.project_id,
                plan_id=self.plan_id,
                session_id=session.id,
                device_id=bound_device,
                kind=kind,
                sequence=sequence,
                expected_state=expected_state,
                expected_checkpoint=expected_checkpoint,
                payload=payload_dict,
                payload_digest=digest,
                lease_seconds=float(lease_seconds),
                created_at=now,
            )
            commands.append(command)
            self._write_commands_unlocked(commands)
            updated_session = session.model_copy(
                update={
                    "command_sequence": sequence,
                    "last_command_id": command.id,
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
                if session.status == "pause_requested" and session.reason == reason:
                    return session
                updated = session.model_copy(
                    update={
                        "status": "pause_requested",
                        "reason": reason,
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
                    "device_id": None,
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
            if session.status == "pause_requested" or _paused(session.status):
                return None
            for index, command in enumerate(commands):
                if command.device_id != device_id or command.status in {"completed", "revoked"}:
                    continue
                if not self._command_state_matches(session, command):
                    continue
                if (
                    command.status == "leased"
                    and command.lease_expires_at is not None
                    and command.lease_expires_at > current_time
                ):
                    return None
                leased = command.model_copy(
                    update={
                        "status": "leased",
                        "lease_expires_at": current_time + command.lease_seconds,
                        "delivered_at": current_time,
                    }
                )
                commands[index] = leased
                self._write_commands_unlocked(commands)
                return leased
            return None

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
            updated_session = self._result_state_unlocked(session, completed)
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
            if reservation.status == "committed":
                raise CoordinatorConflict("artifact is already committed")
            if content_length > reservation.max_length:
                raise CoordinatorConflict("artifact length exceeds reservation")
            _ensure_no_symlink(self.artifacts_dir, kind="directory")
            final_path = self.artifacts_dir / reservation.id
            _ensure_no_symlink(final_path)
            if final_path.exists() or final_path.is_symlink() or _is_reparse(final_path):
                raise CoordinatorConflict("artifact destination already exists")
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
                if reservation.status == "committed":
                    raise CoordinatorConflict("artifact is already committed")
                if reservation.status != "reserved":
                    raise CoordinatorConflict("artifact reservation is not available")
                if content_length > reservation.max_length:
                    raise CoordinatorConflict("artifact length exceeds reservation")
                _ensure_no_symlink(self.artifacts_dir, kind="directory")
                final_path = self.artifacts_dir / reservation.id
                _ensure_no_symlink(final_path)
                if final_path.exists() or final_path.is_symlink() or _is_reparse(final_path):
                    raise CoordinatorConflict("artifact destination already exists")
                _validate_artifact_file(reservation.kind, temporary, total)
                if require_live_command:
                    self._require_live_artifact_command_unlocked(
                        device_id,
                        reservation_id,
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
