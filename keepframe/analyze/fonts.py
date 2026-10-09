"""Font guesses for analysed text: thin wrappers over the render-and-compare matcher (`keepframe.fonts.match`), which
the style phase runs on the matted texture (Task 10)."""
from __future__ import annotations
import math
import numpy as np
from ..fonts import match as fontmatch
from ..fonts.registry import FontRegistry
from ..log import get

log = get("keepframe.analyze.fonts")
MAX_TEXT = 200


def font_candidates(stroke: np.ndarray, text: str, size_px: float, k: int = 3, *,
                    registry: FontRegistry | None = None) -> list[str]:
    """The k best families (uploaded + bundled) for the text's coverage `stroke` (bool or 0..1), best first; [] when
    there is nothing to match or matching fails."""
    stroke = np.asarray(stroke)
    if (not text or not text.strip() or len(text) > MAX_TEXT or not isinstance(size_px, (int, float))
            or not math.isfinite(size_px) or size_px <= 0 or stroke.size == 0 or not stroke.any()):
        return []
    try:
        fits, _ = fontmatch.match_font(stroke.astype(np.float32), text, registry or FontRegistry(), k=k)
    except (OSError, ValueError, LookupError, MemoryError) as e:
        log.warning("font candidates skipped: %s", e)
        return []
    return [f.family for f in fits[:k]]


def font_family_guess(stroke: np.ndarray, text: str, size_px: float, candidates: list[str]) -> str:
    """The best candidate, else the generic sans-serif."""
    return candidates[0] if candidates else "sans-serif"
