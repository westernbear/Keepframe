from __future__ import annotations
from pathlib import Path
import numpy as np
from scipy.optimize import linear_sum_assignment
from ..ir.schema import Scene
from ..verify.matrix import animation_matrix
from ..verify.similarity import centroid_tracks, temporal_similarity
from .composite import composite_scene, load_texture


def _mean_dist(a: np.ndarray, b: np.ndarray) -> float:
    ok = ~(np.isnan(a[:, 0]) | np.isnan(b[:, 0]))
    return float(np.linalg.norm(a[ok] - b[ok], axis=1).mean()) if ok.sum() >= 2 else 1e9


def compare(golden: Scene, golden_dir: Path, analyzed: Scene, analyzed_dir: Path, source_frames: np.ndarray) -> dict:
    G, A = animation_matrix(golden), animation_matrix(analyzed)
    gids, aids = [e.id for e in golden.elements], [e.id for e in analyzed.elements]
    C = np.array([[_mean_dist(G[g][:, :2], A[a][:, :2]) for a in aids] for g in gids]) if gids and aids else np.zeros((0, 0))
    rows, cols = linear_sum_assignment(C) if C.size else ([], [])
    pairs = [(gids[r], aids[c]) for r, c in zip(rows, cols) if C[r, c] <= 5.0]
    errors = (len(gids) - len(pairs)) + (len(aids) - len(pairs))
    pos, scale, rgb, alpha = [], [], [], []
    for g, a in pairs:
        pos.append(C[gids.index(g), aids.index(a)])
        ok = ~(np.isnan(G[g][:, 2]) | np.isnan(A[a][:, 2]))
        scale.append(float(np.abs(G[g][ok, 2] - A[a][ok, 2]).mean()) if ok.any() else 0.0)
        tg = load_texture(golden_dir / golden.element(g).canonical.texture)
        ta = load_texture(analyzed_dir / analyzed.element(a).canonical.texture)
        import cv2
        ta = cv2.resize(ta, (tg.shape[1], tg.shape[0]), interpolation=cv2.INTER_AREA)
        m = (tg[..., 3] > 0.5) | (ta[..., 3] > 0.5)
        rgb.append(float(np.abs(tg[..., :3] - ta[..., :3])[m].mean()) if m.any() else 0.0)
        alpha.append(float(np.abs(tg[..., 3] - ta[..., 3]).mean()))
    cache: dict = {}
    fl1 = float(np.mean([np.abs(composite_scene(analyzed, analyzed_dir, f, cache) - source_frames[f] / 255.0).mean()
                         for f in range(0, analyzed.frames, max(1, analyzed.frames // 10))]))
    return {"matched": len(pairs), "tracking_errors": int(errors), "pos_err_px": float(np.mean(pos)) if pos else 1e9,
            "scale_err": float(np.mean(scale)) if scale else 1e9, "sprite_rgb_l1": float(np.mean(rgb)) if rgb else 1e9,
            "sprite_alpha_l1": float(np.mean(alpha)) if alpha else 1e9,
            "temporal": temporal_similarity(centroid_tracks(golden), centroid_tracks(analyzed)), "frame_l1": fl1}
