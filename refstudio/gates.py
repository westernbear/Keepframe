from __future__ import annotations
from pathlib import Path
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
