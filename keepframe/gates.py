from __future__ import annotations
import math
import tempfile
from pathlib import Path
from statistics import mean
from typing import TYPE_CHECKING
from .analyze.constraints import extract_constraints
from .compose.composer import compose
from .ir.store import save_scene
from .ir.synth import make_synthetic_scene
from .render.renderer import render
from .verify.predicates import build_context, eval_pred
from .verify.verifier import verify

if TYPE_CHECKING:
    from .analyze.pipeline import AnalyzeOptions


def m1_gate(out_root: Path, n: int = 20, frames_per_scene: int = 3) -> dict:
    out_root = Path(out_root)
    rows, c_ok, h_ok, l_ok = [], 0, 0, 0
    for seed in range(1, n + 1):
        d = out_root / f"synth{seed}"
        scene = make_synthetic_scene(d, seed=seed)
        save_scene(scene, d / "scene.json")
        ctx = build_context(scene)
        cs = extract_constraints(scene)
        c_pass = all(eval_pred(c.pred, ctx) for c in cs)
        html = compose(scene, d, d / "composition.html")
        frames = sorted({0, scene.frames // 2, scene.frames - 1} | set(range(0, scene.frames, max(1, scene.frames // frames_per_scene))))
        r1 = render(html, scene, d / "r1", frames=frames)
        r2 = render(html, scene, d / "r2", frames=frames, probe=False)
        h_pass = r1.hashes == r2.hashes
        rep = verify(scene, d, render_result=r1)
        l_pass = rep.layer_max_err_px <= 2.0
        c_ok += c_pass; h_ok += h_pass; l_ok += l_pass
        rows.append({"seed": seed, "constraints": len(cs), "constraints_ok": c_pass, "hash_ok": h_pass,
                     "layer_max_err_px": rep.layer_max_err_px, "layer_ok": l_pass})
    return {"n": n, "constraints_ok": c_ok, "hash_ok": h_ok, "layer_ok": l_ok,
            "passed": c_ok == n and h_ok == n and l_ok == n, "rows": rows}


def m2_gate(out_root: Path, n: int = 20, refine: bool | None = None) -> dict:
    from .analyze.golden import compare
    from .analyze.pipeline import AnalyzeOptions, analyze
    from .analyze.video import read_frames, render_scene_video
    from .ir.store import current_scene, scene_dir
    if refine is None:
        from .analyze.refine import torch_available
        refine = torch_available()
    out_root = Path(out_root); rows = []
    for seed in range(1, n + 1):
        gd = out_root / f"gold{seed}"
        gold = make_synthetic_scene(gd, seed=seed, with_text=False, overlap=True)
        save_scene(gold, gd / "scene.json")
        vid = render_scene_video(gold, gd, out_root / f"gold{seed}.mp4")
        root = out_root / f"proj{seed}"
        analyze(vid, 0, gold.frames - 1, root, AnalyzeOptions(ocr=False, refine=refine))
        scene, _ = current_scene(root, "s1")
        frames, _ = read_frames(vid)
        m = compare(gold, gd, scene, scene_dir(root, "s1"), frames)
        rows.append({"seed": seed, **m})
    l1_ok = sum(r["frame_l1"] <= 0.02 for r in rows)
    trk_ok = all(r["tracking_errors"] <= 5 for r in rows)
    temporal = float(mean(r["temporal"] for r in rows))
    return {"n": n, "refine": refine, "frame_l1_ok": l1_ok, "tracking_ok": trk_ok, "temporal_mean": temporal,
            "passed": l1_ok >= math.ceil(0.8 * n) and trk_ok and temporal >= 0.7, "rows": rows}


def render_fidelity(root: Path, scene_id: str, clip: Path, sample_every: int = 10, max_frames: int = 150) -> dict:
    """Return normalized browser/source L1 and compared frame indices, starting at source zero."""
    import cv2, numpy as np
    from .ir.store import current_scene, scene_dir

    if sample_every <= 0 or max_frames <= 0:
        raise ValueError("sample_every and max_frames must be positive")
    scene, _ = current_scene(root, scene_id)
    indices = list(range(0, min(scene.frames, max_frames), sample_every))
    if not indices:
        raise ValueError("scene has no frames to compare")
    cap = cv2.VideoCapture(str(clip))
    try:
        if not cap.isOpened():
            raise FileNotFoundError(clip)
        with tempfile.TemporaryDirectory(prefix="keepframe-fidelity-") as tmp:
            tmp = Path(tmp)
            html = compose(scene, scene_dir(root, scene_id), tmp / "composition.html")
            rendered = render(html, scene, tmp / "render", frames=indices)
            samples = {f: i for i, f in enumerate(rendered.frames)}
            errors, compared = [], []
            for f in range(indices[-1] + 1):
                ok, source = cap.read()
                if not ok:
                    break
                if f not in samples:
                    continue
                image = rendered.frames_dir / f"f_{samples[f]:05d}.png"
                actual = cv2.imread(str(image), cv2.IMREAD_COLOR)
                if actual is None:
                    raise FileNotFoundError(image)
                if actual.shape[:2] != source.shape[:2]:
                    actual = cv2.resize(actual, (source.shape[1], source.shape[0]))
                errors.append(float(np.abs(actual.astype(np.float32) - source.astype(np.float32)).mean() / 255))
                compared.append(f)
            if not errors:
                raise ValueError(f"no source frames to compare in {clip}")
            return {"render_l1": float(mean(errors)), "frames": compared}
    finally:
        cap.release()


def m2_gate_real(clips_dir: Path, out_root: Path, max_frames: int = 150, options: AnalyzeOptions | None = None,
                 render_check: bool = False) -> dict:
    import json, time, cv2, numpy as np
    from collections import Counter
    from .analyze.pipeline import AnalyzeOptions, analyze
    from .ir.store import current_scene, scene_dir
    from .verify.matrix import animation_matrix
    from .web.liveaction import looks_live_action
    rows = []
    for clip in sorted(Path(clips_dir).glob("*.mp4")):
        cap = cv2.VideoCapture(str(clip))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or max_frames
        cap.release()
        end = min(total, max_frames) - 1
        root = Path(out_root) / clip.stem
        t0 = time.perf_counter()
        analyze(clip, 0, end, root, options or AnalyzeOptions())
        scene, _ = current_scene(root, "s1")
        rep = json.loads((scene_dir(root, "s1") / "report.json").read_text())
        row = {"clip": clip.name, "frames": scene.frames, "size": list(scene.size),
               "live_action": looks_live_action(clip), "elements": len(scene.elements),
               "kinds": dict(Counter(e.kind for e in scene.elements)),
               "mean_l1": rep["reconstruction"]["mean_l1"],
               "low_conf": sum(e.confidence < 0.7 for e in scene.elements),
               "keep_on": sum(c.keep for c in scene.constraints), "constraints": len(scene.constraints),
               "seconds": round(time.perf_counter() - t0, 1), "messages": rep["messages"]}
        row["render_l1"] = None
        row["render_frames"] = None
        if render_check:
            try:
                fidelity = render_fidelity(root, "s1", clip, max_frames=max_frames)
            except Exception as e:
                row["messages"].append(f"render check failed: {type(e).__name__}: {e}"[:200])
            else:
                row["render_l1"] = fidelity["render_l1"]
                row["render_frames"] = len(fidelity["frames"])
        gt = clip.with_suffix(".gt.json")
        if gt.exists():
            try:
                annotations = json.loads(gt.read_text()).get("elements", [])
            except (json.JSONDecodeError, AttributeError, TypeError):
                annotations = None
            if not isinstance(annotations, list):
                row["pos_err_px"] = None
                row["messages"].append("invalid ground truth JSON; pos_err_px unavailable")
                rows.append(row)
                continue
            M = animation_matrix(scene); errs = []; invalid_annotation = False
            for g in annotations:
                try:
                    items = sorted((int(f), np.asarray(xy, dtype=float)) for f, xy in g.get("frames", {}).items())
                    if any(xy.shape != (2,) or not np.isfinite(xy).all() for _, xy in items):
                        raise ValueError("invalid frame coordinates")
                except (AttributeError, TypeError, ValueError):
                    invalid_annotation = True
                    continue
                if not items or not M:
                    continue
                f0, xy0 = items[0]
                candidates = [k for k in M if 0 <= f0 < len(M[k]) and np.isfinite(M[k][f0, :2]).all()]
                if not candidates:
                    continue
                best = min(candidates, key=lambda k: np.linalg.norm(M[k][f0, :2] - xy0))
                errs += [float(np.linalg.norm(np.nan_to_num(M[best][f, :2], nan=1e6) - xy))
                         for f, xy in items if 0 <= f < len(M[best])]
            row["pos_err_px"] = float(np.mean(errs)) if errs else None
            if invalid_annotation:
                row["messages"].append("invalid ground truth annotation; pos_err_px may be incomplete")
        rows.append(row)
    return {"clips": len(rows), "max_frames": max_frames, "rows": rows}


def m3_gate(out_root: Path, n: int = 8) -> dict:
    from .analyze.constraints import extract_constraints
    from .edit.agent import edit
    from .ir.store import init_project

    out_root = Path(out_root)
    rows = []
    keep_ok = 0
    time_ok = 0
    done = 0
    for seed in range(1, n + 1):
        d = out_root / f"m3{seed}"
        root = d / "proj"
        sd = root / "scenes" / "s1"
        scene = make_synthetic_scene(sd, seed=seed, with_text=True)
        scene = scene.model_copy(update={"id": "s1"})
        scene.constraints = [
            c.model_copy(update={"keep": c.pred.startswith("type(")})
            for c in extract_constraints(scene)
        ]
        text = next(e for e in scene.elements if e.kind == "text")
        init_project(
            root,
            {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, scene.frames - 1]},
            scene,
        )
        res = edit(root, "s1", "문구를 Hello로", element=text.id, confirm=True)
        keep = res.verify.keep_pass_rate if res.verify else 0.0
        temporal = res.verify.temporal if res.verify and res.verify.temporal is not None else 0.0
        ok_keep = keep >= 0.95
        ok_time = temporal >= 0.7
        keep_ok += ok_keep
        time_ok += ok_time
        done += res.status == "done"
        rows.append({"seed": seed, "status": res.status, "keep": keep, "temporal": temporal})
    return {
        "n": n,
        "done": done,
        "keep_ok": keep_ok,
        "temporal_ok": time_ok,
        "passed": done == n and keep_ok == n and time_ok == n,
        "rows": rows,
    }
