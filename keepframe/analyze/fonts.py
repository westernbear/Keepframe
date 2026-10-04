from __future__ import annotations
import functools
import cv2, numpy as np
from ..edit.textraster import _HANGUL, font_path, render_lines, resolve_families

# Only families fontconfig resolves exactly: Chromium on the same machine draws the same face.
FONT_FAMILIES = ("Noto Sans CJK KR", "Noto Serif CJK KR", "Pretendard", "Inter", "Roboto", "Montserrat",
                 "DejaVu Sans", "DejaVu Serif", "Liberation Sans", "Liberation Serif", "WenQuanYi Zen Hei")


@functools.lru_cache(maxsize=1)
def installed_families() -> tuple[str, ...]:
    return tuple(f for f in FONT_FAMILIES if f.casefold() in {n.casefold() for n in resolve_families(f)})


def font_candidates(stroke: np.ndarray, text: str, size_px: float, k: int = 3) -> list[str]:
    """Rank installed families by mask IoU against the observed stroke mask.
    ponytail: whole-string IoU after resize; a learned font classifier if IoU ranking proves noisy on real clips."""
    if not text.strip() or len(text) > 200 or not 0 < size_px < float("inf") or not stroke.any():
        return []
    h, w = stroke.shape
    scored, seen = [], set()
    hangul = _HANGUL.search(text) is not None
    for family in installed_families():
        try:
            if hangul:
                path = font_path(family, hangul=False)
                if not path or font_path(family, hangul=True) != path:
                    continue
            img = render_lines([text], min(size_px, 128), (255, 255, 255), family)
            if img is None:
                continue
            raster = (img.shape, img.tobytes())
            if raster in seen:
                continue
            ys, xs = np.nonzero(img[..., 3] > 127)
            if not len(xs):
                continue
            glyph = cv2.resize((img[ys.min():ys.max() + 1, xs.min():xs.max() + 1, 3] > 127).astype(np.uint8), (w, h),
                               interpolation=cv2.INTER_NEAREST).astype(bool)
            scored.append(((glyph & stroke).sum() / max(1, (glyph | stroke).sum()), family))
            seen.add(raster)
        except (OSError, ValueError, MemoryError):
            continue
    return [family for _, family in sorted(scored, key=lambda item: -item[0])[:k]]
