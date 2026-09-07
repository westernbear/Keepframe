from __future__ import annotations
import re
from dataclasses import dataclass
import numpy as np
from ..ir.schema import Scene
from .matrix import Motion, bbox_matrix, extract_motions

TOL = {"cos": 0.9, "mag_rel": 0.10, "mag_abs": 2.0, "dur_frames": 2, "px": 2.0}
_HEAD = re.compile(r"^\s*([a-z_]+)\s*\((.*)\)\s*(?:@\s*(\d+))?\s*$")


def _split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _arg(tok: str):
    if tok.startswith("'") and tok.endswith("'"):
        return tok[1:-1]
    if tok.startswith("["):
        return [float(x) for x in tok[1:-1].split(",")]
    try:
        return float(tok)
    except ValueError:
        return tok


def parse_pred(s: str) -> tuple[str, list, int | None]:
    m = _HEAD.match(s)
    if not m:
        raise ValueError(f"bad predicate syntax: {s!r}")
    name, args, frame = m.group(1), [_arg(t) for t in _split_args(m.group(2))], m.group(3)
    return name, args, (int(frame) if frame is not None else None)


@dataclass
class PredContext:
    scene: Scene
    motions: dict[str, Motion]
    bboxes: dict[str, np.ndarray]


def build_context(scene: Scene) -> PredContext:
    return PredContext(scene=scene, motions={m.id: m for m in extract_motions(scene)}, bboxes=bbox_matrix(scene))


def _bbox(ctx: PredContext, eid, frame: int | None):
    f = ctx.scene.frames - 1 if frame is None else frame
    b = ctx.bboxes.get(eid)
    if b is None or f < 0 or f >= len(b) or np.isnan(b[f]).any():
        return None
    return b[f]


def eval_pred(s: str, ctx: PredContext) -> bool:
    name, a, frame = parse_pred(s)
    M, px = ctx.motions, TOL["px"]
    if name == "type":
        return a[0] in M and M[a[0]].type == a[1]
    if name == "dir":
        m = M.get(a[0])
        if m is None or m.dir is None:
            return False
        v = np.array(a[1], float); n = np.linalg.norm(v)
        return n > 0 and float(np.dot(m.dir, v / n)) >= TOL["cos"]
    if name == "mag":
        m = M.get(a[0])
        return m is not None and abs(m.mag - a[1]) <= max(TOL["mag_rel"] * abs(a[1]), TOL["mag_abs"])
    if name == "dur":
        m = M.get(a[0])
        return m is not None and abs(m.dur - a[1]) <= TOL["dur_frames"]
    if name in ("before", "after", "while"):
        m1, m2 = M.get(a[0]), M.get(a[1])
        if m1 is None or m2 is None:
            return False
        if name == "before":
            return m1.end <= m2.start
        if name == "after":
            return m2.end <= m1.start
        return min(m1.end, m2.end) - max(m1.start, m2.start) >= 1
    if name in ("left", "right", "top", "bottom", "intersect"):
        b1, b2 = _bbox(ctx, a[0], frame), _bbox(ctx, a[1], frame)
        if b1 is None or b2 is None:
            return False
        if name == "left":
            return bool(b1[2] <= b2[0] + px)
        if name == "right":
            return bool(b1[0] >= b2[2] - px)
        if name == "top":
            return bool(b1[3] <= b2[1] + px)
        if name == "bottom":
            return bool(b1[1] >= b2[3] - px)
        ox = min(b1[2], b2[2]) - max(b1[0], b2[0]); oy = min(b1[3], b2[3]) - max(b1[1], b2[1])
        return bool(ox > 0 and oy > 0)
    raise ValueError(f"unknown predicate: {name}")
