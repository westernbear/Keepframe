"""WOFF2 → sfnt bytes through FreeType (C, milliseconds) instead of fontTools' Python glyf reconstruction
(≈ 45 s for Noto Serif KR). Uses the FreeType Pillow ships (which reads WOFF2), else the system one; returns None
when neither works, and callers fall back to fontTools.

FreeType runs in a child process: opening a WOFF2 face makes FreeType 2.13+ dlopen the system HarfBuzz (and with
it a second FreeType), which would capture the text layout Pillow binds lazily in this process (garbage advances
afterwards); the child also keeps a crafted font's native parsing out of the server's memory (1 GiB, 30 s)."""
from __future__ import annotations

import ctypes
import ctypes.util
import functools
import glob
import io
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from ..log import get

log = get("keepframe.fonts")
CHILD_TIMEOUT_S = 30
CHILD_MEMORY = 2**30


@functools.lru_cache(maxsize=1)
def _freetype():
    import PIL
    pil = Path(PIL.__file__).resolve().parent
    names = sorted(glob.glob(str(pil.parent / "pillow.libs" / "libfreetype*"))) + \
        sorted(glob.glob(str(pil / ".dylibs" / "libfreetype*"))) + [ctypes.util.find_library("freetype")]
    for name in names:
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
            lib.FT_Init_FreeType.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
            lib.FT_New_Memory_Face.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_long,
                                               ctypes.POINTER(ctypes.c_void_p)]
            lib.FT_Sfnt_Table_Info.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_ulong),
                                               ctypes.POINTER(ctypes.c_ulong)]
            lib.FT_Load_Sfnt_Table.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long, ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_ulong)]
            lib.FT_Done_Face.argtypes = [ctypes.c_void_p]
            lib.FT_Done_FreeType.argtypes = [ctypes.c_void_p]
            return lib
        except (OSError, AttributeError):
            continue
    return None


def _tag(value: int) -> str:
    return value.to_bytes(4, "big").decode("latin-1")


MAX_SFNT_BYTES = 64 * 2**20   # rebuilt tables, all together
MAX_GROWTH = 16                # ... and at most this multiple of the input file (bundled fonts: <= 5.2x)


def _budget(input_size: int) -> int:
    return min(MAX_SFNT_BYTES, MAX_GROWTH * max(1, input_size))


def _head_long_loca(tables: dict[str, bytes]) -> bool | None:
    head = tables.get("head", b"")
    return None if len(head) < 54 else int.from_bytes(head[50:52], "big", signed=True) == 1


def _glyf_read_length(tables: dict[str, bytes], input_size: int) -> int | None:
    """Bytes of glyf to read: 0 when FreeType's reported length already covers loca's last glyph, the larger
    length otherwise (its directory keeps the WOFF2 header length while the rebuilt glyf can be longer), None when
    that length — font-controlled — is past the budget."""
    long = _head_long_loca(tables)
    if long is None or not {"loca", "glyf"} <= tables.keys():
        return 0
    width = 4 if long else 2
    loca = tables["loca"]
    if len(loca) < width:
        return 0
    end = int.from_bytes(loca[-width:], "big") * (1 if long else 2)
    if end <= len(tables["glyf"]):
        return 0
    return end if end <= _budget(input_size) - sum(len(v) for k, v in tables.items() if k != "glyf") else None


def _loca_ok(tables: dict[str, bytes]) -> bool:
    """FreeType keeps a short loca even when its rebuilt glyf outgrows it; such tables are unusable (fontTools
    decodes those fonts instead). Trims glyf to the last glyph."""
    if "glyf" not in tables:
        return True
    long = _head_long_loca(tables)
    if long is None or not {"loca", "maxp"} <= tables.keys() or len(tables["maxp"]) < 6:
        return False
    width = 4 if long else 2
    if len(tables["loca"]) % width:
        return False
    import numpy as np
    loca = np.frombuffer(tables["loca"], ">u4" if long else ">u2").astype(np.int64) * (1 if long else 2)
    glyphs = int.from_bytes(tables["maxp"][4:6], "big")
    if len(loca) != glyphs + 1 or not bool(np.all(np.diff(loca) >= 0)) or int(loca[-1]) > len(tables["glyf"]):
        return False
    tables["glyf"] = tables["glyf"][:int(loca[-1])]
    return True


def sfnt_bytes(path: Path, index: int = 0) -> bytes | None:
    """The font's tables as a plain sfnt (TTF/OTF) file, decoded by FreeType in a child process; None when
    unavailable, over the size budget or inconsistent (callers then use fontTools)."""
    try:
        tables = _child_tables(Path(path), index)
        return None if tables is None else _assemble(tables)
    except Exception as e:   # any surprise in a font we do not control: the fontTools path decides
        log.warning("FreeType sfnt decode failed for %s (%s); using fontTools", Path(path).name, e)
        return None


def child_env() -> dict[str, str]:
    """Only what the interpreter needs for a font child (this decode, the upload check): no API keys, tokens or
    other server settings reach it."""
    paths = [str(Path(__file__).resolve().parents[2])] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
    env = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONPATH": os.pathsep.join(paths),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "LC_ALL": "C.UTF-8"}
    if os.name == "nt" and os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def _child_tables(path: Path, index: int) -> dict[str, bytes] | None:
    budget = _budget(path.stat().st_size)
    fd, out = tempfile.mkstemp(prefix="kf-sfnt-", suffix=".bin")
    os.close(fd)
    try:   # -P: the server's working directory never lands on the child's sys.path
        r = subprocess.run([sys.executable, "-P", "-B", "-c", "from keepframe.fonts.sfnt import _child; _child()",
                            str(path), str(index), out], env=child_env(), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=CHILD_TIMEOUT_S)
        if r.returncode != 0:
            return None
        data = Path(out).read_bytes()
    except subprocess.TimeoutExpired:
        log.warning("FreeType decode of %s timed out", path.name)
        return None
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    tables, pos, total = {}, 0, 0
    while pos < len(data):      # tag(4) length(4) bytes, as the child wrote them; re-checked against the budget
        tag, length = data[pos:pos + 4].decode("latin-1"), struct.unpack(">I", data[pos + 4:pos + 8])[0]
        total += length
        if total > budget or pos + 8 + length > len(data):
            return None
        tables[tag] = data[pos + 8:pos + 8 + length]
        pos += 8 + length
    return tables or None


def _child() -> None:
    """Child process entry: argv = path, index, output file."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY, CHILD_MEMORY))
    except (ImportError, ValueError, OSError):
        pass
    path, index, out = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    tables = _sfnt_tables(path, index)
    if tables is None:
        sys.exit(3)
    with open(out, "wb") as f:
        for tag, data in tables.items():
            f.write(tag.encode("latin-1")[:4].ljust(4) + struct.pack(">I", len(data)) + data)


def _assemble(tables: dict[str, bytes]) -> bytes | None:
    if "head" not in tables or not ({"glyf", "CFF ", "CFF2"} & tables.keys()) or not _loca_ok(tables):
        return None
    from fontTools.ttLib.sfnt import SFNTWriter
    out = io.BytesIO()
    writer = SFNTWriter(out, len(tables), "OTTO" if "CFF " in tables or "CFF2" in tables else "\0\1\0\0")
    for tag in sorted(tables):
        writer[tag] = tables[tag]
    writer.close()
    return out.getvalue()


def _sfnt_tables(path: Path, index: int) -> dict[str, bytes] | None:
    """The face's tables as FreeType rebuilt them (in the child), within the size budget."""
    lib = _freetype()
    if lib is None:
        return None
    data = path.read_bytes()
    budget = _budget(len(data))
    ftlib, face = ctypes.c_void_p(), ctypes.c_void_p()
    if lib.FT_Init_FreeType(ctypes.byref(ftlib)):
        return None
    try:
        if lib.FT_New_Memory_Face(ftlib, data, len(data), index, ctypes.byref(face)):
            return None
        try:
            count = ctypes.c_ulong()
            if lib.FT_Sfnt_Table_Info(face, 0, None, ctypes.byref(count)) or count.value > 512:
                return None
            tables, total = {}, 0
            for i in range(count.value):
                tag, length = ctypes.c_ulong(), ctypes.c_ulong()
                if lib.FT_Sfnt_Table_Info(face, i, ctypes.byref(tag), ctypes.byref(length)):
                    return None
                total += length.value
                if total > budget:
                    return None
                buf = ctypes.create_string_buffer(max(1, length.value))
                size = ctypes.c_ulong(length.value)
                if lib.FT_Load_Sfnt_Table(face, tag.value, 0, buf, ctypes.byref(size)):
                    return None
                tables[_tag(tag.value)] = buf.raw[:size.value]
            glyf = _glyf_read_length(tables, len(data))
            if glyf is None:
                return None
            if glyf:
                buf, size = ctypes.create_string_buffer(glyf), ctypes.c_ulong(glyf)
                if lib.FT_Load_Sfnt_Table(face, int.from_bytes(b"glyf", "big"), 0, buf, ctypes.byref(size)):
                    return None
                tables["glyf"] = buf.raw[:size.value]
        finally:
            lib.FT_Done_Face(face)
    finally:
        lib.FT_Done_FreeType(ftlib)
    return tables
