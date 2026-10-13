"""Structural diff of an analysis against round-trip truth (P2 Task 3, docs/superpowers/plans/2026-10-13-kfscript-p2-roundtrip.md).

Truth elements are matched one-to-one to IR elements by box IoU at the truth element's mid-visible frame (Hungarian).
`scenes` are the analysed scenes with the source frame each starts at, so a clip the analysis split still diffs whole.
Structure items marked `ir: "P3"` are recorded as unsupported, never as misses."""
from __future__ import annotations

import difflib
import re

import numpy as np
from scipy.optimize import linear_sum_assignment

from ..ir.colour import delta_e, hex_to_rgb8, srgb8_to_lab
from ..ir.schema import Scene
from ..ir.tracks import element_bbox

MATCH_IOU = 0.5


def _span(e, prop: str) -> float:
    keys = e.tracks[prop].keys if prop in e.tracks else []
    return keys[-1].v - keys[0].v if keys else 0.0


def _rise(e, prop: str) -> float:
    """The largest increase of `prop` along its keys (0 when it never rises)."""
    vs = [k.v for k in e.tracks[prop].keys] if prop in e.tracks else []
    return max((b - min(vs[:i + 1]) for i, b in enumerate(vs)), default=0.0)


def _fall(e, prop: str) -> float:
    """How far `prop` falls from its peak to its last key, as a share of the peak."""
    vs = [k.v for k in e.tracks[prop].keys] if prop in e.tracks else []
    return (max(vs) - vs[-1]) / max(vs) if vs and max(vs) > 0 else 0.0


def _built(e) -> bool:
    """Its text comes in step by step: glyph delays that spread, or a reveal rising to the full width."""
    g, rv = e.canonical.glyphs, [k.v for k in e.tracks["reveal"].keys] if "reveal" in e.tracks else []
    return bool(g and max(g.delays) - min(g.delays) >= 2) or bool(rv and min(rv) < 0.9 <= max(rv))


# Structure the IR already expresses, seen on the IR elements inside the targets over their life (half the declared
# move counts). A build is not a growing box: no piece's scale rises by more than 0.2 while it comes in.
FOUND = {"typing": lambda els, p: any(e.canonical.glyphs for e in els),
         "pop": lambda els, p: any("sx" in e.tracks and e.tracks["sx"].keys[0].v < 0.5 <= _span(e, "sx") for e in els),
         "drift": lambda els, p: any(_span(e, p["axis"]) * p["px"] >= 0.5 * p["px"] ** 2 for e in els),
         "build": lambda els, p: any(_built(e) for e in els) and not any(_rise(e, "sx") > 0.2 for e in els),
         "shrink": lambda els, p: any(min(_fall(e, "sx"), _fall(e, "sy")) >= (1 - p["scale"][1]) / 2 for e in els)}


def shot_f1(truth: dict, starts: list[int]) -> dict:
    """Shot boundaries found vs declared: a truth boundary inside a declared transition matches an analysed scene start
    anywhere in that transition (±1 frame), any other within ±1 frame; one match each."""
    want = [s[0] for s in truth.get("shots", [])[1:]]
    spans = [next(((t["frames"][0] - 1, t["frames"][1] + 1) for t in truth.get("transitions", [])
                   if t["frames"][0] <= b <= t["frames"][1] + 1), (b - 1, b + 1)) for b in want]
    got, hit = sorted(starts), 0
    for lo, hi in spans:
        m = next((g for g in got if lo <= g <= hi), None)
        if m is not None:
            got.remove(m); hit += 1
    p = hit / (hit + len(got)) if hit + len(got) else 1.0
    r = hit / len(want) if want else 1.0
    return {"truth": len(want) + 1, "ir_scenes": len(starts) + 1, "boundaries": sorted(starts),
            "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0}


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inter(a, b) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _iou(a, b) -> float:
    i = _inter(a, b)
    return i / (_area(a) + _area(b) - i) if i > 0 else 0.0


def _ir_box(el, start, scene: Scene, f: int):
    g = f - start
    if not (el.visible[0] <= g <= el.visible[1] and 0 <= g < scene.frames):
        return None
    W, H = scene.size
    x0, y0, x1, y1 = element_bbox(el, g, scene.size)
    b = (min(max(x0, 0), W), min(max(y0, 0), H), min(max(x1, 0), W), min(max(y1, 0), H))
    return b if _area(b) > 0 else None


def _inside(irs, f: int, tb) -> set[int]:
    """Indices of IR elements visible at f with at least half their box inside the truth box tb."""
    return {i for i, (el, start, sc) in enumerate(irs) if (b := _ir_box(el, start, sc, f)) and _inter(b, tb) >= 0.5 * _area(b)}


def _de(a: str, b: str) -> float:
    return float(delta_e(srgb8_to_lab(np.array(hex_to_rgb8(a))), srgb8_to_lab(np.array(hex_to_rgb8(b)))))


def _text(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _mean(xs) -> float | None:
    return round(float(np.mean(xs)), 4) if xs else None


def diff(truth: dict, scenes: list[tuple[Scene, int]], view_chars: int | None = None, truth_chars: int | None = None) -> dict:
    tels = [e for e in truth["elements"] if e.get("boxes")]
    irs = [(el, start, sc) for sc, start in scenes for el in sc.elements if el.kind != "group"]
    # the annotated frame nearest the middle (synthetic truth has a box on every visible frame; a read gold, on a few)
    mids = [min(map(int, e["boxes"]), key=lambda f: abs(f - (e["visible"][0] + e["visible"][1]) / 2)) for e in tels]
    tbox = [e["boxes"][str(m)] for e, m in zip(tels, mids)]
    boxes = [[_ir_box(el, s, sc, m) for el, s, sc in irs] for m in mids]
    M = np.array([[_iou(t, b) if b else 0.0 for b in row] for t, row in zip(tbox, boxes)]).reshape(len(tels), len(irs))
    match = {}
    if M.size:
        for r, c in zip(*linear_sum_assignment(-M)):
            if M[r, c] >= MATCH_IOU:
                match[r] = c
    rows, texts, colours, centres, vis = [], [], [], [], []
    for r, e in enumerate(tels):
        # fragments: IR elements with at least half their box inside the truth box; pieces: the same over its life
        frags = sorted(_inside(irs, mids[r], tbox[r]))
        a, z = e["visible"]
        life = {a, z, mids[r], *range(a, z + 1, max(1, (z - a) // 40))}   # ≤ 40 samples: short-lived pieces count too
        pieces = sorted(set().union(*(_inside(irs, f, e["boxes"][str(f)]) for f in life if str(f) in e["boxes"])))
        row = {"id": e["id"], "kind": e["kind"], "match": None, "iou": round(float(M[r].max()), 3) if irs else 0.0,
               "fragments": len(frags), "fragment_ids": [irs[i][0].id for i in frags], "pieces": len(pieces),
               "piece_ids": [irs[i][0].id for i in pieces]}
        c = match.get(r)
        if c is not None:
            el, start, _ = irs[c]
            b, t = boxes[r][c], tbox[r]
            row.update(match=el.id, iou=round(float(M[r, c]), 3))
            row["centre_px"] = round(float(np.hypot((b[0] + b[2] - t[0] - t[2]) / 2, (b[1] + b[3] - t[1] - t[3]) / 2)), 1)
            row["visible_err"] = max(abs(el.visible[0] + start - e["visible"][0]), abs(el.visible[1] + start - e["visible"][1]))
            centres.append(row["centre_px"]), vis.append(row["visible_err"])
        if e["kind"] == "text":
            got = irs[c][0].canonical if c is not None else None
            row["text_ratio"] = round(difflib.SequenceMatcher(None, _text(e["text"]), _text(got and got.text)).ratio(), 3)
            texts.append(row["text_ratio"])
            if got and got.color and e.get("colour"):
                row["colour_de"] = round(_de(e["colour"], got.color), 2)
                colours.append(row["colour_de"])
        rows.append(row)
    by_id = {row["id"]: row for row in rows}
    els = {el.id: el for el, _, _ in irs}
    structure = {"found": [], "missed": [], "unsupported": []}
    for s in truth.get("structure", []):
        if s["ir"] != "now" or s["type"] not in FOUND:
            structure["unsupported"].append(s["type"])
            continue
        over_life = [els[i] for t in s["targets"] if t in by_id for i in by_id[t]["piece_ids"]]
        structure["found" if FOUND[s["type"]](over_life, s.get("params", {})) else "missed"].append(s["type"])
    n = len(tels)
    return {"truth_elements": n, "ir_elements": len(irs), "matched": len(match), "matched_rate": round(len(match) / n, 4) if n else None,
            "fragments_mean": _mean([row["fragments"] for row in rows if row["fragments"]]),
            "over_split": sum(row["fragments"] > 1 for row in rows), "pieces_mean": _mean([row["pieces"] for row in rows if row["pieces"]]),
            "text_ratio": _mean(texts), "colour_de": _mean(colours),
            "centre_px": _mean(centres), "visible_err": _mean(vis), "structure": structure,
            "shots": shot_f1(truth, [start for _, start in scenes[1:]]),
            "keys_mean": _mean([sum(len(t.keys) for t in el.tracks.values()) for el, _, _ in irs]),
            "dl_ratio": round(view_chars / truth_chars, 3) if view_chars and truth_chars else None, "rows": rows}
