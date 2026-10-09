"""Font registry: project uploads > bundled OFL set > system (fontconfig)."""
from __future__ import annotations

import functools
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..log import get

log = get("keepframe.fonts")
ROOT = Path(__file__).resolve().parent
_FILE_RE = re.compile(r"^[0-9a-f]{64}\.(ttf|otf|woff2?)$")
_HANGUL_BY_CATEGORY = {
    "geometric_sans": "Pretendard", "neo_grotesque": "Pretendard", "condensed_sans": "Pretendard",
    "display": "Pretendard", "script": "Pretendard", "mono": "Pretendard",
    "humanist_sans": "Noto Sans KR", "rounded_sans": "Noto Sans KR",
    "serif": "Noto Serif KR", "slab": "Noto Serif KR", "handwriting": "Nanum Pen Script",
}


@dataclass(frozen=True)
class FontFace:
    family: str
    source: str  # uploaded | bundled | system
    path: Path
    index: int = 0
    weight_range: tuple[int, int] = (400, 400)
    italic: bool = False
    category: str = "neo_grotesque"
    postscript: str | None = None
    sha256: str = ""
    latin: bool = True
    hangul: bool = False


def safe_alias(name: str) -> str:
    """CSS-safe, case-insensitive alias for a family name (never carries quotes, braces or slashes)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    if not slug:
        slug = "font" + (f"-{hashlib.sha1(name.encode()).hexdigest()[:6]}" if name.strip() else "")
    return f"kf-{slug}"


@functools.lru_cache(maxsize=1)
def _bundled() -> dict[str, tuple[FontFace, ...]]:
    data = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    out: dict[str, tuple[FontFace, ...]] = {}
    for fam in data["families"]:
        faces = tuple(FontFace(
            fam["family"], "bundled", ROOT / "files" / f["file"], 0, tuple(f["axes"]["wght"]), bool(f.get("italic")),
            fam["category"], f.get("postscript"), f["sha256"], "latin" in fam["scripts"], "hangul" in fam["scripts"])
            for f in fam["files"])
        out[fam["family"].casefold()] = faces
    return out


@functools.lru_cache(maxsize=256)
def _cmap(path: str, index: int, mtime: float) -> frozenset[int]:
    from fontTools.ttLib import TTFont
    with TTFont(path, fontNumber=index, lazy=True) as f:
        return frozenset(f.getBestCmap() or ())


def _load_uploaded(root: Path | None) -> dict[str, tuple[FontFace, ...]]:
    if root is None:
        return {}
    fonts_dir = Path(root) / "fonts"
    index = fonts_dir / "index.json"
    if not index.is_file():
        return {}
    try:
        items = json.loads(index.read_text(encoding="utf-8"))["fonts"]
        if not isinstance(items, list):
            raise ValueError("fonts is not a list")
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("font index %s unusable (%s); no uploaded fonts", index, e)
        return {}
    out: dict[str, list[FontFace]] = {}
    for it in items:
        try:
            name = it["file"]
            if not isinstance(name, str) or not _FILE_RE.match(name):
                raise ValueError(f"bad file {name!r}")
            path = fonts_dir / name
            if not path.is_file():
                raise ValueError(f"missing {name}")
            lo, hi = (int(x) for x in it.get("weight_range") or (400, 400))
            out.setdefault(str(it["family"]).casefold(), []).append(FontFace(
                str(it["family"]), "uploaded", path, 0, (lo, hi), "italic" in str(it.get("style", "")).casefold(),
                it.get("category") or "neo_grotesque", it.get("postscript"), it.get("sha256", ""),
                bool(it.get("latin", True)), bool(it.get("hangul", False))))
        except (KeyError, TypeError, ValueError) as e:
            log.warning("font index entry skipped: %s", e)
    return {k: tuple(v) for k, v in out.items()}


def _distance(face: FontFace, weight: int) -> int:
    lo, hi = face.weight_range
    return 0 if lo <= weight <= hi else min(abs(weight - lo), abs(weight - hi))


class FontRegistry:
    def __init__(self, uploaded: dict[str, tuple[FontFace, ...]] | None = None):
        self._uploaded = uploaded or {}

    @classmethod
    def for_project(cls, root: Path | str | None) -> "FontRegistry":
        return cls(_load_uploaded(Path(root) if root is not None else None))

    def _system_face(self, family: str) -> FontFace | None:
        # fontconfig answers every query with a best match, so accept only an exact family name
        from ..edit.textraster import _font_match, resolve_families
        if family.casefold() not in {n.casefold() for n in resolve_families(family)}:
            return None
        try:
            path, index = _font_match(family, False)
        except LookupError:
            return None
        return FontFace(family, "system", Path(path), index)

    def face(self, family: str, weight: int = 400, *, italic: bool = False) -> FontFace | None:
        key = family.casefold()
        for table in (self._uploaded, _bundled()):
            faces = table.get(key)
            if faces:
                return min(faces, key=lambda f: (f.italic != italic, _distance(f, weight)))
        return self._system_face(family)

    def faces(self, family: str) -> tuple[FontFace, ...]:
        """Every file of an uploaded or bundled family (empty for system names)."""
        key = family.casefold()
        return self._uploaded.get(key) or _bundled().get(key) or ()

    def families(self, *, script: str | None = None) -> list[str]:
        """Uploaded then bundled family names (the system set is not enumerated)."""
        names: dict[str, str] = {}
        for table in (self._uploaded, _bundled()):
            for key, faces in table.items():
                if script is None or any(getattr(f, script, False) for f in faces):
                    names.setdefault(key, faces[0].family)
        return list(names.values())

    def covers(self, family: str, text: str) -> bool:
        face = self.face(family)
        if face is None:
            return False
        try:
            cmap = _cmap(str(face.path), face.index, face.path.stat().st_mtime)
        except Exception as e:  # unreadable font: treat as no coverage
            log.warning("cmap read failed for %s: %s", face.path.name, e)
            return False
        return all(ord(c) in cmap for c in text if not c.isspace() and ord(c) >= 32)

    def hangul_fallback(self, family: str, weight: int = 400) -> tuple[str, int]:
        face = self.face(family, weight)
        target = _HANGUL_BY_CATEGORY.get(face.category if face else "neo_grotesque", "Pretendard")
        w = min(900, max(100, int(weight / 100 + 0.5) * 100))
        return target, 400 if target == "Nanum Pen Script" else w
