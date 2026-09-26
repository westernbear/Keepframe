from __future__ import annotations
import math
from pathlib import Path
from statistics import mean
from .analyze.constraints import extract_constraints
from .compose.composer import compose
from .ir.store import save_scene
from .ir.synth import make_synthetic_scene
from .render.renderer import render
from .verify.predicates import build_context, eval_pred
from .verify.verifier import verify


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


def m2_gate_real(clips_dir: Path, out_root: Path) -> dict:
    import json, numpy as np
    from .analyze.pipeline import AnalyzeOptions, analyze
    from .analyze.video import read_frames
    from .ir.store import current_scene, scene_dir
    from .verify.matrix import animation_matrix
    rows = []
    for clip in sorted(Path(clips_dir).glob("*.mp4")):
        frames, _ = read_frames(clip)
        root = Path(out_root) / clip.stem
        analyze(clip, 0, len(frames) - 1, root, AnalyzeOptions())
        scene, _ = current_scene(root, "s1")
        rep = json.loads((scene_dir(root, "s1") / "report.json").read_text())
        row = {"clip": clip.name, "elements": len(scene.elements), "mean_l1": rep["reconstruction"]["mean_l1"], "messages": rep["messages"]}
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
    return {"clips": len(rows), "rows": rows}


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
