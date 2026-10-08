"""Metrics for clean layers. Images are RGB in 0..255 (uint8 or float); α in 0..1; ΔE is CIE76 (ir.colour).
An `rgba` layer is F (straight colour, 0..255) plus α: float with α in 0..1, or uint8 with α in 0..255."""
from __future__ import annotations
import unicodedata
from difflib import SequenceMatcher
import cv2
import numpy as np
from ..ir.colour import delta_e, hex_to_rgb8, srgb_to_lab

RESIDUE_DE = 10.0      # a hole pixel this far from the ring fit still shows the element
HF_EPS = 0.5           # L units: flat plates (no texture either side) read as hf_ratio 1
LEAK_DE = 5.0


def _lab(img) -> np.ndarray:
    return srgb_to_lab(np.asarray(img, np.float32))


def _de(a, b) -> np.ndarray:
    return delta_e(_lab(a), _lab(b))


def _split(rgba) -> tuple[np.ndarray, np.ndarray]:
    rgba = np.asarray(rgba)
    a = rgba[..., 3].astype(np.float32)
    return rgba[..., :3].astype(np.float32), a / 255.0 if rgba.dtype == np.uint8 else a


def _grow(mask: np.ndarray, px: int) -> np.ndarray:
    if px <= 0:
        return mask.astype(bool)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
    return cv2.dilate(mask.astype(np.uint8), k) > 0


def ring_mask(mask: np.ndarray, ring: tuple[float, float]) -> np.ndarray:
    d = cv2.distanceTransform((~mask.astype(bool)).astype(np.uint8), cv2.DIST_L2, 5)
    return (d >= ring[0]) & (d <= ring[1])


def _ring_fit(values: np.ndarray, src: np.ndarray, dst: np.ndarray) -> tuple[np.ndarray, int]:
    """Least-squares surface per channel fitted on `src` pixels, evaluated on `dst` pixels: degree 2 when the
    ring surrounds the hole, 1 for a one-sided ring, 0 for a few pixels; clamped to the ring's range so a
    one-sided fit cannot run away."""
    ys, xs = np.nonzero(src)
    ty, tx = np.nonzero(dst)
    around = xs.min() <= tx.min() and xs.max() >= tx.max() and ys.min() <= ty.min() and ys.max() >= ty.max()
    degree = 2 if around and len(xs) >= 60 else 1 if len(xs) >= 20 else 0
    cx, cy, s = xs.mean(), ys.mean(), max(np.ptp(xs), np.ptp(ys), 1) / 2

    def design(x, y):
        x, y = (x.astype(np.float64) - cx) / s, (y.astype(np.float64) - cy) / s
        return np.stack([np.ones_like(x), x, y, x * x, x * y, y * y][:(1, 3, 6)[degree]], -1)

    ring = values[src].astype(np.float64)
    coef, *_ = np.linalg.lstsq(design(xs, ys), ring, rcond=None)
    pred = np.clip(design(tx, ty) @ coef, ring.min(0) - 5, ring.max(0) + 5)
    return pred.astype(np.float32), degree


def plate_residue(plate, mask, ring=(6, 16), *, exclude=None) -> dict:
    """How clean a plate is where an element was: a (degree-2) fit of the ring `ring` px around `mask`
    predicts the plate inside. mean/p95 ΔE compare the low-passed plate with that fit, residue_fraction
    counts pixels over RESIDUE_DE, hf_ratio compares fine-detail energy (L) inside vs the ring (1 = same
    texture, < 1 smoother than its surroundings, > 1 edges or ghosts). `exclude` (other layers), grown by 3 px,
    is left out of both."""
    mask = np.asarray(mask, bool)
    keep = ~_grow(np.asarray(exclude, bool), 3) if exclude is not None else np.ones_like(mask)   # past the filters' reach
    inside, ring_px = mask & keep, ring_mask(mask, ring) & keep
    if not inside.any() or ring_px.sum() < 6:
        return {"mean_de": None, "p95_de": None, "hf_ratio": None, "residue_fraction": None, "pixels": int(inside.sum()),
                "fit_degree": None}
    plate = np.asarray(plate, np.float32)
    low = _lab(cv2.GaussianBlur(plate, (0, 0), 1.0))
    fit, degree = _ring_fit(low, ring_px, inside)
    de = delta_e(low[inside], fit)
    L = _lab(plate)[..., 0]
    hf = np.abs(L - cv2.GaussianBlur(L, (0, 0), 1.5))
    return {"mean_de": float(de.mean()), "p95_de": float(np.percentile(de, 95)),
            "hf_ratio": float((hf[inside].mean() + HF_EPS) / (hf[ring_px].mean() + HF_EPS)),
            "residue_fraction": float((de > RESIDUE_DE).mean()), "pixels": int(inside.sum()), "fit_degree": degree}


def bg_leak_fraction(rgba, local_plate) -> float:
    """Share of the layer's α > 0.5 pixels whose colour is the plate behind them (ΔE < 5): background
    baked into the texture."""
    fg, a = _split(rgba)
    m = a > 0.5
    if not m.any():
        return 0.0
    return float((_de(fg[m], np.asarray(local_plate, np.float32)[m]) < LEAK_DE).mean())


def leak_correlation(rgba, plate) -> float:
    """Regression slope of the layer's colour on the plate's (sRGB, α > 0.5 pixels), pooled within the
    layer's own colour clusters (k-means, k = 3), so glyph-vs-background contrast does not count: the share
    of the plate mixed into the texture (0 clean, 0.3 for a 30 % spill, 1 copied plate). 0 on flat plates."""
    fg, a = _split(rgba)
    m = a > 0.5
    if m.sum() < 16:
        return 0.0
    f, b = fg[m], np.asarray(plate, np.float32)[m]
    if b.std(0).max() < 1.0:
        return 0.0
    k = min(3, len(f))
    cv2.setRNGSeed(0)
    _, labels, _ = cv2.kmeans(f.astype(np.float32), k, None, (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.1),
                              2, cv2.KMEANS_PP_CENTERS)
    num = den = 0.0
    for g in range(k):
        sel = labels.ravel() == g
        if sel.sum() < 2:
            continue
        fc, bc = f[sel] - f[sel].mean(0), b[sel] - b[sel].mean(0)
        num += float((fc * bc).sum())
        den += float((bc * bc).sum())
    return num / den if den > 0 else 0.0


def halo_ring(recomposite, ideal, alpha_scene, width=4) -> float:
    """Mean ΔE between a recomposite and the ideal one on the `width`-px ring just outside α > 0.5:
    white fringes and baked background show up here after a background change."""
    inside = np.asarray(alpha_scene) > 0.5
    ring = _grow(inside, width) & ~inside
    if not ring.any():
        return 0.0
    return float(_de(np.asarray(recomposite, np.float32)[ring], np.asarray(ideal, np.float32)[ring]).mean())


def _box_mask(shape, box) -> np.ndarray:
    H, W = shape
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    m = np.zeros((H, W), bool)
    m[max(0, y0):min(H, y1), max(0, x0):min(W, x1)] = True
    return m


def outside_glyph_delta(src, rebuilt, edited, old_a, new_a, box, *, exclude=None) -> float:
    """After a text change, how much worse the pixels inside `box` but outside both glyph sets match the
    source than the unedited rebuild did (mean ΔE, floored at 0): revealed smears, pasted boxes."""
    glyphs = _grow((np.asarray(old_a) > 0.02) | (np.asarray(new_a) > 0.02), 2)
    region = _box_mask(glyphs.shape, box) & ~glyphs
    if exclude is not None:
        region &= ~np.asarray(exclude, bool)
    if not region.any():
        return 0.0
    src = np.asarray(src, np.float32)[region]
    worse = _de(np.asarray(edited, np.float32)[region], src).mean() - _de(np.asarray(rebuilt, np.float32)[region], src).mean()
    return float(max(0.0, worse))


def _fill(lab: np.ndarray, known: np.ndarray, need: np.ndarray) -> np.ndarray:
    """Estimate `need` pixels from `known` ones by normalised Gaussian averaging, nearest scale first."""
    out = np.zeros_like(lab)
    done = np.zeros(need.shape, bool)
    k = known.astype(np.float32)
    for sigma in (3, 6, 12, 24, 48):
        w = cv2.GaussianBlur(k, (0, 0), sigma)
        ok = need & ~done & (w > 0.05)
        if ok.any():
            v = cv2.GaussianBlur(lab * k[..., None], (0, 0), sigma)
            out[ok] = v[ok] / w[ok][:, None]
            done |= ok
    rest = need & ~done
    if rest.any():
        out[rest] = lab[known].mean(0) if known.any() else lab[rest]
    return out


def smear_score(edited, old_a, new_a, *, exclude=None) -> float:
    """Mean ΔE where the old glyphs were and the new ones are not, against the plate estimated from the
    surrounding non-glyph pixels: ghost glyphs a median plate absorbed show up here."""
    old_a, new_a = np.asarray(old_a), np.asarray(new_a)
    reveal = (old_a > 0.5) & ~_grow(new_a > 0.02, 2)
    known = ~_grow((old_a > 0.02) | (new_a > 0.02), 2)
    if exclude is not None:
        reveal &= ~np.asarray(exclude, bool)
        known &= ~np.asarray(exclude, bool)
    if not reveal.any():
        return 0.0
    ys, xs = np.nonzero(reveal)
    pad = 150
    H, W = reveal.shape
    y0, y1, x0, x1 = max(0, ys.min() - pad), min(H, ys.max() + pad + 1), max(0, xs.min() - pad), min(W, xs.max() + pad + 1)
    lab = _lab(np.asarray(edited, np.float32)[y0:y1, x0:x1])
    need = reveal[y0:y1, x0:x1]
    est = _fill(lab, known[y0:y1, x0:x1], need)
    return float(delta_e(lab[need], est[need]).mean())


def glyph_colour_delta(render, alpha, expected_hex) -> float | None:
    """ΔE between the median colour of solid glyph pixels (α > 0.9) and the expected colour; None when no
    glyph is solid (fading glyphs mix with what is behind them)."""
    m = np.asarray(alpha) > 0.9
    if m.sum() < 4:
        return None
    med = np.median(np.asarray(render, np.float32)[m], axis=0)
    return float(delta_e(_lab(med), _lab(np.float32(hex_to_rgb8(expected_hex)))))


def _band(alpha_gt: np.ndarray, band: int) -> np.ndarray:
    hard = (alpha_gt > 0.5).astype(np.uint8)
    edge = (cv2.dilate(hard, np.ones((3, 3), np.uint8)) != cv2.erode(hard, np.ones((3, 3), np.uint8)))
    return _grow(edge | ((alpha_gt > 0.02) & (alpha_gt < 0.98)), band)


def alpha_errors(alpha, alpha_gt, band=3) -> dict:
    """α error in the edge band (`band` px around the true edge): mean absolute (sad) and squared (mse)."""
    alpha, alpha_gt = np.asarray(alpha, np.float32), np.asarray(alpha_gt, np.float32)
    m = _band(alpha_gt, band)
    if not m.any():
        return {"sad": 0.0, "mse": 0.0, "band_px": 0}
    d = alpha[m] - alpha_gt[m]
    return {"sad": float(np.abs(d).mean()), "mse": float((d * d).mean()), "band_px": int(m.sum())}


def foreground_errors(fg, fg_gt, alpha, alpha_gt, band=3) -> dict:
    """Mean ΔE of F against the truth where both layers are visible (α > 0.1): interior vs edge band."""
    alpha, alpha_gt = np.asarray(alpha), np.asarray(alpha_gt)
    edge = _band(alpha_gt, band)
    valid = (alpha > 0.1) & (alpha_gt > 0.1)
    out = {}
    for name, m in (("interior", valid & ~edge & (alpha_gt > 0.98)), ("edge", valid & edge)):
        out[f"f_de_{name}"] = float(_de(np.asarray(fg, np.float32)[m], np.asarray(fg_gt, np.float32)[m]).mean()) if m.any() else None
    return out


def _norm(text: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _fragment(piece: str, whole: str) -> bool:
    if not piece or len(piece) >= 0.9 * len(whole):
        return False
    return SequenceMatcher(None, piece, whole, autojunk=False).find_longest_match(0, len(piece), 0, len(whole)).size >= 0.9 * len(piece)


def title_integrity(scene, titles) -> list[dict]:
    """Per gold title: is it one element whose text matches (NFKC + casefold, ratio ≥ 0.9; the longest-visible
    one when several match, the others are duplicates), and how many elements hold only a piece of it."""
    texts = [(e.id, _norm(e.canonical.text), e.visible[1] - e.visible[0]) for e in scene.elements if _norm(e.canonical.text)]
    out = []
    for t in titles:
        t = t if isinstance(t, dict) else {"text": t}
        gold = _norm(t["text"])
        scored = [(SequenceMatcher(None, s, gold, autojunk=False).ratio(), i, span) for i, s, span in texts]
        matches = [x for x in scored if x[0] >= 0.9]
        ratio, eid, _ = max(matches, key=lambda x: (x[2], x[0])) if matches else max(scored, default=(0.0, None, 0))
        pieces = [i for (i, s, _), (r, _, _) in zip(texts, scored) if r < 0.9 and _fragment(s, gold)]
        out.append({"text": t["text"], "role": t.get("role", "title"), "whole": bool(matches), "element": eid if matches else None,
                    "ratio": round(ratio, 3), "duplicates": max(0, len(matches) - 1), "fragments": len(pieces), "fragment_ids": pieces})
    return out
