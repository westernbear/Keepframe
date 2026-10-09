"""Per-project font uploads: validated and size-capped, stored by sha256 under `<project>/fonts/` with the
`index.json` the registry reads (R4). Font bytes are never served by a route.

Validation, cheapest first: the name's extension, the magic bytes for that extension (no collections), for WOFF2 the
header and table directory sizes against the decoded budget and a bounded brotli pass (R38: fontTools' WOFF2 path is
an unbounded `brotli.decompress`), then — in a child process with a memory cap and a timeout — fontTools decodes
every table (cmap, head, hhea, hmtx, maxp, name and glyf+loca | CFF | CFF2 required; at most 65535 glyphs; Latin or
Hangul letters) and Pillow renders "Aa" (and "가")."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO

from ..ir.schema import FONT_FAMILY_RE, POSTSCRIPT_RE
from ..log import get
from .registry import _FILE_RE
from .sfnt import MAX_SFNT_BYTES

log = get("keepframe.fonts")
MAX_FONT_BYTES = 20 * 2**20
MAX_GLYPHS = 65535
MAX_META_BYTES = 2**20           # WOFF2 extended metadata, decoded
CHUNK_BYTES = 2**20
NAME_RE = re.compile(r"^[^/\\]{1,128}\.(ttf|otf|woff2)$")
CODES = ("too_large", "bad_type", "bad_tables", "unsupported")
REQUIRED_TABLES = ("cmap", "head", "hhea", "hmtx", "maxp", "name")
INSPECT_TIMEOUT_S = 120
INSPECT_MEMORY = 2 * 2**30
INSPECT_SLOTS = threading.BoundedSemaphore(2)   # fontTools decodes are CPU- and memory-heavy: two at a time
_MAGIC = {"ttf": (b"\0\1\0\0", b"true"), "otf": (b"OTTO",), "woff2": (b"wOF2",)}
_FLAVORS = (0x00010000, 0x4F54544F, 0x74727565)   # \0\1\0\0, OTTO, true (WOFF2 'ttcf' collections are refused)
_HANGUL_PROBE = "가나다라마바사아자차카타파하"
_GENERIC = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui"}
_INDEX_LOCKS: dict[str, threading.Lock] = {}
_INDEX_LOCKS_GUARD = threading.Lock()


class FontRejected(ValueError):
    """An upload that is not stored; `code` is what the browser shows (too_large, bad_type, bad_tables,
    unsupported)."""

    def __init__(self, code: str, reason: str = ""):
        assert code in CODES
        super().__init__(f"{code}: {reason}" if reason else code)
        self.code, self.reason = code, reason


class UploadIncomplete(ValueError):
    """The client sent fewer bytes than its Content-Length."""


def font_ext(filename: str) -> str | None:
    """'ttf', 'otf' or 'woff2' for an acceptable upload name (any case of extension), else None."""
    if not isinstance(filename, str) or "." not in filename:
        return None
    stem, ext = filename.rsplit(".", 1)
    m = NAME_RE.fullmatch(f"{stem}.{ext.lower()}")
    return m.group(1) if m else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_BYTES):
            h.update(chunk)
    return h.hexdigest()


def _fonts_dir(project_root: Path, *, create: bool = False) -> Path:
    d = Path(project_root) / "fonts"
    if create:
        d.mkdir(exist_ok=True)
    if d.is_symlink() or (d.exists() and not d.is_dir()):
        raise OSError(f"{d} is not a plain directory")
    return d


def receive(project_root: Path, stream: BinaryIO, length: int) -> Path:
    """Copy exactly `length` request-body bytes, in 1 MiB chunks, to `fonts/.upload-*.part`."""
    if length > MAX_FONT_BYTES:
        raise FontRejected("too_large")
    fd, name = tempfile.mkstemp(dir=_fonts_dir(project_root, create=True), prefix=".upload-", suffix=".part")
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            remaining = length
            while remaining:
                read = getattr(stream, "read1", stream.read)
                chunk = read(min(remaining, CHUNK_BYTES))
                if not chunk:
                    raise UploadIncomplete("upload ended early")
                out.write(chunk)
                remaining -= len(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


# --- checks on the raw bytes --------------------------------------------------------------------------------------

def _check_magic(data: bytes, ext: str) -> None:
    if data[:4] == b"ttcf":
        raise FontRejected("bad_type", "font collections are not supported")
    if data[:4] not in _MAGIC[ext]:
        raise FontRejected("bad_type", f"not a .{ext} file")


def _base128(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for i in range(5):
        if pos >= len(data):
            raise FontRejected("bad_tables", "truncated WOFF2 table directory")
        b = data[pos]
        pos += 1
        if i == 0 and b == 0x80:
            raise FontRejected("bad_tables", "bad WOFF2 number")
        if value & 0xFE000000:
            raise FontRejected("bad_tables", "WOFF2 number overflows")
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, pos
    raise FontRejected("bad_tables", "WOFF2 number too long")


def _bounded_brotli(data: bytes, limit: int) -> int:
    """Bytes `data` decompresses to, refusing as soon as that passes `limit` (never more than ~1 MiB held)."""
    import brotli
    d = brotli.Decompressor()
    try:
        total = len(d.process(data, output_buffer_limit=CHUNK_BYTES))
        while total <= limit and not d.can_accept_more_data():
            total += len(d.process(b"", output_buffer_limit=CHUNK_BYTES))
    except brotli.error as e:
        raise FontRejected("bad_tables", f"brotli: {e}") from None
    if total > limit:
        raise FontRejected("bad_tables", "WOFF2 data decodes past its declared size")
    if not d.is_finished():
        raise FontRejected("bad_tables", "WOFF2 data is truncated")
    return total


def check_woff2(data: bytes) -> None:
    """R38: the header's totalSfntSize and every table's origLength (and their sum) within MAX_SFNT_BYTES, then the
    font and metadata streams decoded under a bound — all before fontTools (or FreeType) decodes anything."""
    from fontTools.ttLib.woff2 import woff2KnownTags
    if len(data) < 48:
        raise FontRejected("bad_tables", "truncated WOFF2 header")
    (_, flavor, length, num_tables, _, total_sfnt, compressed, _, _, meta_offset, meta_length, meta_orig,
     _, _) = struct.unpack(">4sIIHHIIHHIIIII", data[:48])
    if flavor not in _FLAVORS:
        raise FontRejected("bad_type", "WOFF2 collections are not supported")
    if length != len(data) or not 0 < num_tables <= 512 or total_sfnt > MAX_SFNT_BYTES:
        raise FontRejected("bad_tables", "WOFF2 header out of range")
    pos, orig_sum, stream_sum = 48, 0, 0
    for _ in range(num_tables):
        if pos >= len(data):
            raise FontRejected("bad_tables", "truncated WOFF2 table directory")
        flags = data[pos]
        pos += 1
        if flags & 0x3F == 0x3F:
            tag, pos = data[pos:pos + 4].decode("latin-1"), pos + 4
        else:
            tag = woff2KnownTags[flags & 0x3F]
        orig, pos = _base128(data, pos)
        version = flags >> 6
        transformed = version != 3 if tag in ("glyf", "loca") else version != 0
        stream = orig
        if transformed:
            stream, pos = _base128(data, pos)
        orig_sum += orig
        stream_sum += stream
        if orig > MAX_SFNT_BYTES or orig_sum > MAX_SFNT_BYTES or stream_sum > MAX_SFNT_BYTES:
            raise FontRejected("bad_tables", "WOFF2 tables decode past 64 MiB")
    if pos + compressed > len(data):
        raise FontRejected("bad_tables", "WOFF2 font data out of range")
    if _bounded_brotli(data[pos:pos + compressed], stream_sum) != stream_sum:
        raise FontRejected("bad_tables", "WOFF2 data shorter than declared")
    if meta_length:
        if meta_orig > MAX_META_BYTES or meta_offset + meta_length > len(data):
            raise FontRejected("bad_tables", "WOFF2 metadata out of range")
        if _bounded_brotli(data[meta_offset:meta_offset + meta_length], meta_orig) != meta_orig:
            raise FontRejected("bad_tables", "WOFF2 metadata shorter than declared")


# --- table checks, in a child process ------------------------------------------------------------------------------

def _clean(value: str | None, limit: int) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in (value or "")).split())[:limit]


def _category(f) -> str:
    """A coarse category from the font's own classification (drives the Hangul fallback choice)."""
    os2 = f["OS/2"] if "OS/2" in f else None
    if ("post" in f and f["post"].isFixedPitch) or (os2 is not None and os2.panose.bProportion == 9):
        return "mono"
    if os2 is None:
        return "neo_grotesque"
    kind, serif, cls = os2.panose.bFamilyType, os2.panose.bSerifStyle, (os2.sFamilyClass >> 8) & 0xFF
    if kind == 3 or cls == 10:
        return "script"
    if kind == 4 or cls in (9, 12):
        return "display"
    if cls == 5 or (kind == 2 and serif in (4, 5, 6)):
        return "slab"
    if cls in (1, 2, 3, 4, 7) or (kind == 2 and 2 <= serif <= 10):
        return "serif"
    if kind == 2 and serif == 15:
        return "rounded_sans"
    return "neo_grotesque"


def _tables(path: Path) -> dict:
    """Every table decoded by fontTools; the metadata the index keeps. Raises FontRejected."""
    from fontTools.ttLib import TTFont
    with TTFont(str(path), lazy=False) as f:
        missing = [t for t in REQUIRED_TABLES if t not in f]
        if missing or not (("glyf" in f and "loca" in f) or "CFF " in f or "CFF2" in f):
            raise FontRejected("bad_tables", "missing " + (", ".join(missing) or "outlines"))
        f.ensureDecompiled()
        glyphs = f["maxp"].numGlyphs
        if not 0 < glyphs <= MAX_GLYPHS or len(f.getGlyphOrder()) > MAX_GLYPHS:
            raise FontRejected("bad_tables", f"{glyphs} glyphs")
        cmap = f.getBestCmap() or {}
        latin = all(c in cmap for c in range(0x41, 0x5B)) or all(c in cmap for c in range(0x61, 0x7B))
        hangul = all(ord(c) in cmap for c in _HANGUL_PROBE)
        if not (latin or hangul):
            raise FontRejected("unsupported", "no Latin or Hangul letters")
        name = f["name"]
        family = _clean(name.getDebugName(16) or name.getDebugName(1), 128)
        style = _clean(name.getDebugName(17) or name.getDebugName(2), 64) or "Regular"
        postscript = _clean(name.getDebugName(6), 64)
        os2 = f["OS/2"] if "OS/2" in f else None
        italic = bool(os2 is not None and os2.fsSelection & 1) or bool(f["head"].macStyle & 2)
        if italic and not re.search(r"italic|oblique", style, re.I):
            style = f"{style} Italic"
        if "fvar" in f and any(a.axisTag == "wght" for a in f["fvar"].axes):
            axis = next(a for a in f["fvar"].axes if a.axisTag == "wght")
            lo, hi = int(round(axis.minValue)), int(round(axis.maxValue))
        else:
            lo = hi = int(os2.usWeightClass) if os2 is not None and 1 <= os2.usWeightClass <= 1000 else 400
        meta = {"original_family": family, "style": style, "weight_range": [max(1, min(lo, 1000)), max(1, min(hi, 1000))],
                "postscript": postscript if POSTSCRIPT_RE.fullmatch(postscript) else None, "category": _category(f),
                "latin": latin, "hangul": hangul}
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(str(path), 48)
    for text in (["Aa"] if latin else []) + (["가"] if hangul else []):
        img = Image.new("L", (192, 96))
        ImageDraw.Draw(img).text((16, 8), text, font=font, fill=255)
        if img.getbbox() is None:
            raise FontRejected("bad_tables", f"{text!r} renders no ink")
    return meta


def _child() -> None:
    """Child process entry: argv = path; prints {"meta": ...} or {"code", "reason"}."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (INSPECT_MEMORY, INSPECT_MEMORY))
    except (ImportError, ValueError, OSError):
        pass
    try:
        out = {"meta": _tables(Path(sys.argv[1]))}
    except FontRejected as e:
        out = {"code": e.code, "reason": e.reason}
    except Exception as e:   # anything an untrusted font makes fontTools or Pillow raise, MemoryError included
        out = {"code": "bad_tables", "reason": f"{type(e).__name__}: {e}"[:200]}
    sys.stdout.write(json.dumps(out))


def _inspect_child(path: Path) -> dict:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(Path(__file__).resolve().parents[2])] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])))
    with INSPECT_SLOTS:
        try:
            r = subprocess.run([sys.executable, "-P", "-c", "from keepframe.fonts.upload import _child; _child()", str(path)],
                               env=env, capture_output=True, timeout=INSPECT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise FontRejected("bad_tables", f"not decoded within {INSPECT_TIMEOUT_S} s") from None
    try:
        out = json.loads(r.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        out = {}
    if r.returncode != 0 or not isinstance(out, dict) or ("meta" not in out and out.get("code") not in CODES):
        log.warning("font inspection exited %s: %s", r.returncode, r.stderr.decode("utf-8", "replace")[-300:])
        raise FontRejected("bad_tables", "the font could not be decoded")
    if "meta" not in out:
        raise FontRejected(out["code"], _clean(str(out.get("reason", "")), 200))
    return out["meta"]


def _family(original: str, sha: str) -> str:
    """A family name the IR accepts (letters, digits, spaces, hyphens; ≤ 64), never a CSS generic."""
    name = " ".join(re.sub(r"[^A-Za-z0-9 \-가-힣]", " ", original).split())[:64].strip()
    if not name or not FONT_FAMILY_RE.fullmatch(name):
        name = f"Uploaded {sha[:8]}"
    return f"{name[:59]} Font" if name.casefold() in _GENERIC else name


def inspect_font(path: Path, filename: str) -> dict:
    """The index entry (without `file`) of a font file named `filename`, or FontRejected."""
    path = Path(path)
    ext = font_ext(filename)
    if ext is None:
        raise FontRejected("bad_type", "only .ttf, .otf and .woff2 files")
    size = path.stat().st_size
    if size > MAX_FONT_BYTES:
        raise FontRejected("too_large")
    with open(path, "rb") as f:
        head = f.read(4)
    _check_magic(head, ext)
    if ext == "woff2":
        check_woff2(path.read_bytes())
    sha = _sha256(path)
    meta = _inspect_child(path)
    return {"family": _family(meta["original_family"], sha), "original_family": meta["original_family"],
            "style": meta["style"], "weight_range": meta["weight_range"], "postscript": meta["postscript"],
            "category": meta["category"], "ext": ext, "sha256": sha, "bytes": size, "latin": bool(meta["latin"]),
            "hangul": bool(meta["hangul"])}


# --- the index ----------------------------------------------------------------------------------------------------

@contextlib.contextmanager
def _index_lock(fonts_dir: Path):
    with _INDEX_LOCKS_GUARD:
        lock = _INDEX_LOCKS.setdefault(str(fonts_dir.resolve()), threading.Lock())
    with lock, open(fonts_dir / ".index.lock", "a+b") as stream:
        try:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except ImportError:   # no fcntl (Windows): the process lock only
            pass
        yield


def _read_index(fonts_dir: Path) -> list[dict]:
    index = fonts_dir / "index.json"
    if not index.is_file():
        return []
    try:
        items = json.loads(index.read_text(encoding="utf-8"))["fonts"]
        if not isinstance(items, list):
            raise ValueError("fonts is not a list")
    except (OSError, ValueError, KeyError, TypeError) as e:
        aside = index.with_name(f"index.unreadable-{int(time.time())}.json")
        log.warning("font index unreadable (%s); kept as %s, starting a new one", e, aside.name)
        os.replace(index, aside)
        return []
    return [it for it in items if isinstance(it, dict)]


def _write_index(fonts_dir: Path, items: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(dir=fonts_dir, prefix=".index-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"fonts": items}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, fonts_dir / "index.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _stored(fonts_dir: Path, items: list[dict], sha: str) -> dict | None:
    return next((it for it in items if it.get("sha256") == sha and isinstance(it.get("file"), str)
                 and _FILE_RE.fullmatch(it["file"]) and (fonts_dir / it["file"]).is_file()), None)


def store_font(project_root: Path, tmp: Path, filename: str) -> tuple[dict, bool]:
    """Validate `tmp` (consumed either way) and store it as `fonts/<sha256>.<ext>` with its index entry:
    (entry, True) when new, (the stored entry, False) for bytes already uploaded."""
    tmp = Path(tmp)
    try:
        fonts_dir = _fonts_dir(project_root, create=True)
        sha = _sha256(tmp)
        with _index_lock(fonts_dir):
            existing = _stored(fonts_dir, _read_index(fonts_dir), sha)
        if existing is not None:
            return existing, False
        entry = inspect_font(tmp, filename)
        entry["file"] = f"{entry['sha256']}.{entry['ext']}"
        with _index_lock(fonts_dir):
            items = _read_index(fonts_dir)
            existing = _stored(fonts_dir, items, sha)
            if existing is not None:
                return existing, False
            os.replace(tmp, fonts_dir / entry["file"])
            _write_index(fonts_dir, [it for it in items if it.get("sha256") != sha] + [entry])
        log.info("font stored %s %s (%d bytes)", entry["file"], entry["family"], entry["bytes"])
        return entry, True
    finally:
        tmp.unlink(missing_ok=True)


def list_fonts(project_root: Path) -> list[dict]:
    """The project's uploaded fonts (index entries whose file is stored)."""
    fonts_dir = _fonts_dir(project_root)
    if not fonts_dir.is_dir():
        return []
    try:
        with _index_lock(fonts_dir):
            items = _read_index(fonts_dir)
    except OSError as e:
        log.warning("font index unreadable: %s", e)
        return []
    return [it for it in items if isinstance(it.get("file"), str) and _FILE_RE.fullmatch(it["file"])
            and (fonts_dir / it["file"]).is_file()]


def scene_font_asset(scene_dir: Path, entry: dict, project_root: Path) -> str:
    """Copy an uploaded font into the scene's flat assets as `font-<sha16>.<ext>` (render plans pin it); returns the
    scene-relative path. The source must still hash to its entry."""
    name, sha = entry.get("file"), entry.get("sha256")
    if not isinstance(name, str) or not _FILE_RE.fullmatch(name) or name.split(".", 1)[0] != sha:
        raise ValueError("not a stored font entry")
    asset = f"font-{sha[:16]}.{name.rsplit('.', 1)[1]}"
    assets = Path(scene_dir) / "assets"
    dest = assets / asset
    if dest.is_file() and not dest.is_symlink() and _sha256(dest) == sha:
        return f"assets/{asset}"
    src = _fonts_dir(project_root) / name
    if _sha256(src) != sha:
        raise ValueError(f"{name} does not match its index entry")
    assets.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=assets, prefix=f".{asset}.", suffix=".tmp")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return f"assets/{asset}"


def pin_face(scene_dir: Path, face) -> str | None:
    """`scene_font_asset` for an uploaded registry face (its file sits in `<project>/fonts/`); None otherwise."""
    if face is None or face.source != "uploaded" or not face.sha256:
        return None
    return scene_font_asset(scene_dir, {"file": face.path.name, "sha256": face.sha256}, face.path.parent.parent)
