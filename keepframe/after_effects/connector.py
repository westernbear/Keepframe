from __future__ import annotations

import asyncio
import errno
import ctypes
import ctypes.wintypes
import hashlib
import http.client
import importlib
import json
import math
import os
import re
import platform
import stat
import zipfile
import shutil
import struct
import subprocess
import sys
import tempfile
import socket
import zlib
import ssl
import secrets
import time
import urllib.request
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fonts import enrich_heartbeat_fonts
from .models import AECapabilities, canonical_json


# DPAPI flags: UI prompts are forbidden.  The local-machine flag is deliberately
# not used: the token is decryptable only by the current Windows user.
CRYPTPROTECT_UI_FORBIDDEN = 0x1
TOKEN_FILENAME = "device-token.dpapi"
DEVICE_RECORD_FILENAME = "device-record.dpapi"
COMMAND_JOURNAL_FILENAME = "completed-commands.json"
COMMAND_SCOPE_DIRNAME = "command-high-water"
_MAX_JSON_BYTES = 1 * 1024 * 1024
_MAX_ERROR_BYTES = 8 * 1024
_MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024
_MAX_JOURNAL_ENTRIES = 1024
_PAIRING_PROJECT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_PAIRING_SUFFIX = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RELAY_WAIT = 25.0
_DEFAULT_RELAY_TIMEOUT = 35.0
_RELAY_RETRY_DELAY = 1.0
_MAX_FFMPEG_OUTPUT_BYTES = 64 * 1024
_FFMPEG_TIMEOUT = 5 * 60
_MAX_PNG_SCAN_BYTES = 64 * 1024 * 1024
_MAX_FINAL_DIMENSION = 16_384
_MAX_FINAL_PIXELS = 16_384 * 16_384
_MAX_FINAL_FRAMES = 100_000
_MAX_FINAL_TOTAL_PIXELS = 10_000_000_000
_MIN_FINAL_DISK_MARGIN = 512 * 1024 * 1024
_MAX_PACKAGE_MEDIA = 1024
_MAX_PACKAGE_FILENAME = 256
_MAX_PACKAGE_UNCOMPRESSED = 4 * 1024 * 1024 * 1024
_FINAL_SEQUENCE_PATTERN = "final-%06d.png"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SERVER_ONLY_PAYLOAD_FIELDS = frozenset(
    {
        "checkpoint_context",
        "checkpoint_context_digest",
        "checkpoint_artifact",
        "workflow",
        "prepare_manual",
        "final_plan_digest",
        "finalization_key",
        "package_media",
        "dependencies",
    }
)
_SERVER_ONLY_RESULT_FIELDS = _SERVER_ONLY_PAYLOAD_FIELDS | {"checkpoint"}

COMMAND_TO_TOOL: dict[str, str] = {
    "heartbeat": "capability_heartbeat",
    "create_project": "create_or_open_project",
    "open_project": "create_or_open_project",
    "import_asset": "import_server_asset",
    "apply_batch": "apply_operation_batch",
    "inspect_layers": "inspect_mapped_layers",
    "save_checkpoint": "save_checkpoint",
    "sync_manual": "save_checkpoint",
    "render_preview": "render_preview",
    "render_final": "render_final",
    "package_project": "package_project",
}
MCP_TOOL_NAMES = frozenset(COMMAND_TO_TOOL.values())
_COMMAND_FIELDS = frozenset(
    {
        "id",
        "nonce",
        "project_id",
        "project",
        "device_id",
        "device",
        "plan_id",
        "plan_digest",
        "session_id",
        "sequence",
        "expected_state",
        "expected_checkpoint",
        "kind",
        "payload",
        "payload_digest",
        "lease_seconds",
        "lease_expires_at",
        "status",
        "result",
        "result_digest",
        "created_at",
        "delivered_at",
    }
)

# Keep the subprocess environment intentionally small.  In particular, no
# token/provider/proxy variable can reach the optional MCP child.
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "SystemRoot",
        "SystemDrive",
        "WINDIR",
        "TEMP",
        "TMP",
        "KEEPFRAME_AE_BRIDGE_ROOT",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        # Windows Python locates the user site-packages (a plain `pip install`) via APPDATA.
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
    }
)


class ConnectorError(RuntimeError):
    """Base error for fail-closed connector failures."""


class PreflightError(ConnectorError):
    pass


class RelayError(ConnectorError):
    pass


class TransientRelayError(RelayError):
    """A relay transport failure that is safe for the poll loop to retry."""


class TokenStoreError(ConnectorError):
    pass


class MCPError(ConnectorError):
    pass


def _is_windows() -> bool:
    return os.name == "nt" or sys.platform == "win32"


def _require_windows(windows: Callable[[], bool] | None = None) -> None:
    if not (windows or _is_windows)():
        raise PreflightError("After Effects connector requires Windows")


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        stat_result = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return bool(getattr(stat_result, "st_file_attributes", 0) & 0x0400)


def _reject_reparse(path: Path, *, label: str = "path") -> None:
    if _is_reparse(path):
        raise ConnectorError(f"unsafe {label}")


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise ConnectorError(f"{label} is invalid")
    return value


def _credential(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(
        char.isspace() or ord(char) < 0x20 for char in value
    ):
        raise ConnectorError(f"{label} is invalid")
    return value



def _json_bytes(value: Any, *, limit: int = _MAX_JSON_BYTES) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConnectorError("JSON payload is invalid") from exc
    if len(encoded) > limit:
        raise ConnectorError("JSON payload is too large")
    return encoded


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """Write beside the destination and publish only after fsync."""
    path = Path(path)
    _reject_reparse(path, label="destination")
    parent = path.parent
    if not parent.exists() or not parent.is_dir() or _is_reparse(parent):
        raise ConnectorError("destination directory is unsafe")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            dir_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ConnectorError("atomic storage failed") from exc


def normalize_relay_url(url: str) -> str:
    if not isinstance(url, str) or not url or any(char.isspace() for char in url):
        raise PreflightError("relay URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise PreflightError("relay URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise PreflightError("relay URL is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise PreflightError("relay URL credentials are forbidden")
    if parsed.query or parsed.fragment:
        raise PreflightError("relay URL query and fragment are forbidden")
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and host.lower().rstrip(".") not in local_hosts:
        raise PreflightError("relay URL must use HTTPS")
    if port is not None and not 1 <= port <= 65535:
        raise PreflightError("relay URL port is invalid")
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def parse_pairing_code(code: str) -> tuple[str, str]:
    if not isinstance(code, str) or code.count(".") != 1:
        raise ConnectorError("pairing code is invalid")
    project, suffix = code.split(".", 1)
    if not _PAIRING_PROJECT.fullmatch(project) or not _PAIRING_SUFFIX.fullmatch(suffix):
        raise ConnectorError("pairing code is invalid")
    return project, suffix


@dataclass(frozen=True)
class PreflightResult:
    relay_url: str
    private_root: Path
    ffmpeg: str
    ae_version: str
    capabilities: AECapabilities
    @property
    def capability_hash(self) -> str:
        return self.capabilities.capability_hash or ""

@dataclass(frozen=True)
class Pairing:
    project: str
    device_id: str
    token: str
    capability_request: Mapping[str, Any] | None = None

    def __repr__(self) -> str:
        return (
            f"Pairing(project={self.project!r}, device_id={self.device_id!r}, "
            "token=<redacted>, capability_request=<redacted>)"
        )


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", ctypes.wintypes.DWORD),
    ]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


def _signature(function: Any, argtypes: list[Any], restype: Any) -> Any:
    try:
        function.argtypes = argtypes
        function.restype = restype
    except AttributeError:
        pass
    return function


def _win32_libraries() -> tuple[Any, Any, Any]:
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        try:
            advapi32 = ctypes.windll.advapi32
            crypt32 = ctypes.windll.crypt32
            kernel32 = ctypes.windll.kernel32
        except AttributeError as exc:
            raise TokenStoreError("Windows API is unavailable") from exc
    return advapi32, crypt32, kernel32


def _typed_local_free(kernel32: Any, pointer: Any) -> None:
    if pointer:
        free = _signature(kernel32.LocalFree, [ctypes.c_void_p], ctypes.c_void_p)
        if free(pointer):
            raise TokenStoreError("Windows buffer release failed")


def _default_sid_provider() -> str:
    if not _is_windows():
        raise TokenStoreError("Windows SID lookup is unavailable")
    try:
        advapi32, _crypt32, kernel32 = _win32_libraries()
        handle_type = ctypes.wintypes.HANDLE
        bool_type = ctypes.wintypes.BOOL
        process = _signature(kernel32.GetCurrentProcess, [], handle_type)()
        token = handle_type()
        open_token = _signature(
            advapi32.OpenProcessToken,
            [handle_type, ctypes.wintypes.DWORD, ctypes.POINTER(handle_type)],
            bool_type,
        )
        close_handle = _signature(kernel32.CloseHandle, [handle_type], bool_type)
        if not open_token(process, 0x0008, ctypes.byref(token)):
            raise TokenStoreError("current-user SID lookup failed")
        try:
            get_info = _signature(
                advapi32.GetTokenInformation,
                [
                    handle_type,
                    ctypes.wintypes.DWORD,
                    ctypes.c_void_p,
                    ctypes.wintypes.DWORD,
                    ctypes.POINTER(ctypes.wintypes.DWORD),
                ],
                bool_type,
            )
            size = ctypes.wintypes.DWORD(0)
            get_info(token, 1, None, 0, ctypes.byref(size))
            if not size.value:
                raise TokenStoreError("current-user SID lookup failed")
            buffer = ctypes.create_string_buffer(size.value)
            if not get_info(token, 1, ctypes.byref(buffer), size.value, ctypes.byref(size)):
                raise TokenStoreError("current-user SID lookup failed")
            token_user = ctypes.cast(ctypes.byref(buffer), ctypes.POINTER(_TOKEN_USER)).contents
            string_sid = ctypes.wintypes.LPWSTR()
            convert = _signature(
                advapi32.ConvertSidToStringSidW,
                [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.LPWSTR)],
                bool_type,
            )
            if not convert(token_user.User.Sid, ctypes.byref(string_sid)):
                raise TokenStoreError("current-user SID lookup failed")
            try:
                sid = string_sid.value
            finally:
                _typed_local_free(kernel32, string_sid)
            if not isinstance(sid, str) or not re.fullmatch(r"S-1-[0-9-]+", sid):
                raise TokenStoreError("current-user SID lookup failed")
            return sid
        finally:
            if token:
                close_handle(token)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise TokenStoreError("Windows SID lookup is unavailable") from exc

def current_user_sid() -> str:
    return _default_sid_provider()




def _default_acl_runner(argv: list[str], **kwargs: Any) -> Any:
    return subprocess.run(argv, **kwargs)


def _reject_reparse_components(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            if _is_reparse(candidate):
                raise PreflightError("private connector root is unsafe")
        except OSError as exc:
            raise PreflightError("private connector root is unsafe") from exc


def ensure_private_root(
    root: Path | str | None = None,
    *,
    sid_provider: Callable[[], str] | None = None,
    acl_runner: Callable[..., Any] | None = None,
    windows: Callable[[], bool] | None = None,
) -> Path:
    _require_windows(windows)
    if root is None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise PreflightError("LOCALAPPDATA is unavailable")
        root_path = Path(local_app_data) / "Keepframe" / "ae-bridge"
    else:
        root_path = Path(root)
    _reject_reparse_components(root_path)
    if root_path.exists() and (_is_reparse(root_path) or not root_path.is_dir()):
        raise PreflightError("private connector root is unsafe")
    try:
        root_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PreflightError("private connector root cannot be created") from exc
    if _is_reparse(root_path) or not root_path.is_dir():
        raise PreflightError("private connector root is unsafe")
    sid = (sid_provider or current_user_sid)()
    if not isinstance(sid, str) or not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise PreflightError("current-user SID is invalid")
    # icacls treats a bare "S-1-..." as an account name; a raw SID needs the "*" prefix.
    icacls = str(Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "icacls.exe")
    # Reset the tree so every file inherits, then protect only the root: /inheritance:r with /T
    # strips each existing file's inherited entries without applying the (OI)(CI) grant to it,
    # which left files with an empty DACL (unopenable even by their owner).
    commands = [
        [icacls, str(root_path), "/reset", "/T", "/C"],
        [icacls, str(root_path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "/C"],
    ]
    runner = acl_runner or _default_acl_runner
    try:
        for command in commands:
            runner(
                command,
                check=True,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=build_child_env(),
            )
    except subprocess.CalledProcessError as exc:
        detail = " ".join(str(exc.stderr or exc.stdout or "").split())[:160]
        raise PreflightError(
            "private connector ACL could not be applied" + (f": {detail}" if detail else "")
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError(f"private connector ACL could not be applied: {type(exc).__name__}") from exc
    except Exception as exc:  # noqa: BLE001 - ACL failures fail closed
        raise PreflightError("private connector ACL could not be applied") from exc
    try:  # the bridge must be able to open its lock file under the new ACL
        os.close(os.open(root_path / ".bridge.lock", os.O_RDWR | os.O_CREAT, 0o600))
    except OSError as exc:
        raise PreflightError(f"private connector ACL left the bridge unusable: {exc}") from exc
    return root_path


def _dpapi_call(data: bytes, function_name: str, message: str) -> bytes:
    if not _is_windows():
        raise TokenStoreError("DPAPI is unavailable")
    if not isinstance(data, bytes) or not data:
        raise TokenStoreError("DPAPI data is invalid")
    try:
        _advapi32, crypt32, kernel32 = _win32_libraries()
        function = _signature(
            getattr(crypt32, function_name),
            [
                ctypes.POINTER(_DATA_BLOB),
                ctypes.wintypes.LPCWSTR,
                ctypes.POINTER(_DATA_BLOB),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.wintypes.DWORD,
                ctypes.POINTER(_DATA_BLOB),
            ],
            ctypes.wintypes.BOOL,
        )
        source = ctypes.create_string_buffer(data)
        source_blob = _DATA_BLOB(
            len(data),
            ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)),
        )
        output_blob = _DATA_BLOB()
        if not function(
            ctypes.byref(source_blob),
            None,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            raise TokenStoreError(message)
        try:
            if not output_blob.pbData or not output_blob.cbData:
                raise TokenStoreError(message)
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            _typed_local_free(kernel32, output_blob.pbData)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise TokenStoreError("DPAPI is unavailable") from exc


def _dpapi_protect(data: bytes) -> bytes:
    return _dpapi_call(data, "CryptProtectData", "DPAPI encryption failed")


def _dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi_call(data, "CryptUnprotectData", "DPAPI decryption failed")


class DPAPITokenStore:
    def __init__(
        self,
        root: Path | str,
        *,
        filename: str = TOKEN_FILENAME,
        protect: Callable[..., bytes] | None = None,
        unprotect: Callable[..., bytes] | None = None,
        windows: Callable[[], bool] | None = None,
        sid_provider: Callable[[], str] | None = None,
        acl_runner: Callable[..., Any] | None = None,
    ) -> None:
        self._windows = windows or _is_windows
        self.root = ensure_private_root(
            root,
            sid_provider=sid_provider,
            acl_runner=acl_runner,
            windows=self._windows,
        )
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", filename):
            raise TokenStoreError("token filename is invalid")
        self.path = self.root / filename
        self._protect = protect or (lambda value, flags=CRYPTPROTECT_UI_FORBIDDEN: _dpapi_protect(value))
        self._unprotect = unprotect or (lambda value, flags=CRYPTPROTECT_UI_FORBIDDEN: _dpapi_unprotect(value))

    @staticmethod
    def _token(value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > 4096 or any(
            char.isspace() or ord(char) < 0x20 for char in value
        ):
            raise TokenStoreError("device token is invalid")
        return value

    @classmethod
    def _record(cls, value: object) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise TokenStoreError("device record is invalid")
        if set(value) != {"relay_url", "project", "device_id", "token"}:
            raise TokenStoreError("device record is invalid")
        try:
            raw_relay_url = value["relay_url"]
            if not isinstance(raw_relay_url, str):
                raise TokenStoreError("device record is invalid")
            relay_url = normalize_relay_url(raw_relay_url)
            project = _safe_id(value["project"], "project")
            device_id = _safe_id(value["device_id"], "device")
            token = cls._token(value["token"])
        except (ConnectorError, PreflightError, TypeError, ValueError) as exc:
            raise TokenStoreError("device record is invalid") from exc
        return {
            "relay_url": relay_url,
            "project": project,
            "device_id": device_id,
            "token": token,
        }

    def save_record(self, record: Mapping[str, object]) -> None:
        normalized = self._record(record)
        encoded = _json_bytes(normalized)
        try:
            encrypted = self._protect(encoded, CRYPTPROTECT_UI_FORBIDDEN)
            if not isinstance(encrypted, (bytes, bytearray)) or not encrypted:
                raise TokenStoreError("DPAPI encryption failed")
            if bytes(encrypted) == encoded:
                raise TokenStoreError("DPAPI encryption failed")
            _atomic_write(self.path, bytes(encrypted))
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed at the secret boundary
            raise TokenStoreError("device record could not be stored") from exc

    def load_record(self) -> dict[str, str]:
        _reject_reparse(self.path, label="device record")
        try:
            encrypted = self.path.read_bytes()
            if not encrypted:
                raise TokenStoreError("device record is unavailable")
            raw = self._unprotect(encrypted, CRYPTPROTECT_UI_FORBIDDEN)
            if not isinstance(raw, bytes):
                raise TokenStoreError("device record decryption failed")
            value = json.loads(raw.decode("utf-8"))
        except ConnectorError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise TokenStoreError("device record decryption failed") from exc
        return self._record(value)

    def save(self, token: str) -> None:
        token = self._token(token)
        try:
            encrypted = self._protect(token.encode("utf-8"), CRYPTPROTECT_UI_FORBIDDEN)
            if not isinstance(encrypted, (bytes, bytearray)) or not encrypted:
                raise TokenStoreError("DPAPI encryption failed")
            if bytes(encrypted) == token.encode("utf-8"):
                raise TokenStoreError("DPAPI encryption failed")
            _atomic_write(self.path, bytes(encrypted))
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed at the secret boundary
            raise TokenStoreError("device token could not be stored") from exc

    def load(self) -> str:
        _reject_reparse(self.path, label="token file")
        try:
            encrypted = self.path.read_bytes()
        except (OSError, UnicodeError) as exc:
            raise TokenStoreError("device token is unavailable") from exc
        if not encrypted:
            raise TokenStoreError("device token is unavailable")
        try:
            raw = self._unprotect(encrypted, CRYPTPROTECT_UI_FORBIDDEN)
            token = raw.decode("utf-8")
        except (ConnectorError, UnicodeError, ValueError, TypeError) as exc:
            raise TokenStoreError("device token decryption failed") from exc
        return self._token(token)

    def clear(self) -> None:
        _reject_reparse(self.path, label="token file")
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise TokenStoreError("device token could not be removed") from exc




def read_panel_heartbeat(root: Path | str, *, filename: str = "heartbeat.json") -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", filename):
        raise PreflightError("panel heartbeat filename is invalid")
    path = Path(root) / filename
    _reject_reparse(path, label="panel heartbeat")
    try:
        size = path.stat().st_size
        if size <= 0 or size > 64 * 1024:
            raise PreflightError("panel heartbeat is invalid")
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightError("panel heartbeat is unavailable") from exc
    if not isinstance(payload, dict):
        raise PreflightError("panel heartbeat is invalid")
    return payload


def validate_ae_version(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PreflightError("After Effects version is unavailable")
    match = re.search(r"(?<!\d)(\d{4})(?:\.\d+)*", value)
    if match is None:
        match = re.search(r"(?<!\d)(\d{2})(?:\.\d+)*", value)
        if match is None:
            raise PreflightError("After Effects version is invalid")
        year = 2000 + int(match.group(1))
    else:
        year = int(match.group(1))
    if year < 2022:
        raise PreflightError("After Effects 2022 or newer is required")
    return value.strip()


def _probe_ffmpeg(path: str, runner: Callable[..., Any]) -> None:
    try:
        runner(
            [path, "-version"],
            check=True,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=build_child_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError("ffmpeg preflight failed") from exc
    except Exception as exc:  # noqa: BLE001 - probe failures fail closed
        raise PreflightError("ffmpeg preflight failed") from exc


def preflight(
    relay_url: str,
    deployment_token: str,
    *,
    private_root: Path | str | None = None,
    ffmpeg: str = "ffmpeg",
    panel_heartbeat: Mapping[str, Any] | Callable[[Path], Mapping[str, Any]] | None = None,
    now: float | None = None,
    heartbeat_max_age: float = 60.0,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., Any] | None = None,
    sid_provider: Callable[[], str] | None = None,
    windows: Callable[[], bool] | None = None,
) -> PreflightResult:
    _require_windows(windows)
    if not isinstance(deployment_token, str) or not deployment_token or any(
        char.isspace() or ord(char) < 0x20 for char in deployment_token
    ):
        raise PreflightError("deployment token is invalid")
    normalized_url = normalize_relay_url(relay_url)
    try:
        heartbeat_age = float(heartbeat_max_age)
    except (TypeError, ValueError) as exc:
        raise PreflightError("panel heartbeat age is invalid") from exc
    if not math.isfinite(heartbeat_age) or heartbeat_age < 0:
        raise PreflightError("panel heartbeat age is invalid")
    runner = run or _default_acl_runner
    root = ensure_private_root(
        private_root,
        sid_provider=sid_provider,
        acl_runner=runner,
        windows=windows,
    )
    if not isinstance(ffmpeg, str) or not ffmpeg or "\x00" in ffmpeg:
        raise PreflightError("ffmpeg is invalid")
    executable = (which or shutil.which)(ffmpeg)
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise PreflightError("ffmpeg is not available")
    _probe_ffmpeg(executable, runner)
    if callable(panel_heartbeat):
        try:
            heartbeat = dict(panel_heartbeat(root))
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed
            raise PreflightError("panel heartbeat is unavailable") from exc
    elif panel_heartbeat is None:
        heartbeat = read_panel_heartbeat(root)
    else:
        heartbeat = dict(panel_heartbeat)
    heartbeat = enrich_heartbeat_fonts(heartbeat)
    try:
        capabilities = AECapabilities.from_heartbeat(heartbeat)
    except (TypeError, ValueError) as exc:
        raise PreflightError("panel heartbeat is invalid") from exc
    version = validate_ae_version(capabilities.version)
    if not capabilities.ready:
        raise PreflightError("After Effects panel is not ready")
    timestamp = capabilities.timestamp
    if timestamp is None:
        raise PreflightError("panel heartbeat timestamp is invalid")
    try:
        timestamp_value = float(timestamp)
        current_time = time.time() if now is None else float(now)
    except (TypeError, ValueError) as exc:
        raise PreflightError("panel heartbeat timestamp is invalid") from exc
    if not math.isfinite(timestamp_value) or timestamp_value < 0 or not math.isfinite(current_time) or current_time < 0:
        raise PreflightError("panel heartbeat timestamp is invalid")
    if timestamp_value > current_time + 5 or current_time - timestamp_value > heartbeat_age:
        raise PreflightError("panel heartbeat is stale")
    return PreflightResult(
        relay_url=normalized_url,
        private_root=root,
        ffmpeg=str(executable),
        ae_version=version,
        capabilities=capabilities,
    )


class _HTTPResponse:
    def __init__(self, response: Any):
        self.response = response

    def __enter__(self):
        return self.response.__enter__() if hasattr(self.response, "__enter__") else self.response

    def __exit__(self, *args: Any):
        if hasattr(self.response, "__exit__"):
            return self.response.__exit__(*args)
        close = getattr(self.response, "close", None)
        if close:
            close()
        return False


def _response_status(response: Any) -> int:
    status = getattr(response, "status", getattr(response, "code", 200))
    if not isinstance(status, int):
        raise RelayError("relay response is invalid")
    return status


def _is_transient_relay_status(status: int) -> bool:
    return 500 <= status < 600


_TRANSIENT_CONNECTION_ERRNOS = frozenset(
    {
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ETIMEDOUT,
        errno.EPIPE,
        getattr(errno, "ENETRESET", errno.ECONNRESET),
    }
)


def _is_transient_network_error(error: BaseException) -> bool:
    current: BaseException = error
    visited_ids: set[int] = set()
    for _ in range(16):
        current_id = id(current)
        if current_id in visited_ids:
            return False
        visited_ids.add(current_id)
        if isinstance(current, (ssl.SSLError, ssl.CertificateError)):
            return False
        if isinstance(current, urllib.error.URLError):
            reason = getattr(current, "reason", None)
            if not isinstance(reason, BaseException):
                return False
            current = reason
            continue
        if isinstance(current, socket.gaierror):
            eai_again = getattr(socket, "EAI_AGAIN", None)
            return eai_again is not None and getattr(current, "errno", None) == eai_again
        if isinstance(current, (TimeoutError, ConnectionError)):
            return True
        return isinstance(current, OSError) and getattr(current, "errno", None) in _TRANSIENT_CONNECTION_ERRNOS
    return False


def _header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    return str(value) if value is not None else None


def _read_bounded(stream: Any, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise RelayError("relay response is invalid")
        chunk = bytes(chunk)
        total += len(chunk)
        if total > limit:
            raise RelayError("relay response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _response_json(response: Any) -> dict[str, Any] | None:
    status = _response_status(response)
    if status == 204:
        return None
    if status < 200 or status >= 300:
        error = f"relay request failed ({status})"
        detail: str | None = None
        try:
            body = _read_bounded(response, _MAX_ERROR_BYTES)
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            candidate = parsed.get("error") if isinstance(parsed, dict) else None
            if candidate in {
                "command lease is draining",
                "command lease has expired",
            }:
                detail = candidate
        except ConnectorError as exc:
            if _is_transient_relay_status(status):
                raise TransientRelayError(error) from exc
            raise
        except OSError as exc:
            if _is_transient_relay_status(status):
                raise TransientRelayError(error) from exc
            raise RelayError(error) from exc
        if detail is not None:
            error = f"{error}: {detail}"
        if _is_transient_relay_status(status):
            raise TransientRelayError(error)
        raise RelayError(error)
    body = _read_bounded(response, _MAX_JSON_BYTES)
    if not body:
        raise RelayError("relay response is empty")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelayError("relay response is invalid") from exc
    if not isinstance(value, dict):
        raise RelayError("relay response is invalid")
    return value




class RelayClient:
    def __init__(
        self,
        relay_url: str,
        deployment_token: str | None = None,
        *,
        device_token: str | None = None,
        opener: Callable[..., Any] | None = None,
        timeout: float = _DEFAULT_RELAY_TIMEOUT,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = normalize_relay_url(relay_url)
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise RelayError("relay timeout is invalid") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0 or timeout_value > 300:
            raise RelayError("relay timeout is invalid")
        self.deployment_token = deployment_token
        self.device_token = device_token
        self.project_id: str | None = None
        self.device_id: str | None = None
        self.timeout = timeout_value
        self._opener = opener
        self._connection_factory = connection_factory

    def set_device_token(self, token: str) -> None:
        self.device_token = _credential(token, "device token")

    def _url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise RelayError("relay path is invalid")
        base = self.base_url.rstrip("/")
        url = f"{base}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    @staticmethod
    def _headers(token: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        try:
            token = _credential(token, "relay credential")
        except ConnectorError as exc:
            raise RelayError("relay credential is unavailable") from exc
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers
    def _open(self, request: urllib.request.Request) -> Any:
        opener = self._opener or urllib.request.urlopen
        return opener(request, timeout=self.timeout)

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        payload: Any = None,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        body = None if payload is None else _json_bytes(payload)
        request_headers = self._headers(token, headers)
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path, query),
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            response = self._open(request)
            with _HTTPResponse(response) as opened:
                return _response_json(opened)
        except urllib.error.HTTPError as exc:
            try:
                _response_json(exc)
            except TransientRelayError:
                raise
            except RelayError:
                raise
            except (ConnectorError, OSError):
                pass
            error = f"relay request failed ({exc.code})"
            if _is_transient_relay_status(exc.code):
                raise TransientRelayError(error) from None
            raise RelayError(error) from None
        except ConnectorError:
            raise
        except OSError as exc:
            if _is_transient_network_error(exc):
                raise TransientRelayError("relay request failed") from exc
            raise RelayError("relay request failed") from exc
        except http.client.HTTPException as exc:
            raise RelayError("relay request failed") from exc
        except (ValueError, TypeError) as exc:
            raise RelayError("relay request failed") from exc

    def pair(self, pairing_code: str) -> Pairing:
        project, _ = parse_pairing_code(pairing_code)
        if not self.deployment_token:
            raise RelayError("deployment credential is unavailable")
        payload = self._request(
            "POST",
            "/pair",
            token=self.deployment_token,
            payload={"project": project, "code": pairing_code},
        )
        if not isinstance(payload, dict):
            raise RelayError("pairing response is invalid")
        response_project = payload.get("project")
        raw_device_id = payload.get("device", payload.get("device_id"))
        raw_token = payload.get("token")
        if response_project != project:
            raise RelayError("pairing response is invalid")
        try:
            device_id = _safe_id(raw_device_id, "device")
            token = _credential(raw_token, "device token")
        except ConnectorError as exc:
            raise RelayError("pairing response is invalid") from exc
        capability_request = payload.get("capability_request")
        if capability_request is not None:
            if not isinstance(capability_request, dict):
                raise RelayError("pairing response is invalid")
            try:
                _json_bytes(capability_request, limit=64 * 1024)
            except ConnectorError as exc:
                raise RelayError("pairing response is invalid") from exc
        self.project_id = project
        self.device_id = device_id
        self.device_token = token
        return Pairing(project, device_id, token, capability_request)

    def publish_capabilities(
        self,
        capabilities: AECapabilities | Mapping[str, Any],
        *,
        project: str | None = None,
    ) -> dict[str, Any]:
        try:
            payload = (
                capabilities.model_dump(mode="json")
                if isinstance(capabilities, AECapabilities)
                else capabilities
            )
            snapshot = AECapabilities.model_validate(payload)
            payload = snapshot.model_dump(mode="json")
            canonical_json(payload)
            _json_bytes(payload)
        except (TypeError, ValueError, ConnectorError) as exc:
            raise RelayError("capability snapshot is invalid") from exc
        project = project or self.project_id
        if project is None:
            raise RelayError("project is unavailable")
        headers = {"X-Keepframe-Project": _safe_id(project, "project")}
        response = self._request(
            "POST",
            "/capabilities",
            token=self.device_token or "",
            payload=payload,
            headers=headers,
        )
        if (
            not isinstance(response, dict)
            or response.get("capability_hash") != snapshot.capability_hash
        ):
            raise RelayError("capability publication response is invalid")
        return response


    def next(
        self,
        project: str | None = None,
        *,
        wait: float = _MAX_RELAY_WAIT,
        plan: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            wait_value = float(wait)
        except (TypeError, ValueError) as exc:
            raise RelayError("poll wait is invalid") from exc
        if not math.isfinite(wait_value) or wait_value < 0 or wait_value > _MAX_RELAY_WAIT:
            raise RelayError("poll wait is invalid")
        project = project or self.project_id
        headers: dict[str, str] = {}
        if project is not None:
            headers["X-Keepframe-Project"] = _safe_id(project, "project")
        query = {"wait": str(wait_value)}
        if plan is not None:
            query["plan"] = _safe_id(plan, "plan")
        payload = self._request(
            "GET",
            "/next",
            token=self.device_token or "",
            query=query,
            headers=headers,
        )
        if payload is None:
            return None
        command = payload.get("command")
        if not isinstance(command, dict):
            raise RelayError("command response is invalid")
        return command

    next_command = next

    def download_asset(
        self,
        asset_id: str,
        destination: Path | str,
        *,
        expected_sha256: str,
        expected_length: int,
        project: str | None = None,
        plan: str | None = None,
        _route: str = "assets",
        _max_length: int = _MAX_ASSET_BYTES,
        _label: str = "asset",
    ) -> Path:
        if _route not in {"assets", "artifacts"}:
            raise RelayError("download route is invalid")
        if _label not in {"asset", "artifact"}:
            raise RelayError("download label is invalid")
        asset_id = _safe_id(asset_id, f"{_label} id")
        if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
            raise RelayError(f"{_label} hash is invalid")
        if not isinstance(expected_length, int) or isinstance(expected_length, bool) or not 0 <= expected_length <= _max_length:
            raise RelayError(f"{_label} length is invalid")
        destination = Path(destination)
        _reject_reparse(destination, label=f"{_label} destination")
        if not destination.parent.is_dir() or _is_reparse(destination.parent):
            raise RelayError(f"{_label} destination is unsafe")
        headers: dict[str, str] = {}
        if project is not None:
            headers["X-Keepframe-Project"] = _safe_id(project, "project")
        query = {"plan": _safe_id(plan, "plan")} if plan is not None else None
        request = urllib.request.Request(
            self._url(f"/{_route}/{asset_id}", query),
            headers=self._headers(self.device_token or "", headers),
            method="GET",
        )
        temporary: Path | None = None
        try:
            response = self._open(request)
            with _HTTPResponse(response) as opened:
                if _response_status(opened) < 200 or _response_status(opened) >= 300:
                    _response_json(opened)
                    raise RelayError(f"{_label} download failed")
                server_length = _header(opened, "Content-Length")
                if server_length is None:
                    raise RelayError(f"{_label} length is unavailable")
                try:
                    if int(server_length) != expected_length:
                        raise RelayError(f"{_label} length mismatch")
                except ValueError as exc:
                    raise RelayError(f"{_label} length is invalid") from exc
                server_hash = _header(opened, "X-Keepframe-SHA256") or _header(
                    opened, "X-Asset-SHA256"
                )
                if server_hash is not None and server_hash.lower() != expected_sha256:
                    raise RelayError(f"{_label} hash mismatch")
                fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent))
                os.close(fd)
                temporary = Path(name)
                digest = hashlib.sha256()
                received = 0
                with temporary.open("wb") as stream:
                    while True:
                        chunk = opened.read(min(1024 * 1024, expected_length - received + 1))
                        if not chunk:
                            break
                        if not isinstance(chunk, (bytes, bytearray, memoryview)):
                            raise RelayError(f"{_label} response is invalid")
                        chunk = bytes(chunk)
                        received += len(chunk)
                        if received > expected_length:
                            raise RelayError(f"{_label} length mismatch")
                        digest.update(chunk)
                        stream.write(chunk)
                    if received != expected_length or digest.hexdigest() != expected_sha256:
                        raise RelayError(f"{_label} verification failed")
                    stream.flush()
                    os.fsync(stream.fileno())
            _reject_reparse(destination, label=f"{_label} destination")
            os.replace(temporary, destination)
            temporary = None
            if os.name != "nt":
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return destination
        except urllib.error.HTTPError as exc:
            error = f"{_label} download failed ({exc.code})"
            if _is_transient_relay_status(exc.code):
                raise TransientRelayError(error) from None
            raise RelayError(error) from None
        except ConnectorError:
            raise
        except OSError as exc:
            if _is_transient_network_error(exc):
                raise TransientRelayError(f"{_label} download failed") from exc
            raise RelayError(f"{_label} download failed") from exc
        except http.client.HTTPException as exc:
            raise RelayError(f"{_label} download failed") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def download_artifact(
        self,
        artifact_id: str,
        destination: Path | str,
        *,
        expected_sha256: str,
        expected_length: int,
        project: str | None = None,
        plan: str | None = None,
    ) -> Path:
        return self.download_asset(
            artifact_id,
            destination,
            expected_sha256=expected_sha256,
            expected_length=expected_length,
            project=project,
            plan=plan,
            _route="artifacts",
            _max_length=_MAX_ARTIFACT_BYTES,
            _label="artifact",
        )

    def post_result(
        self,
        command_id: str,
        sequence: int,
        result: Mapping[str, Any],
        *,
        project: str | None = None,
        plan: str | None = None,
    ) -> dict[str, Any] | None:
        command_id = _safe_id(command_id, "command id")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or not 1 <= sequence <= 1_000_000_000:
            raise RelayError("command sequence is invalid")
        if not isinstance(result, Mapping):
            raise RelayError("command result is invalid")
        headers: dict[str, str] = {}
        if project is not None:
            headers["X-Keepframe-Project"] = _safe_id(project, "project")
        query = {"plan": _safe_id(plan, "plan")} if plan is not None else None
        return self._request(
            "POST",
            f"/results/{command_id}",
            token=self.device_token or "",
            payload={"sequence": sequence, "result": dict(result)},
            query=query,
            headers=headers,
        )

    def renew_command(
        self,
        command_id: str,
        sequence: int,
        nonce: str,
        *,
        project: str | None = None,
        plan: str | None = None,
    ) -> dict[str, Any]:
        command_id = _safe_id(command_id, "command id")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or not 1 <= sequence <= 1_000_000_000:
            raise RelayError("command sequence is invalid")
        if not isinstance(nonce, str) or not nonce or len(nonce) > 512:
            raise RelayError("command nonce is invalid")
        headers: dict[str, str] = {}
        if project is not None:
            headers["X-Keepframe-Project"] = _safe_id(project, "project")
        query = {"plan": _safe_id(plan, "plan")} if plan is not None else None
        response = self._request(
            "POST",
            f"/renew/{command_id}",
            token=self.device_token or "",
            payload={"sequence": sequence, "nonce": nonce},
            query=query,
            headers=headers,
        )
        if not isinstance(response, Mapping) or not isinstance(response.get("command"), dict):
            raise RelayError("lease renewal response is invalid")
        return dict(response["command"])

    def upload_artifact(
        self,
        reservation_id: str,
        source: Path | str,
        *,
        project: str | None = None,
        plan: str | None = None,
        expected_length: int | None = None,
    ) -> dict[str, Any] | None:
        reservation_id = _safe_id(reservation_id, "artifact reservation")
        source = Path(source)
        _reject_reparse(source, label="artifact source")
        if not source.is_file():
            raise RelayError("artifact source is unavailable")
        try:
            length = source.stat().st_size
        except OSError as exc:
            raise RelayError("artifact source is unavailable") from exc
        if length > _MAX_ARTIFACT_BYTES or (expected_length is not None and length != expected_length):
            raise RelayError("artifact length is invalid")
        parsed = urllib.parse.urlsplit(self._url("/"))
        path = parsed.path.rstrip("/") + f"/artifacts/{reservation_id}"
        if plan is not None:
            path += "?" + urllib.parse.urlencode({"plan": _safe_id(plan, "plan")})
        host = parsed.hostname
        if not host:
            raise RelayError("relay URL is invalid")
        port = parsed.port
        factory = self._connection_factory
        if factory is None:
            factory = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection: Any = None
        try:
            connection = factory(host, port, timeout=self.timeout)
            connection.putrequest("PUT", path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", parsed.netloc)
            connection.putheader("Authorization", self._headers(self.device_token or "")["Authorization"])
            if project is not None:
                connection.putheader("X-Keepframe-Project", _safe_id(project, "project"))
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(length))
            connection.endheaders()
            with source.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
            response = connection.getresponse()
            return _response_json(response)
        except ConnectorError:
            raise
        except OSError as exc:
            if _is_transient_network_error(exc):
                raise TransientRelayError("artifact upload failed") from exc
            raise RelayError("artifact upload failed") from exc
        except http.client.HTTPException as exc:
            raise RelayError("artifact upload failed") from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (AttributeError, OSError):
                    pass

    stream_artifact = upload_artifact


class CommandJournal:
    def __init__(self, root: Path | str):
        self.path = Path(root) / COMMAND_JOURNAL_FILENAME
        self.scope_dir = self.path.parent / COMMAND_SCOPE_DIRNAME
        _reject_reparse_components(self.path.parent)
        _reject_reparse(self.path, label="command journal")
        if self.scope_dir.exists():
            _reject_reparse_components(self.scope_dir)
            if not self.scope_dir.is_dir():
                raise ConnectorError("command journal scope directory is unsafe")
        self._entries = self._load()

    @staticmethod
    def _scope(value: object) -> tuple[str, str, str]:
        if not isinstance(value, list) or len(value) != 3:
            raise ConnectorError("command journal is invalid")
        try:
            first, second, third = value
            return (
                _safe_id(first, "command scope"),
                _safe_id(second, "command scope"),
                _safe_id(third, "command scope"),
            )
        except ConnectorError as exc:
            raise ConnectorError("command journal is invalid") from exc

    @staticmethod
    def _scope_digest(scope: tuple[str, str, str]) -> str:
        return hashlib.sha256(canonical_json(list(scope))).hexdigest()

    @staticmethod
    def _scope_state(value: object) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or set(value) != {"plan_digest", "next_sequence"}
            or not isinstance(value["plan_digest"], str)
            or not _SHA256.fullmatch(value["plan_digest"])
            or not isinstance(value["next_sequence"], int)
            or isinstance(value["next_sequence"], bool)
            or not 1 <= value["next_sequence"] <= 1_000_000_001
        ):
            raise ConnectorError("command journal is invalid")
        return {
            "plan_digest": value["plan_digest"],
            "next_sequence": value["next_sequence"],
        }

    def _scope_path(self, scope: tuple[str, str, str]) -> Path:
        scope_values = self._scope(list(scope))
        _reject_reparse_components(self.scope_dir)
        path = self.scope_dir / f"{self._scope_digest(scope_values)}.json"
        _reject_reparse(path, label="command journal scope")
        return path

    def _ensure_scope_dir(self) -> None:
        _reject_reparse_components(self.scope_dir)
        if not self.scope_dir.exists():
            try:
                self.scope_dir.mkdir()
            except OSError as exc:
                raise ConnectorError("command journal scope directory is unavailable") from exc
        _reject_reparse(self.scope_dir, label="command journal scope directory")
        if not self.scope_dir.is_dir():
            raise ConnectorError("command journal scope directory is unsafe")

    def _read_scope(self, scope: tuple[str, str, str]) -> dict[str, Any] | None:
        path = self._scope_path(scope)
        if not self.scope_dir.exists():
            return None
        _reject_reparse(self.scope_dir, label="command journal scope directory")
        if not self.scope_dir.is_dir():
            raise ConnectorError("command journal scope directory is unsafe")
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConnectorError("command journal is invalid") from exc
        return self._scope_state(raw)

    def _entry_scope_state(self, scope: tuple[str, str, str]) -> dict[str, Any] | None:
        scope_list = list(scope)
        state: dict[str, Any] | None = None
        for entry in self._entries.values():
            if entry["scope"] != scope_list:
                continue
            entry_state = {
                "plan_digest": entry["plan_digest"],
                "next_sequence": entry["sequence"] + 1,
            }
            if state is not None and state["plan_digest"] != entry_state["plan_digest"]:
                raise ConnectorError("command journal is invalid")
            if state is None or entry_state["next_sequence"] > state["next_sequence"]:
                state = entry_state
        return state

    def _current_scope_state(self, scope: tuple[str, str, str]) -> dict[str, Any] | None:
        tombstone = self._read_scope(scope)
        entry_state = self._entry_scope_state(scope)
        if tombstone is None:
            return entry_state
        if entry_state is not None:
            if tombstone["plan_digest"] != entry_state["plan_digest"]:
                raise ConnectorError("command journal is invalid")
            if entry_state["next_sequence"] > tombstone["next_sequence"]:
                return entry_state
        return tombstone

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConnectorError("command journal is invalid") from exc
        if not isinstance(raw, dict):
            raise ConnectorError("command journal is invalid")
        if len(raw) > _MAX_JOURNAL_ENTRIES:
            raise ConnectorError("command journal is invalid")
        entries: dict[str, dict[str, Any]] = {}
        required_states: dict[str, tuple[tuple[str, str, str], str, int]] = {}
        for key, value in raw.items():
            if not _OPAQUE_ID.fullmatch(key) or not isinstance(value, dict):
                raise ConnectorError("command journal is invalid")
            sequence = value.get("sequence")
            fingerprint = value.get("fingerprint")
            plan_digest = value.get("plan_digest")
            scope = self._scope(value.get("scope"))
            acknowledged = value.get("acknowledged", False)
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or not 1 <= sequence <= 1_000_000_000
                or not isinstance(fingerprint, str)
                or not _SHA256.fullmatch(fingerprint)
                or not isinstance(plan_digest, str)
                or not _SHA256.fullmatch(plan_digest)
                or not isinstance(acknowledged, bool)
            ):
                raise ConnectorError("command journal is invalid")
            if not isinstance(value.get("result"), dict):
                raise ConnectorError("command journal is invalid")
            try:
                _json_bytes(value["result"])
            except ConnectorError as exc:
                raise ConnectorError("command journal is invalid") from exc
            scope_digest = self._scope_digest(scope)
            prior = required_states.get(scope_digest)
            next_sequence = sequence + 1
            if prior is not None:
                if prior[1] != plan_digest:
                    raise ConnectorError("command journal is invalid")
                next_sequence = max(prior[2], next_sequence)
            required_states[scope_digest] = (scope, plan_digest, next_sequence)
            entries[key] = {
                "scope": list(scope),
                "sequence": sequence,
                "fingerprint": fingerprint,
                "plan_digest": plan_digest,
                "result": dict(value["result"]),
                "acknowledged": acknowledged,
            }
        repairs: list[tuple[Path, dict[str, Any]]] = []
        for scope, plan_digest, next_sequence in required_states.values():
            state = self._read_scope(scope)
            if state is not None and state["plan_digest"] != plan_digest:
                raise ConnectorError("command journal is invalid")
            if state is None or next_sequence > state["next_sequence"]:
                repairs.append(
                    (
                        self._scope_path(scope),
                        {"plan_digest": plan_digest, "next_sequence": next_sequence},
                    )
                )
        if repairs:
            self._ensure_scope_dir()
            for path, state in repairs:
                _atomic_write(path, _json_bytes(state))
        return entries

    def _persist(self) -> None:
        _atomic_write(self.path, _json_bytes(self._entries))

    def _persist_scope(self, scope: tuple[str, str, str], plan_digest: str, sequence: int) -> None:
        self._ensure_scope_dir()
        _atomic_write(
            self._scope_path(scope),
            _json_bytes({"plan_digest": plan_digest, "next_sequence": sequence + 1}),
        )

    def get(self, command_id: str) -> dict[str, Any] | None:
        return self._entries.get(command_id)

    def check_scope(
        self,
        scope: tuple[str, str, str],
        plan_digest: str,
        sequence: int,
    ) -> None:
        scope_values = self._scope(list(scope))
        if not isinstance(plan_digest, str) or not _SHA256.fullmatch(plan_digest):
            raise ConnectorError("command plan digest is invalid")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 1 <= sequence <= 1_000_000_000
        ):
            raise ConnectorError("command sequence is invalid")
        state = self._current_scope_state(scope_values)
        if state is None:
            return
        if state["plan_digest"] != plan_digest:
            raise ConnectorError("command plan digest changed within scope")
        if sequence < state["next_sequence"]:
            raise ConnectorError("command sequence is not contiguous")

    def check_replay_scope(self, scope: tuple[str, str, str], plan_digest: str) -> None:
        scope_values = self._scope(list(scope))
        if not isinstance(plan_digest, str) or not _SHA256.fullmatch(plan_digest):
            raise ConnectorError("command plan digest is invalid")
        state = self._current_scope_state(scope_values)
        if state is None or state["plan_digest"] != plan_digest:
            raise ConnectorError("command plan digest changed within scope")

    def _prune_acknowledged(self, *, keep: str | None = None) -> None:
        while len(self._entries) >= _MAX_JOURNAL_ENTRIES:
            candidate = next(
                (
                    command_id
                    for command_id, value in self._entries.items()
                    if value.get("acknowledged") and command_id != keep
                ),
                None,
            )
            if candidate is None:
                raise ConnectorError("command replay journal is full")
            del self._entries[candidate]

    def record(
        self,
        command_id: str,
        sequence: int,
        scope: tuple[str, str, str],
        plan_digest: str,
        fingerprint: str,
        result: Mapping[str, Any],
    ) -> None:
        command_id = _safe_id(command_id, "command")
        scope_values = self._scope(list(scope))
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 1 <= sequence <= 1_000_000_000
        ):
            raise ConnectorError("command sequence is invalid")
        if not isinstance(plan_digest, str) or not _SHA256.fullmatch(plan_digest):
            raise ConnectorError("command plan digest is invalid")
        if not _SHA256.fullmatch(fingerprint):
            raise ConnectorError("command fingerprint is invalid")
        if not isinstance(result, Mapping):
            raise ConnectorError("command result is invalid")
        _json_bytes(result)
        self.check_scope(scope_values, plan_digest, sequence)
        entries_before = {key: dict(value) for key, value in self._entries.items()}
        self._prune_acknowledged(keep=command_id)
        self._ensure_scope_dir()
        self._entries[command_id] = {
            "scope": list(scope_values),
            "sequence": sequence,
            "fingerprint": fingerprint,
            "plan_digest": plan_digest,
            "result": dict(result),
            "acknowledged": False,
        }
        persisted = False
        try:
            self._persist()
            persisted = True
            self._persist_scope(scope_values, plan_digest, sequence)
        except ConnectorError:
            if not persisted:
                self._entries = entries_before
            raise

    def acknowledge(self, command_id: str) -> None:
        entry = self._entries.get(command_id)
        if entry is None or entry.get("acknowledged"):
            return
        entry["acknowledged"] = True
        try:
            self._persist()
        except ConnectorError:
            entry["acknowledged"] = False
            raise


class _CommandLeaseRenewal:
    def __init__(
        self,
        relay: Any,
        command_id: str,
        sequence: int,
        nonce: str,
        lease_seconds: float,
        lease_expires_at: float | None,
        *,
        project: str,
        plan: str,
    ) -> None:
        self._relay = relay
        self._command_id = command_id
        self._sequence = sequence
        self._nonce = nonce
        self._lease_seconds = lease_seconds
        self._lease_expires_at = lease_expires_at
        self._local_deadline = time.monotonic() + lease_seconds
        self._project = project
        self._plan = plan
        self._stop = threading.Event()
        self._failure: Exception | None = None
        self._draining = False
        self._thread: threading.Thread | None = None
        self._enabled = callable(getattr(relay, "renew_command", None))

    def _renew_once(self) -> None:
        if not self._enabled:
            return
        renew = self._relay.renew_command
        try:
            response = renew(
                self._command_id,
                self._sequence,
                self._nonce,
                project=self._project,
                plan=self._plan,
            )
            if not isinstance(response, Mapping):
                raise ConnectorError("lease renewal response is invalid")
            expiry = response.get("lease_expires_at")
            if (
                not isinstance(expiry, (int, float))
                or isinstance(expiry, bool)
                or not math.isfinite(float(expiry))
            ):
                raise ConnectorError("lease renewal response is invalid")
            lease_seconds = response.get("lease_seconds", self._lease_seconds)
            if (
                not isinstance(lease_seconds, (int, float))
                or isinstance(lease_seconds, bool)
                or not math.isfinite(float(lease_seconds))
                or not 0 < float(lease_seconds) <= 86_400
            ):
                raise ConnectorError("lease renewal response is invalid")
            self._lease_expires_at = float(expiry)
            self._lease_seconds = float(lease_seconds)
            self._local_deadline = time.monotonic() + self._lease_seconds
        except TransientRelayError:
            # A transport failure is safe to retry while the server-side
            # lease remains live; only a definitive relay rejection settles
            # the command as failed.
            return
        except RelayError as exc:
            if "command lease is draining" in str(exc).lower():
                self._draining = True
                self._stop.set()
                return
            self._failure = exc
            self._stop.set()
        except Exception as exc:  # noqa: BLE001 - lease loss stops the command
            self._failure = exc
            self._stop.set()

    def start(self) -> None:
        if not self._enabled:
            return
        self._renew_once()
        self.check()
        self._thread = threading.Thread(
            target=self._run,
            name="keepframe-ae-lease",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        interval = max(1.0, min(30.0, self._lease_seconds / 3.0))
        while not self._stop.is_set():
            remaining = self._local_deadline - time.monotonic()
            if remaining <= 0:
                self._failure = ConnectorError("command lease expired")
                self._stop.set()
                return
            if self._stop.wait(min(interval, max(1.0, remaining / 3.0))):
                return
            self._renew_once()

    def check(self) -> None:
        if self._enabled and self._failure is None and time.monotonic() >= self._local_deadline:
            self._failure = ConnectorError("command lease expired")
        if self._failure is not None:
            raise ConnectorError("command lease renewal failed") from self._failure

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None

class Connector:
    def __init__(
        self,
        relay: RelayClient,
        mcp: Any,
        *,
        project_id: str,
        device_id: str,
        plan_digest: str | None = None,
        capability_hash: str | None = None,
        private_root: Path | str,
        journal: CommandJournal | None = None,
        ffmpeg: str | None = None,
        process_runner: Callable[..., Any] | None = None,
    ) -> None:
        self.relay = relay
        self.mcp = mcp
        self.project_id = _safe_id(project_id, "project")
        if not _PAIRING_PROJECT.fullmatch(self.project_id):
            raise ConnectorError("project is not a safe directory name")
        self.device_id = _safe_id(device_id, "device")
        if plan_digest is not None and (not isinstance(plan_digest, str) or not _SHA256.fullmatch(plan_digest)):
            raise ConnectorError("plan digest is invalid")
        if capability_hash is not None and (
            not isinstance(capability_hash, str) or not _SHA256.fullmatch(capability_hash)
        ):
            raise ConnectorError("capability hash is invalid")
        if ffmpeg is not None and (not isinstance(ffmpeg, str) or not ffmpeg or "\x00" in ffmpeg):
            raise ConnectorError("ffmpeg is invalid")
        self.plan_digest = plan_digest
        self.capability_hash = capability_hash
        self.ffmpeg = ffmpeg
        self._process_runner = process_runner or subprocess.run
        self.private_root = Path(private_root)
        if not self.private_root.is_dir() or _is_reparse(self.private_root):
            raise ConnectorError("connector root is unsafe")
        self.project_root = self.private_root / "projects" / self.project_id
        _reject_reparse_components(self.project_root)
        try:
            self.project_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConnectorError("connector project directory is unavailable") from exc
        if _is_reparse(self.project_root) or not self.project_root.is_dir():
            raise ConnectorError("connector project directory is unsafe")
        self.assets_root = self.private_root / "assets"
        try:
            self.assets_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConnectorError("connector asset directory is unavailable") from exc
        if _is_reparse(self.assets_root) or not self.assets_root.is_dir():
            raise ConnectorError("connector asset directory is unsafe")
        self.journal = journal or CommandJournal(self.project_root)

    @staticmethod
    def _as_command(value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if not isinstance(value, dict):
            raise ConnectorError("command is invalid")
        return value

    @staticmethod
    def _normalize_mcp_result(
        raw: Any,
        command_id: str,
        nonce: str,
        *,
        tool: str | None = None,
        payload_digest: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise MCPError("MCP result is invalid")
        value = dict(raw)
        envelope_keys = {"schema_version", "command_id", "nonce"}
        is_envelope = bool(envelope_keys.intersection(value))
        if not is_envelope:
            direct_ok = value.get("ok")
            direct_error = value.get("error")
            if direct_ok is not None and not isinstance(direct_ok, bool):
                raise MCPError("MCP result status is invalid")
            if direct_ok is False:
                if not isinstance(direct_error, str) or not direct_error or len(direct_error) > _MAX_ERROR_BYTES:
                    raise MCPError("MCP result error is invalid")
            elif direct_error is not None and (
                not isinstance(direct_error, str) or not direct_error or len(direct_error) > _MAX_ERROR_BYTES
            ):
                raise MCPError("MCP result error is invalid")
            _json_bytes(value)
            return value
        allowed = envelope_keys | {"ok", "result", "kind", "payload_digest", "result_digest", "error"}
        if set(value) - allowed:
            raise MCPError("MCP result envelope is invalid")
        if value.get("schema_version") != 1:
            raise MCPError("MCP result envelope is invalid")
        if value.get("command_id") != command_id or value.get("nonce") != nonce:
            raise MCPError("MCP result identity does not match command")
        returned_kind = value.get("kind")
        if tool is not None and returned_kind is not None and returned_kind != tool:
            raise MCPError("MCP result kind does not match command")
        returned_payload_digest = value.get("payload_digest")
        if returned_payload_digest is not None:
            if (
                not isinstance(returned_payload_digest, str)
                or not _SHA256.fullmatch(returned_payload_digest)
                or (payload_digest is not None and returned_payload_digest != payload_digest)
            ):
                raise MCPError("MCP result payload digest does not match")
        ok = value.get("ok")
        if not isinstance(ok, bool):
            raise MCPError("MCP result status is invalid")
        result = value.get("result")
        if not isinstance(result, Mapping):
            raise MCPError("MCP result payload is invalid")
        result_dict = dict(result)
        _json_bytes(result_dict)
        result_digest = value.get("result_digest")
        if result_digest is not None:
            if not isinstance(result_digest, str) or not _SHA256.fullmatch(result_digest):
                raise MCPError("MCP result digest is invalid")
            if result_digest != _canonical_digest(result_dict):
                raise MCPError("MCP result digest does not match")
        error = value.get("error")
        if error is not None and (not isinstance(error, str) or not error or len(error) > _MAX_ERROR_BYTES):
            raise MCPError("MCP result error is invalid")
        if not ok:
            if error is None:
                raise MCPError("MCP result error is missing")
            return {"ok": False, "error": error}
        if error is not None:
            raise MCPError("MCP result error is unexpected")
        return result_dict
    @staticmethod
    def _strip_server_only_result_fields(result: Mapping[str, Any]) -> dict[str, Any]:
        output = dict(result)
        for key in _SERVER_ONLY_RESULT_FIELDS:
            output.pop(key, None)
        _json_bytes(output)
        return output
    def _compact_inspection_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        output = dict(result)
        inspection: Mapping[str, Any] | None = None
        shape = "direct"
        if isinstance(output.get("inspection"), Mapping):
            inspection = output["inspection"]
            shape = "inspection"
        elif isinstance(output.get("result"), Mapping) and isinstance(
            output["result"].get("inspection"), Mapping
        ):
            inspection = output["result"]["inspection"]
            shape = "nested"
        elif isinstance(output.get("schema_version"), str):
            inspection = output
        if inspection is None:
            raise ConnectorError("inspection result is missing inspection")
        heartbeat = inspection.get("heartbeat")
        if not isinstance(heartbeat, Mapping):
            raise ConnectorError("inspection heartbeat is missing")
        published_hash = heartbeat.get("capability_hash")
        if isinstance(heartbeat.get("capabilities"), Mapping):
            try:
                heartbeat = enrich_heartbeat_fonts(heartbeat)
                capabilities = AECapabilities.from_heartbeat(heartbeat)
            except (TypeError, ValueError, OSError) as exc:
                raise ConnectorError("inspection heartbeat is invalid") from exc
            published_hash = capabilities.capability_hash
            identity = {
                "capability_hash": capabilities.capability_hash,
                "version": capabilities.version,
                "major": capabilities.major,
                "host": capabilities.host,
            }
        else:
            if not isinstance(published_hash, str) or not _SHA256.fullmatch(published_hash):
                raise ConnectorError("inspection heartbeat is invalid")
            identity = {
                "capability_hash": published_hash,
                "version": heartbeat.get("version"),
                "major": heartbeat.get("major"),
                "host": heartbeat.get("host"),
            }
        if self.capability_hash is not None and published_hash != self.capability_hash:
            raise ConnectorError("inspection capabilities changed")
        compact = dict(inspection)
        compact["heartbeat"] = identity
        if shape == "inspection":
            output["inspection"] = compact
        elif shape == "nested":
            nested = dict(output["result"])
            nested["inspection"] = compact
            output["result"] = nested
        else:
            output = compact
        _json_bytes(output)
        return output

    def _refresh_live_capabilities(
        self,
        command_id: str,
        nonce: str,
        plan_id: str,
        session_id: str,
    ) -> AECapabilities:
        if self.capability_hash is None:
            raise ConnectorError("approved capabilities are unavailable")
        payload = {
            "project_id": self.project_id,
            "plan_id": plan_id,
            "session_id": session_id,
        }
        probe_command_id = "command-capability-" + secrets.token_hex(8)
        probe_nonce = "nonce-capability-" + secrets.token_hex(8)
        raw_result = self.mcp.call_tool(
            "capability_heartbeat",
            {
                "command_id": probe_command_id,
                "nonce": probe_nonce,
                "payload": payload,
            },
        )
        result = self._normalize_mcp_result(
            raw_result,
            probe_command_id,
            probe_nonce,
            tool="capability_heartbeat",
            payload_digest=_canonical_digest(payload),
        )
        heartbeat = result.get("heartbeat", result)
        if not isinstance(heartbeat, Mapping):
            raise ConnectorError("live capability heartbeat is invalid")
        heartbeat = enrich_heartbeat_fonts(heartbeat)
        try:
            capabilities = AECapabilities.from_heartbeat(heartbeat)
        except (TypeError, ValueError) as exc:
            raise ConnectorError("live capability heartbeat is invalid") from exc
        if capabilities.capability_hash != self.capability_hash:
            raise ConnectorError("capabilities changed")
        return capabilities

    @staticmethod
    def _local_failure_result(kind: str) -> dict[str, Any]:
        error = (
            "connector preview processing failed"
            if kind == "render_preview"
            else "connector artifact processing failed"
        )
        return {"ok": False, "error": error}

    @staticmethod
    def _local_filename(value: object, label: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", value):
            raise ConnectorError(f"{label} is invalid")
        return value

    def _prepare_assets(
        self,
        payload: Mapping[str, Any],
        plan_id: str,
        session_id: str,
    ) -> None:
        records = payload.get("assets")
        if records is None:
            return
        if not isinstance(records, list) or len(records) > 1024:
            raise ConnectorError("command assets are invalid")
        session_media = (
            self._scope_root(plan_id, session_id) / "package" / "collected_media"
        )
        _reject_reparse_components(session_media)
        session_media.mkdir(parents=True, exist_ok=True)
        _reject_reparse_components(session_media)
        for item in records:
            if not isinstance(item, Mapping):
                raise ConnectorError("command asset is invalid")
            asset_id = _safe_id(item.get("id", item.get("asset_id")), "asset id")
            asset_name = self._local_filename(asset_id, "asset id")
            digest = item.get("sha256")
            length = item.get("length")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ConnectorError("command asset hash is invalid")
            if not isinstance(length, int) or isinstance(length, bool) or not 0 <= length <= _MAX_ASSET_BYTES:
                raise ConnectorError("command asset length is invalid")
            cached = self.assets_root / asset_name
            _reject_reparse(cached, label="asset destination")
            if cached.exists():
                cached_digest, cached_length = self._hash_file(cached)
                if cached_digest != digest or cached_length != length:
                    raise ConnectorError("immutable command asset does not match")
            else:
                self.relay.download_asset(
                    asset_id,
                    cached,
                    expected_sha256=digest,
                    expected_length=length,
                    project=self.project_id,
                    plan=plan_id,
                )
            destination = session_media / asset_name
            _reject_reparse(destination, label="session media")
            if destination.exists():
                local_digest, local_length = self._hash_file(destination)
                if local_digest != digest or local_length != length:
                    raise ConnectorError("session media does not match immutable asset")
                continue
            temporary = session_media / f".{asset_name}.{secrets.token_hex(8)}.tmp"
            try:
                with cached.open("rb") as source, temporary.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, destination)
                _reject_reparse(destination, label="session media")
            except OSError as exc:
                raise ConnectorError("session media materialization failed") from exc
            finally:
                temporary.unlink(missing_ok=True)


    def _prepare_checkpoint(
        self,
        payload: Mapping[str, Any],
        plan_id: str,
        session_id: str,
    ) -> None:
        record = payload.get("checkpoint_artifact")
        if record is None:
            return
        if not isinstance(record, Mapping):
            raise ConnectorError("checkpoint artifact is invalid")
        if set(record) - {"id", "reservation_id", "sha256", "length"}:
            raise ConnectorError("checkpoint artifact is invalid")
        artifact_id_value = record.get("id")
        reservation_id_value = record.get("reservation_id")
        if (
            artifact_id_value is not None
            and reservation_id_value is not None
            and artifact_id_value != reservation_id_value
        ):
            raise ConnectorError("checkpoint artifact identity is ambiguous")
        artifact_id = artifact_id_value or reservation_id_value
        artifact_id = _safe_id(artifact_id, "checkpoint artifact")
        digest = record.get("sha256")
        length = record.get("length")
        if (
            not isinstance(length, int)
            or isinstance(length, bool)
            or not 0 <= length <= _MAX_ARTIFACT_BYTES
        ):
            raise ConnectorError("checkpoint artifact length is invalid")
        checkpoint_index = self._preview_int(
            payload.get("checkpoint_index"),
            "checkpoint index",
        )
        scope = self._scope_root(plan_id, session_id)
        checkpoints = scope / "checkpoints"
        _reject_reparse_components(checkpoints)
        checkpoints.mkdir(parents=True, exist_ok=True)
        _reject_reparse_components(checkpoints)
        destination = checkpoints / f"checkpoint-{checkpoint_index}.aep"
        _reject_reparse(destination, label="checkpoint destination")
        if destination.is_file() and destination.stat().st_size == length:
            local_digest = hashlib.sha256()
            with destination.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    local_digest.update(chunk)
            if local_digest.hexdigest() == digest:
                return
        self.relay.download_artifact(
            artifact_id,
            destination,
            expected_sha256=digest,
            expected_length=length,
            project=self.project_id,
            plan=plan_id,
        )
        _reject_reparse(destination, label="checkpoint destination")
        if not destination.is_file() or destination.stat().st_size != length:
            raise ConnectorError("checkpoint artifact materialization failed")
        local_digest = hashlib.sha256()
        with destination.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                local_digest.update(chunk)
        if local_digest.hexdigest() != digest:
            raise ConnectorError("checkpoint artifact digest mismatch")

    @staticmethod
    def _preview_int(value: object, label: str, *, maximum: int = 1_000_000) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
            raise ConnectorError(f"{label} is invalid")
        return value

    @staticmethod
    def _preview_fps(value: object, label: str = "preview fps") -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or not 0 < float(value) <= 99
        ):
            raise ConnectorError(f"{label} is invalid")
        return float(value)

    @classmethod
    def _preview_spec(
        cls,
        payload: Mapping[str, Any],
    ) -> tuple[int, int, float, list[int], list[str]]:
        checkpoint = cls._preview_int(payload.get("checkpoint_index"), "preview checkpoint")
        frame_count = cls._preview_int(payload.get("frame_count"), "preview frame count")
        if frame_count < 1:
            raise ConnectorError("preview frame count is invalid")
        fps = cls._preview_fps(payload.get("fps"))
        raw_representatives = payload.get("representative_frames")
        if raw_representatives is None:
            raw_representatives = payload.get("selected_frames")
        if (
            not isinstance(raw_representatives, list)
            or not raw_representatives
            or len(raw_representatives) > 12
        ):
            raise ConnectorError("preview representative frames are invalid")
        representatives = [
            cls._preview_int(item, "preview representative frame", maximum=frame_count - 1)
            for item in raw_representatives
        ]
        if len(set(representatives)) != len(representatives):
            raise ConnectorError("preview representative frames are invalid")
        names = [
            f"checkpoint-{checkpoint}-{frame:06d}.png"
            for frame in range(frame_count)
        ]
        return checkpoint, frame_count, fps, representatives, names

    @staticmethod
    def _same_preview_fps(value: object, expected: float) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) == expected
        )

    def _validate_preview_result(
        self,
        result: Mapping[str, Any],
        *,
        checkpoint: int,
        frame_count: int,
        fps: float,
        representatives: list[int],
        sequence_names: list[str],
    ) -> None:
        if result.get("rendered") is not True or result.get("kind") != "preview":
            raise ConnectorError("preview result is not successful")
        for key in ("path", "filepath", "sequence_path", "directory_path"):
            if key in result:
                raise ConnectorError("preview result contains an unsafe path")
        if result.get("checkpoint_index") != checkpoint:
            raise ConnectorError("preview checkpoint metadata does not match")
        if result.get("frame_count") != frame_count:
            raise ConnectorError("preview frame count metadata does not match")
        if not self._same_preview_fps(result.get("fps"), fps):
            raise ConnectorError("preview fps metadata does not match")
        for name, maximum in (("width", 1280), ("height", 720)):
            value = result.get(name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 < value <= maximum
            ):
                raise ConnectorError("preview dimensions are invalid")
        if result.get("representative_frames") != representatives:
            raise ConnectorError("preview representative frames do not match")
        sequence = result.get("sequence")
        if not isinstance(sequence, Mapping):
            raise ConnectorError("preview sequence metadata is missing")
        allowed = {
            "directory",
            "pattern",
            "files",
            "frame_count",
            "first_frame",
            "last_frame",
        }
        if set(sequence) - allowed:
            raise ConnectorError("preview sequence metadata contains unsupported fields")
        if sequence.get("directory") != "renders":
            raise ConnectorError("preview sequence directory is invalid")
        if sequence.get("pattern") not in {
            "checkpoint-N-%06d.png",
            f"checkpoint-{checkpoint}-%06d.png",
        }:
            raise ConnectorError("preview sequence pattern does not match")
        if sequence.get("frame_count") != frame_count:
            raise ConnectorError("preview sequence frame count does not match")
        if "files" in sequence:
            if sequence.get("first_frame") != 0 or sequence.get("last_frame") != frame_count - 1:
                raise ConnectorError("preview sequence frame bounds do not match")
            if sequence.get("files") != sequence_names:
                raise ConnectorError("preview sequence filenames do not match")
        elif (
            sequence.get("first_frame") != sequence_names[0]
            or sequence.get("last_frame") != sequence_names[-1]
        ):
            raise ConnectorError("preview sequence frame bounds do not match")
        representative_files = result.get("representative_files")
        expected_representative_files = [sequence_names[frame] for frame in representatives]
        if representative_files != expected_representative_files:
            raise ConnectorError("preview representative filenames do not match")

    def _preview_artifacts(
        self,
        payload: Mapping[str, Any],
        *,
        checkpoint: int,
        representatives: list[int],
    ) -> list[dict[str, Any]]:
        records = payload.get("artifacts")
        if not isinstance(records, list) or len(records) != len(representatives) + 1:
            raise ConnectorError("preview artifact records are invalid")
        expected_mp4 = f"checkpoint-{checkpoint}.mp4"
        expected_png = {
            frame: f"checkpoint-{checkpoint}-{frame:06d}.png"
            for frame in representatives
        }
        mp4: dict[str, Any] | None = None
        png_by_frame: dict[int, dict[str, Any]] = {}
        for value in records:
            if not isinstance(value, Mapping):
                raise ConnectorError("preview artifact record is invalid")
            item = dict(value)
            if item.get("directory", "renders") != "renders":
                raise ConnectorError("preview artifact directory is invalid")
            kind = item.get("kind")
            filename = self._local_filename(item.get("filename"), "artifact filename")
            if kind == "mp4":
                if mp4 is not None or filename != expected_mp4:
                    raise ConnectorError("preview MP4 artifact is invalid")
                mp4 = item
                continue
            if kind != "png":
                raise ConnectorError("preview artifact kind is invalid")
            matches = [frame for frame, name in expected_png.items() if name == filename]
            if len(matches) != 1 or matches[0] in png_by_frame:
                raise ConnectorError("preview representative artifact is invalid")
            png_by_frame[matches[0]] = item
        if mp4 is None or len(png_by_frame) != len(representatives):
            raise ConnectorError("preview artifact records are incomplete")
        return [mp4, *(png_by_frame[frame] for frame in representatives)]

    def _scope_root(self, plan_id: str, session_id: str) -> Path:
        plan_id = self._local_filename(plan_id, "plan")
        session_id = self._local_filename(session_id, "session")
        scope = self.project_root / "plans" / plan_id / "sessions" / session_id
        _reject_reparse_components(scope)
        try:
            scope.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConnectorError("connector session directory is unavailable") from exc
        _reject_reparse_components(scope)
        if not scope.is_dir() or _is_reparse(scope):
            raise ConnectorError("connector session directory is unsafe")
        return scope

    def _renders_directory(self, plan_id: str, session_id: str) -> Path:
        renders = self._scope_root(plan_id, session_id) / "renders"
        _reject_reparse_components(renders)
        try:
            renders.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConnectorError("render directory is unavailable") from exc
        if not renders.is_dir() or _is_reparse(renders):
            raise ConnectorError("render directory is unsafe")
        return renders

    @staticmethod
    def _validate_png_sequence_file(
        path: Path,
        *,
        max_width: int = 1280,
        max_height: int = 720,
        label: str = "preview",
        max_scan_bytes: int = _MAX_PNG_SCAN_BYTES,
    ) -> tuple[int, int]:
        _reject_reparse(path, label=f"{label} sequence file")
        try:
            if not path.is_file() or path.stat().st_size < len(_PNG_SIGNATURE):
                raise ConnectorError("preview sequence file is invalid")
            with path.open("rb") as stream:
                if stream.read(len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
                    raise ConnectorError("preview sequence file is not PNG")
                scanned = len(_PNG_SIGNATURE)
                seen_ihdr = False
                seen_iend = False
                width = 0
                height = 0

                def read_exact(length: int) -> bytes:
                    chunks: list[bytes] = []
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise ConnectorError("preview PNG is truncated")
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    return b"".join(chunks)

                def skip_exact(length: int) -> None:
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise ConnectorError("preview PNG is truncated")
                        remaining -= len(chunk)

                while not seen_iend:
                    if scanned + 12 > max_scan_bytes:
                        raise ConnectorError(f"{label} PNG is too large to validate")
                    header = read_exact(8)
                    scanned += 8
                    length = struct.unpack(">I", header[:4])[0]
                    chunk_type = header[4:]
                    if any(byte < 65 or byte > 122 or 90 < byte < 97 for byte in chunk_type):
                        raise ConnectorError("preview PNG chunk type is invalid")
                    if length > max_scan_bytes or scanned + length + 4 > max_scan_bytes:
                        raise ConnectorError(f"{label} PNG chunk is too large")
                    if not seen_ihdr:
                        if chunk_type != b"IHDR" or length != 13:
                            raise ConnectorError("preview PNG IHDR is invalid")
                        ihdr = read_exact(length)
                        scanned += length
                        crc = struct.unpack(">I", read_exact(4))[0]
                        scanned += 4
                        if crc != zlib.crc32(chunk_type + ihdr) & 0xFFFFFFFF:
                            raise ConnectorError("preview PNG IHDR checksum is invalid")
                        width, height = struct.unpack(">II", ihdr[:8])
                        bit_depth, color_type, compression, filter_method, interlace = ihdr[8:]
                        valid_depths = {
                            0: {1, 2, 4, 8, 16},
                            2: {8, 16},
                            3: {1, 2, 4, 8},
                            4: {8, 16},
                            6: {8, 16},
                        }
                        if (
                            not 0 < width <= max_width
                            or not 0 < height <= max_height
                            or width * height > _MAX_FINAL_PIXELS
                            or color_type not in valid_depths
                            or bit_depth not in valid_depths[color_type]
                            or compression != 0
                            or filter_method != 0
                            or interlace not in {0, 1}
                        ):
                            raise ConnectorError(f"{label} PNG IHDR structure is invalid")
                        seen_ihdr = True
                        continue
                    if chunk_type == b"IHDR":
                        raise ConnectorError("preview PNG has duplicate IHDR")
                    if chunk_type == b"IEND":
                        if length != 0:
                            raise ConnectorError("preview PNG IEND is invalid")
                        crc = read_exact(4)
                        scanned += 4
                        if len(crc) != 4 or struct.unpack(">I", crc)[0] != zlib.crc32(chunk_type) & 0xFFFFFFFF:
                            raise ConnectorError("preview PNG IEND checksum is invalid")
                        if stream.read(1):
                            raise ConnectorError("preview PNG has trailing data")
                        seen_iend = True
                        continue
                    skip_exact(length)
                    read_exact(4)
                    scanned += length + 4
                if not seen_ihdr:
                    raise ConnectorError("preview PNG is missing IHDR")
                return width, height
        except OSError as exc:
            raise ConnectorError("preview sequence file is unavailable") from exc

    def _run_preview_ffmpeg(
        self,
        *,
        renders: Path,
        checkpoint: int,
        frame_count: int,
        fps: float,
        sequence_names: list[str],
    ) -> Path:
        if self.ffmpeg is None:
            raise ConnectorError("preflighted ffmpeg is unavailable")
        dimensions: tuple[int, int] | None = None
        for filename in sequence_names:
            current = self._validate_png_sequence_file(renders / filename)
            if dimensions is None:
                dimensions = current
            elif current != dimensions:
                raise ConnectorError("preview PNG dimensions do not match")
        output = renders / f"checkpoint-{checkpoint}.mp4"
        _reject_reparse(output, label="preview MP4")
        argv = [
            self.ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-framerate",
            str(fps),
            "-i",
            str(renders / f"checkpoint-{checkpoint}-%06d.png"),
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "-frames:v",
            str(frame_count),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "18",
            str(output),
        ]

        def check_output(stream: Any) -> None:
            total = 0
            while True:
                chunk = stream.read(min(64 * 1024, _MAX_FFMPEG_OUTPUT_BYTES - total + 1))
                if not chunk:
                    return
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise ConnectorError("preview ffmpeg output is invalid")
                total += len(chunk)
                if total > _MAX_FFMPEG_OUTPUT_BYTES:
                    raise ConnectorError("preview ffmpeg output is too large")

        try:
            with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
                mode="w+b"
            ) as stderr_file:
                completed = self._process_runner(
                    argv,
                    check=True,
                    shell=False,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=_FFMPEG_TIMEOUT,
                    cwd=str(self.private_root),
                    env=build_child_env(),
                )
                stdout_file.flush()
                stderr_file.flush()
                stdout_file.seek(0)
                stderr_file.seek(0)
                check_output(stdout_file)
                check_output(stderr_file)
        except subprocess.TimeoutExpired as exc:
            raise ConnectorError("preview ffmpeg timed out") from exc
        except ConnectorError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConnectorError("preview ffmpeg failed") from exc
        except Exception as exc:  # noqa: BLE001 - process failures fail closed
            raise ConnectorError("preview ffmpeg failed") from exc
        for stream in ("stdout", "stderr"):
            output_bytes = getattr(completed, stream, None)
            if output_bytes is None:
                continue
            if isinstance(output_bytes, str):
                output_bytes = output_bytes.encode("utf-8", "replace")
            if not isinstance(output_bytes, (bytes, bytearray, memoryview)):
                raise ConnectorError("preview ffmpeg output is invalid")
            if len(output_bytes) > _MAX_FFMPEG_OUTPUT_BYTES:
                raise ConnectorError("preview ffmpeg output is too large")
        _reject_reparse(output, label="preview MP4")
        try:
            if not output.is_file() or output.stat().st_size <= 0:
                raise ConnectorError("preview MP4 was not created")
        except OSError as exc:
            raise ConnectorError("preview MP4 is unavailable") from exc
        return output


    def _render_preview(
        self,
        payload: Mapping[str, Any],
        result: Mapping[str, Any],
        plan_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        checkpoint, frame_count, fps, representatives, sequence_names = self._preview_spec(payload)
        self._validate_preview_result(
            result,
            checkpoint=checkpoint,
            frame_count=frame_count,
            fps=fps,
            representatives=representatives,
            sequence_names=sequence_names,
        )
        artifacts = self._preview_artifacts(
            payload,
            checkpoint=checkpoint,
            representatives=representatives,
        )
        renders = self._renders_directory(plan_id, session_id)
        self._run_preview_ffmpeg(
            renders=renders,
            checkpoint=checkpoint,
            frame_count=frame_count,
            fps=fps,
            sequence_names=sequence_names,
        )
        upload_payload = dict(payload)
        upload_payload["artifacts"] = artifacts
        return self._upload_artifacts(upload_payload, result, plan_id, session_id)

    @classmethod
    def _final_spec(
        cls,
        payload: Mapping[str, Any],
    ) -> tuple[int, int, float, int, int, list[str]]:
        checkpoint = cls._preview_int(payload.get("checkpoint_index"), "final checkpoint")
        frame_count = cls._preview_int(
            payload.get("frame_count"),
            "final frame count",
            maximum=_MAX_FINAL_FRAMES,
        )
        if frame_count < 1:
            raise ConnectorError("final frame count is invalid")
        fps = cls._preview_fps(payload.get("fps"), "final fps")
        width = cls._preview_int(
            payload.get("width"),
            "final width",
            maximum=_MAX_FINAL_DIMENSION,
        )
        height = cls._preview_int(
            payload.get("height"),
            "final height",
            maximum=_MAX_FINAL_DIMENSION,
        )
        if width < 1 or height < 1 or width * height > _MAX_FINAL_PIXELS:
            raise ConnectorError("final dimensions are invalid")
        if frame_count * width * height > _MAX_FINAL_TOTAL_PIXELS:
            raise ConnectorError("final render exceeds the resource budget")
        final_plan_digest = payload.get("final_plan_digest")
        if not isinstance(final_plan_digest, str) or not _SHA256.fullmatch(
            final_plan_digest
        ):
            raise ConnectorError("final plan digest is invalid")
        _credential(payload.get("finalization_key"), "finalization key")
        names = [f"final-{frame:06d}.png" for frame in range(frame_count)]
        return checkpoint, frame_count, fps, width, height, names

    @staticmethod
    def _validate_final_result(
        result: Mapping[str, Any],
        *,
        checkpoint: int,
        frame_count: int,
        fps: float,
        width: int,
        height: int,
        sequence_names: list[str],
    ) -> None:
        if result.get("rendered") is not True or result.get("kind") != "final":
            raise ConnectorError("final result is not successful")
        for key in ("path", "filepath", "sequence_path", "directory_path"):
            if key in result:
                raise ConnectorError("final result contains an unsafe path")
        if result.get("checkpoint_index") != checkpoint:
            raise ConnectorError("final checkpoint metadata does not match")
        if result.get("frame_count") != frame_count:
            raise ConnectorError("final frame count metadata does not match")
        if not Connector._same_preview_fps(result.get("fps"), fps):
            raise ConnectorError("final fps metadata does not match")
        if result.get("width") != width or result.get("height") != height:
            raise ConnectorError("final dimensions metadata does not match")
        sequence = result.get("sequence")
        if not isinstance(sequence, Mapping):
            raise ConnectorError("final sequence metadata is missing")
        if set(sequence) != {
            "directory",
            "pattern",
            "frame_count",
            "first_frame",
            "last_frame",
        }:
            raise ConnectorError("final sequence metadata is invalid")
        if (
            sequence.get("directory") != "renders"
            or sequence.get("pattern") != _FINAL_SEQUENCE_PATTERN
            or sequence.get("frame_count") != frame_count
            or sequence.get("first_frame") != sequence_names[0]
            or sequence.get("last_frame") != sequence_names[-1]
        ):
            raise ConnectorError("final sequence metadata does not match")
        duration = result.get("duration")
        if duration is not None and (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(float(duration))
            or float(duration) != frame_count / fps
        ):
            raise ConnectorError("final duration metadata does not match")
        frame_rate = result.get("frame_rate")
        if frame_rate is not None and not Connector._same_preview_fps(frame_rate, fps):
            raise ConnectorError("final frame rate metadata does not match")

    @staticmethod
    def _final_artifacts(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        records = payload.get("artifacts")
        if not isinstance(records, list) or len(records) != 1:
            raise ConnectorError("final artifact records are invalid")
        item = records[0]
        if not isinstance(item, Mapping):
            raise ConnectorError("final artifact record is invalid")
        allowed = {"reservation_id", "kind", "filename", "directory", "length"}
        if set(item) - allowed:
            raise ConnectorError("final artifact record is invalid")
        if (
            item.get("kind") != "mp4"
            or item.get("filename") != "final.mp4"
            or item.get("directory") != "renders"
        ):
            raise ConnectorError("final MP4 artifact is invalid")
        return [dict(item)]

    def _run_final_ffmpeg(
        self,
        *,
        renders: Path,
        frame_count: int,
        fps: float,
        sequence_names: list[str],
        width: int,
        height: int,
    ) -> Path:
        if self.ffmpeg is None:
            raise ConnectorError("preflighted ffmpeg is unavailable")
        dimensions: tuple[int, int] | None = None
        for filename in sequence_names:
            current = self._validate_png_sequence_file(
                renders / filename,
                max_width=_MAX_FINAL_DIMENSION,
                max_height=_MAX_FINAL_DIMENSION,
                label="final",
                max_scan_bytes=width * height * 8 + height + 1024 * 1024,
            )
            if current != (width, height):
                raise ConnectorError("final PNG dimensions do not match")
            if dimensions is None:
                dimensions = current
            elif current != dimensions:
                raise ConnectorError("final PNG dimensions do not match")
        expected = set(sequence_names)
        try:
            for entry in renders.iterdir():
                if entry.name.startswith("final-") and entry.name.endswith(".png"):
                    _reject_reparse(entry, label="final sequence file")
                    if entry.name not in expected:
                        raise ConnectorError("final PNG sequence contains an extra frame")
        except OSError as exc:
            raise ConnectorError("final sequence directory is unavailable") from exc
        output = renders / "final.mp4"
        _reject_reparse(output, label="final MP4")
        argv = [
            self.ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-framerate",
            str(fps),
            "-i",
            str(renders / _FINAL_SEQUENCE_PATTERN),
            "-frames:v",
            str(frame_count),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "18",
            str(output),
        ]

        def check_output(stream: Any) -> None:
            total = 0
            while True:
                chunk = stream.read(min(64 * 1024, _MAX_FFMPEG_OUTPUT_BYTES - total + 1))
                if not chunk:
                    return
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise ConnectorError("final ffmpeg output is invalid")
                total += len(chunk)
                if total > _MAX_FFMPEG_OUTPUT_BYTES:
                    raise ConnectorError("final ffmpeg output is too large")

        try:
            with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
                mode="w+b"
            ) as stderr_file:
                completed = self._process_runner(
                    argv,
                    check=True,
                    shell=False,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=_FFMPEG_TIMEOUT,
                    cwd=str(self.private_root),
                    env=build_child_env(),
                )
                stdout_file.flush()
                stderr_file.flush()
                stdout_file.seek(0)
                stderr_file.seek(0)
                check_output(stdout_file)
                check_output(stderr_file)
        except subprocess.TimeoutExpired as exc:
            raise ConnectorError("final ffmpeg timed out") from exc
        except ConnectorError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConnectorError("final ffmpeg failed") from exc
        except Exception as exc:  # noqa: BLE001 - process failures fail closed
            raise ConnectorError("final ffmpeg failed") from exc
        for stream in ("stdout", "stderr"):
            output_bytes = getattr(completed, stream, None)
            if output_bytes is None:
                continue
            if isinstance(output_bytes, str):
                output_bytes = output_bytes.encode("utf-8", "replace")
            if not isinstance(output_bytes, (bytes, bytearray, memoryview)):
                raise ConnectorError("final ffmpeg output is invalid")
            if len(output_bytes) > _MAX_FFMPEG_OUTPUT_BYTES:
                raise ConnectorError("final ffmpeg output is too large")
        _reject_reparse(output, label="final MP4")
        try:
            if not output.is_file() or output.stat().st_size <= 0:
                raise ConnectorError("final MP4 was not created")
        except OSError as exc:
            raise ConnectorError("final MP4 is unavailable") from exc
        return output

    def _render_final(
        self,
        payload: Mapping[str, Any],
        result: Mapping[str, Any],
        plan_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        checkpoint, frame_count, fps, width, height, sequence_names = self._final_spec(
            payload
        )
        self._validate_final_result(
            result,
            checkpoint=checkpoint,
            frame_count=frame_count,
            fps=fps,
            width=width,
            height=height,
            sequence_names=sequence_names,
        )
        artifacts = self._final_artifacts(payload)
        renders = self._renders_directory(plan_id, session_id)
        self._run_final_ffmpeg(
            renders=renders,
            frame_count=frame_count,
            fps=fps,
            sequence_names=sequence_names,
            width=width,
            height=height,
        )
        upload_payload = dict(payload)
        upload_payload["artifacts"] = artifacts
        return self._upload_artifacts(upload_payload, result, plan_id, session_id)

    @staticmethod
    def _safe_package_filename(value: object) -> str | None:
        if not isinstance(value, str) or not re.fullmatch(
            rf"[A-Za-z0-9][A-Za-z0-9_.-]{{0,{_MAX_PACKAGE_FILENAME - 1}}}",
            value,
        ):
            return None
        return value

    @staticmethod
    def _hash_file(path: Path, *, maximum: int = _MAX_ARTIFACT_BYTES) -> tuple[str, int]:
        _reject_reparse(path, label="media file")
        try:
            if not path.is_file():
                raise ConnectorError("media file is unavailable")
            size = path.stat().st_size
            if size < 0 or size > maximum:
                raise ConnectorError("media file is too large")
            digest = hashlib.sha256()
            length = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    length += len(chunk)
                    if length > maximum:
                        raise ConnectorError("media file is too large")
                    digest.update(chunk)
            return digest.hexdigest(), length
        except OSError as exc:
            raise ConnectorError("media file is unavailable") from exc

    @classmethod
    def _package_artifacts(
        cls,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        records = payload.get("artifacts")
        if not isinstance(records, list) or len(records) != 2:
            raise ConnectorError("package artifact records are invalid")
        aep: dict[str, Any] | None = None
        archive: dict[str, Any] | None = None
        for raw in records:
            if not isinstance(raw, Mapping):
                raise ConnectorError("package artifact record is invalid")
            item = dict(raw)
            if set(item) - {"reservation_id", "kind", "filename", "directory", "length"}:
                raise ConnectorError("package artifact record is invalid")
            kind = item.get("kind")
            filename = item.get("filename")
            directory = item.get("directory")
            if kind == "aep" and filename == "project.aep":
                if aep is not None:
                    raise ConnectorError("package AEP artifact is duplicated")
                aep = item
            elif kind == "zip" and filename == "project.zip":
                if archive is not None:
                    raise ConnectorError("package ZIP artifact is duplicated")
                archive = item
            else:
                raise ConnectorError("package artifact is invalid")
            if directory not in {"checkpoints", "renders", "package"}:
                raise ConnectorError("package artifact directory is invalid")
        if aep is None or archive is None:
            raise ConnectorError("package artifacts are incomplete")
        return aep, archive

    @classmethod
    def _package_media(
        cls,
        payload: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        assets = payload.get("assets")
        media = payload.get("package_media")
        if not isinstance(assets, list) or len(assets) > _MAX_PACKAGE_MEDIA:
            raise ConnectorError("package assets are invalid")
        if not isinstance(media, list) or len(media) > _MAX_PACKAGE_MEDIA:
            raise ConnectorError("package media is invalid")
        by_id: dict[str, dict[str, Any]] = {}
        for raw in assets:
            if not isinstance(raw, Mapping):
                raise ConnectorError("package asset is invalid")
            asset_id = cls._local_filename(raw.get("id", raw.get("asset_id")), "asset id")
            digest = raw.get("sha256")
            length = raw.get("length")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ConnectorError("package asset hash is invalid")
            if not isinstance(length, int) or isinstance(length, bool) or not 0 <= length <= _MAX_ASSET_BYTES:
                raise ConnectorError("package asset length is invalid")
            if asset_id in by_id:
                raise ConnectorError("package assets contain duplicates")
            by_id[asset_id] = {
                "id": asset_id,
                "sha256": digest,
                "length": length,
                "media_kind": raw.get("media_kind"),
                "role": raw.get("role"),
            }
        normalized: list[dict[str, Any]] = []
        names: set[str] = {"project.aep", "dependencies.json"}
        for raw in media:
            if not isinstance(raw, Mapping):
                raise ConnectorError("package media record is invalid")
            asset_id = cls._local_filename(raw.get("asset_id", raw.get("id")), "package media asset")
            source = by_id.get(asset_id)
            if source is None:
                raise ConnectorError("package media references an unlisted asset")
            digest = raw.get("sha256")
            length = raw.get("length")
            if digest != source["sha256"] or length != source["length"]:
                raise ConnectorError("package media metadata does not match asset")
            filename = cls._safe_package_filename(raw.get("filename"))
            if filename != asset_id or filename in names:
                raise ConnectorError("package media filename is invalid")
            names.add(filename)
            item = {
                "asset_id": asset_id,
                "filename": filename,
                "sha256": source["sha256"],
                "length": source["length"],
                "media_kind": raw.get("media_kind", source.get("media_kind")),
                "role": raw.get("role", source.get("role")),
            }
            _json_bytes(item)
            normalized.append(item)
        normalized.sort(key=lambda item: (item["filename"], item["asset_id"]))
        return normalized

    def _copy_package_media(
        self,
        scope: Path,
        records: list[dict[str, Any]],
    ) -> Path:
        collected = scope / "package" / "collected_media"
        _reject_reparse_components(collected)
        if not collected.is_dir() or _is_reparse(collected):
            raise ConnectorError("collected media directory is unavailable")
        expected = {str(record["filename"]) for record in records}
        try:
            actual = {
                entry.name
                for entry in collected.iterdir()
                if entry.is_file() and not _is_reparse(entry)
            }
        except OSError as exc:
            raise ConnectorError("collected media directory is unavailable") from exc
        if actual != expected:
            raise ConnectorError("collected media does not match immutable assets")
        for record in records:
            source = collected / record["filename"]
            _reject_reparse(source, label="collected media")
            digest, length = self._hash_file(source)
            if digest != record["sha256"] or length != record["length"]:
                raise ConnectorError("collected media is tampered")
        return collected

    @staticmethod
    def _identity_records(value: Any, fields: set[str]) -> Any:
        if isinstance(value, list):
            result: list[Any] = []
            for item in value:
                if isinstance(item, Mapping):
                    result.append(
                        {
                            key: item[key]
                            for key in sorted(fields)
                            if key in item
                        }
                    )
                elif isinstance(item, str):
                    result.append(item)
                else:
                    raise ConnectorError("dependency identity is invalid")
            return result
        if isinstance(value, Mapping):
            if all(isinstance(item, (str, int, float, bool)) or item is None for item in value.values()):
                return dict(value)
            return {
                str(key): Connector._identity_records(item, fields)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if value is None:
            return []
        raise ConnectorError("dependency identity is invalid")

    @staticmethod
    def _approved_capability_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            approved = json.loads(canonical_json(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConnectorError("approved capability manifest is invalid") from exc
        if not isinstance(approved, dict):
            raise ConnectorError("approved capability manifest is invalid")
        return approved

    @staticmethod
    def _dependency_identity(
        dependencies: Mapping[str, Any],
        capabilities: AECapabilities | None,
    ) -> tuple[Any, Any, Any, Any]:
        requested = dependencies.get("capability_manifest", {})
        if not isinstance(requested, Mapping):
            raise ConnectorError("dependency capability manifest is invalid")
        if capabilities is not None:
            snapshot = capabilities.model_dump(mode="json")
            catalog = snapshot.get("capabilities", {})
            fonts = catalog.get("fonts", catalog.get("font_names", []))
            effects = catalog.get("effects", catalog.get("effect_names", []))
            plugins = catalog.get("plugin_versions", {})
            ae = {
                "version": snapshot.get("version"),
                "major": snapshot.get("major"),
                "host": snapshot.get("host"),
                "capability_hash": snapshot.get("capability_hash"),
            }
        else:
            fonts = requested.get("fonts", requested.get("font_names", []))
            effects = requested.get("effects", requested.get("effect_names", []))
            plugins = requested.get("plugins", requested.get("plugin_versions", {}))
            ae = {}
        fonts = Connector._identity_records(
            fonts,
            {"match_name", "family", "style", "version", "version_or_hash", "sha256"},
        )
        effects = Connector._identity_records(
            effects,
            {"match_name", "display_name", "version", "version_or_hash", "properties"},
        )
        plugins = Connector._identity_records(
            plugins,
            {"match_name", "name", "version", "version_or_hash", "sha256"},
        )
        for value in (fonts, effects, plugins):
            _json_bytes(value)
        return ae, fonts, effects, plugins

    def _package_manifest(
        self,
        payload: Mapping[str, Any],
        *,
        media: list[dict[str, Any]],
        capabilities: AECapabilities | None,
    ) -> dict[str, Any]:
        dependencies = payload.get("dependencies")
        if not isinstance(dependencies, Mapping):
            raise ConnectorError("package dependencies are invalid")
        ae, fonts, effects, plugins = self._dependency_identity(dependencies, capabilities)
        missing = dependencies.get("missing_nonportable_dependencies", [])
        missing_nonportable = missing
        if isinstance(missing, Mapping):
            missing_dependencies = missing.get("missing", [])
            nonportable = missing.get("nonportable", [])
        else:
            missing_dependencies = dependencies.get("missing_dependencies", missing)
            nonportable = dependencies.get("nonportable_dependencies", [])
        substitutions = dependencies.get("substitutions", [])
        for value in (missing_dependencies, nonportable, substitutions):
            _json_bytes(value)
        live_capability_manifest = {
            "capability_hash": ae.get("capability_hash") if isinstance(ae, Mapping) else None,
            "fonts": fonts,
            "effects": effects,
            "plugins": plugins,
        }
        manifest = {
            "schema_version": "keepframe.dependencies/1",
            "after_effects": ae,
            "ae": ae,
            "os": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
            },
            "capability_manifest": {
                "approved": self._approved_capability_manifest(
                    dependencies.get("capability_manifest", {})
                ),
                "live": live_capability_manifest,
            },
            "fonts": fonts,
            "effects": effects,
            "plugins": plugins,
            "media": media,
            "substitutions": substitutions,
            "selected_checkpoint": payload.get("selected_checkpoint"),
            "plan_digest": payload.get("final_plan_digest"),
            "final_plan_digest": payload.get("final_plan_digest"),
            "finalization_key": payload.get("finalization_key"),
            "missing_dependencies": missing_dependencies,
            "nonportable_dependencies": nonportable,
            "missing_nonportable_dependencies": missing_nonportable,
        }
        _json_bytes(manifest)
        return manifest

    def _build_package_zip(
        self,
        archive: Path,
        *,
        aep: Path,
        manifest: Path,
        collected: Path,
    ) -> None:
        _reject_reparse(archive, label="package ZIP")
        _reject_reparse(aep, label="project AEP")
        _reject_reparse(manifest, label="dependency manifest")
        files: list[tuple[str, Path]] = [("project.aep", aep), ("dependencies.json", manifest)]
        total = 0
        for filename in sorted(item.name for item in collected.iterdir()):
            if self._safe_package_filename(filename) is None:
                raise ConnectorError("collected media filename is invalid")
            source = collected / filename
            _reject_reparse(source, label="collected media")
            files.append((f"collected_media/{filename}", source))
        for name, source in files:
            digest, length = self._hash_file(source)
            del digest
            total += length
            if total > _MAX_PACKAGE_UNCOMPRESSED:
                raise ConnectorError("package is too large")
            if name.startswith("/") or ".." in name.split("/") or name in {"", "project.zip"}:
                raise ConnectorError("package entry name is invalid")
        _reject_reparse_components(archive.parent)
        temporary = archive.with_name(f".{archive.name}.{secrets.token_hex(8)}.tmp")
        try:
            with zipfile.ZipFile(
                temporary,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=True,
            ) as output:
                for name, source in files:
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    with output.open(info, "w") as destination, source.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            destination.write(chunk)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            if archive.exists():
                existing_digest, existing_length = self._hash_file(archive)
                retry_digest, retry_length = self._hash_file(temporary)
                if (
                    existing_digest != retry_digest
                    or existing_length != retry_length
                ):
                    raise ConnectorError("package ZIP conflicts with retained package")
            else:
                os.replace(temporary, archive)
            _reject_reparse(archive, label="package ZIP")
        except OSError as exc:
            raise ConnectorError("package ZIP creation failed") from exc
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    def _package_project(
        self,
        payload: Mapping[str, Any],
        result: Mapping[str, Any],
        plan_id: str,
        session_id: str,
        *,
        capabilities: AECapabilities | None,
    ) -> dict[str, Any]:
        selected_checkpoint = self._preview_int(
            payload.get("selected_checkpoint"),
            "selected checkpoint",
        )
        final_plan_digest = payload.get("final_plan_digest")
        if not isinstance(final_plan_digest, str) or not _SHA256.fullmatch(
            final_plan_digest
        ):
            raise ConnectorError("final plan digest is invalid")
        _credential(payload.get("finalization_key"), "finalization key")
        if result.get("saved") is not True or result.get("kind") != "package":
            raise ConnectorError("package result is not successful")
        for key in ("path", "filepath", "directory_path"):
            if key in result:
                raise ConnectorError("package result contains an unsafe path")
        aep_record, zip_record = self._package_artifacts(payload)
        media = self._package_media(payload)
        scope = self._scope_root(plan_id, session_id)
        aep = scope / aep_record["directory"] / "project.aep"
        archive = scope / zip_record["directory"] / "project.zip"
        _reject_reparse_components(aep)
        _reject_reparse_components(archive)
        _reject_reparse(archive, label="package ZIP")
        if not aep.is_file() or _is_reparse(aep):
            raise ConnectorError("project AEP is unavailable")
        collected = self._copy_package_media(scope, media)
        manifest_value = self._package_manifest(
            payload,
            media=media,
            capabilities=capabilities,
        )
        manifest_path = scope / "package" / "dependencies.json"
        _reject_reparse(manifest_path, label="dependency manifest")
        manifest_bytes = canonical_json(manifest_value)
        if manifest_path.exists():
            try:
                if manifest_path.read_bytes() != manifest_bytes:
                    raise ConnectorError("dependency manifest is already committed")
            except OSError as exc:
                raise ConnectorError("dependency manifest is unavailable") from exc
        else:
            _atomic_write(manifest_path, manifest_bytes)
        self._build_package_zip(
            archive,
            aep=aep,
            manifest=manifest_path,
            collected=collected,
        )
        upload_payload = dict(payload)
        upload_payload["artifacts"] = [aep_record, zip_record]
        output = self._upload_artifacts(upload_payload, result, plan_id, session_id)
        output["dependencies"] = manifest_value
        output["selected_checkpoint"] = selected_checkpoint
        _json_bytes(output)
        return output

    def _artifact_source(
        self,
        item: Mapping[str, Any],
        plan_id: str,
        session_id: str,
    ) -> tuple[str, Path, int | None]:
        kind = item.get("kind")
        if kind not in {"png", "mp4", "aep", "zip"}:
            raise ConnectorError("artifact kind is invalid")
        reservation = _safe_id(
            item.get("reservation_id", item.get("id")),
            "artifact reservation",
        )
        filename = self._local_filename(item.get("filename"), "artifact filename")
        directory_name = item.get("directory", "renders")
        if not isinstance(directory_name, str) or directory_name not in {
            "checkpoints",
            "renders",
            "package",
        }:
            raise ConnectorError("artifact directory is invalid")
        directory = self._scope_root(plan_id, session_id) / directory_name
        _reject_reparse_components(directory)
        if not directory.is_dir() or _is_reparse(directory):
            raise ConnectorError("artifact directory is unavailable")
        source = directory / filename
        _reject_reparse(source, label="artifact source")
        expected_length = item.get("length")
        if expected_length is not None and (
            not isinstance(expected_length, int)
            or isinstance(expected_length, bool)
            or not 0 <= expected_length <= _MAX_ARTIFACT_BYTES
        ):
            raise ConnectorError("artifact length is invalid")
        return reservation, source, expected_length

    def _upload_artifacts(
        self,
        payload: Mapping[str, Any],
        result: Mapping[str, Any],
        plan_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        records = payload.get("artifacts")
        if records is None:
            return dict(result)
        if not isinstance(records, list) or len(records) > 64:
            raise ConnectorError("command artifacts are invalid")
        uploaded: list[dict[str, Any]] = []
        for item in records:
            if not isinstance(item, Mapping):
                raise ConnectorError("command artifact is invalid")
            reservation, source, expected_length = self._artifact_source(
                item, plan_id, session_id
            )
            response = self.relay.upload_artifact(
                reservation,
                source,
                project=self.project_id,
                plan=plan_id,
                expected_length=expected_length,
            )
            if not isinstance(response, Mapping):
                raise RelayError("artifact response is invalid")
            artifact = response.get("artifact", response)
            if not isinstance(artifact, Mapping):
                raise RelayError("artifact response is invalid")
            uploaded.append(dict(artifact))
        output = dict(result)
        output["artifacts"] = uploaded
        _json_bytes(output)
        return output

    def execute_command(self, value: Any) -> dict[str, Any]:
        command = self._as_command(value)
        if not set(command).issubset(_COMMAND_FIELDS):
            raise ConnectorError("command contains unsupported fields")
        required = {
            "id",
            "nonce",
            "plan_id",
            "plan_digest",
            "session_id",
            "sequence",
            "expected_state",
            "expected_checkpoint",
            "kind",
            "payload",
            "payload_digest",
        }
        if not required.issubset(command):
            raise ConnectorError("command is missing required fields")
        command_id = _safe_id(command["id"], "command")
        for first, second, expected in (
            ("project_id", "project", self.project_id),
            ("device_id", "device", self.device_id),
        ):
            values = [command[name] for name in (first, second) if name in command]
            if not values or any(value != expected for value in values) or any(
                value != values[0] for value in values[1:]
            ):
                raise ConnectorError("command identity does not match pairing")
        nonce = _safe_id(command["nonce"], "nonce")
        plan_id = _safe_id(command["plan_id"], "plan")
        session_id = _safe_id(command["session_id"], "session")
        expected_state = command["expected_state"]
        valid_states = {
            "awaiting_approval",
            "waiting_for_connector",
            "baseline",
            "iterating",
            "pause_requested",
            "manual_edit",
            "finalizing",
            "done",
            "failed",
        }
        if (
            not isinstance(expected_state, str)
            or not expected_state
            or len(expected_state) > 256
            or (expected_state not in valid_states and not expected_state.startswith("paused:"))
        ):
            raise ConnectorError("command expected state is invalid")
        expected_checkpoint = command["expected_checkpoint"]
        if expected_checkpoint is not None and (
            not isinstance(expected_checkpoint, int)
            or isinstance(expected_checkpoint, bool)
            or not 0 <= expected_checkpoint <= 1_000_000
        ):
            raise ConnectorError("command expected checkpoint is invalid")
        command_digest = command["plan_digest"]
        if not isinstance(command_digest, str) or not _SHA256.fullmatch(command_digest):
            raise ConnectorError("command plan digest is invalid")
        if self.plan_digest is not None and command_digest != self.plan_digest:
            raise ConnectorError("command plan does not match approved digest")
        sequence = command["sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or not 1 <= sequence <= 1_000_000_000:
            raise ConnectorError("command sequence is invalid")
        kind = command["kind"]
        tool = COMMAND_TO_TOOL.get(kind) if isinstance(kind, str) else None
        if tool is None:
            raise ConnectorError("command kind is not supported")
        status = command.get("status")
        if status is not None and (not isinstance(status, str) or status not in {"queued", "leased"}):
            raise ConnectorError("command status is invalid")
        payload = command["payload"]
        if not isinstance(payload, dict):
            raise ConnectorError("command payload is invalid")
        payload_digest = command["payload_digest"]
        if not isinstance(payload_digest, str) or not _SHA256.fullmatch(payload_digest):
            raise ConnectorError("command payload digest is invalid")
        if payload_digest != _canonical_digest(payload):
            raise ConnectorError("command payload digest does not match")
        payload_plan_digest = payload.get("plan_digest")
        if payload_plan_digest is not None and payload_plan_digest != command_digest:
            raise ConnectorError("command payload plan does not match")
        project_id = _safe_id(command.get("project_id", command.get("project")), "project")
        device_id = _safe_id(command.get("device_id", command.get("device")), "device")
        lease_seconds = command.get("lease_seconds", 30.0)
        if (
            not isinstance(lease_seconds, (int, float))
            or isinstance(lease_seconds, bool)
            or not 0 < lease_seconds <= 86_400
            or not math.isfinite(lease_seconds)
        ):
            raise ConnectorError("command lease is invalid")
        lease_seconds = float(lease_seconds)
        lease_expires_at = command.get("lease_expires_at")
        if lease_expires_at is not None and (
            not isinstance(lease_expires_at, (int, float))
            or isinstance(lease_expires_at, bool)
            or not math.isfinite(float(lease_expires_at))
            or float(lease_expires_at) <= 0
        ):
            raise ConnectorError("command lease expiry is invalid")
        lease_expires_at = (
            None if lease_expires_at is None else float(lease_expires_at)
        )
        scope = (project_id, plan_id, session_id)
        fingerprint = _canonical_digest(
            {
                "command_id": command_id,
                "nonce": nonce,
                "project_id": project_id,
                "device_id": device_id,
                "plan_id": plan_id,
                "plan_digest": command_digest,
                "session_id": session_id,
                "sequence": sequence,
                "expected_state": expected_state,
                "expected_checkpoint": expected_checkpoint,
                "kind": kind,
                "payload_digest": payload_digest,
                "lease_seconds": lease_seconds,
            }
        )
        prior = self.journal.get(command_id)
        if prior is not None:
            self.journal.check_replay_scope(scope, command_digest)
            if (
                prior["scope"] != list(scope)
                or prior["sequence"] != sequence
                or prior["fingerprint"] != fingerprint
            ):
                raise ConnectorError("conflicting completed command")
            result = prior["result"]
            self.relay.post_result(
                command_id,
                sequence,
                result,
                project=self.project_id,
                plan=plan_id,
            )
            self.journal.acknowledge(command_id)
            return result
        self.journal.check_scope(scope, command_digest, sequence)

        lease = _CommandLeaseRenewal(
            self.relay,
            command_id,
            sequence,
            nonce,
            lease_seconds,
            lease_expires_at,
            project=self.project_id,
            plan=plan_id,
        )

        def finish(result: dict[str, Any]) -> dict[str, Any]:
            # Journal before posting. A lost relay response therefore causes a
            # result retry, never a second mutation in After Effects.
            lease.close()
            lease.check()
            self.journal.record(
                command_id,
                sequence,
                scope,
                command_digest,
                fingerprint,
                result,
            )
            self.relay.post_result(
                command_id,
                sequence,
                result,
                project=self.project_id,
                plan=plan_id,
            )
            self.journal.acknowledge(command_id)
            return result
        lease.start()
        try:
            self._prepare_assets(payload, plan_id, session_id)
            if kind == "open_project" or payload.get("checkpoint_artifact") is not None:
                self._prepare_checkpoint(payload, plan_id, session_id)
            if kind == "render_final" and (
                payload.get("artifacts") is not None
                or "final_plan_digest" in payload
            ):
                (
                    _checkpoint,
                    frame_count,
                    _fps,
                    width,
                    height,
                    _names,
                ) = self._final_spec(payload)
                renders = self._renders_directory(plan_id, session_id)
                required = frame_count * width * height * 4
                if shutil.disk_usage(renders).free < required + _MIN_FINAL_DISK_MARGIN:
                    return finish(
                        {
                            "ok": False,
                            "error": "render_failed",
                            "reason": "render_failed",
                        }
                    )
        except ConnectorError:
            if kind == "render_preview" or payload.get("artifacts") is not None:
                return finish(self._local_failure_result(kind))
            lease.close()
            raise
        except Exception:
            lease.close()
            raise
        try:
            server_only = {"assets", "artifacts"} | _SERVER_ONLY_PAYLOAD_FIELDS
            mcp_payload = {
                key: item
                for key, item in payload.items()
                if key not in server_only
            }
            if tool == "import_server_asset" and "asset_id" not in mcp_payload:
                assets = payload.get("assets")
                if isinstance(assets, list) and len(assets) == 1 and isinstance(assets[0], Mapping):
                    asset_id = self._local_filename(
                        assets[0].get("id", assets[0].get("asset_id")),
                        "asset id",
                    )
                    mcp_payload["asset_id"] = asset_id
            mcp_payload.update(
                {
                    "project_id": self.project_id,
                    "plan_id": plan_id,
                    "session_id": session_id,
                }
            )
            if kind in {"apply_batch", "inspect_layers", "render_final"} and self.capability_hash is not None:
                try:
                    self._refresh_live_capabilities(
                        command_id,
                        nonce,
                        plan_id,
                        session_id,
                    )
                except ConnectorError:
                    return finish(
                        {
                            "ok": False,
                            "error": "capabilities_changed",
                            "reason": "capabilities_changed",
                        }
                    )
            _json_bytes(mcp_payload)
            envelope = {
                "command_id": command_id,
                "nonce": nonce,
                "payload": mcp_payload,
            }
            try:
                raw_result = self.mcp.call_tool(tool, envelope)
                result_dict = self._normalize_mcp_result(
                    raw_result,
                    command_id,
                    nonce,
                    tool=tool,
                    payload_digest=_canonical_digest(mcp_payload),
                )
                if kind == "inspect_layers" and result_dict.get("ok") is not False:
                    result_dict = self._compact_inspection_result(result_dict)
                result_dict = self._strip_server_only_result_fields(result_dict)
                if (
                    kind == "package_project"
                    and result_dict.get("ok") is False
                    and result_dict.get("error")
                    == "used project footage is outside the approved asset contract"
                ):
                    result_dict["reason"] = "unavailable_dependency"
            except ConnectorError:
                raise
            except Exception as exc:  # noqa: BLE001 - no untrusted MCP error escapes
                raise MCPError("MCP command failed") from exc
            try:
                if kind == "render_preview" and result_dict.get("ok") is not False:
                    result_dict = self._render_preview(
                        payload, result_dict, plan_id, session_id
                    )
                elif (
                    kind == "render_final"
                    and result_dict.get("ok") is not False
                    and (
                        payload.get("artifacts") is not None
                        or "final_plan_digest" in payload
                    )
                ):
                    result_dict = self._render_final(
                        payload,
                        result_dict,
                        plan_id,
                        session_id,
                    )
                elif (
                    kind == "package_project"
                    and result_dict.get("ok") is not False
                    and (
                        payload.get("artifacts") is not None
                        or "package_media" in payload
                    )
                ):
                    capabilities = None
                    if self.capability_hash is not None and "package_media" in payload:
                        try:
                            capabilities = self._refresh_live_capabilities(
                                command_id,
                                nonce,
                                plan_id,
                                session_id,
                            )
                        except ConnectorError:
                            result_dict = {
                                "ok": False,
                                "error": "capabilities_changed",
                                "reason": "capabilities_changed",
                            }
                    if result_dict.get("ok") is not False:
                        result_dict = self._package_project(
                            payload,
                            result_dict,
                            plan_id,
                            session_id,
                            capabilities=capabilities,
                        )
                elif result_dict.get("ok") is not False:
                    result_dict = self._upload_artifacts(
                        payload, result_dict, plan_id, session_id
                    )
            except RelayError:
                if payload.get("artifacts") is not None:
                    result_dict = {
                        "ok": False,
                        "error": "connector artifact upload failed",
                        "reason": "upload_failed",
                    }
                else:
                    raise
            except ConnectorError:
                if kind == "render_preview" or payload.get("artifacts") is not None:
                    result_dict = self._local_failure_result(kind)
                else:
                    raise
            return finish(result_dict)
        finally:
            lease.close()

    def run_once(self, *, wait: float = _MAX_RELAY_WAIT) -> bool:
        command = self.relay.next(project=self.project_id, wait=wait)
        if command is None:
            return False
        self.execute_command(command)
        return True

    def run_forever(self, *, stop_event: Any = None, wait: float = _MAX_RELAY_WAIT) -> None:
        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once(wait=wait)
            except TransientRelayError:
                if stop_event is not None:
                    waiter = getattr(stop_event, "wait", None)
                    if callable(waiter):
                        waiter(_RELAY_RETRY_DELAY)
                        continue
                time.sleep(_RELAY_RETRY_DELAY)


def _resolve_mcp_interpreter(value: str | None) -> str:
    candidate_value = sys.executable if value is None else value
    if not isinstance(candidate_value, str) or not candidate_value or "\x00" in candidate_value:
        raise MCPError("MCP interpreter is invalid")
    candidate = Path(candidate_value)
    if not candidate.is_absolute():
        raise MCPError("MCP interpreter must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise MCPError("MCP interpreter is unavailable") from exc
    _reject_reparse_components(resolved)
    if not resolved.is_file():
        raise MCPError("MCP interpreter is unavailable")
    # A POSIX venv's bin/python is a symlink to the base interpreter; running the target would
    # drop the venv's site-packages (no keepframe, no mcp), so keep the venv's own path.
    if value is None and os.name != "nt" and sys.prefix != sys.base_prefix and candidate.parent.parent == Path(sys.prefix):
        return str(candidate)
    return str(resolved)


def _trusted_mcp_cwd() -> Path:
    try:
        package_root = Path(__file__).resolve().parents[2]
    except (OSError, RuntimeError) as exc:
        raise MCPError("MCP package location is unavailable") from exc
    if not package_root.is_absolute() or not (package_root / "keepframe").is_dir():
        raise MCPError("MCP package location is invalid")
    _reject_reparse_components(package_root)
    return package_root


def build_child_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    result: dict[str, str] = {}
    for key, value in source.items():
        if key not in _CHILD_ENV_ALLOWLIST:
            continue
        if not isinstance(value, str) or "\x00" in value:
            raise MCPError("MCP child environment is invalid")
        result[key] = value
    return result


class MCPStdioClient:
    def __init__(
        self,
        *,
        python: str | None = None,
        environment: Mapping[str, str] | None = None,
        bridge_root: Path | str | None = None,
        import_module: Callable[[str], Any] | None = None,
        stdio_client: Any = None,
    ) -> None:
        self.python = _resolve_mcp_interpreter(python)
        self.cwd = _trusted_mcp_cwd()
        source = dict(os.environ if environment is None else environment)
        if bridge_root is not None:
            source["KEEPFRAME_AE_BRIDGE_ROOT"] = str(Path(bridge_root))
        self.environment = build_child_env(source)
        self._import_module = import_module or importlib.import_module
        self._stdio_client = stdio_client

    def child_argv(self) -> list[str]:
        # -E ignores PYTHON* variables and -P never puts the cwd on sys.path (no shadow imports);
        # unlike -I this keeps the user site-packages, where `pip install keepframe[ae]` usually lands.
        return [self.python, "-E", "-P", "-m", "keepframe.after_effects.mcp_server"]

    @staticmethod
    def _tool_failed(result: Any) -> MCPError:
        content = result.get("content") if isinstance(result, Mapping) else getattr(result, "content", None)
        texts = [
            item.get("text") if isinstance(item, Mapping) else getattr(item, "text", None)
            for item in (content if isinstance(content, (list, tuple)) else ())
        ]
        detail = " ".join(" ".join(t for t in texts if isinstance(t, str)).split())[:240]
        return MCPError("MCP tool failed" + (f": {detail}" if detail else ""))

    @staticmethod
    def _result(result: Any) -> dict[str, Any]:
        if isinstance(result, Mapping):
            error_flag = result.get("is_error", result.get("isError"))
            if error_flag is not None and not isinstance(error_flag, bool):
                raise MCPError("MCP tool error flag is invalid")
            if error_flag:
                raise MCPStdioClient._tool_failed(result)
            structured = result.get("structured_content")
            if structured is None:
                structured = result.get("structuredContent")
            if structured is not None:
                if not isinstance(structured, Mapping):
                    raise MCPError("MCP structured result is invalid")
                return dict(structured)
            if "content" in result:
                content = result.get("content")
                if not isinstance(content, (list, tuple)):
                    raise MCPError("MCP tool content is invalid")
                for item in content:
                    text = (
                        item.get("text")
                        if isinstance(item, Mapping)
                        else getattr(item, "text", None)
                    )
                    if not isinstance(text, str):
                        continue
                    try:
                        value = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, Mapping):
                        return dict(value)
                raise MCPError("MCP tool returned no structured result")
            return dict(result)
        error_flag = getattr(result, "is_error", None)
        if error_flag is None:
            error_flag = getattr(result, "isError", None)
        if error_flag is not None and not isinstance(error_flag, bool):
            raise MCPError("MCP tool error flag is invalid")
        if error_flag:
            raise MCPStdioClient._tool_failed(result)
        structured = getattr(result, "structured_content", None)
        if structured is None:
            structured = getattr(result, "structuredContent", None)
        if structured is not None:
            if not isinstance(structured, Mapping):
                raise MCPError("MCP structured result is invalid")
            return dict(structured)
        content = getattr(result, "content", None)
        if isinstance(content, (list, tuple)):
            for item in content:
                text = (
                    item.get("text")
                    if isinstance(item, Mapping)
                    else getattr(item, "text", None)
                )
                if not isinstance(text, str):
                    continue
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, Mapping):
                    return dict(value)
        raise MCPError("MCP tool returned no structured result")

    async def _call_async(self, tool: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._stdio_client is None:
            mcp = self._import_module("mcp")
            stdio_module = self._import_module("mcp.client.stdio")
            self._stdio_client = stdio_module.stdio_client
        mcp = self._import_module("mcp")
        # The child's stderr (an import error, a traceback) is the only real failure reason;
        # a console handle is not reliably inherited on Windows, so collect it in a file.
        errlog = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
        try:
            argv = self.child_argv()
            params = mcp.StdioServerParameters(
                command=argv[0],
                args=argv[1:],
                env=dict(self.environment),
                cwd=str(self.cwd),
            )
            async with self._stdio_client(params, errlog=errlog) as (read_stream, write_stream):
                async with mcp.ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments=dict(payload))
                    return self._result(result)
        except Exception as exc:  # noqa: BLE001 - optional SDK internals; report the root cause
            leaves = _exception_leaves(exc)  # anyio task groups wrap our own errors too
            leaf = next((e for e in leaves if isinstance(e, ConnectorError)), leaves[0])
            said = _stderr_tail(errlog)
            if isinstance(leaf, ConnectorError) and not (said and str(leaf) == "MCP tool failed"):
                if leaf is exc:
                    raise
                raise leaf from exc
            text = str(leaf) if isinstance(leaf, MCPError) else (
                "MCP stdio command failed: " + " ".join(f"{type(leaf).__name__}: {leaf}".split())[:160]
            )
            raise MCPError(text + (f"; MCP server said: {said}" if said else "")) from exc
        finally:
            errlog.close()

    def call_tool(self, tool: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if tool not in MCP_TOOL_NAMES:
            raise MCPError("MCP tool is not supported")
        if not isinstance(payload, Mapping):
            raise MCPError("MCP payload is invalid")
        try:
            return asyncio.run(self._call_async(tool, payload))
        except ConnectorError:
            raise
        except RuntimeError as exc:
            raise MCPError("MCP event loop is unavailable") from exc





_EXCEPTION_LINE = re.compile(r"^(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*: \S")


def _stderr_tail(log: Any) -> str:
    """The MCP child's root-cause exception line (the first one of a chained traceback; the
    SDK's own wrapper comes last), else its last non-empty stderr line."""
    try:
        log.seek(0)
        lines = [line.rstrip() for line in log.read()[-65536:].splitlines() if line.strip()]
    except (OSError, ValueError):
        return ""
    lines = [line.strip() for line in lines]  # log handlers indent the traceback
    cause = next((line for line in lines if _EXCEPTION_LINE.match(line)), lines[-1] if lines else "")
    return cause.strip()[:240]


def _exception_leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return [leaf for sub in exc.exceptions for leaf in _exception_leaves(sub)]
    return [exc]


def _probe_panel_with_mcp(root: Path) -> dict[str, Any]:
    command_id = "preflight-" + secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    raw = MCPStdioClient(bridge_root=root).call_tool(
        "capability_heartbeat",
        {"command_id": command_id, "nonce": nonce, "payload": {}},
    )
    result = Connector._normalize_mcp_result(raw, command_id, nonce)
    if not isinstance(result, dict):
        raise PreflightError("panel heartbeat is invalid")
    result.setdefault("timestamp", time.time())
    return result


def run_connector(
    url: str,
    code: str | None = None,
    project: str | None = None,
) -> int:
    """Run the Windows connector until its caller's process is stopped."""
    try:
        if project is not None:
            project = _safe_id(project, "project")
            if not _PAIRING_PROJECT.fullmatch(project):
                raise ConnectorError("project is not a safe directory name")
        if code is not None:
            parsed_project, _ = parse_pairing_code(code)
            if project is not None and project != parsed_project:
                raise ConnectorError("pairing project mismatch")
            project = parsed_project
        if project is None:
            raise ConnectorError("project or pairing code is required")
        deployment_token = os.environ.get("KEEPFRAME_AE_RELAY_TOKEN")
        if not deployment_token:
            raise PreflightError("deployment credential is unavailable")
        checked = preflight(
            url,
            deployment_token=deployment_token,
            panel_heartbeat=_probe_panel_with_mcp,
        )
        project_root = Path(checked.private_root) / "projects" / project
        _reject_reparse_components(project_root)
        project_root.mkdir(parents=True, exist_ok=True)
        record_store = DPAPITokenStore(
            project_root,
            filename=DEVICE_RECORD_FILENAME,
        )
        relay = RelayClient(checked.relay_url, deployment_token=deployment_token)
        if code is not None:
            pairing = relay.pair(code)
            if pairing.project != project:
                raise RelayError("pairing project mismatch")
            device_id = pairing.device_id
            device_token = pairing.token
            record_store.save_record(
                {
                    "relay_url": checked.relay_url,
                    "project": pairing.project,
                    "device_id": pairing.device_id,
                    "token": pairing.token,
                }
            )
        else:
            if not record_store.path.exists():
                raise TokenStoreError("pairing record is unavailable")
            record = record_store.load_record()
            if record["relay_url"] != checked.relay_url or record["project"] != project:
                raise TokenStoreError("device record does not match project")
            device_id = record["device_id"]
            device_token = record["token"]
        relay.project_id = project
        relay.device_id = device_id
        relay.set_device_token(device_token)
        relay.publish_capabilities(checked.capabilities, project=project)
        print(
            f"ae-connect: {'paired and ' if code is not None else ''}connected — project {project}, "
            f"device {str(device_id)[:8]}, relay {checked.relay_url}. "
            "Keep this window open; press Ctrl+C to stop.",
            flush=True,
        )
        client = MCPStdioClient(bridge_root=checked.private_root)
        Connector(
            relay,
            client,
            project_id=project,
            device_id=device_id,
            capability_hash=getattr(
                checked,
                "capability_hash",
                getattr(checked.capabilities, "capability_hash", None),
            ),
            private_root=checked.private_root,
            ffmpeg=getattr(checked, "ffmpeg", None),
        ).run_forever()
        return 0
    except ConnectorError as exc:
        print(f"ae-connect failed: {_connector_failure_text(exc)}", file=sys.stderr, flush=True)
        return 1


_FAILURE_HINTS = (
    ("deployment credential", "set KEEPFRAME_AE_RELAY_TOKEN in this shell (the relay deployment token)"),
    ("deployment token", "check KEEPFRAME_AE_RELAY_TOKEN matches the server's relay token"),
    ("panel", "open After Effects with a project, then Window > Keepframe Panel, and retry"),
    ("ffmpeg", "install ffmpeg and make sure `ffmpeg` is on PATH"),
    ("pairing", "press Pair on the project's agent page again and use the new code within its expiry"),
    ("unauthorized", "the pairing code was rejected or expired; press Pair again for a new code"),
)


def _connector_failure_text(exc: Exception) -> str:
    """One readable line for the CLI; never echoes the deployment token."""
    text = str(exc) or type(exc).__name__
    token = os.environ.get("KEEPFRAME_AE_RELAY_TOKEN")
    if token:
        text = text.replace(token, "[redacted]")
    hint = next((h for key, h in _FAILURE_HINTS if key in text.lower()), None)
    return f"{text} — {hint}" if hint else text


__all__ = [
    "COMMAND_TO_TOOL",
    "COMMAND_JOURNAL_FILENAME",
    "CRYPTPROTECT_UI_FORBIDDEN",
    "DEVICE_RECORD_FILENAME",
    "Connector",
    "ConnectorError",
    "TransientRelayError",
    "MCPError",
    "MCPStdioClient",
    "MCP_TOOL_NAMES",
    "Pairing",
    "PreflightError",
    "PreflightResult",
    "RelayClient",
    "RelayError",
    "TOKEN_FILENAME",
    "TokenStoreError",
    "build_child_env",
    "current_user_sid",
    "ensure_private_root",
    "normalize_relay_url",
    "parse_pairing_code",
    "preflight",
    "read_panel_heartbeat",
    "run_connector",
    "validate_ae_version",
]
