from __future__ import annotations
import cv2, numpy as np
from ..ir.schema import Scene
from .matrix import animation_matrix


def centroid_tracks(scene: Scene) -> dict[str, np.ndarray]:
    return {eid: m[:, :2].copy() for eid, m in animation_matrix(scene).items()}


def tracklet_correlation(a: np.ndarray, b: np.ndarray, static_eps: float = 0.25) -> float | None:
    n = min(len(a), len(b)) - 1
    if n < 1:
        return None
    da, db = np.diff(a[: n + 1], axis=0), np.diff(b[: n + 1], axis=0)
    ok = ~(np.isnan(da).any(1) | np.isnan(db).any(1))
    if not ok.any():
        return None
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
    def best_scores(source: dict[str, np.ndarray], target: dict[str, np.ndarray]) -> list[float]:
        best = []
        for track in source.values():
            scores = [score for counterpart in target.values()
                      if (score := tracklet_correlation(track, counterpart)) is not None]
            if scores:
                best.append(max(scores))
        return best

    r2o, o2r = best_scores(ref, out), best_scores(out, ref)
    if not r2o and not o2r:
        identical = ref.keys() == out.keys() and all(np.array_equal(ref[key], out[key], equal_nan=True) for key in ref)
        return 1.0 if identical else 0.0
    return float(np.clip(0.5 * (np.mean(r2o) + np.mean(o2r)), 0.0, 1.0))


def _evidence_weight(track: np.ndarray) -> int:
    finite = np.isfinite(track).all(axis=1)
    return int(np.count_nonzero(finite[:-1] & finite[1:]))


def temporal_similarity_by_id(ref: dict[str, np.ndarray], out: dict[str, np.ndarray]) -> float:
    total_weight, weighted_score = 0, 0.0
    for eid, track in ref.items():
        weight = _evidence_weight(track)
        if weight == 0:
            continue
        score = tracklet_correlation(track, out[eid]) if eid in out else None
        weighted_score += weight * (score if score is not None else 0.0)
        total_weight += weight
    if total_weight == 0:
        motionless = all(_evidence_weight(out[eid]) == 0 for eid in ref if eid in out)
        return 1.0 if motionless else 0.0
    return float(np.clip(weighted_score / total_weight, 0.0, 1.0))


def temporal_by_id_detail(ref: dict[str, np.ndarray], out: dict[str, np.ndarray],
                          min_share: float = 0.10) -> tuple[float, tuple[str, float] | None]:
    weights = {eid: _evidence_weight(track) for eid, track in ref.items()}
    total = sum(weights.values())
    worst = None
    for eid, weight in weights.items():
        if not total or weight == 0 or weight / total < min_share:
            continue
        score = tracklet_correlation(ref[eid], out[eid]) if eid in out else None
        score = 0.0 if score is None else score
        if worst is None or score < worst[1]:
            worst = (eid, score)
    return temporal_similarity_by_id(ref, out), worst


def appearance_similarity(tex_a: np.ndarray, tex_b: np.ndarray) -> float:
    if tex_b.shape[:2] != tex_a.shape[:2]:
        tex_b = cv2.resize(tex_b, (tex_a.shape[1], tex_a.shape[0]), interpolation=cv2.INTER_AREA)
    pa, pb = tex_a.copy(), tex_b.copy()
    pa[..., :3] *= pa[..., 3:4]; pb[..., :3] *= pb[..., 3:4]
    return float(1.0 - np.abs(pa - pb).mean())


def frame_l1(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean())
