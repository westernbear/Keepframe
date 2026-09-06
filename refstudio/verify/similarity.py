from __future__ import annotations
import cv2, numpy as np
from ..ir.schema import Scene
from .matrix import animation_matrix


def centroid_tracks(scene: Scene) -> dict[str, np.ndarray]:
    return {eid: m[:, :2].copy() for eid, m in animation_matrix(scene).items()}


def tracklet_correlation(a: np.ndarray, b: np.ndarray, static_eps: float = 0.25) -> float:
    n = min(len(a), len(b)) - 1
    if n < 1:
        return 0.0
    da, db = np.diff(a[: n + 1], axis=0), np.diff(b[: n + 1], axis=0)
    ok = ~(np.isnan(da).any(1) | np.isnan(db).any(1))
    if not ok.any():
        return 0.0
    da, db = da[ok], db[ok]
    na, nb = np.linalg.norm(da, axis=1), np.linalg.norm(db, axis=1)
    both_static = (na < static_eps) & (nb < static_eps)
    one_static = ((na < static_eps) | (nb < static_eps)) & ~both_static
    with np.errstate(divide="ignore", invalid="ignore"):
        cos = (da * db).sum(1) / (na * nb)
        ratio = np.minimum(na, nb) / np.maximum(na, nb)
    score = np.where(both_static, 1.0, np.where(one_static, 0.0, cos * ratio))
    return float(np.mean(score))


def temporal_similarity(ref: dict[str, np.ndarray], out: dict[str, np.ndarray]) -> float:
    if not ref or not out:
        return 0.0
    r2o = np.mean([max(tracklet_correlation(r, o) for o in out.values()) for r in ref.values()])
    o2r = np.mean([max(tracklet_correlation(o, r) for r in ref.values()) for o in out.values()])
    return float(np.clip(0.5 * (r2o + o2r), 0.0, 1.0))


def appearance_similarity(tex_a: np.ndarray, tex_b: np.ndarray) -> float:
    if tex_b.shape[:2] != tex_a.shape[:2]:
        tex_b = cv2.resize(tex_b, (tex_a.shape[1], tex_a.shape[0]), interpolation=cv2.INTER_AREA)
    pa, pb = tex_a.copy(), tex_b.copy()
    pa[..., :3] *= pa[..., 3:4]; pb[..., :3] *= pb[..., 3:4]
    return float(1.0 - np.abs(pa - pb).mean())


def frame_l1(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean())
