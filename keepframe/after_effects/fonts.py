from __future__ import annotations

import ctypes
import copy
import hashlib
import importlib
import os
import stat
import struct
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


MAX_FONT_FILE_BYTES = 32 * 1024 * 1024
MAX_FONT_FILES = 4_096
MAX_SFNT_FACES = 64
MAX_SFNT_TABLES = 256
MAX_NAME_RECORDS = 4_096
MAX_NAME_TABLE_BYTES = 1 * 1024 * 1024
MAX_ENCODED_NAME_BYTES = 16 * 1024
MAX_DECODED_NAME_BYTES = 16 * 1024
MAX_NAME_LENGTH = 4_096
MAX_PARSE_WORK_BYTES = 8 * 1024 * 1024
_REPARSE_POINT = 0x0400
_NAME_TABLE_TAG = 0x656D616E
_FONT_REGISTRY_PATH = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"

def _gdi_handle_invalid(value: Any) -> bool:
    return value in (None, 0, -1, ctypes.c_void_p(-1).value)


class _ParseBudget:
    __slots__ = ("remaining",)

    def __init__(self) -> None:
        self.remaining = MAX_PARSE_WORK_BYTES

    def take(self, amount: int) -> bool:
        if amount < 0 or amount > self.remaining:
            return False
        self.remaining -= amount
        return True


def enrich_heartbeat_fonts(
    heartbeat: Mapping[str, Any],
    *,
    font_files: Iterable[str | os.PathLike[str]] | str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return a copied heartbeat with safe, pathless font metadata.

    Heartbeat paths are only hashed when they resolve beneath the local system
    font directory.  ``font_files`` is an explicit in-process input used by
    tests and follows the same local, non-reparse file checks.
    """
    enriched = copy.deepcopy(dict(heartbeat))
    raw_capabilities = enriched.get("capabilities")
    if not isinstance(raw_capabilities, Mapping):
        return enriched
    capabilities = dict(raw_capabilities)
    enriched["capabilities"] = capabilities
    system_font_root = _system_font_root()

    raw_fonts = capabilities.get("fonts")
    if isinstance(raw_fonts, (list, tuple)):
        fonts: list[Any] = []
        local_catalog_cache: dict[str, dict[str, dict[str, Any]]] = {}
        trusted_local_hashes: dict[str, str] = {}
        for raw_font in raw_fonts:
            if not isinstance(raw_font, Mapping):
                continue
            font = dict(raw_font)
            local_path = font.pop("local_path", None)
            if local_path is not None and system_font_root is not None:
                cache_key = _path_cache_key(local_path)
                if cache_key is not None and cache_key not in local_catalog_cache:
                    local_catalog_cache[cache_key] = {
                        item["match_name"]: item
                        for item in _discover_fonts([local_path], system_font_root)
                        if isinstance(item.get("match_name"), str)
                    }
                name = font.get("match_name")
                parsed = (
                    local_catalog_cache.get(cache_key, {}).get(name)
                    if cache_key is not None and isinstance(name, str)
                    else None
                )
                if isinstance(name, str) and isinstance(parsed, Mapping):
                    digest = parsed.get("sha256")
                    if isinstance(digest, str) and digest:
                        font["sha256"] = digest
                        trusted_local_hashes[name] = digest
                        if not _has_font_version(font):
                            font["version_or_hash"] = digest
                        for key in ("family", "style", "version"):
                            value = parsed.get(key)
                            if not _font_value_present(font.get(key)) and _font_value_present(value):
                                font[key] = value
            fonts.append(font)
        capabilities["fonts"] = fonts
    else:
        fonts = []
        trusted_local_hashes = {}

    paths = _coerce_font_files(font_files) if font_files is not None else _registry_font_files(system_font_root)
    system_catalog = _discover_fonts(paths, system_font_root)
    discovered = _merge_catalog(system_catalog, _discover_gdi_fonts())
    merged_fonts = _merge_panel_catalog(fonts, discovered, trusted_local_hashes)
    capabilities["fonts"] = merged_fonts
    capabilities["font_names"] = [font["match_name"] for font in merged_fonts]
    return enriched


def _has_font_version(font: Mapping[str, Any]) -> bool:
    value = font.get("version")
    return isinstance(value, str) and bool(value)


def _path_cache_key(value: Any) -> str | None:
    raw = _path_text(value)
    return raw


def _path_text(value: Any) -> str | None:
    try:
        raw = os.fspath(value)
    except (TypeError, ValueError):
        return None
    if isinstance(raw, bytes):
        try:
            raw = os.fsdecode(raw)
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    return raw



def _read_font_file(value: Any, root: Path | None, *, retain_payload: bool = True) -> tuple[bytes, str] | None:
    path = _trusted_font_path(value, root)
    if path is None or root is None:
        return None
    descriptor = _open_font_descriptor(path, root)
    if descriptor is None:
        return None
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            try:
                file_stat = os.fstat(stream.fileno())
            except OSError:
                return None
            if not _regular_non_reparse(file_stat):
                return None
            size = file_stat.st_size
            if size <= 0 or size > MAX_FONT_FILE_BYTES:
                return None
            digest = hashlib.sha256()
            chunks: list[bytes] | None = [] if retain_payload else None
            total = 0
            while True:
                chunk = stream.read(min(1024 * 1024, MAX_FONT_FILE_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FONT_FILE_BYTES:
                    return None
                digest.update(chunk)
                if chunks is not None:
                    chunks.append(chunk)
            if total != size:
                return None
            payload = b"" if chunks is None else b"".join(chunks)
            return payload, digest.hexdigest()
    except (OSError, ValueError):
        return None


class _Win32API:
    __slots__ = (
        "ctypes",
        "create_file",
        "close_handle",
        "final_path",
        "get_file_info",
        "get_drive_type",
        "query_dos_device",
        "get_system_directory",
        "get_windows_directory",
        "file_info_type",
        "invalid_handle",
    )
    def __init__(
        self,
        ctypes_module: Any,
        create_file: Any,
        close_handle: Any,
        final_path: Any,
        get_file_info: Any,
        get_drive_type: Any,
        query_dos_device: Any,
        get_system_directory: Any,
        get_windows_directory: Any,
        file_info_type: Any,
        invalid_handle: Any,
    ) -> None:
        self.ctypes = ctypes_module
        self.create_file = create_file
        self.close_handle = close_handle
        self.final_path = final_path
        self.get_file_info = get_file_info
        self.get_drive_type = get_drive_type
        self.query_dos_device = query_dos_device
        self.get_system_directory = get_system_directory
        self.get_windows_directory = get_windows_directory
        self.file_info_type = file_info_type
        self.invalid_handle = invalid_handle



def _load_win32_api() -> _Win32API | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, ImportError, OSError):
        return None

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", FILETIME),
            ("ftLastAccessTime", FILETIME),
            ("ftLastWriteTime", FILETIME),
            # Missing this field made the struct 48 bytes instead of 52; Windows wrote past it
            # and corrupted the heap (access violation in the next allocation).
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    try:
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(SECURITY_ATTRIBUTES),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        final_path = kernel32.GetFinalPathNameByHandleW
        final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final_path.restype = wintypes.DWORD

        get_file_info = kernel32.GetFileInformationByHandle
        get_file_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
        get_file_info.restype = wintypes.BOOL

        get_drive_type = kernel32.GetDriveTypeW
        get_drive_type.argtypes = [wintypes.LPCWSTR]
        get_drive_type.restype = wintypes.UINT

        query_dos_device = kernel32.QueryDosDeviceW
        query_dos_device.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        query_dos_device.restype = wintypes.DWORD

        get_system_directory = kernel32.GetSystemWindowsDirectoryW
        get_system_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        get_system_directory.restype = wintypes.UINT

        get_windows_directory = kernel32.GetWindowsDirectoryW
        get_windows_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        get_windows_directory.restype = wintypes.UINT
    except (AttributeError, TypeError, ValueError):
        return None

    return _Win32API(
        ctypes,
        create_file,
        close_handle,
        final_path,
        get_file_info,
        get_drive_type,
        query_dos_device,
        get_system_directory,
        get_windows_directory,
        BY_HANDLE_FILE_INFORMATION,
        ctypes.c_void_p(-1).value,
    )


_FILE_ATTRIBUTE_DIRECTORY = 0x10


def _windows_handle_invalid(handle: Any, api: _Win32API) -> bool:
    return handle in (None, 0, -1, api.invalid_handle)


def _open_windows_handle(api: _Win32API, path: Path, flags: int) -> Any | None:
    try:
        handle = api.create_file(
            os.fspath(path),
            0x80000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            flags,
            None,
        )
    except (OSError, TypeError, ValueError):
        return None
    return None if _windows_handle_invalid(handle, api) else handle


def _close_windows_handle(api: _Win32API, handle: Any) -> None:
    if not _windows_handle_invalid(handle, api):
        try:
            api.close_handle(handle)
        except (OSError, TypeError, ValueError):
            pass


def _windows_final_name(api: _Win32API, handle: Any) -> str | None:
    try:
        required = api.final_path(handle, None, 0, 0)
        if not required:
            return None
        buffer = api.ctypes.create_unicode_buffer(required + 1)
        length = api.final_path(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            return None
        value = buffer.value
    except (OSError, TypeError, ValueError, OverflowError):
        return None
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _windows_file_info(api: _Win32API, handle: Any) -> Any | None:
    info = api.file_info_type()
    try:
        if not api.get_file_info(handle, api.ctypes.byref(info)):
            return None
    except (OSError, TypeError, ValueError):
        return None
    return info


def _canonical_windows_directory(path: Path, api: _Win32API) -> str | None:
    handle = _open_windows_handle(api, path, 0x00200000 | 0x02000000)
    if handle is None:
        return None
    try:
        info = _windows_file_info(api, handle)
        if info is None or not info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
            return None
        if info.dwFileAttributes & _REPARSE_POINT:
            return None
        value = _windows_final_name(api, handle)
        if value is None or _unsafe_namespace(value):
            return None
        return value
    finally:
        _close_windows_handle(api, handle)


def _system_font_root() -> Path | None:
    api = _load_win32_api()
    if api is None:
        return None
    for getter in (api.get_system_directory, api.get_windows_directory):
        try:
            buffer = api.ctypes.create_unicode_buffer(32768)
            length = getter(buffer, len(buffer))
        except (OSError, TypeError, ValueError, OverflowError):
            continue
        if not length or length >= len(buffer):
            continue
        windows = Path(buffer.value)
        if _unsafe_namespace(os.fspath(windows)) or not _drive_is_local(os.fspath(windows), api):
            continue
        root = windows / "Fonts"
        canonical = _canonical_windows_directory(root, api)
        if canonical is not None:
            return Path(canonical)
    return None


def _trusted_font_path(value: Any, root: Path | None) -> Path | None:
    raw = _path_text(value)
    if raw is None or root is None or _unsafe_namespace(raw):
        return None
    try:
        path = Path(raw)
    except (TypeError, ValueError):
        return None
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        return None
    if not _drive_is_local(raw):
        return None
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        return None
    return path


def _unsafe_namespace(raw: str) -> bool:
    normalized = raw.replace("/", "\\")
    if normalized.startswith(("\\\\", "\\?\\", "\\.\\", "\\??\\")):
        return True
    if ":" in raw and not (len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha()):
        return True
    return False


def _drive_is_local(raw: str, api: _Win32API | None = None) -> bool:
    if os.name != "nt":
        return True
    if len(raw) < 3 or raw[1] != ":" or raw[2] not in ("/", "\\"):
        return False
    api = api or _load_win32_api()
    if api is None:
        return False
    try:
        if api.get_drive_type(raw[:3]) != 3:  # DRIVE_FIXED; reject mapped/remote/removable media.
            return False
        target = api.ctypes.create_unicode_buffer(1024)
        if not api.query_dos_device(raw[:2], target, len(target)):
            return False
        return not target.value.startswith("\\??\\")
    except (OSError, TypeError, ValueError, OverflowError):
        return False


def _regular_non_reparse(value: os.stat_result) -> bool:
    return stat.S_ISREG(value.st_mode) and not _is_reparse(value)


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & _REPARSE_POINT)


def _open_font_descriptor(path: Path, root: Path) -> int | None:
    if os.name == "nt":
        return _open_windows_font(path, root)
    return _open_posix_font(path, root)


def _open_posix_font(path: Path, root: Path) -> int | None:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory or not hasattr(os, "supports_dir_fd"):
        return None
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    current = _open_posix_directory_tree(root, no_follow, directory)
    if current is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | no_follow
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | directory | no_follow, dir_fd=current)
            os.close(current)
            current = child
        descriptor = os.open(relative.parts[-1], flags, dir_fd=current)
    except (OSError, TypeError, ValueError):
        return None
    finally:
        try:
            os.close(current)
        except OSError:
            pass
    return descriptor


def _open_posix_directory_tree(path: Path, no_follow: int, directory: int) -> int | None:
    try:
        anchor = Path(path.anchor)
        relative = path.relative_to(anchor)
    except (TypeError, ValueError):
        return None
    try:
        current = os.open(os.fspath(anchor), os.O_RDONLY | directory | no_follow)
    except (OSError, TypeError, ValueError):
        return None
    try:
        for component in relative.parts:
            child = os.open(component, os.O_RDONLY | directory | no_follow, dir_fd=current)
            os.close(current)
            current = child
    except (OSError, TypeError, ValueError):
        try:
            os.close(current)
        except OSError:
            pass
        return None
    return current


def _open_windows_font(path: Path, root: Path) -> int | None:
    api = _load_win32_api()
    if api is None:
        return None
    try:
        import msvcrt
    except ImportError:
        return None
    root_name = _canonical_windows_directory(root, api)
    if root_name is None or not _drive_is_local(root_name, api):
        return None
    handle = _open_windows_handle(api, path, 0x00200000)
    if handle is None:
        return None
    file_name = _windows_final_name(api, handle)
    info = _windows_file_info(api, handle)
    if (
        file_name is None
        or _unsafe_namespace(file_name)
        or not _windows_path_under(file_name, root_name)
        or info is None
        or bool(info.dwFileAttributes & (_FILE_ATTRIBUTE_DIRECTORY | _REPARSE_POINT))
    ):
        _close_windows_handle(api, handle)
        return None
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except (OSError, TypeError, ValueError):
        _close_windows_handle(api, handle)
        return None
    return descriptor


def _windows_path_under(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.normpath(path)).rstrip("\\/")
    root = os.path.normcase(os.path.normpath(root)).rstrip("\\/")
    return bool(path and root and path.startswith(root + "\\"))


def _coerce_font_files(font_files: Any) -> list[Any]:
    if isinstance(font_files, (str, bytes, os.PathLike)):
        return [font_files]
    try:
        return list(font_files)
    except (TypeError, ValueError):
        return []


def _registry_font_files(root: Path | None) -> list[Path]:
    if root is None:
        return []
    try:
        winreg = importlib.import_module("winreg")
    except ImportError:
        return []
    hive = getattr(winreg, "HKEY_LOCAL_MACHINE", None)
    if hive is None:
        return []
    key = _open_font_registry_key(winreg, hive)
    if key is None:
        return []
    values: list[str] = []
    try:
        for index in range(MAX_FONT_FILES):
            try:
                entry = winreg.EnumValue(key, index)
            except (OSError, IndexError, StopIteration, TypeError):
                break
            if not isinstance(entry, tuple) or len(entry) < 2:
                continue
            value = entry[1]
            if isinstance(value, str) and _relative_registry_filename(value):
                values.append(value)
    finally:
        close_key = getattr(winreg, "CloseKey", None)
        if close_key is not None:
            try:
                close_key(key)
            except (OSError, TypeError, ValueError):
                pass
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = root / value
        key = os.path.normcase(os.path.normpath(os.fspath(path)))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _relative_registry_filename(value: str) -> bool:
    if not value or "/" in value or "\\" in value or _unsafe_namespace(value):
        return False
    if value != os.path.basename(value):
        return False
    return value not in (".", "..") and ".." not in value


def _open_font_registry_key(winreg: Any, hive: Any) -> Any | None:
    open_key = getattr(winreg, "OpenKey", None)
    if open_key is None:
        return None
    access = getattr(winreg, "KEY_READ", 0)
    try:
        return open_key(hive, _FONT_REGISTRY_PATH, 0, access)
    except TypeError:
        try:
            return open_key(hive, _FONT_REGISTRY_PATH)
        except (OSError, TypeError, ValueError):
            return None
    except (OSError, ValueError):
        return None


class _GDIAPI:
    __slots__ = (
        "_ctypes",
        "_logfont_type",
        "_callback_type",
        "_create_dc",
        "_delete_dc",
        "_create_font",
        "_select_object",
        "_get_font_data",
        "_delete_object",
        "_enum_fonts",
    )

    def __init__(
        self,
        ctypes_module: Any,
        logfont_type: Any,
        callback_type: Any,
        create_dc: Any,
        delete_dc: Any,
        create_font: Any,
        select_object: Any,
        get_font_data: Any,
        delete_object: Any,
        enum_fonts: Any,
    ) -> None:
        self._ctypes = ctypes_module
        self._logfont_type = logfont_type
        self._callback_type = callback_type
        self._create_dc = create_dc
        self._delete_dc = delete_dc
        self._create_font = create_font
        self._select_object = select_object
        self._get_font_data = get_font_data
        self._delete_object = delete_object
        self._enum_fonts = enum_fonts

    def create_dc(self) -> Any:
        return self._create_dc(None)

    def delete_dc(self, dc: Any) -> Any:
        return self._delete_dc(dc)

    def create_font(self, logfont: Any) -> Any:
        return self._create_font(self._ctypes.byref(logfont))

    def select_font(self, dc: Any, font: Any) -> Any:
        return self._select_object(dc, font)

    def delete_object(self, font: Any) -> Any:
        return self._delete_object(font)

    def get_name_table(self, dc: Any) -> bytes | None:
        size = self._get_font_data(dc, _NAME_TABLE_TAG, 0, None, 0)
        if size in (0, 0xFFFFFFFF) or size > MAX_NAME_TABLE_BYTES:
            return None
        buffer = self._ctypes.create_string_buffer(size)
        received = self._get_font_data(dc, _NAME_TABLE_TAG, 0, buffer, size)
        if received != size:
            return None
        return bytes(buffer.raw[:size])

    def enumerate(self, dc: Any, callback: Any) -> Any:
        def native_callback(logfont: Any, _textmetric: Any, _font_type: Any, _lparam: Any) -> int:
            try:
                return int(callback(logfont.contents))
            except Exception:  # ctypes callbacks must fail closed, never unwind into GDI.
                return 0

        filter_logfont = self._logfont_type()
        filter_logfont.lfCharSet = 1
        callback_ref = self._callback_type(native_callback)
        return self._enum_fonts(dc, self._ctypes.byref(filter_logfont), callback_ref, 0, 0)


def _load_gdi_api() -> _GDIAPI | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    except (AttributeError, ImportError, OSError):
        return None

    class LOGFONTW(ctypes.Structure):
        _fields_ = [
            ("lfHeight", ctypes.c_long),
            ("lfWidth", ctypes.c_long),
            ("lfEscapement", ctypes.c_long),
            ("lfOrientation", ctypes.c_long),
            ("lfWeight", ctypes.c_long),
            ("lfItalic", ctypes.c_ubyte),
            ("lfUnderline", ctypes.c_ubyte),
            ("lfStrikeOut", ctypes.c_ubyte),
            ("lfCharSet", ctypes.c_ubyte),
            ("lfOutPrecision", ctypes.c_ubyte),
            ("lfClipPrecision", ctypes.c_ubyte),
            ("lfQuality", ctypes.c_ubyte),
            ("lfPitchAndFamily", ctypes.c_ubyte),
            ("lfFaceName", ctypes.c_wchar * 32),
        ]

    try:
        handle_type = ctypes.c_void_p
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.POINTER(LOGFONTW),
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.LPARAM,
        )
        create_dc = gdi32.CreateCompatibleDC
        create_dc.argtypes = [handle_type]
        create_dc.restype = handle_type
        delete_dc = gdi32.DeleteDC
        delete_dc.argtypes = [handle_type]
        delete_dc.restype = wintypes.BOOL
        create_font = gdi32.CreateFontIndirectW
        create_font.argtypes = [ctypes.POINTER(LOGFONTW)]
        create_font.restype = handle_type
        select_object = gdi32.SelectObject
        select_object.argtypes = [handle_type, handle_type]
        select_object.restype = handle_type
        get_font_data = gdi32.GetFontData
        get_font_data.argtypes = [handle_type, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        get_font_data.restype = wintypes.DWORD
        delete_object = gdi32.DeleteObject
        delete_object.argtypes = [handle_type]
        delete_object.restype = wintypes.BOOL
        enum_fonts = gdi32.EnumFontFamiliesExW
        enum_fonts.argtypes = [handle_type, ctypes.POINTER(LOGFONTW), callback_type, wintypes.LPARAM, wintypes.DWORD]
        enum_fonts.restype = ctypes.c_int
    except (AttributeError, TypeError, ValueError):
        return None
    return _GDIAPI(
        ctypes,
        LOGFONTW,
        callback_type,
        create_dc,
        delete_dc,
        create_font,
        select_object,
        get_font_data,
        delete_object,
        enum_fonts,
    )


def _discover_gdi_fonts(api: Any | None = None) -> list[dict[str, Any]]:
    adapter = _load_gdi_api() if api is None else api
    if adapter is None:
        return []
    try:
        dc = adapter.create_dc()
    except (OSError, TypeError, ValueError):
        return []
    if _gdi_handle_invalid(dc):
        return []

    budget = _ParseBudget()
    catalog: dict[str, dict[str, Any]] = {}
    seen_faces: set[tuple[str, int, int, int]] = set()

    def visit(logfont: Any) -> int:
        if len(catalog) >= MAX_FONT_FILES or budget.remaining <= 0:
            return 0
        key = _gdi_face_key(logfont)
        if key in seen_faces:
            return 1
        seen_faces.add(key)
        try:
            font = adapter.create_font(logfont)
        except (OSError, TypeError, ValueError):
            return 1
        if _gdi_handle_invalid(font):
            return 1
        old_font: Any = None
        try:
            old_font = adapter.select_font(dc, font)
            if _gdi_handle_invalid(old_font):
                return 1
            table = adapter.get_name_table(dc)
            if table is None:
                return 1
            names = _parse_name_table(table, budget)
            metadata = _font_metadata(
                names,
                identity_hash=hashlib.sha256(table).hexdigest(),
            )
            if metadata is not None:
                catalog.setdefault(metadata["match_name"], metadata)
        except (OSError, TypeError, ValueError, OverflowError, struct.error):
            return 1
        finally:
            if not _gdi_handle_invalid(old_font):
                try:
                    adapter.select_font(dc, old_font)
                except (OSError, TypeError, ValueError):
                    pass
            try:
                adapter.delete_object(font)
            except (OSError, TypeError, ValueError):
                pass
        return 1

    try:
        try:
            adapter.enumerate(dc, visit)
        except (OSError, TypeError, ValueError):
            pass
    finally:
        try:
            adapter.delete_dc(dc)
        except (OSError, TypeError, ValueError):
            pass
    return [catalog[name] for name in sorted(catalog)]


def _gdi_face_key(logfont: Any) -> tuple[str, int, int, int]:
    face_name = getattr(logfont, "lfFaceName", "")
    if isinstance(face_name, bytes):
        face_name = os.fsdecode(face_name)
    else:
        face_name = str(face_name)
    return (
        face_name,
        int(getattr(logfont, "lfWeight", 0)),
        int(getattr(logfont, "lfItalic", 0)),
        int(getattr(logfont, "lfCharSet", 0)),
    )


def _parse_name_table(table: bytes, budget: _ParseBudget) -> dict[int, str] | None:
    if len(table) > MAX_NAME_TABLE_BYTES or not budget.take(len(table)):
        return None
    return _name_records(table, budget)


def _font_metadata(
    names: Mapping[int, str] | None,
    binary_digest: str | None = None,
    *,
    identity_hash: str | None = None,
) -> dict[str, Any] | None:
    if not names:
        return None
    match_name = names.get(6)
    if not match_name:
        return None
    version = names.get(5)
    metadata: dict[str, Any] = {
        "match_name": match_name,
        "family": names.get(1, ""),
        "style": names.get(2, ""),
        "version": version,
        "version_or_hash": version or identity_hash or binary_digest,
    }
    if binary_digest is not None:
        metadata["sha256"] = binary_digest
    return metadata


def _font_value_present(value: Any) -> bool:
    return bool(value.strip()) if isinstance(value, str) else value is not None


def _merge_catalog(*catalogs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for catalog in catalogs:
        for raw_font in catalog:
            name = raw_font.get("match_name")
            if not isinstance(name, str) or not name:
                continue
            font = merged.setdefault(name, {"match_name": name})
            for key, value in raw_font.items():
                if not _font_value_present(font.get(key)) and _font_value_present(value):
                    font[key] = value
    return [merged[name] for name in sorted(merged)]


def _merge_panel_catalog(
    panel_fonts: Iterable[Any],
    discovered_fonts: Iterable[Mapping[str, Any]],
    trusted_local_hashes: Mapping[str, str],
) -> list[dict[str, Any]]:
    discovered = {
        font["match_name"]: dict(font)
        for font in discovered_fonts
        if isinstance(font.get("match_name"), str) and font.get("match_name")
    }
    trusted_names = set(discovered) | set(trusted_local_hashes)
    merged: dict[str, dict[str, Any]] = {}
    for raw_font in panel_fonts:
        if not isinstance(raw_font, Mapping):
            continue
        name = raw_font.get("match_name")
        if not isinstance(name, str) or name not in trusted_names:
            continue
        font = merged.setdefault(name, {"match_name": name})
        for key, value in raw_font.items():
            if key in {"local_path", "sha256", "version_or_hash"}:
                continue
            if key not in font or (not _font_value_present(font[key]) and _font_value_present(value)):
                font[key] = value

    for name, candidate in discovered.items():
        font = merged.setdefault(name, {"match_name": name})
        for key, value in candidate.items():
            if key in {"version", "version_or_hash", "sha256"} and _font_value_present(value):
                font[key] = value
            elif not _font_value_present(font.get(key)) and _font_value_present(value):
                font[key] = value

    for name, digest in trusted_local_hashes.items():
        font = merged.setdefault(name, {"match_name": name})
        font["sha256"] = digest
        if name not in discovered or not _font_value_present(discovered[name].get("version_or_hash")):
            font["version_or_hash"] = digest

    return [merged[name] for name in sorted(merged) if name in trusted_names]


def _discover_fonts(paths: Iterable[Any], root: Path | None) -> list[dict[str, Any]]:
    if root is None:
        return []
    unique_paths: dict[str, Any] = {}
    for value in paths:
        key = _path_cache_key(value)
        if key is None:
            continue
        normalized = os.path.normcase(os.path.normpath(key))
        unique_paths.setdefault(normalized, value)
        if len(unique_paths) >= MAX_FONT_FILES:
            break

    discovered: dict[str, dict[str, Any]] = {}
    for key in sorted(unique_paths):
        loaded = _read_font_file(unique_paths[key], root)
        if loaded is None:
            continue
        payload, digest = loaded
        for metadata in _parse_sfnt(payload, digest):
            discovered.setdefault(metadata["match_name"], metadata)
    return [discovered[name] for name in sorted(discovered)]


def _parse_sfnt(payload: bytes, digest: str) -> list[dict[str, Any]]:
    budget = _ParseBudget()
    offsets = _face_offsets(payload, budget)
    if offsets is None:
        return []
    name_cache: dict[tuple[int, int], dict[int, str] | None] = {}
    result: list[dict[str, Any]] = []
    for offset in offsets:
        names = _face_names(payload, offset, budget, name_cache)
        metadata = _font_metadata(names, digest)
        if metadata is not None:
            result.append(metadata)
    return result


def _face_offsets(payload: bytes, budget: _ParseBudget) -> list[int] | None:
    if len(payload) < 4:
        return None
    if payload[:4] != b"ttcf":
        return [0] if _valid_sfnt_signature(payload[:4]) else None
    if len(payload) < 12:
        return None
    try:
        count = struct.unpack_from(">I", payload, 8)[0]
    except struct.error:
        return None
    if count == 0 or count > MAX_SFNT_FACES or 12 + count * 4 > len(payload):
        return None
    if not budget.take(12 + count * 4):
        return None
    offsets: list[int] = []
    for index in range(count):
        try:
            offset = struct.unpack_from(">I", payload, 12 + index * 4)[0]
        except struct.error:
            return None
        if offset >= len(payload) or offset in offsets:
            continue
        offsets.append(offset)
    return offsets


def _valid_sfnt_signature(signature: bytes) -> bool:
    return signature in {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"}


def _face_names(
    payload: bytes,
    face_offset: int,
    budget: _ParseBudget,
    name_cache: dict[tuple[int, int], dict[int, str] | None],
) -> dict[int, str] | None:
    if face_offset < 0 or face_offset + 12 > len(payload):
        return None
    signature = payload[face_offset : face_offset + 4]
    if not _valid_sfnt_signature(signature):
        return None
    try:
        table_count = struct.unpack_from(">H", payload, face_offset + 4)[0]
    except struct.error:
        return None
    if table_count > MAX_SFNT_TABLES:
        return None
    directory_size = 12 + table_count * 16
    directory_end = face_offset + directory_size
    if directory_end > len(payload) or not budget.take(directory_size):
        return None
    name_range: tuple[int, int] | None = None
    for index in range(table_count):
        entry = face_offset + 12 + index * 16
        tag = payload[entry : entry + 4]
        try:
            table_offset, table_length = struct.unpack_from(">II", payload, entry + 8)
        except struct.error:
            return None
        if table_offset > len(payload) or table_length > len(payload) - table_offset:
            return None
        if tag == b"name":
            if table_length > MAX_NAME_TABLE_BYTES:
                return None
            name_range = (table_offset, table_length)
    if name_range is None:
        return None
    cached = name_cache.get(name_range)
    if name_range in name_cache:
        return cached
    table_offset, table_length = name_range
    if not budget.take(table_length):
        return None
    table = payload[table_offset : table_offset + table_length]
    parsed = _name_records(table, budget)
    name_cache[name_range] = parsed
    return parsed


def _name_records(table: bytes, budget: _ParseBudget) -> dict[int, str] | None:
    if len(table) < 6:
        return None
    try:
        _, record_count, string_offset = struct.unpack_from(">HHH", table, 0)
    except struct.error:
        return None
    records_end = 6 + record_count * 12
    if record_count > MAX_NAME_RECORDS or records_end > len(table):
        return None
    if string_offset < records_end or string_offset > len(table):
        return None
    if not budget.take(records_end):
        return None
    selected: dict[int, tuple[tuple[int, int, int], str]] = {}
    for index in range(record_count):
        entry = 6 + index * 12
        try:
            platform, encoding, language, name_id, length, offset = struct.unpack_from(">HHHHHH", table, entry)
        except struct.error:
            return None
        start = string_offset + offset
        if start > len(table) or length > len(table) - start:
            return None
        if name_id not in (1, 2, 5, 6):
            continue
        if length > MAX_ENCODED_NAME_BYTES or not budget.take(length):
            return None
        text = _decode_name(platform, encoding, table[start : start + length])
        if text is None:
            continue
        if len(text) > MAX_DECODED_NAME_BYTES or not budget.take(len(text)):
            return None
        score = _name_score(platform, encoding, language)
        previous = selected.get(name_id)
        if previous is None or score > previous[0]:
            selected[name_id] = (score, text)
    return {name_id: value for name_id, (_, value) in selected.items()}


def _decode_name(platform: int, encoding: int, value: bytes) -> str | None:
    if platform in (0, 3):
        if len(value) % 2:
            return None
        codec = "utf-16-be"
    elif platform == 1 and encoding == 0:
        codec = "mac_roman"
    else:
        return None
    try:
        text = value.decode(codec).strip("\x00").strip()
    except (UnicodeDecodeError, UnicodeError):
        return None
    if not text or "\x00" in text or len(text) > MAX_NAME_LENGTH:
        return None
    return text


def _name_score(platform: int, encoding: int, language: int) -> tuple[int, int, int]:
    if platform == 3:
        return (4 if language == 0x0409 else 3, 1 if encoding in (1, 10) else 0, 0)
    if platform == 0:
        return (2, 1 if encoding in (3, 4, 5, 6) else 0, 0)
    return (1, 0, 0)


__all__ = ["enrich_heartbeat_fonts"]
