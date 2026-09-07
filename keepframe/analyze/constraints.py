from __future__ import annotations
from itertools import combinations
from ..ir.schema import Constraint, Scene
from ..verify.matrix import extract_motions
from ..verify.predicates import build_context, eval_pred


def extract_constraints(scene: Scene) -> list[Constraint]:
    motions = extract_motions(scene)
    preds: list[str] = []
    for m in motions:
        preds.append(f"type({m.id},'{m.type}')")
        if m.type == "translation" and m.dir is not None:
            preds.append(f"dir({m.id},[{m.dir[0]:.2f},{m.dir[1]:.2f}])")
        preds.append(f"mag({m.id},{m.mag:.1f})")
        preds.append(f"dur({m.id},{m.dur})")
    for m1, m2 in combinations(motions, 2):
        if m1.element == m2.element:
            continue
        if m1.end <= m2.start:
            preds.append(f"before({m1.id},{m2.id})")
        elif m2.end <= m1.start:
            preds.append(f"after({m1.id},{m2.id})")
        elif min(m1.end, m2.end) - max(m1.start, m2.start) >= 1:
            preds.append(f"while({m1.id},{m2.id})")
    last = scene.frames - 1
    ctx = build_context(scene)
    visible = [e for e in scene.elements if e.visible[0] <= last <= e.visible[1]]
    for a, b in combinations(visible, 2):
        for rel in ("left", "right", "top", "bottom", "intersect"):
            p = f"{rel}({a.id},{b.id})"
            if eval_pred(p, ctx):
                preds.append(p)
    return [Constraint(pred=p, keep=False) for p in preds]
