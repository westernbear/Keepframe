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


def _mask_iou(img: np.ndarray, stroke: np.ndarray) -> float:
    ys, xs = np.nonzero(img[..., 3] > 127)
    if not len(xs):
        return 0.0
    h, w = stroke.shape
    glyph = cv2.resize((img[ys.min():ys.max() + 1, xs.min():xs.max() + 1, 3] > 127).astype(np.uint8), (w, h),
                       interpolation=cv2.INTER_NEAREST).astype(bool)
    return float((glyph & stroke).sum() / max(1, (glyph | stroke).sum()))


def font_family_guess(stroke: np.ndarray, text: str, size_px: float, candidates: list[str]) -> str:
    if not candidates:
        return "sans-serif"
    try:
        top = render_lines([text], min(size_px, 128), (255, 255, 255), candidates[0])
        baseline = render_lines([text], min(size_px, 128), (255, 255, 255), "sans-serif")
        if top is not None and baseline is not None and _mask_iou(top, stroke) >= _mask_iou(baseline, stroke) + 0.05:
            return candidates[0]
    except (OSError, ValueError, MemoryError):
        pass
    return "sans-serif"


def font_candidates(stroke: np.ndarray, text: str, size_px: float, k: int = 3) -> list[str]:
    """Rank installed families by mask IoU against the observed stroke mask.
    ponytail: whole-string IoU after resize; a learned font classifier if IoU ranking proves noisy on real clips."""
    if not text.strip() or len(text) > 200 or not 0 < size_px < float("inf") or not stroke.any():
        return []
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
            scored.append((_mask_iou(img, stroke), family))
            seen.add(raster)
        except (OSError, ValueError, MemoryError):
            continue
    return [family for _, family in sorted(scored, key=lambda item: -item[0])[:k]]
