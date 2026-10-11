"""Title fragments: text tracks, junk-OCR shape tracks and glyph-sized sprites that belong to one title, grouped by
geometry, colour, onset and motion (no VLM needed; with VLM strings both texts must sit on one common line), and
merged into one text prop over the full string: its plane tracked by homography, its string re-read by OCR and
reconciled with the VLM's; a title whose glyphs come in one by one gets per-glyph delays (D15); a merged title that
renders worse than its fragments goes back to them, grouped (D16)."""
from __future__ import annotations
import math, re, time
from dataclasses import dataclass
from difflib import SequenceMatcher
import cv2, numpy as np
from ..ir.colour import rgb8_to_hex
from ..ir.schema import DEFAULTS, PROPS, GlyphReveal, Group
from ..ir.tracks import glyph_edges, is_tilted, perspective_px, projective_matrix
from ..log import describe, get
from .background import foreground_mask_plate
from .keyframes import fill_gaps
from .matting import as_plate
from .plane import PAD, contrast, fit_props, plane_raw, quad_tilted, rectify, text_quad, track_plane
from .text import _compact, _glyph_core_mask, on_line, partial_ratio, read_cover
from .textstyle import glyph_centres

log = get("keepframe.analyze")

LINE_H_RATIO, LINE_DY, GAP_H, COLOUR_DE, ONSET_S, MOTION_PX, SUCCESSION_S, SUBSTRING = 1.6, 0.5, 1.2, 20.0, 1.0, 2.0, 0.5, 0.8
GLYPH_MAX = (1.6, 2.5)   # a sprite fragment's (height, width) in line heights
ROT_DEG, CONTAIN, CONCURRENT, INSIDE = 5.0, 0.5, 0.5, 0.25
PIECE = 0.6   # a piece's misread text in its title's (`partial_ratio`, "vew" in "New Arrivals" 0.67); "STOP" in "SCROLLING" 0.5
MIN_FRAMES = 3   # R25: a piece seen in fewer frames is OCR flicker, not a fragment (it would pull cf to its frame)


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
    kind: str = "text"                                     # the prop's: "text" (a text track) or "sprite"


def fragment(key: str, p: dict, size, members=()) -> Fragment | None:
    """None for movers, solids, solid members (`members`: their `o<id>` keys, still props before the solid takes them) and video
    sprites (and anything visible in fewer than MIN_FRAMES frames)."""
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
    if len(boxes) < MIN_FRAMES:
        return None
    on = canon[..., 3] > 127 if canon.shape[2] == 4 else np.ones(canon.shape[:2], bool)
    rgb = canon[..., :3][on] if on.any() else canon[..., :3].reshape(-1, 3)
    lab = cv2.cvtColor(rgb.mean(0).astype(np.float32).reshape(1, 1, 3) / 255, cv2.COLOR_RGB2Lab)[0, 0]
    return Fragment(key, p.get("text") or p.get("ocr_text") or "", min(boxes), max(boxes), boxes, lab, float(np.median(hs)),
                    float(np.median(rots)), p["kind"])


def _digits_agree(a: str, b: str) -> bool:
    """One string's digits inside the other's: a reveal ("Episode 1" → "Episode 12"), not numbered cards in place
    ("Step 1" / "Step 2", "Step 9" / "Step 10")."""
    da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    return da in db or db in da


def _size(b):
    return b[2] - b[0], b[3] - b[1]


def _is_glyph(s: Fragment, ref: Fragment) -> bool:
    w, h = map(float, np.median([_size(b) for b in s.boxes.values()], axis=0))
    return h <= GLYPH_MAX[0] * ref.h and w <= GLYPH_MAX[1] * ref.h


def _sprite(f: Fragment) -> bool:
    return not f.text


def _inside(a: Fragment, b: Fragment, h: float, fps: float) -> bool:
    """V5: of two text tracks, the one with the shorter string, a piece of the other's (`partial_ratio` ≥ PIECE, its
    digits in the other's: not "Step 9" before "Step 10", not "STOP" before "SCROLLING"), lies inside the other's
    widest box along the line (± INSIDE·h) and within INSIDE·h of its median line centre at ≥ half its frames, within
    the other's life (± SUCCESSION_S): the title read in pieces while a sprite crossed it or as it faded in (seeds
    4/6), whatever the pieces' onsets, the jumps of their boxes (each holds what is read at a frame) and the colour of
    their canons (a sprite crossing one at its canonical frame).
    ponytail: a title that holds still (a moving one's widest box is one frame's); upgrade path: its widest box near
    each frame."""
    p, t = sorted((a, b), key=lambda f: len(_compact(f.text)))
    span = SUCCESSION_S * fps
    if (a.kind, b.kind) != ("text", "text") or len(_compact(p.text)) >= len(_compact(t.text)) \
            or p.first < t.first - span or p.last > t.last + span \
            or partial_ratio(p.text, t.text) < PIECE or not _digits_agree(p.text, t.text):
        return False
    W, m = max(t.boxes.values(), key=lambda q: q[2] - q[0]), INSIDE * h
    c = np.median([(q[1] + q[3]) / 2 for q in t.boxes.values()])
    return float(np.mean([W[0] - m <= q[0] and q[2] <= W[2] + m and abs((q[1] + q[3]) / 2 - c) <= m
                          for q in p.boxes.values()])) >= CONTAIN


def same_title(a: Fragment, b: Fragment, strings: list[str], fps: float) -> bool:
    h = max(a.h, b.h)
    if max(a.h, b.h) > LINE_H_RATIO * min(a.h, b.h) or abs(a.rot - b.rot) > ROT_DEG:
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
    side = _inside(a, b, h, fps)   # V5: a piece of the title, whatever the colour of its canon
    if not side and np.linalg.norm(a.lab - b.lab) > COLOUR_DE:
        return False
    if not side and common:   # concurrent and side by side
        gap = np.median([max(a.boxes[i][0], b.boxes[i][0]) - min(a.boxes[i][2], b.boxes[i][2]) for i in common])
        va = np.diff([(a.boxes[i][0] + a.boxes[i][2]) / 2 for i in common]), np.diff([cy(a, i) for i in common])
        vb = np.diff([(b.boxes[i][0] + b.boxes[i][2]) / 2 for i in common]), np.diff([cy(b, i) for i in common])
        dv = float(np.median(np.hypot(va[0] - vb[0], va[1] - vb[1]))) if len(common) > 1 else 0.0
        side = gap <= GAP_H * h and abs(a.first - b.first) / fps <= ONSET_S and dv <= MOTION_PX
    if not side:   # successive in place: the later one starts after the earlier one ends, inside its box, repeating part of its string
        e, l = (a, b) if a.last <= b.first else (b, a)
        if l.first < e.last:
            return False
        be, bl = e.boxes[e.last], l.boxes[l.first]
        w, hh = min(be[2], bl[2]) - max(be[0], bl[0]), min(be[3], bl[3]) - max(be[1], bl[1])
        small = min(np.prod(_size(be)), np.prod(_size(bl)))
        if not ((l.first - e.last) / fps <= SUCCESSION_S and w > 0 and hh > 0 and small > 0 and w * hh / small >= CONTAIN
                and partial_ratio(a.text, b.text) >= SUBSTRING and _digits_agree(a.text, b.text)):
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


# --- the merged title ---------------------------------------------------------------------------------------------

MERGE_FAILED = "merge_failed"   # fail-soft code (R52): a title left as its fragments
CF_OPACITY, CF_AREA, CF_CLUTTER, SPACE_GAP, SPACE_H = 0.9, 0.8, 0.02, 2.0, 0.3
TILT_ROT, TILT_CONVERGE, TILT_LEN = 2.0, 0.8, 5.0   # D12 with R8 (a yawed title converges only ≈ cap·tan(ry)/d)
LEN_TOL = 0.25   # a VLM line replaces an OCR string only within 25 % of its compact length (final review I2)


def _tilted(quad, info) -> bool:
    """D12 (R8): the quad turns ≥ 2° or converges ≥ 0.8°, measured only on a line of ink ≥ 5 cap heights wide: on a
    shorter word the cap, ascender, x-height and descender edges of 3–4 glyphs slope either line by degrees.
    ponytail: a short word stays flat, however it turns; upgrade path: the glyph stems' vanishing point."""
    width = float(np.linalg.norm(quad[1] - quad[0])) - 2 * PAD[0] * info["cap_px"]
    return width >= TILT_LEN * info["cap_px"] and quad_tilted(info, rot=TILT_ROT, converge=TILT_CONVERGE)


def _perspective(info, size) -> bool:
    """V3: rx/ry only where perspective is observable: the cap line and baseline converge ≥ 0.8° (a turn in the
    plane alone does not: rot carries it, rx = ry = 0) on a cap a 45° yaw converges by that much (cap·tan(ry)/d,
    d the stage perspective). On a smaller cap the line fits and ECC's projective terms are glyph noise that
    `fit_props` reads as large angles (ig1's notes, caps of 23 and 28 px at d 2667: rx 46°, ry 58°).
    ponytail: a title with a cap under d·tan(0.8°) (37 px at 1920 wide) stays flat however it turns; upgrade path:
    the foreshortening of the glyph advances along the line."""
    return abs(info["converge_deg"]) >= TILT_CONVERGE \
        and info["cap_px"] >= perspective_px(size) * math.tan(math.radians(TILT_CONVERGE))


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _compact(a), _compact(b)).ratio()


def _close(a: str, b: str) -> bool:
    """Compact lengths within LEN_TOL of the shorter's (one character on short words): a typo, not a wider block."""
    la, lb = len(_compact(a)), len(_compact(b))
    return abs(la - lb) <= max(1.0, LEN_TOL * min(la, lb))


def _vlm_line(text: str, strings: list[str]) -> str | None:
    """The VLM line (the strings split at their line breaks) of a close length (`_close`) nearest `text`, when within
    ratio 0.8 of it; None otherwise. A title's next line or an extra word never becomes its text."""
    lines = [ln for s in strings for ln in s.splitlines() if _close(ln, text)]
    best = max(lines, key=lambda s: _ratio(s, text), default=None)
    return best if best is not None and _ratio(best, text) >= SUBSTRING else None


def _union(boxes):
    bs = list(boxes)
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)) if bs else None


def _at(group: list[Fragment], f: int, keys=None):
    """The union box of the members (those in `keys`) visible at f; None when there are none."""
    return _union(g.boxes[f] for g in group if f in g.boxes and (keys is None or g.key in keys))


def _opacity(group: list[Fragment], props: dict, n: int) -> np.ndarray:
    """Per frame, the most visible member's opacity × revealed fraction (1 where none says)."""
    op = np.full(n, np.nan)
    for g in group:
        r, i, j = props[g.key]["raw"], PROPS.index("opacity"), PROPS.index("reveal")
        if r.shape[1] > i:
            op = np.fmax(op, r[:, i] * (np.nan_to_num(r[:, j], nan=1.0) if r.shape[1] > j else 1.0))
    return np.where(np.isnan(op), 1.0, op)


def _box(group: list[Fragment], f: int, shape) -> tuple[int, int, int, int]:
    """The members' union box at f, padded by 0.1·h and cut to the frame (whole pixels)."""
    (x0, y0, x1, y1), p = _at(group, f), PAD[0] * max(g.h for g in group)
    return max(0, int(x0 - p)), max(0, int(y0 - p)), min(shape[1], math.ceil(x1 + p)), min(shape[0], math.ceil(y1 + p))


def _on_colour(crop, behind) -> tuple[np.ndarray, np.ndarray]:
    """(foreground, the part with the title's colour): C is the foreground's median; a pixel has it within ΔE 25 of
    the line from the plate through C (a fade toward the plate, a gradient toward a darker C, anti-aliased edges)."""
    fg = foreground_mask_plate(crop, behind)
    if not fg.any():
        return fg, fg
    return fg, fg & on_line(crop, behind, np.median(crop[fg].astype(np.float32), axis=0))


def _title_contrast(crop, B, colour) -> np.ndarray:
    """The contrast of the foreground `on_line` of the title's colour at cf: what crosses the title is not tracked
    (V5: the crop's own median foreground was a sprite's colour where one filled it, and ECC saw glyph edges only)."""
    B = np.ascontiguousarray(np.broadcast_to(B, crop.shape))
    return contrast(crop, B) * (foreground_mask_plate(crop, B) & on_line(crop, B, colour))


def _settled(group: list[Fragment], frames, plate, op) -> int:
    """cf: of the frames that show the most members at ≥ 90 % of their peak opacity `op` (per frame) and with a union
    box ≥ 80 % of the largest (a title turning in perspective changes its box; ≤ 16 of them, evenly spread), those
    whose foreground in the box is most the title's colour (within 0.02 of the best: the least clutter on or under
    it), then the one with the least motion in the box (the mean change to frames f ± 1)."""
    seen = sorted(set().union(*(g.boxes for g in group)))
    count = {f: sum(f in g.boxes for g in group) for f in seen}
    top = [f for f in seen if count[f] == max(count.values())]
    top = [f for f in top if op[f] >= CF_OPACITY * max(op[top])] or top
    area = {f: float(np.prod(_size(_at(group, f)))) for f in top}
    cand = [f for f, a in area.items() if a >= CF_AREA * max(area.values())]
    cand = [cand[i] for i in sorted(set(np.linspace(0, len(cand) - 1, min(16, len(cand))).round().astype(int)))]
    boxes = {f: _box(group, f, frames.shape[1:3]) for f in cand}

    def clutter(f):
        x0, y0, x1, y1 = boxes[f]
        fg, on = _on_colour(frames[f, y0:y1, x0:x1], np.broadcast_to(plate.at(f), frames.shape[1:])[y0:y1, x0:x1])
        return 1.0 - on.sum() / max(fg.sum(), 1)

    def motion(f):
        x0, y0, x1, y1 = boxes[f]
        here = frames[f, y0:y1, x0:x1].astype(np.int16)
        d = [np.abs(frames[e, y0:y1, x0:x1] - here).mean() for e in (f - 1, f + 1) if 0 <= e < len(frames)]
        return float(np.mean(d)) if d and here.size else 0.0

    c = {f: clutter(f) for f in cand}
    return min((f for f in cand if c[f] <= min(c.values()) + CF_CLUTTER), key=lambda f: (motion(f), f))


def _still(group: list[Fragment], frames, cf: int, box) -> np.ndarray:
    """The frame at cf inside `box` (x0, y0, x1, y1), as the per-pixel median of up to 9 frames (≥ 3, spread) that
    show the same members in a union box of the same size (moved by whole pixels): a held title stays, a mover
    crossing it goes. ponytail: a title that turns or scales keeps the frame at cf, clutter and all; upgrade path:
    the median of frames warped by the tracked homography."""
    H, W = frames.shape[1:3]
    u = _at(group, cf)
    keys = {g.key for g in group if cf in g.boxes}
    picks = []
    for f in sorted(set().union(*(g.boxes for g in group))):
        v = _at(group, f, keys)
        if {g.key for g in group if f in g.boxes} != keys or np.round(_size(v)).tolist() != np.round(_size(u)).tolist():
            continue   # another member set, or a box that changed size (a title turning or scaling is not held)
        dx, dy = int(round(v[0] - u[0])), int(round(v[1] - u[1]))
        if box[0] + dx >= 0 and box[1] + dy >= 0 and box[2] + dx <= W and box[3] + dy <= H:
            picks.append((f, dx, dy))
    if len(picks) < 3:
        return frames[cf, box[1]:box[3], box[0]:box[2]]
    picks = [picks[i] for i in sorted(set(np.linspace(0, len(picks) - 1, min(9, len(picks))).round().astype(int)))]
    crops = [frames[f, box[1] + dy:box[3] + dy, box[0] + dx:box[2] + dx] for f, dx, dy in picks]
    return np.median(np.stack(crops), axis=0).round().astype(np.uint8)


def _glyph_mask(group: list[Fragment], frames, B, cf: int) -> np.ndarray:
    """The union glyph mask at cf (frame-sized): `_on_colour` on `_still`, inside the members' union box padded by
    0.1·h."""
    box = _box(group, cf, frames.shape[1:3])
    m = np.zeros(frames.shape[1:3], bool)
    if box[2] > box[0] and box[3] > box[1]:
        behind = np.ascontiguousarray(np.broadcast_to(B, frames.shape[1:])[box[1]:box[3], box[0]:box[2]])
        m[box[1]:box[3], box[0]:box[2]] = _on_colour(_still(group, frames, cf, box), behind)[1]
    return m


def _join(pieces, alpha, h: float) -> str:
    """Piece texts (text, x0, x1 in the rectified patch) in x order, a space only where the ink gap between two
    pieces is > 2× the median ink gap of the title's glyph runs (when there are ≥ 3) and > 0.3·h."""
    pieces = sorted(((t.strip(), (x0 + x1) / 2) for t, x0, x1 in pieces if t.strip()), key=lambda q: q[1])
    ink = np.flatnonzero(np.asarray(alpha).any(0))
    gaps = [(int(a) + 1, int(b)) for a, b in zip(ink, ink[1:]) if b - a > 1]   # ink-free runs [start, end)
    lens = [e - s for s, e in gaps]
    med = float(np.median(lens)) if len(lens) >= 3 else 0.0
    out = pieces[0][0] if pieces else ""
    for (_, ca), (tb, cb) in zip(pieces, pieces[1:]):
        gap = max((e - s for s, e in gaps if ca <= (s + e) / 2 <= cb), default=0)
        out += (" " if gap > SPACE_GAP * med and gap > SPACE_H * h else "") + tb
    return out


def _reconcile(read: str, joined: str, texts: list[str], strings: list[str], multi: bool = True,
               seen: list[int] | None = None) -> tuple[str, str, str | None]:
    """(text, text_source, text_unverified), D2/D8: the VLM line within ratio 0.8 and of a close length (`_vlm_line`)
    of the OCR string (the re-read, else the fragments joined) wins; else the line of a length close to the joined
    fragments that all fragment texts sit on wins, flagged as disagreeing with OCR; without such a line the OCR string
    stays, flagged as disagreeing with the reading (or as unverified without one). R24, without a VLM: the re-read
    stands only for a group of several fragments (`multi`) and only when it is no shorter than its longest fragment
    text; else the fragments' string (a single tilted track: its own consensus text). R25: that string is the first
    fragment text (`texts` come most-seen first) that holds every other (`read_cover` ≥ 0.8), else the join. V5: the
    text seen in more frames (`seen`, per text) than all the others together, which holds each other text at the
    misread bar (`read_cover` ≥ PIECE: "Arwwials" 0.75), is the string, unless the re-read is longer: the title read
    whole most of the time, its pieces and misreads split off while a sprite crossed it.
    ponytail: a misread piece below PIECE ("Am Tvals") keeps the join; upgrade path: weigh each piece by its frames."""
    if not strings:
        if not (multi and len(_compact(read)) >= max((len(_compact(t)) for t in texts), default=0)):
            read = ""
        joined = next((c for c in texts if all(read_cover(t, c) >= SUBSTRING for t in texts)), joined)
        if seen and len(texts) > 1 and 2 * seen[0] > sum(seen) and all(read_cover(t, texts[0]) >= PIECE for t in texts[1:]) \
                and len(_compact(read)) <= len(_compact(texts[0])):
            read, joined = "", texts[0]
    ocr, source = (read, "ocr") if read else (joined, "fragments")
    if strings:
        if best := _vlm_line(ocr, strings):
            return best, "vlm", None
        lines = [ln for s in strings for ln in s.splitlines()
                 if texts and _close(ln, joined) and all(partial_ratio(t, ln) >= SUBSTRING for t in texts)]
        if lines:
            return max(lines, key=lambda s: _ratio(s, joined)), "vlm", "vlm_ocr_disagree"
        return ocr, source, "vlm_ocr_disagree"
    return ocr, source, "no_vlm"


def _affine(H, wh) -> np.ndarray:
    """The affine map nearest H (least squares) on the corners of the box `wh`, as a 3×3."""
    w, h = wh
    src = np.array([[0.0, 0, 1], [w, 0, 1], [w, h, 1], [0, h, 1]])
    q = src @ np.asarray(H, float).T
    A = np.linalg.lstsq(src, q[:, :2] / q[:, 2:], rcond=None)[0].T
    return np.vstack([A, [0.0, 0.0, 1.0]])


def _hold_on_boxes(raw, group: list[Fragment], held, cf: int) -> None:
    """Frames ECC held: x, y, sx and sy follow the union box of the members visible there and at cf."""
    for f in held:
        keys = {g.key for g in group if f in g.boxes and cf in g.boxes}
        a, b = _at(group, f, keys), _at(group, cf, keys)
        if a is None:
            continue
        raw[f, 0] = raw[cf, 0] + (a[0] + a[2] - b[0] - b[2]) / 2
        raw[f, 1] = raw[cf, 1] + (a[1] + a[3] - b[1] - b[3]) / 2
        raw[f, 2] = raw[cf, 2] * (a[2] - a[0]) / max(b[2] - b[0], 1e-6)
        raw[f, 3] = raw[cf, 3] * (a[3] - a[1]) / max(b[3] - b[1], 1e-6)


def merge_title(group, props, frames, plate, *, ocr, strings, size, fps) -> dict | None:
    """One text prop over a title's fragments (or a tilted single text track): the plane of the union glyph mask at
    the settled frame (`text_quad`; a rectangle on its baseline when it is upright), tracked by ECC homography
    (`track_plane`; its affine part only when upright) into an n×11 raw (`plane_raw`; held frames placed by the union
    box), the rectified union at cf as canon (RGBA, binary α), and the string re-read from the rectified patch by
    `ocr` (None: no re-read) and reconciled with the VLM `strings`. None when the title's plane cannot be measured."""
    plate = as_plate(plate)
    n = len(frames)
    seen = sorted(set().union(*(g.boxes for g in group)))
    op = _opacity(group, props, n)
    cf = _settled(group, frames, plate, op)
    B = np.broadcast_to(plate.at(cf), frames[cf].shape)
    mask = _glyph_mask(group, frames, B, cf)
    found = text_quad(mask)
    if found is None:
        return None
    quad, info = found
    tilted = _tilted(quad, info) and _perspective(info, size)
    # ponytail: an upright title stays flat (rx = ry = 0) even if it tilts after cf; upgrade path: a tilt test per frame.
    if not tilted:   # upright: a rectangle on the baseline (the cap line's slope is glyph noise there)
        tl, tr, br, bl = quad
        h = (np.linalg.norm(tl - bl) + np.linalg.norm(tr - br)) / 2
        quad = np.array([bl + (tl - bl) * h / np.linalg.norm(tl - bl), br + (tr - br) * h / np.linalg.norm(tr - br), br, bl])
    tpl, H0 = rectify(contrast(frames[cf], B) * mask, quad)   # the glyphs only: clutter at cf is not the title
    wh = tpl.shape[1::-1]
    colour = np.median(frames[cf][mask].astype(np.float32), axis=0)
    Hs, held = track_plane(frames, plate, tpl, H0, cf, (seen[0], seen[-1]), prep=lambda c, b: _title_contrast(c, b, colour))
    if not tilted:   # sub-pixel ECC noise reads as degrees of rx/ry: fit the affine part only
        Hs = {f: _affine(H, wh) for f, H in Hs.items()}
    raw = plane_raw({f: Hs[f] for f in seen}, wh, size, n, op)
    if not tilted:
        raw[np.ix_(seen, [PROPS.index("rx"), PROPS.index("ry")])] = 0.0
    _hold_on_boxes(raw, group, held & set(seen), cf)
    rgb, _ = rectify(frames[cf], quad, wh)
    alpha = rectify(mask.astype(np.uint8) * 255, quad, wh)[0] > 127
    core = _glyph_core_mask(rgb, alpha, None, rectify(np.ascontiguousarray(B), quad, wh)[0])
    color = rgb8_to_hex(np.median(rgb[core if core.any() else alpha] if alpha.any() else rgb.reshape(-1, 3), axis=0))
    cap = wh[1] / (1 + 2 * PAD[1])
    read = ""
    if ocr is not None:   # the padded rectified patch, read again
        p = int(round(0.5 * cap))
        try:
            got = ocr(cv2.copyMakeBorder(rgb, p, p, p, p, cv2.BORDER_REPLICATE))
            read = _join([(t, b[0] - p, b[2] - p) for t, b, _ in got or []], alpha, cap)
        except Exception as e:   # fail soft: the fragments' string
            log.warning("title re-read failed: %s", describe(e, trace=True))
    Hi = np.linalg.inv(H0)
    pieces = []
    for g in group:   # the fragments' texts at cf, in the rectified patch
        if g.text and cf in g.boxes:
            x0, y0, x1, y1 = g.boxes[cf]
            q = np.array([[x0, y0, 1], [x1, y0, 1], [x1, y1, 1], [x0, y1, 1]], float) @ Hi.T
            pieces.append((g.text, float((q[:, 0] / q[:, 2]).min()), float((q[:, 0] / q[:, 2]).max())))
    texted = sorted((g for g in group if g.text), key=lambda g: -len(g.boxes))   # most-seen first (R25)
    texts = [g.text for g in texted]
    text, source, unverified = _reconcile(read, _join(pieces, alpha, cap) or " ".join(texts), texts, strings, len(group) > 1,
                                          [len(g.boxes) for g in texted])
    boxes = np.full((n, 4), np.nan)
    for f in seen:
        boxes[f] = np.round(_at(group, f))
    font = next((props[g.key]["font"] for g in group if props[g.key].get("font") is not None), None)
    return {"raw": raw, "canon": np.dstack([rgb, alpha.astype(np.uint8) * 255]), "cf": cf, "kind": "text",
            "first": seen[0], "last": seen[-1], "text": text, "font": font, "color": color, "core_color": color,
            "fragments": {g.key: props[g.key] for g in group}, "boxes": boxes, "plane": tilted,
            "plane_fit_px": fit_props(H0, wh, size)[1], "text_source": source, "text_unverified": unverified}


# --- per-glyph reveal (D15) ---------------------------------------------------------------------------------------

GLYPHS_FAILED = "glyphs_failed"   # fail-soft code (R52): a title left in box mode
GLYPH_SPREAD, GLYPH_ON, GLYPH_RAMP, GLYPH_S = 3, 0.5, (0.1, 0.9), 3.0   # onset spread (frames), onset and ramp levels, window (s)
GLYPH_IN, GLYPH_FIT, BOX_FIT = 0.98, 0.05, 0.02   # in-frame share; the glyph model's largest mean |Δ|; box-fit slack (R18)
GROW_W, GROW_X = 0.8, 0.25   # a growing box (R16): its first width over cf's, its left edge's drift in line heights


def _crossing(a, level: float) -> float:
    """The first time (in frames, linear between frames) the series `a` reaches `level`; inf when it never does."""
    hit = np.flatnonzero(a >= level)
    if not len(hit):
        return math.inf
    i = int(hit[0])
    return float(i) if i == 0 else i - 1 + float((level - a[i - 1]) / (a[i] - a[i - 1]))


def glyph_reveal(p: dict, frames, plate, size, text: str, *, fps: float = 30.0) -> dict | None:
    """The per-glyph reveal of a text prop (a GlyphReveal dump), or None (box mode). Over the prop's first 3 s, glyph
    i's opacity at f is the rectified (I_f − B_f) projected onto the reference over the ink of its column: its onset
    is the first frame (interpolated) at ≥ 0.5, the ramp the median 10→90 % time over 0.8 (a linear ramp's
    0→100 %), its delay the onset less half the ramp, after the prop's first frame. The ink is the canon α in the
    title's colour at cf (`_on_colour`: a sprite under it at cf is not the title); the reference is (I_cf − B_cf),
    as the per-pixel median of ≤ 9 frames at ≥ 90 % of cf's opacity from cf to the window's end (a glyph still
    coming in at cf, a sprite crossing it, go). The glyph centres are `glyph_centres` on that ink, spread uniformly
    over its extent when the glyphs cannot be told apart. Box mode when the onsets spread < max(GLYPH_SPREAD,
    ramp/2) frames (R19), a glyph never comes in, a glyph's column is < 98 % inside the frame before its onset (it
    slid in, it was not revealed), the glyph model misses the curves over the stagger by a mean |Δ| > GLYPH_FIT, or,
    for a `reveal` prop, its box reveal (opacity × each column's revealed share) fits them within BOX_FIT of the
    glyph model (R18: box mode stays where it is right).
    ponytail: opacity stagger only; per-glyph slides are not reproduced (upgrade: a per-glyph offset track); delays
    are ≥ 0, so a glyph already in on the first frame starts there at α 0 (upgrade: delays down to −ramp)."""
    if "\n" in text.strip():   # ponytail: one line, columns cannot cut several; upgrade path: per-glyph boxes
        return None
    plate = as_plate(plate)
    canon, raw, cf, first = p["canon"], fill_gaps(p["raw"]), int(p["cf"]), int(p["first"])
    end = min(int(p["last"]), first + round(GLYPH_S * fps))
    h, w = canon.shape[:2]
    L = np.array([[1.0, 0.0, -w / 2], [0.0, 1.0, -h / 2], [0.0, 0.0, 1.0]])
    frame_ones = np.ones(frames.shape[1:3], np.float32)

    def rectified(f):   # (I_f, B_f, the share inside the frame) in canonical space
        q = {**DEFAULTS, **{k: float(v) for k, v in zip(PROPS, raw[f]) if np.isfinite(v)}, "sky": 0.0}
        M = projective_matrix(q, size) @ L
        return [cv2.warpPerspective(np.ascontiguousarray(img), M, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
                for img in (frames[f], np.broadcast_to(plate.at(f), frames[f].shape), frame_ones)]

    ink = (canon[..., 3] > 127) & _on_colour(canon[..., :3], rectified(cf)[1])[1]
    cols, n = np.flatnonzero(ink.any(0)), len("".join(text.split()))
    if n < 2 or not len(cols):
        return None
    x = glyph_centres(ink.astype(np.float32), text)
    if x is None:
        x = cols[0] + (np.arange(n) + 0.5) * (cols[-1] + 1 - cols[0]) / n
    x = np.asarray(x, float) / w
    if np.any(np.diff(x) <= 0):
        return None
    edges = glyph_edges(GlyphReveal(x=x.tolist(), delays=[0.0] * n), w)
    op, rv = PROPS.index("opacity"), PROPS.index("reveal")
    full = [f for f in range(cf, max(cf, end) + 1) if f == cf or raw[f, op] >= CF_OPACITY * raw[cf, op]]   # not an exit
    picks = [full[i] for i in sorted({*np.linspace(0, len(full) - 1, 9).round().astype(int)})]
    ref = np.median([np.subtract(*rectified(f)[:2], dtype=np.float32) for f in picks], axis=0) * ink[..., None]

    def columns(d):   # Σ d·ref over each glyph's column
        c = np.concatenate([[0.0], np.cumsum((d * ref).sum((0, 2)))])
        return c[edges[1:]] - c[edges[:-1]]

    den = np.maximum(columns(ref), 1e-6)

    def measure(f):   # (each glyph's opacity, the share of its ink inside the frame)
        I, B, m = rectified(f)
        return columns(np.subtract(I, B, dtype=np.float32)) / den, columns(m[..., None] * ref) / den

    a, inside = (np.array(v) for v in zip(*[measure(f) for f in range(first, end + 1)]))
    on = np.array([_crossing(a[:, i], GLYPH_ON) for i in range(n)])
    rise = np.array([_crossing(a[:, i], GLYPH_RAMP[1]) - _crossing(a[:, i], GLYPH_RAMP[0]) for i in range(n)])
    rise = rise[np.isfinite(rise)]
    ramp = float(np.clip(np.median(rise) / (GLYPH_RAMP[1] - GLYPH_RAMP[0]) if len(rise) else 1.0, 1.0, 120.0))
    if not np.isfinite(on).all() or on.max() - on.min() < max(GLYPH_SPREAD, ramp / 2):
        return None
    if any(inside[:math.ceil(t) + 1, i].min() < GLYPH_IN for i, t in enumerate(on)):
        return None
    delays = np.maximum(on - ramp / 2, 0.0)
    k = min(len(a), math.ceil(delays.max() + ramp) + 1)   # the stagger window
    fit = float(np.abs(np.clip((np.arange(k)[:, None] - delays) / ramp, 0.0, 1.0) - a[:k]).mean())
    if fit > GLYPH_FIT:   # the glyph model does not explain the curves (a title sliding as it fades, a bad merge)
        return None
    if p.get("reveal"):
        e, rows = np.asarray(edges, float), raw[first:first + k]
        box = rows[:, op, None] * np.clip((rows[:, rv, None] * w - e[:-1]) / np.maximum(np.diff(e), 1.0), 0.0, 1.0)
        if np.abs(box - a[:k]).mean() <= fit + BOX_FIT:
            return None
    return GlyphReveal(x=x.tolist(), delays=delays.round(2).tolist(), ramp=round(ramp, 2)).model_dump()


def _grows(p: dict) -> bool:
    """R16: an unmerged text whose OCR box widens from a steady left edge over its first frames: < 0.8× its width at
    cf on its first frame, the left edge there within 0.25 line heights of cf's (a reveal read as it grew)."""
    raw, (ch, cw), f, cf = p["raw"], p["canon"].shape[:2], int(p["first"]), int(p["cf"])
    left = raw[:, 0] - raw[:, 2] * cw / 2
    return bool(raw[f, 2] < GROW_W * raw[cf, 2] and abs(left[f] - left[cf]) <= GROW_X * raw[cf, 3] * ch)


def _glyph_mode(p: dict, frames, plate, size, fps, hold: bool = False) -> bool:
    """`glyph_reveal` into p["glyphs"]; over the stagger window the glyph α carries the reveal (R14): opacity keeps
    the title's own (at cf) and reveal is 1; `hold` (a growing box, R16) also holds x, y, sx and sy at cf, where the
    glyphs are measured too. ponytail: a title that also fades as a whole while its glyphs come in holds its cf
    opacity there; upgrade path: divide the measured opacity by the mean glyph α."""
    raw, first, cf = p["raw"], int(p["first"]), int(p["cf"])
    held = raw.copy()
    if hold:
        held[first:int(p["last"]) + 1, :4] = raw[cf, :4]
    g = glyph_reveal({**p, "raw": held}, frames, plate, size, p.get("text") or "", fps=fps)
    if g is None:
        return False
    window = slice(first, min(int(p["last"]), first + math.ceil(max(g["delays"]) + g["ramp"])) + 1)
    put = [(PROPS.index("opacity"), raw[cf, PROPS.index("opacity")]), (PROPS.index("reveal"), 1.0)]
    for j, v in put + ([(j, raw[cf, j]) for j in range(4)] if hold else []):
        if raw.shape[1] > j:
            col = raw[window, j]
            col[np.isfinite(col)] = v
    p["glyphs"] = g
    return True


def _tilted_track(f: Fragment, props: dict, frames, plate, notes: list[str]) -> bool:
    """D12 on a single text track at its settled frame: the quad of its glyph mask turns ≥ 2° or converges ≥ 0.8°.
    A failure leaves the track on the text_props path and `notes` one message per phase."""
    try:
        cf = _settled([f], frames, plate, _opacity([f], props, len(frames)))
        found = text_quad(_glyph_mask([f], frames, plate.at(cf), cf))
        return found is not None and _tilted(*found)
    except Exception as e:   # fail soft: the track stays on the text_props path
        log.warning("tilt test failed for %s: %s", f.key, describe(e, trace=True))
        if (note := f"title tilt test failed ({MERGE_FAILED})") not in notes:
            notes.append(note)
        return False


def merge_titles(props, frames, plate, *, ocr, reading, fps, notes, members=()) -> list[str]:
    """In place: each title's fragments (`group_fragments`, a group with a text track) and each tilted single text
    track (D12) become one text prop `title_<first text key>` (`merge_title`), the fragments under its `fragments`;
    a VLM line within ratio 0.8 and of a close length (`_vlm_line`) corrects an unmerged text's OCR typos; a merged
    title, a revealed text or a text whose box grows (R16) gets `glyphs` when its glyph onsets spread (`_glyph_mode`;
    a failure leaves it in box mode and adds one note). A title that cannot be merged keeps its fragments, each marked
    `merge_note` = MERGE_FAILED; a failure of the whole phase keeps every fragment and adds a note. `members`: the
    solid members' keys (never fragments). Returns the new keys."""
    t0 = time.perf_counter()
    done: list[str] = []
    try:
        plate = as_plate(plate)
        size = (frames.shape[2], frames.shape[1])
        strings = [r.string for r in reading.strings] if reading is not None else []
        frags = [g for k, p in props.items() if not k.startswith("_") and isinstance(p, dict) and (g := fragment(k, p, size, members))]
        groups = [g for g in group_fragments(frags, strings, fps) if len(g) > 1 and any(props[f.key]["kind"] == "text" for f in g)]
        grouped = {f.key for g in groups for f in g}
        groups += [[f] for f in frags if f.key not in grouped and props[f.key]["kind"] == "text" and _tilted_track(f, props, frames, plate, notes)]
        for group in groups:
            keys = [f.key for f in group]
            try:
                p = merge_title(group, props, frames, plate, ocr=ocr, strings=strings, size=size, fps=fps)
            except Exception as e:   # fail soft: the fragments stay, less sure
                log.warning("title merge failed for %s: %s", keys, describe(e, trace=True))
                p = None
            if p is None:
                for k in keys:
                    props[k]["merge_note"] = MERGE_FAILED
                continue
            key = "title_" + next(k for k in keys if props[k]["kind"] == "text")
            for k in keys:
                del props[k]
            props[key] = p
            done.append(key)
        for k, p in props.items():   # a matching VLM line corrects OCR typos in a title left as it was
            if strings and k not in done and isinstance(p, dict) and p.get("kind") == "text" and p.get("text"):
                best = _vlm_line(p["text"], strings)
                if best and best != p["text"]:
                    p["text"], p["text_source"] = best, "vlm"
        t1, glyphs, tried = time.perf_counter(), 0, 0
        for k, p in props.items():   # D15: merged titles, revealed texts and texts whose box grows (R16)
            text = k not in done and not k.startswith("_") and isinstance(p, dict) and p.get("kind") == "text"
            try:
                grows = text and not p.get("reveal") and _grows(p)
                if k in done or grows or (text and p.get("reveal")):
                    tried += 1
                    glyphs += _glyph_mode(p, frames, plate, size, fps, hold=grows)
            except Exception as e:   # fail soft: box mode
                log.warning("glyph reveal failed for %s: %s", k, describe(e, trace=True))
                if (note := f"glyph reveal failed ({GLYPHS_FAILED})") not in notes:
                    notes.append(note)
        log.info("titles glyphs %.2fs glyph_mode=%s/%s", time.perf_counter() - t1, glyphs, tried)
    except Exception as e:   # fail soft: every fragment stays
        log.warning("titles merge failed: %s", describe(e, trace=True))
        notes.append(f"title merge failed ({MERGE_FAILED})")
    log.info("titles merge %.2fs merged=%s", time.perf_counter() - t0, len(done))
    return done


# --- the guard (D16) ----------------------------------------------------------------------------------------------

TITLE_GUARD = 0.005   # choose_solid's slack: a merged title stays when it renders within this of its fragments
GUARD_FAILED, MERGE_WORSE = "guard_failed", "merge_worse"   # fail-soft code (R52); a merge the guard undid


def guard_titles(props, frames, plate_or_bg, notes) -> None:
    """D16, in place and before matting (binary textures on both sides): a merged title whose still renders worse than
    its fragments by more than TITLE_GUARD (`solid_errors`: local RGB L1 in the union box, every 5th visible frame,
    over the plate at its cf) goes back to its fragments under their own keys, each marked `merge_note` = MERGE_WORSE
    and `text_group` = the title's key (`text_groups`). A failure keeps that merge and adds one note.
    ponytail: restored fragments miss the VLM typo fix and glyph mode (both ran on the merged title); upgrade path:
    run them again on what the guard restores."""
    from .solid_assets import solid_errors
    t0, undone = time.perf_counter(), 0
    plate = as_plate(plate_or_bg)
    for key in [k for k, p in props.items() if k.startswith("title_") and isinstance(p, dict)]:
        p = props[key]
        try:
            ok = np.isfinite(p["boxes"]).all(1)
            boxes = np.zeros(p["boxes"].shape, int)   # NaN outside the visible frames, whole pixels inside
            boxes[ok] = np.rint(p["boxes"][ok])
            err = solid_errors(frames, plate.at(int(p["cf"])), {**p, "boxes": boxes}, None)
            log.info("titles guard %s still=%.4f fragments=%.4f", key, err["still"], err["fragments"])
            if err["still"] > err["fragments"] + TITLE_GUARD:
                restored = {k: {**f, "merge_note": MERGE_WORSE, "text_group": key} for k, f in p["fragments"].items()}
                del props[key]
                props.update(restored)
                undone += 1
        except Exception as e:   # fail soft: the merge stays
            log.warning("title guard failed for %s: %s", key, describe(e, trace=True))
            if (note := f"title guard failed ({GUARD_FAILED})") not in notes:
                notes.append(note)
    log.info("titles guard %.2fs undone=%s", time.perf_counter() - t0, undone)


def text_groups(props, ids) -> list[Group]:
    """One `text_fragments` group per title the guard undid: its fragments' element ids, in the title's order."""
    groups: dict[str, list[str]] = {}
    for k, p in props.items():
        if isinstance(p, dict) and p.get("text_group") and k in ids:
            groups.setdefault(p["text_group"], []).append(ids[k])
    return [Group(id=f"text{i}", members=m, reason="text_fragments")
            for i, m in enumerate((m for m in groups.values() if len(m) > 1), 1)]
