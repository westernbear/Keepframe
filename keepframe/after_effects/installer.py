"""Windows-only installation of the Keepframe After Effects panel."""

from __future__ import annotations

import ctypes
import os
import platform as _platform
import re
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

MIN_AE_YEAR = 2022
PANEL_FILENAME = "keepframe_panel.jsx"
MANUAL_INSTRUCTIONS = (
    "Restart After Effects after installation. Then enable Edit > Preferences > "
    "Scripting & Expressions > Allow Scripts to Write Files and Access Network, "
    "and open Window > Keepframe Panel."
)


class InstallerError(RuntimeError):
    """Base class for actionable panel installation failures."""


class NotWindowsError(InstallerError):
    """Raised when the Windows-only installer is called on another platform."""


class InvalidAEPathError(InstallerError):
    """Raised when an explicit After Effects installation path is invalid."""


class NoInstallationsError(InstallerError):
    """Raised when no supported After Effects installation can be found."""


class UnsafePathError(InstallerError):
    """Raised when a symlink or reparse point would be traversed or replaced."""


class MultipleInstallationsError(InstallerError):
    """Raised when automatic discovery finds more than one supported install."""

    def __init__(self, installations: Iterable[Path]):
        self.installations: tuple[Path, ...] = tuple(Path(path) for path in installations)
        choices = "\n".join(f"  {path}" for path in self.installations)
        super().__init__(
            f"Multiple supported After Effects installations were found. Select one with --ae-path:\n{choices}"
        )


def _copy_file(source: Path, destination: Path) -> None:
    _ = shutil.copyfile(source, destination)


@dataclass(frozen=True)
class InstallerFS:
    """Small filesystem seam used by Linux tests and the real Windows installer."""

    exists: Callable[[Path], bool] = os.path.lexists
    is_dir: Callable[[Path], bool] = lambda path: path.is_dir()
    is_file: Callable[[Path], bool] = lambda path: path.is_file()
    is_symlink: Callable[[Path], bool] = lambda path: path.is_symlink()
    is_reparse: Callable[[Path], bool] = lambda path: _is_reparse_point(path)
    iterdir: Callable[[Path], Iterable[Path]] = lambda path: path.iterdir()
    mkdir: Callable[[Path], None] = lambda path: path.mkdir()
    copy_file: Callable[[Path, Path], None] = _copy_file


FileSystem = InstallerFS


def _is_reparse_point(path: Path) -> bool:
    """Return the Windows reparse-point bit without importing Win32 modules."""

    if os.name != "nt":
        return False
    try:
        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    except (AttributeError, OSError):
        return False
    return attributes != 0xFFFFFFFF and bool(attributes & 0x400)


def manual_instructions() -> str:
    """Return the one manual Adobe preference/panel-opening step."""

    return MANUAL_INSTRUCTIONS


def _platform_name(value: str | Callable[[], str] | None, system: str | Callable[[], str] | None) -> str:
    selected = system if system is not None else value
    if selected is None:
        selected = _platform.system
    return str(selected() if callable(selected) else selected)


def _require_windows(
    platform: str | Callable[[], str] | None,
    system: str | Callable[[], str] | None,
) -> None:
    if _platform_name(platform, system).casefold() != "windows":
        raise NotWindowsError("ae-install is supported only on Windows")


def _env_value(env: Mapping[str, str], key: str) -> str | None:
    wanted = key.casefold()
    for name, value in env.items():
        if str(name).casefold() == wanted:
            return str(value)
    return None


def _program_files_roots(env: Mapping[str, str]) -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[str] = set()
    for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        value = _env_value(env, key)
        if not value:
            continue
        path = Path(value)
        identity = os.path.normcase(os.path.abspath(os.fspath(path)))
        if identity not in seen:
            seen.add(identity)
            roots.append(path)
    return tuple(roots)


def _unsafe(path: Path, fs: InstallerFS) -> bool:
    return fs.is_symlink(path) or fs.is_reparse(path)


def _validate_existing_tree(path: Path, fs: InstallerFS, *, expect_dir: bool) -> None:
    """Validate every existing component, including ancestors of ``path``."""

    if not fs.exists(path):
        raise InvalidAEPathError(f"Path does not exist: {path}")

    current = path
    while True:
        if fs.exists(current):
            if _unsafe(current, fs):
                raise UnsafePathError(f"Refusing unsafe symlink/reparse path: {current}")
            if current != path and not fs.is_dir(current):
                raise InvalidAEPathError(f"Path component is not a directory: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent

    if expect_dir and not fs.is_dir(path):
        raise InvalidAEPathError(f"Expected a directory: {path}")


def _ae_year(name: str) -> int | None:
    match = re.match(r"^Adobe After Effects\s+(\d{2,4})(?:\.\d+)?(?:\b|$)", name, re.IGNORECASE)
    if match is None:
        return None
    value = int(match.group(1))
    return value if value >= 1000 else 2000 + value

def _validate_product_root(path: Path, fs: InstallerFS, *, strict: bool) -> bool:
    support_files = path / "Support Files"
    executable = support_files / "AfterFX.exe"
    try:
        _validate_existing_tree(support_files, fs, expect_dir=True)
        _validate_existing_tree(executable, fs, expect_dir=False)
    except InvalidAEPathError:
        if strict:
            raise
        return False
    if not fs.is_file(executable):
        if strict:
            raise InvalidAEPathError(f"AfterFX.exe must be a regular file: {executable}")
        return False
    return True


def _path_identity(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def discover_installations(
    *,
    env: Mapping[str, str] | None = None,
    platform: str | Callable[[], str] | None = None,
    system: str | Callable[[], str] | None = None,
    fs: InstallerFS | None = None,
    roots: Iterable[str | os.PathLike[str]] | None = None,
) -> tuple[Path, ...]:
    """Discover supported After Effects installs below the Program Files Adobe roots."""

    _require_windows(platform, system)
    env = os.environ if env is None else env
    fs = InstallerFS() if fs is None else fs
    search_roots = tuple(Path(root) for root in roots) if roots is not None else _program_files_roots(env)
    found: dict[str, Path] = {}

    for root in search_roots:
        adobe = root / "Adobe"
        if not fs.exists(adobe):
            continue
        _validate_existing_tree(adobe, fs, expect_dir=True)
        try:
            entries = tuple(fs.iterdir(adobe))
        except OSError:
            continue
        for entry in entries:
            candidate = Path(entry)
            if not fs.exists(candidate):
                continue
            if _unsafe(candidate, fs):
                raise UnsafePathError(f"Refusing unsafe symlink/reparse path: {candidate}")
            if not fs.is_dir(candidate) or (_ae_year(candidate.name) or 0) < MIN_AE_YEAR:
                continue
            _validate_existing_tree(candidate, fs, expect_dir=True)
            if not _validate_product_root(candidate, fs, strict=False):
                continue
            _ = found.setdefault(_path_identity(candidate), candidate)

    return tuple(sorted(found.values(), key=lambda path: str(path).casefold()))


def _validate_explicit_path(path: Path, fs: InstallerFS) -> Path:
    if not fs.exists(path):
        raise InvalidAEPathError(f"After Effects path does not exist: {path}")
    _validate_existing_tree(path, fs, expect_dir=True)
    year = _ae_year(path.name)
    if year is None or year < MIN_AE_YEAR:
        raise InvalidAEPathError(
            f"After Effects path must be Adobe After Effects {MIN_AE_YEAR} or newer: {path}"
        )
    _ = _validate_product_root(path, fs, strict=True)
    return path


def _select_installation(
    ae_path: str | os.PathLike[str] | None,
    *,
    env: Mapping[str, str],
    platform: str | Callable[[], str] | None,
    system: str | Callable[[], str] | None,
    fs: InstallerFS,
) -> Path:
    if ae_path is not None:
        return _validate_explicit_path(Path(ae_path).expanduser(), fs)

    installations: tuple[Path, ...] = discover_installations(
        env=env,
        platform=platform,
        system=system,
        fs=fs,
    )
    if not installations:
        raise NoInstallationsError(
            f"No After Effects {MIN_AE_YEAR}+ installation was found under Program Files/Adobe"
        )
    if len(installations) > 1:
        raise MultipleInstallationsError(installations)
    return next(iter(installations))


def _ensure_child_directory(parent: Path, name: str, fs: InstallerFS) -> Path:
    child = parent / name
    if fs.exists(child):
        _validate_existing_tree(child, fs, expect_dir=True)
        return child
    _validate_existing_tree(parent, fs, expect_dir=True)
    fs.mkdir(child)
    _validate_existing_tree(child, fs, expect_dir=True)
    return child


def _panel_source(path: str | os.PathLike[str] | None) -> Path:
    return (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parent / "assets" / PANEL_FILENAME
    )


def install_panel(
    ae_path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    platform: str | Callable[[], str] | None = None,
    system: str | Callable[[], str] | None = None,
    fs: InstallerFS | None = None,
    panel_source: str | os.PathLike[str] | None = None,
) -> Path:
    """Install the packaged panel and return its destination path.

    The function only copies the panel. Adobe preferences are intentionally left
    untouched; callers should print :func:`manual_instructions` after success.
    """

    _require_windows(platform, system)
    env = os.environ if env is None else env
    fs = InstallerFS() if fs is None else fs
    selected = _select_installation(
        ae_path,
        env=env,
        platform=platform,
        system=system,
        fs=fs,
    )
    source = _panel_source(panel_source)
    _validate_existing_tree(source, fs, expect_dir=False)
    if fs.is_dir(source):
        raise InstallerError(f"Packaged panel asset is not a file: {source}")

    support_files = selected / "Support Files"
    _validate_existing_tree(support_files, fs, expect_dir=True)
    scripts = _ensure_child_directory(support_files, "Scripts", fs)
    panel_dir = _ensure_child_directory(scripts, "ScriptUI Panels", fs)
    destination = panel_dir / PANEL_FILENAME
    if fs.exists(destination):
        _validate_existing_tree(destination, fs, expect_dir=False)
        if fs.is_dir(destination):
            raise InstallerError(f"Panel destination is a directory: {destination}")
    fs.copy_file(source, destination)
    _validate_existing_tree(destination, fs, expect_dir=False)
    return destination


__all__ = [
    "FileSystem",
    "InstallerError",
    "InstallerFS",
    "InvalidAEPathError",
    "MANUAL_INSTRUCTIONS",
    "MIN_AE_YEAR",
    "MultipleInstallationsError",
    "NoInstallationsError",
    "NotWindowsError",
    "PANEL_FILENAME",
    "UnsafePathError",
    "discover_installations",
    "install_panel",
    "manual_instructions",
]
