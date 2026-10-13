"""kf-script: the AI-facing view of a scene (docs/superpowers/specs/2026-10-13-kf-script-design.md).

A kf-script page is `compose()` with the timeline written out: one `tl.fromTo` per merged keyframe segment on the
per-element state objects `S[id]` that the composer runtime applies (so rx/ry keep Keepframe's transform order)."""
from __future__ import annotations
import html
import json
import re
from pathlib import Path

import numpy as np

from ..compose.composer import _element_html, _script_json, compose
from ..fonts.registry import FontRegistry
from ..ir.colour import delta_e, hex_to_rgb8, srgb8_to_lab
from ..ir.gradient import gradient_at, gradient_css
from ..ir.importance import rank_elements
from ..ir.schema import Element, Scene
from ..ir.tracks import PRESET_EASES, eval_track
from ..session.brief import group_lines

_PRESET_NAMES = {tuple(v): k for k, v in PRESET_EASES.items() if k != "linear"}
VIEW_CHARS = 48_000   # the agent view budget (≈ 12k tokens; P1 Task 5, sizes in docs/qa/kfscript/p1.md)
_URI = re.compile(r'(src|poster|data-model)="data:[^"]*"')
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
        "data-track-index is the stacking order (higher is in front). "
        "Each element's summary comment gives its size, entrance position and visible range; glyph-by-glyph typing is "
        "marked there (reveal is a box wipe, not typing).",
        *group_lines(scene),
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


def _markup(el: Element, scene_dir: Path, fps: float, ui, fonts) -> str:
    """The element's page markup with data URIs replaced by its asset paths (the view carries no pixels)."""
    c = el.canonical
    names = {"src": c.video or c.texture, "poster": c.texture, "data-model": c.model}
    return _URI.sub(lambda m: f'{m.group(1)}="{names.get(m.group(1)) or "asset"}"',
                    _element_html(el, scene_dir, fps, ui, fonts, attrs(el)))


_ROUND = {"x": 1, "y": 1, "rot": 1, "skx": 1, "rx": 1, "ry": 1}   # display precision; scale, opacity, reveal: 3


def _r(prop: str, v: float) -> str:
    return _num(round(float(v), _ROUND.get(prop, 3)))


def _element_view_js(el: Element, fps: float, names: dict[tuple, str]) -> str:
    """One line per property: the start value, then GSAP keyframes (display-rounded; the page has the exact tweens)."""
    if not el.tracks:
        return ""
    target = f"S[{_script_json(el.id)}]"
    lines = [f"gsap.set({target}, {{{', '.join(f'{p}: {_r(p, t.keys[0].v)}' for p, t in el.tracks.items())}}});"]
    for prop, track in el.tracks.items():
        # holds before the first change and after the last are no steps (values clamp outside the keys): read as motion
        moving = [i for i, (a, b) in enumerate(zip(track.keys, track.keys[1:])) if a.v != b.v]
        if not moving:
            continue
        keys = track.keys[moving[0]:moving[-1] + 2]
        steps = [f'{{{prop}: {_r(prop, b.v)}, duration: {(b.t - a.t) / fps:.3f}, ease: "{names[tuple(a.ease)] if a.ease and tuple(a.ease) != _LINEAR else "none"}"}}'
                 for a, b in zip(keys, keys[1:])]
        body = steps[0][1:-1] if len(steps) == 1 else f"keyframes: [{', '.join(steps)}]"
        lines.append(f"tl.to({target}, {{{body}}}, {keys[0].t / fps:.3f});   // {prop} f{keys[0].t}–f{keys[-1].t}")
    return "\n".join(lines)


def _stage(scene: Scene) -> str:
    bg = scene.background
    if bg.kind == "gradient":
        css = gradient_css(gradient_at(bg, 0), *scene.size) + (f"   /* animates: {len(bg.gradient_keys)} keys */" if bg.gradient_keys else "")
    elif bg.kind in ("image", "video"):
        css = f'url("{bg.poster or bg.value}")' + (f"   /* video {bg.value} */" if bg.kind == "video" else "")
    else:
        css = bg.value
    return f'<div id="stage" style="width:{scene.size[0]}px;height:{scene.size[1]}px;background:{css}">\n'


def _stub(el: Element, fps: float, shown: bool = False) -> str:
    """The element's summary comment, like a brief row: kind/role, text, size, entrance position, visible range and
    glyph-by-glyph typing. Before every shown element's markup (its position is otherwise only in the script), and in
    place of an element whose full block does not fit the budget (never dropped)."""
    c, f = el.canonical, el.visible[0]
    pos = [eval_track(el.tracks[p], f) if p in el.tracks else 0.0 for p in ("x", "y")]
    text = f' "{html.escape(" ".join(c.text.split())[:60])}"' if c.text else ""
    body = (f"{_script_json(el.id)[1:-1]} {el.kind}/{html.escape(el.label or el.role)}{text} {c.width:.0f}x{c.height:.0f}px "
            f"at ({pos[0]:.0f}, {pos[1]:.0f}) visible {el.visible[0] / fps:.2f}–{(el.visible[1] + 1) / fps:.2f} s "
            f"(f{el.visible[0]}–f{el.visible[1]})")
    if c.glyphs:
        body += f" · types glyph by glyph {(f + min(c.glyphs.delays)) / fps:.2f}–{(f + max(c.glyphs.delays) + c.glyphs.ramp) / fps:.2f} s"
    return f"<!-- {(body + ('' if shown else ' (motion not shown)')).replace('--', '—')} -->"


def view(scene: Scene, scene_dir: Path, *, max_chars: int = VIEW_CHARS, fonts: FontRegistry | None = None) -> str:
    """The agent's kf-script: header, stage, element markup and timeline without vendor code, fonts or pixels.
    Elements are kept in `rank_elements` order while they fit `max_chars`; the rest are listed as omitted."""
    names = _ease_names(scene)
    eases = [f'CustomEase.create("{n}", "M0,0 C{_num(e[0])},{_num(e[1])} {_num(e[2])},{_num(e[3])} 1,1");' for e, n in names.items()]
    blocks = {el.id: (_stub(el, scene.fps, shown=True) + "\n" + _markup(el, Path(scene_dir), scene.fps, scene.ui, fonts),
                      _element_view_js(el, scene.fps, names)) for el in scene.elements}
    stubs = {el.id: _stub(el, scene.fps) for el in scene.elements}
    fixed = header(scene) + _stage(scene) + "</div>\n<script>\n" + "\n".join(eases) + "\n</script>\n"
    used, kept = len(fixed) + sum(len(x) + 1 for x in stubs.values()), set()
    for el in rank_elements(scene):   # a full block replaces the element's stub while the budget allows
        size = sum(map(len, blocks[el.id])) + 2 - len(stubs[el.id]) - 1
        if kept and used + size > max_chars:
            break
        kept.add(el.id)
        used += size
    markup = [blocks[el.id][0] if el.id in kept else stubs[el.id] for el in scene.elements]
    out = (header(scene) + _stage(scene) + "\n".join(markup) + "\n</div>\n<script>\n"
           + "\n".join(eases + [blocks[el.id][1] for el in scene.elements if el.id in kept and blocks[el.id][1]]) + "\n</script>\n")
    if len(kept) < len(scene.elements):
        out += f"<!-- {len(scene.elements) - len(kept)} elements shown as one-line summaries (motion not shown) to fit the budget -->\n"
    return out
