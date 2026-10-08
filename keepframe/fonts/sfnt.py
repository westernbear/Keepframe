"""WOFF2 → sfnt bytes through FreeType (C, milliseconds) instead of fontTools' Python glyf reconstruction
(≈ 45 s for Noto Serif KR). Uses the FreeType Pillow ships (which reads WOFF2), else the system one; returns None
when neither works, and callers fall back to fontTools."""
from __future__ import annotations

import ctypes
import ctypes.util
import functools
import glob
import io
from pathlib import Path

from ..log import get

log = get("keepframe.fonts")


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
    """The font's tables as a plain sfnt (TTF/OTF) file, decoded by FreeType; None when unavailable, over the
    size budget or inconsistent (callers then use fontTools)."""
    try:
        return _sfnt_bytes(Path(path), index)
    except Exception as e:   # any surprise in a font we do not control: the fontTools path decides
        log.warning("FreeType sfnt decode failed for %s (%s); using fontTools", Path(path).name, e)
        return None


def _sfnt_bytes(path: Path, index: int) -> bytes | None:
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
    if "head" not in tables or not ({"glyf", "CFF ", "CFF2"} & tables.keys()) or not _loca_ok(tables):
        return None
    from fontTools.ttLib.sfnt import SFNTWriter
    out = io.BytesIO()
    writer = SFNTWriter(out, len(tables), "OTTO" if "CFF " in tables or "CFF2" in tables else "\0\1\0\0")
    for tag in sorted(tables):
        writer[tag] = tables[tag]
    writer.close()
    return out.getvalue()
