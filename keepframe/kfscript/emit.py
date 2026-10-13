"""kf-script: the AI-facing view of a scene (docs/superpowers/specs/2026-10-13-kf-script-design.md).

A kf-script page is `compose()` with the timeline written out: one `tl.fromTo` per merged keyframe segment on the
per-element state objects `S[id]` that the composer runtime applies (so rx/ry keep Keepframe's transform order)."""
from __future__ import annotations
import html
import json
from pathlib import Path

import numpy as np

from ..compose.composer import _script_json, compose
from ..fonts.registry import FontRegistry
from ..ir.colour import delta_e, hex_to_rgb8, srgb8_to_lab
from ..ir.schema import Element, Scene
from ..ir.tracks import PRESET_EASES

_PRESET_NAMES = {tuple(v): k for k, v in PRESET_EASES.items() if k != "linear"}
_LINEAR = tuple(PRESET_EASES["linear"])   # the bezier y = x: written as GSAP's "none"


def _num(v: float) -> str:
    """Shortest exact float text (json/repr), ints without a dot."""
    return str(int(v)) if float(v).is_integer() else json.dumps(float(v))


def _sec(t: int, fps: float) -> str:
    return f"{t / fps:.4f}"


def _ease_names(scene: Scene) -> dict[tuple, str]:
    names: dict[tuple, str] = {}
    n = 0
    for el in scene.elements:
        for track in el.tracks.values():
            for k in track.keys[:-1]:
                if k.ease is None or tuple(k.ease) == _LINEAR or tuple(k.ease) in names:
                    continue
                if tuple(k.ease) in _PRESET_NAMES:
                    names[tuple(k.ease)] = _PRESET_NAMES[tuple(k.ease)]
                else:
                    n += 1
                    names[tuple(k.ease)] = f"ease{n}"
    return names


def _obj(props: dict[str, float]) -> str:
    return "{" + ", ".join(f"{p}: {_num(v)}" for p, v in props.items()) + "}"


def _element_js(el: Element, fps: float, names: dict[tuple, str]) -> list[str]:
    target = f"S[{_script_json(el.id)}]"
    lines = [f"gsap.set({target}, {_obj({p: t.keys[0].v for p, t in el.tracks.items()})});"] if el.tracks else []
    spans: dict[tuple, tuple[dict, dict]] = {}
    for prop, track in el.tracks.items():
        for a, b in zip(track.keys, track.keys[1:]):
            frm, to = spans.setdefault((a.t, b.t, tuple(a.ease) if a.ease else None), ({}, {}))
            frm[prop], to[prop] = a.v, b.v
    for (t0, t1, ease), (frm, to) in sorted(spans.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        name = names[ease] if ease and ease != _LINEAR else "none"
        dur = float(_sec(t1, fps)) - float(_sec(t0, fps))   # both ends on the 4-dp grid, not the duration alone
        lines.append(f'tl.fromTo({target}, {_obj(frm)}, {_obj(to)[:-1]}, duration: {dur:.4f}, '
                     f'ease: "{name}"}}, {_sec(t0, fps)});   // f{t0}–f{t1}')
    return lines


def timeline_js(scene: Scene) -> str:
    """The explicit timeline: named eases, then per element its start values and merged segments."""
    names = _ease_names(scene)
    out = ["tl.vars.defaults = {immediateRender: false};   // every tween is a fromTo with explicit ends"]
    for ease, name in names.items():
        out.append(f'CustomEase.create("{name}", "M0,0 C{_num(ease[0])},{_num(ease[1])} {_num(ease[2])},{_num(ease[3])} 1,1");')
    for el in scene.elements:
        lines = _element_js(el, scene.fps, names)
        if lines:
            out.append(f"// {_script_json(el.id)[1:-1]} · {el.kind} · visible f{el.visible[0]}–f{el.visible[1]}")
            out.extend(lines)
    return "\n".join(out)


def _palette(scene: Scene) -> str:
    bg = scene.background
    entries: list[tuple[str, list[str]]] = []
    if bg.kind == "color":
        entries.append((bg.value, ["bg"]))
    elif bg.kind == "gradient" and bg.gradient:
        entries.extend((s.color, ["bg"]) for s in bg.gradient.stops)
    for el in scene.elements:
        c = el.canonical.color
        if c and len(c) == 7 and c.startswith("#"):
            entries.append((c, [el.id]))
    merged: list[tuple[str, list[str], np.ndarray]] = []
    for colour, ids in entries:
        lab = srgb8_to_lab(np.array(hex_to_rgb8(colour), float))
        for i, (c, have, ref) in enumerate(merged):
            if float(delta_e(lab, ref)) < 3:
                have.extend(x for x in ids if x not in have)
                break
        else:
            merged.append((colour, list(ids), lab))
    return " · ".join(f"{c} {' '.join(ids[:6])}{' …' if len(ids) > 6 else ''}" for c, ids, _ in merged) or "—"


def _type(scene: Scene) -> str:
    seen: dict[tuple, list[str]] = {}
    for el in scene.elements:
        f = el.canonical.font
        if el.kind == "text" and f is not None:
            seen.setdefault((f.family_guess, f.weight, round(f.size_px)), []).append(el.id)
    return " · ".join(f"{fam} {w} {s}px {' '.join(ids[:6])}{' …' if len(ids) > 6 else ''}"
                      for (fam, w, s), ids in seen.items()) or "—"


def header(scene: Scene) -> str:
    """The `<!-- kf-script v0 … -->` comment (spec §Format 1). Shots and intent come in P3/P4."""
    dur = scene.frames / scene.fps
    body = "\n".join([
        f"kf-script v0 · {scene.id}",
        f"size {scene.size[0]}x{scene.size[1]} · fps {scene.fps:g} · frames {scene.frames} ({dur:.2f} s) · "
        "time in seconds (frame = s × fps)",
        f"shots: sh1 0.00–{dur:.2f}",
        f"style: palette {_palette(scene)} | type {_type(scene)}",
        "intent: (unread)",
        "conventions: each .el is placed at its anchor; S[id] holds its state: x, y anchor px; sx, sy scale; "
        "rot, skx deg; rx, ry tilt deg; opacity; reveal = visible fraction left→right. "
        "Glyph reveals and visibility follow data-glyph-edges and the element's visible range.",
    ])
    # inside an HTML comment: no "--", no ">" and no template placeholders
    body = body.replace("--", "—").replace(">", "›").replace("{{", "{ {")
    return f"<!-- {body} -->\n"


def attrs(el: Element) -> str:
    """kf-script attributes on .el: role, provenance, confidence, caption (escaped observed data)."""
    out = f' data-role="{html.escape(el.label or el.role)}" data-src="{el.provenance}" data-conf="{el.confidence:.2f}"'
    if el.caption:
        out += f' data-caption="{html.escape(el.caption)}"'
    return out


def emit_page(scene: Scene, scene_dir: Path, out: Path, *, fonts: FontRegistry | None = None) -> Path:
    """An executable kf-script page: renders like `compose()` and reads as the scene's description."""
    return compose(scene, scene_dir, out, fonts=fonts, timeline=timeline_js(scene), header=header(scene), attrs=attrs)
