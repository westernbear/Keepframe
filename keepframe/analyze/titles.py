"""Title fragments: text tracks, junk-OCR shape tracks and glyph-sized sprites that belong to one title, grouped by
geometry, colour, onset and motion (no VLM needed; with VLM strings both texts must sit on one common line)."""
from __future__ import annotations
from dataclasses import dataclass
from difflib import SequenceMatcher
import cv2, numpy as np
from ..ir.schema import DEFAULTS, PROPS
from ..ir.tracks import is_tilted, projective_matrix

LINE_H_RATIO, LINE_DY, GAP_H, COLOUR_DE, ONSET_S, MOTION_PX, SUCCESSION_S, SUBSTRING = 1.6, 0.5, 1.2, 20.0, 1.0, 2.0, 0.5, 0.8
GLYPH_MAX = (1.6, 2.5)   # a sprite fragment's (height, width) in line heights
ROT_DEG, CONTAIN, CONCURRENT = 5.0, 0.5, 0.5


@dataclass
class Fragment:
    key: str
    text: str
    first: int
    last: int
    boxes: dict[int, tuple[float, float, float, float]]   # frame -> scene bbox
    lab: np.ndarray                                        # mean opaque colour, CIE Lab
    h: float                                               # median line height (canonical height × sy)
    rot: float                                             # median rotation, degrees


def fragment(key: str, p: dict, size, members=()) -> Fragment | None:
    """None for movers, solids, solid members (`members`: their `o<id>` keys, still props before the solid takes them) and video
    sprites (and anything without a visible frame)."""
    if key in members or key.startswith(("m", "solid")) or p.get("mover") or p.get("video") or p.get("kind") not in ("text", "sprite"):
        return None
    raw, canon = p["raw"], p["canon"]
    ch, cw = canon.shape[:2]
    corners = np.array([[-cw / 2, -ch / 2, 1], [cw / 2, -ch / 2, 1], [cw / 2, ch / 2, 1], [-cw / 2, ch / 2, 1]], float)
    boxes, hs, rots = {}, [], []
    for f in np.flatnonzero(np.isfinite(raw[:, 0])):
        q = {k: float(raw[f, i]) if i < raw.shape[1] and np.isfinite(raw[f, i]) else DEFAULTS[k] for i, k in enumerate(PROPS)}
        q["sky"] = 0.0
        if is_tilted(q, "sprite"):
            P = corners @ projective_matrix(q, size).T
            P = P[:, :2] / P[:, 2:]
        else:   # affine box; the rotation (not the skew) is what the bbox follows
            r, k = np.radians(q["rot"]), np.tan(np.radians(q["skx"]))
            M = np.array([[np.cos(r), -np.sin(r)], [np.sin(r), np.cos(r)]]) @ np.array([[1, k], [0, 1]]) @ np.diag([q["sx"], q["sy"]])
            P = corners[:, :2] @ M.T + [q["x"], q["y"]]
        boxes[int(f)] = (float(P[:, 0].min()), float(P[:, 1].min()), float(P[:, 0].max()), float(P[:, 1].max()))
        hs.append(ch * q["sy"]); rots.append(q["rot"])
    if not boxes:
        return None
    on = canon[..., 3] > 127 if canon.shape[2] == 4 else np.ones(canon.shape[:2], bool)
    rgb = canon[..., :3][on] if on.any() else canon[..., :3].reshape(-1, 3)
    lab = cv2.cvtColor(rgb.mean(0).astype(np.float32).reshape(1, 1, 3) / 255, cv2.COLOR_RGB2Lab)[0, 0]
    return Fragment(key, p.get("text") or p.get("ocr_text") or "", min(boxes), max(boxes), boxes, lab, float(np.median(hs)), float(np.median(rots)))


def _compact(s: str) -> str:
    return "".join(s.split()).casefold()


def partial_ratio(a: str, b: str) -> float:
    """Best difflib ratio of the shorter compact, casefolded string against any same-length window of the longer."""
    a, b = _compact(a), _compact(b)
    if not a or not b:
        return 0.0
    a, b = sorted((a, b), key=len)
    n = len(a)
    return max(SequenceMatcher(None, a, b[i:i + n]).ratio() for i in range(len(b) - n + 1))


def _size(b):
    return b[2] - b[0], b[3] - b[1]


def _is_glyph(s: Fragment, ref: Fragment) -> bool:
    w, h = map(float, np.median([_size(b) for b in s.boxes.values()], axis=0))
    return h <= GLYPH_MAX[0] * ref.h and w <= GLYPH_MAX[1] * ref.h


def _sprite(f: Fragment) -> bool:
    return not f.text


def same_title(a: Fragment, b: Fragment, strings: list[str], fps: float) -> bool:
    h = max(a.h, b.h)
    if max(a.h, b.h) > LINE_H_RATIO * min(a.h, b.h) or np.linalg.norm(a.lab - b.lab) > COLOUR_DE or abs(a.rot - b.rot) > ROT_DEG:
        return False
    common = sorted(a.boxes.keys() & b.boxes.keys())
    cy = lambda f, i: (f.boxes[i][1] + f.boxes[i][3]) / 2
    dy = np.median([abs(cy(a, i) - cy(b, i)) for i in common]) if common else \
        abs(np.median([cy(a, i) for i in a.boxes]) - np.median([cy(b, i) for i in b.boxes]))
    if dy > LINE_DY * h:
        return False
    if _sprite(a) and _sprite(b):   # nothing anchors the glyph size
        return False
    for s, ref in ((a, b), (b, a)):   # glyph-sized and mostly concurrent with the text it joins
        if _sprite(s) and (not _is_glyph(s, ref) or len(common) < CONCURRENT * len(s.boxes)):
            return False
    if common:   # concurrent and side by side
        gap = np.median([max(a.boxes[i][0], b.boxes[i][0]) - min(a.boxes[i][2], b.boxes[i][2]) for i in common])
        va = np.diff([(a.boxes[i][0] + a.boxes[i][2]) / 2 for i in common]), np.diff([cy(a, i) for i in common])
        vb = np.diff([(b.boxes[i][0] + b.boxes[i][2]) / 2 for i in common]), np.diff([cy(b, i) for i in common])
        dv = float(np.median(np.hypot(va[0] - vb[0], va[1] - vb[1]))) if len(common) > 1 else 0.0
        side = gap <= GAP_H * h and abs(a.first - b.first) / fps <= ONSET_S and dv <= MOTION_PX
    else:
        side = False
    if not side:   # successive in place: the later one starts after the earlier one ends, inside its box, repeating part of its string
        e, l = (a, b) if a.last <= b.first else (b, a)
        if l.first < e.last:
            return False
        be, bl = e.boxes[e.last], l.boxes[l.first]
        w, hh = min(be[2], bl[2]) - max(be[0], bl[0]), min(be[3], bl[3]) - max(be[1], bl[1])
        small = min(np.prod(_size(be)), np.prod(_size(bl)))
        if not ((l.first - e.last) / fps <= SUCCESSION_S and w > 0 and hh > 0 and small > 0 and w * hh / small >= CONTAIN
                and partial_ratio(a.text, b.text) >= SUBSTRING):
            return False
    if strings:   # ponytail: a text-less sprite cannot be checked against the lines; the geometry gates vouch for it
        texts = [t for t in (a.text, b.text) if t]
        lines = [ln for s in strings for ln in (s.splitlines() or [s])]
        return any(all(partial_ratio(t, ln) >= SUBSTRING for t in texts) for ln in lines)
    return True


def group_fragments(frags: list[Fragment], strings: list[str], fps: float) -> list[list[Fragment]]:
    """Texts join through text-text `same_title` links only (union-find); a text-less sprite joins the one text group it
    qualifies for, and no group when it qualifies for several (it would bridge separate titles). Input order."""
    parent = list(range(len(frags)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i

    texts = [i for i, f in enumerate(frags) if not _sprite(f)]
    for n, i in enumerate(texts):
        for j in texts[n + 1:]:
            if same_title(frags[i], frags[j], strings, fps):
                parent[find(j)] = find(i)
    roots = {find(i) for i in texts}
    for i, f in enumerate(frags):
        if _sprite(f):
            near = {find(j) for j in texts if same_title(f, frags[j], strings, fps)}
            if len(near) == 1:
                parent[i] = near.pop()
    groups: dict[int, list[Fragment]] = {}
    for i, f in enumerate(frags):
        groups.setdefault(find(i), []).append(f)
    return list(groups.values())
