from __future__ import annotations
import functools
import cv2, numpy as np
from ..edit.textraster import render_lines, resolve_families

# Only families fontconfig resolves exactly: Chromium on the same machine draws the same face.
FONT_FAMILIES = ("Noto Sans CJK KR", "Noto Serif CJK KR", "Pretendard", "Inter", "Roboto", "Montserrat",
                 "DejaVu Sans", "DejaVu Serif", "Liberation Sans", "Liberation Serif", "WenQuanYi Zen Hei")


@functools.lru_cache(maxsize=1)
def installed_families() -> tuple[str, ...]:
    return tuple(f for f in FONT_FAMILIES if f.casefold() in {n.casefold() for n in resolve_families(f)})


def font_candidates(stroke: np.ndarray, text: str, size_px: float, k: int = 3) -> list[str]:
    """Rank installed families by mask IoU against the observed stroke mask.
    ponytail: whole-string IoU after resize; a learned font classifier if IoU ranking proves noisy on real clips."""
    if not text.strip() or not stroke.any():
        return []
    h, w = stroke.shape
    scored = []
    for family in installed_families():
        img = render_lines([text], size_px, (255, 255, 255), family)
        if img is None:
            continue
        ys, xs = np.nonzero(img[..., 3] > 127)
        if not len(xs):
            continue
        glyph = cv2.resize((img[ys.min():ys.max() + 1, xs.min():xs.max() + 1, 3] > 127).astype(np.uint8), (w, h),
                           interpolation=cv2.INTER_NEAREST).astype(bool)
        scored.append(((glyph & stroke).sum() / max(1, (glyph | stroke).sum()), family))
    return [family for _, family in sorted(scored, reverse=True)[:k]]
