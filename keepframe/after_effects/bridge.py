from __future__ import annotations

"""The private file bridge between the connector MCP process and AE panel.

Only this module knows the bridge filenames.  Every record is finite canonical
JSON, written through a sibling temporary file, and bound to a random nonce so
an old panel result cannot satisfy a new command.
"""
import hashlib
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


BRIDGE_SCHEMA_VERSION = 1
MAX_BRIDGE_JSON_BYTES = 1 * 1024 * 1024
MAX_BRIDGE_STRING_LENGTH = 4096
_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REASON_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

FIXED_KINDS = frozenset(
    {
        "capability_heartbeat",
        "create_or_open_project",
        "import_server_asset",
        "apply_operation_batch",
        "inspect_mapped_layers",
        "save_checkpoint",
        "render_preview",
        "render_final",
        "package_project",
    }
)
BridgeKind = Literal[
    "capability_heartbeat",
    "create_or_open_project",
    "import_server_asset",
    "apply_operation_batch",
    "inspect_mapped_layers",
    "save_checkpoint",
    "render_preview",
    "render_final",
    "package_project",
]


class BridgeError(RuntimeError):
    """Base error for invalid or unavailable bridge records."""


class BridgeBusy(BridgeError):
    """A command is already in flight."""


class BridgeConflict(BridgeError):
    """A nonce, digest, command, or result does not match."""


class BridgeUnavailable(BridgeError):
    """The root or one of its records is not safe to use."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 64:
        raise ValueError("bridge JSON nesting is too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > MAX_BRIDGE_STRING_LENGTH:
            raise ValueError("bridge string is too long")
        if value.lower().startswith(("http:", "https:", "file:", "javascript:", "data:")):
            raise ValueError("bridge payload may not contain URLs")
        return value
    if isinstance(value, int):
        if abs(value) > 1_000_000:
            raise ValueError("bridge number exceeds the absolute ceiling")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > 1_000_000:
            raise ValueError("bridge number must be finite and bounded")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > MAX_BRIDGE_STRING_LENGTH:
                raise ValueError("bridge object keys must be bounded strings")
            if key.lower() in {
                "argv",
                "code",
                "command",
                "executable",
                "expression",
                "file",
                "file_path",
                "filepath",
                "path",
                "script",
                "uri",
                "url",
            }:
                raise ValueError(f"bridge payload key {key!r} is not allowed")
            result[key] = _safe_value(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_safe_value(item, depth=depth + 1) for item in value]
    raise ValueError("bridge payload must be JSON data")


def canonical_bridge_json(value: Any) -> bytes:
    safe = _safe_value(value)
    try:
        encoded = json.dumps(
            safe,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("bridge payload is not finite JSON") from exc
    if len(encoded) > MAX_BRIDGE_JSON_BYTES:
        raise ValueError("bridge JSON exceeds 1 MiB")
    return encoded


def payload_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bridge_json(value)).hexdigest()


def _record_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _COMMAND_ID_RE.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


class BridgeCommand(_Record):
    schema_version: Literal[1] = BRIDGE_SCHEMA_VERSION
    command_id: str
    nonce: str
    kind: BridgeKind
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_digest: str

    _command_id = field_validator("command_id", "nonce")(
        lambda value, info: _record_id(value, label=info.field_name)
    )
    _payload_digest = field_validator("payload_digest")(
        lambda value: _digest(value, label="payload digest")
    )

    @field_validator("payload")
    @classmethod
    def _payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Round-trip through the bounded walker to reject non-JSON values.
        normalized = _safe_value(value)
        if not isinstance(normalized, dict):  # pragma: no cover - Pydantic already enforces this
            raise ValueError("command payload must be an object")
        return normalized

    @model_validator(mode="after")
    def _matching_digest(self) -> "BridgeCommand":
        if payload_digest(self.payload) != self.payload_digest:
            raise ValueError("payload digest does not match payload")
        return self


class BridgeResult(_Record):
    schema_version: Literal[1] = BRIDGE_SCHEMA_VERSION
    command_id: str
    nonce: str
    kind: BridgeKind | None = None
    payload_digest: str | None = Field(
        default=None,
        validation_alias=AliasChoices("payload_digest", "command_payload_digest"),
    )
    ok: bool = True
    result: dict[str, Any] = Field(default_factory=dict)
    result_digest: str | None = None
    error: str | None = None

    _command_id = field_validator("command_id", "nonce")(
        lambda value, info: _record_id(value, label=info.field_name)
    )

    @field_validator("payload_digest", "result_digest")
    @classmethod
    def _digest_fields(cls, value: str | None, info) -> str | None:
        return None if value is None else _digest(value, label=info.field_name)

    @field_validator("result")
    @classmethod
    def _result(cls, value: dict[str, Any]) -> dict[str, Any]:
        normalized = _safe_value(value)
        if not isinstance(normalized, dict):  # pragma: no cover
            raise ValueError("bridge result must be an object")
        return normalized

    @field_validator("error")
    @classmethod
    def _error(cls, value: str | None) -> str | None:
        if value is not None and (not isinstance(value, str) or len(value) > MAX_BRIDGE_STRING_LENGTH):
            raise ValueError("bridge error is too long")
        return value

    @model_validator(mode="after")
    def _matching_digest(self) -> "BridgeResult":
        expected = payload_digest(self.result)
        if self.result_digest is None:
            object.__setattr__(self, "result_digest", expected)
        elif self.result_digest != expected:
            raise ValueError("result digest does not match result")
        return self

class _StopRecord(_Record):
    schema_version: Literal[1] = BRIDGE_SCHEMA_VERSION
    requested: bool = True
    reason: str
    command_id: str | None = None
    nonce: str | None = None

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        if not isinstance(value, str) or not _SAFE_REASON_RE.fullmatch(value):
            raise ValueError("stop reason is invalid")
        return value

    @field_validator("command_id", "nonce")
    @classmethod
    def _optional_id(cls, value: str | None, info) -> str | None:
        return None if value is None else _record_id(value, label=info.field_name)


class Bridge:
    """Serialized one-command bridge rooted in a private local directory."""

    command_filename = "command.json"
    result_filename = "result.json"
    completed_directory = "completed"
    stop_filename = "stop.json"

    def __init__(self, root: str | os.PathLike[str], *, max_json_bytes: int = MAX_BRIDGE_JSON_BYTES) -> None:
        if not isinstance(max_json_bytes, int) or max_json_bytes <= 0 or max_json_bytes > MAX_BRIDGE_JSON_BYTES:
            raise ValueError("max_json_bytes is invalid")
        self.root = Path(root)
        self.max_json_bytes = max_json_bytes
        self._lock = threading.RLock()
        self.completed_root = self.root / self.completed_directory
        self._prepare_root()

    @staticmethod
    def payload_digest(value: Any) -> str:
        return payload_digest(value)

    def _prepare_root(self) -> None:
        self._reject_reparse_components(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._reject_reparse_components(self.root)
        root_stat = self._stat_without_follow(self.root)
        if root_stat is None or not stat.S_ISDIR(root_stat.st_mode):
            raise BridgeUnavailable("bridge root is not a private directory")
        self._reject_reparse_components(self.completed_root)
        self.completed_root.mkdir(exist_ok=True)
        self._reject_reparse_components(self.completed_root)
        completed_stat = self._stat_without_follow(self.completed_root)
        if completed_stat is None or not stat.S_ISDIR(completed_stat.st_mode):
            raise BridgeUnavailable("completed bridge directory is not private")
        try:
            os.chmod(self.root, 0o700)
            os.chmod(self.completed_root, 0o700)
        except OSError:
            # Windows ACLs are applied by the connector installer; chmod is a
            # useful best effort on POSIX without making the protocol unusable.
            pass

    @staticmethod
    def _stat_without_follow(path: Path) -> os.stat_result | None:
        try:
            return path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BridgeUnavailable("cannot inspect bridge path") from exc

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        stat_result = Bridge._stat_without_follow(path)
        if stat_result is None:
            return False
        if stat.S_ISLNK(getattr(stat_result, "st_mode", 0)):
            return True
        try:
            is_junction = getattr(path, "is_junction", None)
            if callable(is_junction) and is_junction():
                return True
        except OSError as exc:
            raise BridgeUnavailable("cannot inspect bridge path") from exc
        return bool(getattr(stat_result, "st_file_attributes", 0) & 0x0400)

    @classmethod
    def _reject_reparse_components(cls, path: Path) -> None:
        for candidate in (path, *path.parents):
            if cls._is_reparse(candidate):
                raise BridgeUnavailable("bridge path contains a reparse point")

    def _path(self, filename: str) -> Path:
        path = self.root / filename
        if path.parent != self.root:
            raise BridgeUnavailable("bridge path escaped root")
        self._reject_reparse_components(self.root)
        if self._is_reparse(path):
            raise BridgeUnavailable(f"bridge record {filename} is a reparse point")
        return path

    def _completed_path(self, command_id: str, nonce: str) -> Path:
        try:
            safe_command_id = _record_id(command_id, label="command_id")
            safe_nonce = _record_id(nonce, label="nonce")
        except ValueError as exc:
            raise BridgeConflict("completed result identity is invalid") from exc
        path = self.completed_root / f"{safe_command_id}--{safe_nonce}.json"
        if path.parent != self.completed_root:
            raise BridgeUnavailable("completed bridge path escaped root")
        self._reject_reparse_components(self.completed_root)
        if self._is_reparse(path):
            raise BridgeUnavailable("completed bridge record is a reparse point")
        return path

    @contextmanager
    def _exclusive_process_lock(self):
        """Serialize writers across Bridge instances/processes."""
        lock_path = self._path(".bridge.lock")
        fd: int | None = None
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            raise BridgeUnavailable("cannot lock bridge root") from exc
        finally:
            if fd is not None:
                os.close(fd)

    def _read_bytes(self, path: Path) -> bytes | None:
        self._reject_reparse_components(path)
        stat_result = self._stat_without_follow(path)
        if stat_result is None:
            return None
        if self._is_reparse(path) or not stat.S_ISREG(stat_result.st_mode):
            raise BridgeUnavailable(f"bridge record {path.name} is not a regular file")
        try:
            size = stat_result.st_size
            if size > self.max_json_bytes:
                raise ValueError("bridge record exceeds the JSON bound")
            data = path.read_bytes()
        except OSError as exc:
            raise BridgeUnavailable(f"cannot read bridge record {path.name}") from exc
        if len(data) > self.max_json_bytes:
            raise ValueError("bridge record exceeds the JSON bound")
        return data

    def _read_json(self, path: Path) -> Any | None:
        raw = self._read_bytes(path)
        if raw is None:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
            _safe_value(value)
            return value
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BridgeConflict(f"bridge record {path.name} is invalid") from exc

    def _atomic_write(self, path: Path, data: bytes) -> None:
        if len(data) > self.max_json_bytes:
            raise ValueError("bridge record exceeds the JSON bound")
        if path.parent != self.root:
            raise BridgeUnavailable("bridge write escaped root")
        self._reject_reparse_components(self.root)
        self._reject_reparse_components(path)
        temp = self.root / f".{path.name}.{secrets.token_hex(8)}.tmp"
        self._reject_reparse_components(temp)
        fd: int | None = None
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._reject_reparse_components(self.root)
            self._reject_reparse_components(temp)
            self._reject_reparse_components(path)
            os.replace(temp, path)
            self._reject_reparse_components(self.root)
            try:
                directory_fd = os.open(self.root, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise BridgeUnavailable(f"cannot atomically write bridge record {path.name}") from exc
        finally:
            if fd is not None:
                os.close(fd)
            try:
                self._reject_reparse_components(self.root)
                if self._stat_without_follow(temp) is not None:
                    temp.unlink()
            except (BridgeUnavailable, OSError):
                pass

    def _atomic_write_completed(self, path: Path, data: bytes) -> None:
        if len(data) > self.max_json_bytes:
            raise ValueError("bridge record exceeds the JSON bound")
        if path.parent != self.completed_root:
            raise BridgeUnavailable("completed bridge write escaped root")
        self._reject_reparse_components(self.completed_root)
        self._reject_reparse_components(path)
        temp = self.completed_root / f".{path.name}.{secrets.token_hex(8)}.tmp"
        self._reject_reparse_components(temp)
        fd: int | None = None
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._reject_reparse_components(self.completed_root)
            self._reject_reparse_components(temp)
            self._reject_reparse_components(path)
            try:
                os.link(temp, path)
            except FileExistsError:
                existing = self._read_bytes(path)
                if existing != data:
                    raise BridgeConflict("completed result record conflicts")
            except OSError as exc:
                self._reject_reparse_components(path)
                if self._stat_without_follow(path) is not None:
                    existing = self._read_bytes(path)
                    if existing != data:
                        raise BridgeConflict("completed result record conflicts")
                else:
                    raise BridgeUnavailable("filesystem cannot atomically publish completed record") from exc
            self._reject_reparse_components(self.completed_root)
            try:
                directory_fd = os.open(self.completed_root, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise BridgeUnavailable(f"cannot atomically write completed record {path.name}") from exc
        finally:
            if fd is not None:
                os.close(fd)
            try:
                self._reject_reparse_components(self.completed_root)
                if self._stat_without_follow(temp) is not None:
                    temp.unlink()
            except (BridgeUnavailable, OSError):
                pass


    def _command_from_file(self) -> BridgeCommand | None:
        raw = self._read_json(self._path(self.command_filename))
        if raw is None:
            return None
        try:
            return BridgeCommand.model_validate(raw)
        except ValueError as exc:
            raise BridgeConflict("bridge command record is invalid") from exc

    def _result_from_file(self) -> BridgeResult | None:
        raw = self._read_json(self._path(self.result_filename))
        if raw is None:
            return None
        try:
            return BridgeResult.model_validate(raw)
        except ValueError as exc:
            raise BridgeConflict("bridge result record is invalid") from exc

    def _journal(self) -> list[BridgeResult]:
        records: list[BridgeResult] = []
        self._reject_reparse_components(self.completed_root)
        try:
            entries = list(self.completed_root.iterdir())
        except OSError as exc:
            raise BridgeUnavailable("cannot scan completed bridge records") from exc
        for path in entries:
            if path.name.startswith(".") or path.suffix != ".json":
                continue
            self._reject_reparse_components(path)
            path_stat = self._stat_without_follow(path)
            if path_stat is None or not stat.S_ISREG(path_stat.st_mode):
                raise BridgeConflict("completed bridge record is not a regular file")
            raw = self._read_json(path)
            if raw is None:
                continue
            try:
                result = BridgeResult.model_validate(raw)
                expected = self._completed_path(result.command_id, result.nonce)
                if expected != path:
                    raise BridgeConflict("completed bridge record name does not match identity")
                records.append(result)
            except (TypeError, ValueError, BridgeConflict) as exc:
                if isinstance(exc, BridgeConflict):
                    raise
                raise BridgeConflict("completed bridge record is invalid") from exc
        return records

    def _write_completed(self, result: BridgeResult) -> None:
        encoded = canonical_bridge_json(result.model_dump(mode="json"))
        self._atomic_write_completed(
            self._completed_path(result.command_id, result.nonce),
            encoded,
        )

    def _journal_result(
        self,
        command_id: str,
        nonce: str,
        *,
        kind: str | None = None,
        command_digest: str | None = None,
    ) -> BridgeResult | None:
        for result in reversed(self._journal()):
            if result.command_id == command_id:
                if result.nonce != nonce:
                    raise BridgeConflict("completed result nonce does not match")
                if (
                    kind is not None
                    and (
                        result.kind != kind
                        or result.payload_digest != command_digest
                    )
                ):
                    raise BridgeConflict("completed command payload conflicts")
                return result
        return None

    def write_command(
        self,
        kind: str | BridgeCommand,
        payload: Mapping[str, Any] | None = None,
        *,
        command_id: str | None = None,
        nonce: str | None = None,
    ) -> BridgeCommand | BridgeResult:
        if isinstance(kind, BridgeCommand):
            if payload is not None or command_id is not None or nonce is not None:
                raise BridgeConflict("a BridgeCommand cannot be combined with command fields")
            command = kind
            kind = command.kind
            payload = command.payload
            command_id = command.command_id
            nonce = command.nonce
        if kind not in FIXED_KINDS:
            raise BridgeConflict("bridge command kind is not fixed")
        normalized_payload = dict(payload or {})
        command_id = command_id or f"command-{secrets.token_hex(16)}"
        nonce = nonce or secrets.token_hex(16)
        try:
            command = BridgeCommand(
                command_id=command_id,
                nonce=nonce,
                kind=cast(BridgeKind, kind),
                payload=normalized_payload,
                payload_digest=payload_digest(normalized_payload),
            )
        except (TypeError, ValueError) as exc:
            raise BridgeConflict("bridge command is invalid") from exc
        with self._lock:
            with self._exclusive_process_lock():
                replay = self._journal_result(
                    command.command_id,
                    command.nonce,
                    kind=command.kind,
                    command_digest=command.payload_digest,
                )
                if replay is not None:
                    # A completed command is idempotent only when its kind/payload
                    # are unchanged; a nonce alone must not authorize another action.
                    return replay
                active = self._command_from_file()
                completed_file_result = self._result_from_file()
                if (
                    active is not None
                    and completed_file_result is not None
                    and active.command_id == completed_file_result.command_id
                    and active.nonce == completed_file_result.nonce
                ):
                    self._path(self.command_filename).unlink(missing_ok=True)
                    self._path(self.result_filename).unlink(missing_ok=True)
                    active = None
                if active is not None:
                    if active.command_id == command.command_id and active.nonce == command.nonce:
                        if active.kind != command.kind or active.payload_digest != command.payload_digest:
                            raise BridgeConflict("in-flight command payload conflicts")
                        return active
                    raise BridgeBusy("one bridge command is already in flight")
                stale_result = self._result_from_file()
                if stale_result is not None:
                    self._path(self.result_filename).unlink(missing_ok=True)
                encoded = canonical_bridge_json(command.model_dump(mode="json"))
                self._atomic_write(self._path(self.command_filename), encoded)
                return command

    def read_command(self) -> BridgeCommand | None:
        with self._lock:
            return self._command_from_file()

    def write_result(
        self,
        command_id: str | BridgeResult,
        nonce: str | None = None,
        result: Mapping[str, Any] | BridgeResult | None = None,
        *,
        ok: bool = True,
        error: str | None = None,
        result_digest: str | None = None,
    ) -> BridgeResult:
        if isinstance(command_id, BridgeResult):
            if nonce is not None or result is not None:
                raise BridgeConflict("a BridgeResult cannot be combined with result fields")
            candidate_input = command_id
            command_id = candidate_input.command_id
            nonce = candidate_input.nonce
            result = candidate_input
            ok = candidate_input.ok
            error = candidate_input.error
            result_digest = candidate_input.result_digest
        if nonce is None or result is None:
            raise BridgeConflict("result identity and payload are required")
        with self._lock:
            with self._exclusive_process_lock():
                try:
                    command_id = _record_id(command_id, label="command_id")
                    nonce = _record_id(nonce, label="nonce")
                except ValueError as exc:
                    raise BridgeConflict("result identity is invalid") from exc
                replay = self._journal_result(command_id, nonce)
                if replay is not None:
                    if isinstance(result, BridgeResult):
                        candidate = result
                    else:
                        candidate = BridgeResult(
                            command_id=command_id,
                            nonce=nonce,
                            ok=ok,
                            result=dict(result),
                            result_digest=result_digest,
                            error=error,
                        )
                    if (
                        replay.result_digest != candidate.result_digest
                        or replay.ok != candidate.ok
                        or replay.error != candidate.error
                        or replay.result != candidate.result
                        or (
                            candidate.kind is not None
                            and replay.kind != candidate.kind
                        )
                        or (
                            candidate.payload_digest is not None
                            and replay.payload_digest != candidate.payload_digest
                        )
                    ):
                        raise BridgeConflict("conflicting replay result")
                    return replay
                command = self._command_from_file()
                if command is None or command.command_id != command_id or command.nonce != nonce:
                    raise BridgeConflict("result does not match the in-flight command")
                if isinstance(result, BridgeResult):
                    if result.command_id != command_id or result.nonce != nonce:
                        raise BridgeConflict("result identity does not match command")
                    if (
                        result.kind is not None
                        and result.kind != command.kind
                    ) or (
                        result.payload_digest is not None
                        and result.payload_digest != command.payload_digest
                    ):
                        raise BridgeConflict("result command metadata does not match")
                    try:
                        candidate = BridgeResult(
                            command_id=command_id,
                            nonce=nonce,
                            kind=command.kind,
                            payload_digest=command.payload_digest,
                            ok=result.ok,
                            result=result.result,
                            result_digest=result.result_digest,
                            error=result.error,
                        )
                    except (TypeError, ValueError) as exc:
                        raise BridgeConflict("bridge result is invalid") from exc
                else:
                    try:
                        candidate = BridgeResult(
                            command_id=command_id,
                            nonce=nonce,
                            kind=command.kind,
                            payload_digest=command.payload_digest,
                            ok=ok,
                            result=dict(result),
                            result_digest=result_digest,
                            error=error,
                        )
                    except (TypeError, ValueError) as exc:
                        raise BridgeConflict("bridge result is invalid") from exc
                # Publish the immutable replay record before the consumable
                # result file so a crash cannot leave a mutation unjournaled.
                self._write_completed(candidate)
                encoded = canonical_bridge_json(candidate.model_dump(mode="json"))
                self._atomic_write(self._path(self.result_filename), encoded)
                return candidate

    def read_result(self, command_id: str | None = None, nonce: str | None = None) -> BridgeResult | None:
        with self._lock:
            result = self._result_from_file()
            if result is None and command_id is not None and nonce is not None:
                result = self._journal_result(command_id, nonce)
                if result is None:
                    active = self._command_from_file()
                    if (
                        active is not None
                        and active.command_id == command_id
                        and active.nonce != nonce
                    ):
                        raise BridgeConflict("result nonce does not match in-flight command")
            if result is None:
                return None
            if command_id is not None and result.command_id != command_id:
                raise BridgeConflict("result identity does not match requested command")
            if nonce is not None and result.nonce != nonce:
                raise BridgeConflict("result identity does not match requested nonce")
            return result

    def acknowledge(self, command_id: str, nonce: str) -> None:
        with self._lock:
            with self._exclusive_process_lock():
                result = self.read_result(command_id, nonce)
                if result is None:
                    raise BridgeConflict("cannot acknowledge a missing result")
                command_path = self._path(self.command_filename)
                active = self._command_from_file()
                if active is not None and active.command_id == command_id and active.nonce == nonce:
                    command_path.unlink(missing_ok=True)
                result_path = self._path(self.result_filename)
                current = self._result_from_file()
                if current is not None and current.command_id == command_id and current.nonce == nonce:
                    result_path.unlink(missing_ok=True)

    def replay(self, command_id: str, nonce: str | None = None) -> BridgeResult | None:
        with self._lock:
            records = self._journal()
            for result in reversed(records):
                if result.command_id != command_id:
                    continue
                if nonce is not None and result.nonce != nonce:
                    raise BridgeConflict("completed result nonce does not match")
                return result
            return None

    def dispatch(
        self,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        command_id: str | None = None,
        nonce: str | None = None,
        timeout: float = 30.0,
        poll_interval: float = 0.05,
    ) -> BridgeResult:
        if not math.isfinite(timeout) or timeout < 0 or timeout > 86_400:
            raise ValueError("bridge timeout is invalid")
        if not math.isfinite(poll_interval) or poll_interval <= 0 or poll_interval > 60:
            raise ValueError("bridge poll interval is invalid")
        submitted = self.write_command(kind, payload, command_id=command_id, nonce=nonce)
        if isinstance(submitted, BridgeResult):
            return submitted
        deadline = time.monotonic() + timeout
        while True:
            result = self.read_result(submitted.command_id, submitted.nonce)
            if result is not None:
                self.acknowledge(submitted.command_id, submitted.nonce)
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for the AE panel")
            time.sleep(poll_interval)

    def request_stop(
        self,
        reason: str = "user",
        *,
        command_id: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        try:
            record = _StopRecord(reason=reason, command_id=command_id, nonce=nonce)
        except ValueError as exc:
            raise BridgeConflict("stop request is invalid") from exc
        with self._lock:
            encoded = canonical_bridge_json(record.model_dump(mode="json", exclude_none=True))
            self._atomic_write(self._path(self.stop_filename), encoded)
            return record.model_dump(mode="json", exclude_none=True)

    def read_stop(self) -> dict[str, Any] | None:
        with self._lock:
            raw = self._read_json(self._path(self.stop_filename))
            if raw is None:
                return None
            try:
                return _StopRecord.model_validate(raw).model_dump(mode="json", exclude_none=True)
            except ValueError as exc:
                raise BridgeConflict("stop record is invalid") from exc

    def clear_stop(self) -> None:
        with self._lock:
            self._path(self.stop_filename).unlink(missing_ok=True)

    def stop_requested(self) -> bool:
        record = self.read_stop()
        return bool(record and record.get("requested") is True)

    def completed_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(item.command_id for item in self._journal())

    # Names used by the connector implementation and panel tests.
    write_request = write_command
    read_request = read_command
    write_response = write_result
    read_response = read_result
    complete = write_result
    issue = write_command
    read = read_command
    respond = write_result


BridgeStore = Bridge
LocalBridge = Bridge


BridgeRoot = Bridge
BridgeProtocol = Bridge


__all__ = [
    "BRIDGE_SCHEMA_VERSION",
    "Bridge",
    "BridgeBusy",
    "BridgeCommand",
    "BridgeConflict",
    "BridgeError",
    "BridgeKind",
    "BridgeProtocol",
    "BridgeResult",
    "BridgeRoot",
    "BridgeStore",
    "LocalBridge",
    "BridgeUnavailable",
    "FIXED_KINDS",
    "MAX_BRIDGE_JSON_BYTES",
    "canonical_bridge_json",
    "payload_digest",
]
