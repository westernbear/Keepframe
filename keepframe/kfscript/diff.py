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


# Structure the IR already expresses, seen on the IR elements inside the targets (half the declared move counts).
FOUND = {"typing": lambda els, p: any(e.canonical.glyphs for e in els),
         "pop": lambda els, p: any("sx" in e.tracks and e.tracks["sx"].keys[0].v < 0.5 <= _span(e, "sx") for e in els),
         "drift": lambda els, p: any(_span(e, p["axis"]) * p["px"] >= 0.5 * p["px"] ** 2 for e in els)}


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


def _de(a: str, b: str) -> float:
    return float(delta_e(srgb8_to_lab(np.array(hex_to_rgb8(a))), srgb8_to_lab(np.array(hex_to_rgb8(b)))))


def _text(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _mean(xs) -> float | None:
    return round(float(np.mean(xs)), 4) if xs else None


def diff(truth: dict, scenes: list[tuple[Scene, int]], view_chars: int | None = None, truth_chars: int | None = None) -> dict:
    tels = [e for e in truth["elements"] if e.get("boxes")]
    irs = [(el, start, sc) for sc, start in scenes for el in sc.elements if el.kind != "group"]
    mids = [(e["visible"][0] + e["visible"][1]) // 2 for e in tels]
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
        # fragments: IR elements with at least half their box inside the truth box
        frags = [i for i, b in enumerate(boxes[r]) if b and _inter(b, tbox[r]) >= 0.5 * _area(b)]
        row = {"id": e["id"], "kind": e["kind"], "match": None, "iou": round(float(M[r].max()), 3) if irs else 0.0,
               "fragments": len(frags), "fragment_ids": [irs[i][0].id for i in frags]}
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
        inside = [els[i] for t in s["targets"] if t in by_id for i in by_id[t]["fragment_ids"]]
        structure["found" if FOUND[s["type"]](inside, s.get("params", {})) else "missed"].append(s["type"])
    n = len(tels)
    return {"truth_elements": n, "ir_elements": len(irs), "matched": len(match), "matched_rate": round(len(match) / n, 4) if n else None,
            "fragments_mean": _mean([row["fragments"] for row in rows if row["fragments"]]),
            "over_split": sum(row["fragments"] > 1 for row in rows), "text_ratio": _mean(texts), "colour_de": _mean(colours),
            "centre_px": _mean(centres), "visible_err": _mean(vis), "structure": structure,
            "shots": {"truth": len(truth.get("shots", [])), "ir_scenes": len(scenes)},
            "dl_ratio": round(view_chars / truth_chars, 3) if view_chars and truth_chars else None, "rows": rows}
