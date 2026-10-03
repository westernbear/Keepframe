from __future__ import annotations
from itertools import combinations
from ..ir.schema import Constraint, Scene
from ..verify.matrix import extract_motions
from ..verify.predicates import build_context, eval_pred


KEEP_PRESETS: dict[str, frozenset[str]] = {
    # spec §6: "문구·이미지만 바꾸고 나머지 유지" — every motion fact, no layout facts (new copy changes widths)
    "content_only": frozenset({"type", "dir", "mag", "dur", "before", "after", "while"}),
    # motion shape and order only: speed/size edits allowed
    "motion_shape": frozenset({"type", "dir", "before", "after"}),
    "all": frozenset({"type", "dir", "mag", "dur", "before", "after", "while", "left", "right", "top", "bottom", "intersect"}),
    "none": frozenset(),
}
DEFAULT_KEEP_PRESET = "content_only"


def apply_keep_preset(constraints: list[Constraint], preset: str) -> list[Constraint]:
    if preset not in KEEP_PRESETS:
        raise ValueError(f"unknown keep preset {preset!r}")
    names = KEEP_PRESETS[preset]
    return [c.model_copy(update={"keep": c.pred.split("(", 1)[0].strip() in names}) for c in constraints]


def carry_keep(constraints: list[Constraint], previous: list[Constraint]) -> list[Constraint]:
    """Reanalysis keeps the user's keep choice for predicates that still exist."""
    old = {c.pred: c.keep for c in previous}
    return [c.model_copy(update={"keep": old[c.pred]}) if c.pred in old else c for c in constraints]


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
