"""Render a page (kf-script, composer or a hand-authored reference) and compare it with the analysed source frames."""
from __future__ import annotations
import tempfile
from pathlib import Path

import numpy as np

from ..ae.verify import compare_frames
from ..ir.store import current_scene
from ..qa.edits import _source_frames
from ..qa.sheets import comparison_sheet
from ..render.renderer import load_frame, render


def fidelity(root: Path, scene_id: str, html: Path, *, every: int = 4, max_frames: int | None = None,
             sheet: Path | None = None) -> dict:
    """Mean and worst RGB L1 (0..1) of `html` against the scene's source frames, every `every`-th frame. The page
    only needs the renderer contract (__ready, __seek); the scene supplies size, fps and the frame range."""
    root = Path(root)
    scene, _ = current_scene(root, scene_id)
    frames = list(range(0, scene.frames, every))[:max_frames]
    src = _source_frames(root, scene_id, frames)
    frames = [f for f in frames if f in src]
    with tempfile.TemporaryDirectory(prefix="keepframe-kf-") as td:
        res = render(Path(html), scene, Path(td), frames=frames, probe=False)
        got = [(load_frame(res.frames_dir / f"f_{i:05d}.png") * 255).round().astype(np.uint8) for i in range(len(frames))]
    want = [src[f].round().clip(0, 255).astype(np.uint8) for f in frames]
    pairs = [compare_frames(w, g) for w, g in zip(want, got)]
    l1 = [p[0] for p in pairs]
    worst = int(np.argmax(l1))
    if sheet:
        pick = sorted({0, worst, len(frames) // 3, 2 * len(frames) // 3, len(frames) - 1})
        comparison_sheet([(want[i], got[i], pairs[i][1]) for i in pick], ("source", "page", "diff ×4"), sheet,
                         row_labels=[f"f{frames[i]} L1 {l1[i]:.4f}" for i in pick])
    return {"l1": float(np.mean(l1)), "worst": float(l1[worst]), "worst_frame": frames[worst], "frames": len(frames)}
