"""`keepframe eval-edits`: analyse references, edit a temp copy (title text, hide an element, replace the
background) and measure what pixel L1 cannot see. Synthetic scenes carry exact ground truth (double render);
real clips carry gold titles (docs/qa/reference-analysis/gold)."""
from __future__ import annotations
import json, shutil, tempfile, time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Literal, Sequence
import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment
from ..analyze.pipeline import AnalyzeOptions, analyze
from ..edit.agent import edit
from ..edit.intent import Intent, Target
from ..ir.colour import delta_e, hex_to_rgb8, srgb_to_lab
from ..ir.schema import Background, Scene
from ..ir.store import current_scene, load_project, save_scene, scene_dir
from ..ir.synth import ground_truth, layers, make_reference_scene, render_frames, render_reference, true_plate
from ..log import get
from .metrics import (_grow, alpha_errors, bg_leak_fraction, foreground_errors, glyph_colour_delta, halo_ring,
                      leak_correlation, outside_glyph_delta, plate_residue, smear_score, title_integrity)
from .sheets import comparison_sheet

log = get("keepframe.qa")
Renderer = Literal["numpy", "browser"]
TITLE_TEXT = "Fall Drop Sale"
BACKGROUND_EDITS: list[tuple[str, str]] = [("replace", "#1a2a6c")]   # Task 12 adds ("tint", …) with the background choice
SHEET_LABELS = ("source", "rebuilt", "title edited", "element hidden", "background replaced")
PLATES = ("gradient", "flat", "animated", "image")
# name: (sample key, op, threshold). Every sample must pass, except "rate" gates (share of hits).
SYNTHETIC_GATES = {
    "title_outside_glyph_delta": ("title.outside_glyph_delta", "le", 1.0),
    "title_smear_score": ("title.smear_score", "le", 3.0),
    "title_glyph_de": ("title.glyph_de", "lt", 5.0),
    "hide_plate_de": ("hide.mean_de", "le", 2.0),
    "hide_hf_ratio": ("hide.hf_ratio", "in", (0.5, 2.0)),
    "hide_residue": ("hide.residue_fraction", "le", 0.02),
    "recolour_halo_ring": ("background.halo_ring", "lt", 2.0),
    "alpha_sad_edge": ("alpha.sad", "le", 0.03),
    "f_de_interior": ("alpha.f_de_interior", "lt", 3.0),
    "f_de_edge": ("alpha.f_de_edge", "lt", 6.0),
    "font_top3": ("font_hits", "rate", 0.90),
}


def make_ocr(truth: Scene | None):
    """OCR engine for one analysis; None lets the pipeline load RapidOCR. Tests swap in a fake reader."""
    return None


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return float(np.mean(values)) if values else None


def _means(per: list[dict], keys: Sequence[str]) -> dict:
    return {k: _mean(p.get(k) for p in per) for k in keys}


def _only(scene: Scene, eid: str) -> Scene:
    return scene.model_copy(update={"elements": [scene.element(eid)]})


def _source_frames(root: Path, scene_id: str, frames: Sequence[int]) -> dict[int, np.ndarray]:
    """The analysed source frames (the stage cache, else the project's video)."""
    npy = scene_dir(root, scene_id) / "stages" / "frames.npy"
    if npy.exists():
        stack = np.load(npy, mmap_mode="r")
        return {f: np.asarray(stack[f], np.float32) for f in frames}
    project = load_project(root)
    first = next(s.frames[0] for s in project.scenes if s.id == scene_id)
    want, out = {first + f: f for f in frames}, {}
    cap = cv2.VideoCapture(str(project.source["file"]))
    try:
        for i in range(max(want) + 1):
            ok, bgr = cap.read()
            if not ok:
                break
            if i in want:
                out[want[i]] = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
    finally:
        cap.release()
    return out


def _coverage(scene: Scene, sd: Path, frames: Sequence[int], ids: Sequence[str]) -> tuple[dict, dict, dict]:
    """One pass over the analysed layers: α of each id, union α of all other layers per id, union α of all."""
    W, H = scene.size
    alone: dict = {i: {} for i in ids}
    rest = {i: {f: np.ones((H, W), np.float32) for f in frames} for i in ids}
    every = {f: np.ones((H, W), np.float32) for f in frames}
    for eid, f, a, _ in layers(scene, sd, frames, renderer="numpy"):
        every[f] *= 1 - a
        for i in ids:
            if eid == i:
                alone[i][f] = a
            else:
                rest[i][f] *= 1 - a
    return alone, {i: {f: 1 - p for f, p in r.items()} for i, r in rest.items()}, {f: 1 - p for f, p in every.items()}


def _copy_project(root: Path, temp: str) -> Path:
    dst = Path(temp) / "proj"
    shutil.copytree(root, dst, ignore=shutil.ignore_patterns("frames.npy", "*.pkl"))
    return dst


def _agent_edit(root: Path, scene_id: str, temp: str, target: Target, choices: dict) -> tuple[dict, Scene | None, Path | None]:
    tmp = _copy_project(root, temp)
    res = edit(tmp, scene_id, target.value or "", element=target.element, confirm=True, intent=Intent(targets=[target]),
               choices={**choices, "keep_violation": "release_keep"})
    out = {"status": res.status, "error": res.error}
    if res.status != "done":
        return out, None, None
    return out, current_scene(tmp, scene_id)[0], scene_dir(tmp, scene_id)


def _box(*alphas: np.ndarray, margin: int = 8) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(np.logical_or.reduce([a > 0.02 for a in alphas]))
    if not len(xs):
        return (0, 0, 0, 0)
    return (int(xs.min()) - margin, int(ys.min()) - margin, int(xs.max()) + margin + 1, int(ys.max()) + margin + 1)


def edit_checks(root, scene_id, *, title, hide, frames, renderer: Renderer, truth: dict | None = None) -> dict:
    """Edit-after checks on a temp copy of an analysed project. `title`: element to retext (or None);
    `hide`: element ids to hide one at a time. `truth` (synthetic only) supplies exact layers:
    {"scene", "dir", "layers": {id: {f: (α, F)}}, "plates": {f: rgb}, "title": truth id, "pairs": {analysed id: truth id}}.
    Returns per-check metrics plus "images" (per frame, in SHEET_LABELS order) for the comparison sheet."""
    root, frames = Path(root), list(frames)
    hide = [h for h in (hide or []) if h]
    scene, _ = current_scene(root, scene_id)
    sd = scene_dir(root, scene_id)
    src = _source_frames(root, scene_id, frames)
    rebuilt = dict(zip(frames, render_frames(scene, sd, frames, renderer=renderer)))
    ids = list(dict.fromkeys(([title] if title else []) + hide))
    alone, others, cover = _coverage(scene, sd, frames, ids)
    pairs = (truth or {}).get("pairs", {})
    images = {f: [src[f], rebuilt[f], None, None, None] for f in frames}
    cache: dict = {}

    def exclusion(eid: str, f: int) -> np.ndarray:
        """Pixels other layers may legitimately cover: the true other layers when known (so an analysed layer
        that still holds this element's pixels is measured, not excused), else the other analysed layers."""
        if not truth:
            return others[eid][f] > 0.02
        tid = pairs.get(eid, truth.get("title") if eid == title else None)
        if (tid, f) not in cache:
            keep = np.ones(scene.size[::-1], np.float32)
            for t, per_frame in truth["layers"].items():
                if t != tid:
                    keep *= 1 - per_frame[f][0]
            cache[tid, f] = keep < 0.98
        return cache[tid, f]

    result: dict = {"frames": frames, "title": None, "hide": [], "background": [],
                    "render_l1": _mean(float(np.abs(rebuilt[f] - src[f]).mean() / 255) for f in frames)}

    if title:
        with tempfile.TemporaryDirectory(prefix="keepframe-eval-") as temp:
            check, edited, esd = _agent_edit(root, scene_id, temp, Target(element=title, property="text", value=TITLE_TEXT),
                                              {"overflow": "expand_box"})
            if edited is not None:
                imgs = dict(zip(frames, render_frames(edited, esd, frames, renderer=renderer)))
                new_a = {f: a for _, f, a, _ in layers(_only(edited, title), esd, frames, renderer="numpy")}
                expected = edited.element(title).canonical.color or "#ffffff"
                per = []
                for f in frames:
                    if truth and truth.get("title"):
                        old_a = truth["layers"][truth["title"]][f][0]
                    else:
                        old_a = alone[title][f]
                    ex = exclusion(title, f)
                    images[f][2] = imgs[f]
                    if not ((new_a[f] > 0.5) & ~ex).any():   # the new title is not on screen: nothing passes
                        per.append({"frame": f, "visible": False, "outside_glyph_delta": None, "smear_score": None, "glyph_de": None})
                        continue
                    per.append({"frame": f, "visible": True,
                                "outside_glyph_delta": outside_glyph_delta(src[f], rebuilt[f], imgs[f], old_a, new_a[f],
                                                                           _box(old_a, new_a[f]), exclude=ex),
                                "smear_score": smear_score(imgs[f], old_a, new_a[f], exclude=ex),
                                "glyph_de": glyph_colour_delta(imgs[f], np.where(ex, 0, new_a[f]), expected)})
                keys = ("outside_glyph_delta", "smear_score", "glyph_de")
                shown = all(p["visible"] for p in per)
                check.update(element=title, text=TITLE_TEXT, colour=expected, visible_frames=sum(p["visible"] for p in per),
                             **(_means(per, keys) if shown else dict.fromkeys(keys)), frames=per)
            result["title"] = check

    for h in hide:
        edited = scene.model_copy(update={"elements": [e for e in scene.elements if e.id != h]})
        imgs = dict(zip(frames, render_frames(edited, sd, frames, renderer=renderer)))
        per = []
        for f in frames:
            mask = alone[h][f] > 0.02
            if h in pairs:
                mask |= truth["layers"][pairs[h]][f][0] > 0.02
            mask, ex = _grow(mask, 2), exclusion(h, f)
            r = {"frame": f, **plate_residue(imgs[f], mask, exclude=ex)}
            if truth:
                m = mask & ~ex
                r["truth_de"] = float(delta_e(srgb_to_lab(imgs[f][m]), srgb_to_lab(truth["plates"][f][m])).mean()) if m.any() else None
            per.append(r)
            if h == hide[0]:
                images[f][3] = imgs[f]
        result["hide"].append({"element": h, "truth": pairs.get(h),
                               **_means(per, ("mean_de", "p95_de", "hf_ratio", "residue_fraction", "truth_de")), "frames": per})

    for mode, colour in BACKGROUND_EDITS:
        with tempfile.TemporaryDirectory(prefix="keepframe-eval-") as temp:
            check, edited, esd = _agent_edit(root, scene_id, temp, Target(property="background", value=colour), {"background": mode})
            check.update(mode=mode, colour=colour, halo_ring=None)
            if edited is not None:
                imgs = dict(zip(frames, render_frames(edited, esd, frames, renderer=renderer)))
                if truth:
                    recoloured = truth["scene"].model_copy(update={"background": Background(kind="color", value=colour)})
                    ideal = dict(zip(frames, render_frames(recoloured, truth["dir"], frames, renderer=renderer)))
                    W, H = scene.size
                    union = {f: np.ones((H, W), np.float32) for f in frames}
                    for per_frame in truth["layers"].values():
                        for f in frames:
                            union[f] *= 1 - per_frame[f][0]
                    alpha_scene = {f: 1 - union[f] for f in frames}
                else:   # no truth: swap the analysed plate for the new colour under the analysed coverage
                    old = dict(zip(frames, render_frames(scene.model_copy(update={"elements": []}), sd, frames, renderer=renderer)))
                    new = np.float32(hex_to_rgb8(colour))
                    ideal = {f: src[f] + (new - old[f]) * (1 - cover[f])[..., None] for f in frames}
                    alpha_scene = cover
                per = [{"frame": f, "halo_ring": halo_ring(imgs[f], ideal[f], alpha_scene[f])} for f in frames]
                check.update(**_means(per, ("halo_ring",)), frames=per)
                if mode == BACKGROUND_EDITS[0][0]:
                    for f in frames:
                        images[f][4] = imgs[f]
            result["background"].append(check)
    result["images"] = images
    return result


def _match(truth: dict, analysed: dict, frames: Sequence[int], fixed: dict[str, str] | None = None) -> dict[str, str]:
    """Truth ↔ analysed layers: `fixed` pairs (titles matched by text) first, the rest by α IoU over the
    checked frames (Hungarian, IoU ≥ 0.3). Returns {analysed: truth}."""
    fixed = dict(fixed or {})
    tids = [t for t in truth if t not in fixed.values()]
    aids = [a for a in analysed if a not in fixed]
    if not tids or not aids:
        return fixed
    iou = np.zeros((len(tids), len(aids)))
    for i, t in enumerate(tids):
        for j, a in enumerate(aids):
            inter = union = 0
            for f in frames:
                g, h = truth[t][f][0] > 0.5, analysed[a][f][0] > 0.5
                inter += int((g & h).sum())
                union += int((g | h).sum())
            iou[i, j] = inter / union if union else 0.0
    rows, cols = linear_sum_assignment(-iou)
    return {**fixed, **{aids[c]: tids[r] for r, c in zip(rows, cols) if iou[r, c] >= 0.3}}


def _analyse(video: Path, n_frames: int, root: Path, opts: AnalyzeOptions, ocr, reuse: bool) -> float:
    meta = root.parent / f"{root.name}.analysis.json"
    if reuse and (root / "project.json").exists() and meta.exists():
        return json.loads(meta.read_text())["seconds"]
    shutil.rmtree(root, ignore_errors=True)
    t0 = time.perf_counter()
    analyze(video, 0, n_frames - 1, root, opts, ocr=ocr, captioner=None)   # no captioner: no VLM calls
    seconds = time.perf_counter() - t0
    meta.write_text(json.dumps({"seconds": seconds, "options": asdict(opts)}))
    return seconds


def _messages(root: Path, scene_id: str = "s1") -> list[str]:
    p = scene_dir(root, scene_id) / "report.json"
    return json.loads(p.read_text()).get("messages", []) if p.exists() else []


def _font_hit(truth_el, el) -> bool:
    font = el.canonical.font
    names = (font.candidates[:3] or [font.family_guess]) if font else []
    return truth_el.canonical.font.family_guess.casefold() in {n.casefold() for n in names}


def _largest_sprite(scene: Scene, skip: Sequence[str] = ()) -> str | None:
    sprites = [e for e in scene.elements if e.kind != "text" and not e.canonical.text and e.id not in skip]
    return max(sprites, key=lambda e: e.canonical.width * e.canonical.height).id if sprites else None


def _sheet(res: dict, frames: Sequence[int], out: Path, name: str) -> str:
    """Writes sheets/<name>.png under `out`; returns that path relative to `out`."""
    comparison_sheet([res["images"][f] for f in frames], SHEET_LABELS, out / "sheets" / f"{name}.png", row_labels=[f"f{f}" for f in frames])
    return f"sheets/{name}.png"


def _eval_seed(out: Path, seed: int, plate: str, renderer: Renderer, opts: AnalyzeOptions, reuse: bool) -> dict:
    d = out / "synthetic" / f"seed{seed}"
    tdir = d / "truth"
    truth = make_reference_scene(tdir, seed, plate=plate, mover=seed % 2 == 0, n_titles=2 if seed % 3 == 0 else 1)
    save_scene(truth, tdir / "scene.json")
    video = render_reference(truth, tdir, d / "reference.mp4", renderer=renderer)
    proj = d / "proj"
    seconds = _analyse(video, truth.frames, proj, opts, make_ocr(truth), reuse)
    scene, _ = current_scene(proj, "s1")
    sd = scene_dir(proj, "s1")
    titles = [e for e in truth.elements if e.kind == "text"]
    settle = titles[0].tracks["opacity"].keys[-1].t
    frames = sorted({min(settle + 1, truth.frames - 1), truth.frames // 2, truth.frames - 1})
    gt = ground_truth(truth, tdir, frames, renderer=renderer)
    plates = {f: true_plate(truth, tdir, f, renderer=renderer) for f in frames}
    analysed = ground_truth(scene, sd, frames, renderer="numpy")
    integrity = title_integrity(scene, [{"text": t.canonical.text, "role": "title"} for t in titles])
    pairs = _match(gt, analysed, frames, {i["element"]: t.id for t, i in zip(titles, integrity) if i["element"]})

    alpha_rows, leak_px, opaque_px, corr = [], 0, 0, []
    for a_id, t_id in pairs.items():
        for f in frames:
            (a, fg), (ta, tfg) = analysed[a_id][f], gt[t_id][f]
            if ta.max() > 0.02:
                alpha_rows.append({"element": a_id, **alpha_errors(a, ta), **foreground_errors(fg, tfg, a, ta)})
    for a_id, per_frame in analysed.items():
        for f in frames:
            a, fg = per_frame[f]
            n = int((a > 0.5).sum())
            if n:
                layer = np.dstack([fg, a])
                leak_px += bg_leak_fraction(layer, plates[f]) * n
                opaque_px += n
                corr.append(leak_correlation(layer, plates[f]))
    found = [(t, scene.element(i["element"]) if i["element"] else None) for t, i in zip(titles, integrity)]
    title_id = integrity[0]["element"]
    t_mask = {f: _grow(gt[titles[0].id][f][0] > 0.02, 2) for f in frames}
    a_plates = dict(zip(frames, render_frames(scene.model_copy(update={"elements": []}), sd, frames, renderer="numpy")))
    logo = next((a for a, t in pairs.items() if t == "logo"), None)
    plate_de = {"overall": [], "under_title": [], "under_logo": []}
    for f in frames:
        de = delta_e(srgb_to_lab(a_plates[f]), srgb_to_lab(plates[f]))
        plate_de["overall"].append(float(de.mean()))
        plate_de["under_title"].append(float(de[t_mask[f]].mean()))
        if "logo" in gt:
            plate_de["under_logo"].append(float(de[_grow(gt["logo"][f][0] > 0.02, 2)].mean()))
    hide = [title_id or _largest_sprite(scene)]
    if logo and logo not in hide:
        hide.append(logo)
    res = edit_checks(proj, "s1", title=title_id, hide=hide, frames=frames, renderer=renderer,
                      truth={"scene": truth, "dir": tdir, "layers": gt, "plates": plates, "title": titles[0].id, "pairs": pairs})
    sheet = _sheet(res, frames, out, f"synthetic-seed{seed}")
    return {"seed": seed, "plate": plate, "mover": seed % 2 == 0, "frames": frames, "seconds": round(seconds, 2),
            "titles": [{"text": t.canonical.text, "font": t.canonical.font.family_guess, "colour": t.canonical.color} for t in titles],
            "elements": {"truth": len(truth.elements), "analysed": len(scene.elements)}, "matched": len(pairs), "pairs": pairs,
            "unmatched_truth": [e.id for e in truth.elements if e.id not in pairs.values()],
            "integrity": integrity,
            "title_colour_de": [float(delta_e(srgb_to_lab(np.float32(hex_to_rgb8(el.canonical.color))),
                                              srgb_to_lab(np.float32(hex_to_rgb8(t.canonical.color)))))
                                if el is not None and el.canonical.color else None for t, el in found],
            "font_hits": [el is not None and _font_hit(t, el) for t, el in found],
            "font_top3": _mean(float(el is not None and _font_hit(t, el)) for t, el in found),
            "alpha": {**_means(alpha_rows, ("sad", "mse", "f_de_interior", "f_de_edge")), "layers": len(alpha_rows)},
            "leak": {"bg_leak_fraction": leak_px / opaque_px if opaque_px else None, "leak_correlation": _mean(corr)},
            "plate_de": {k: _mean(v) for k, v in plate_de.items()},
            "ring_floor": _mean(plate_residue(plates[f], t_mask[f])["mean_de"] for f in frames),
            "title": res["title"], "hide": res["hide"], "background": res["background"], "render_l1": res["render_l1"],
            "sheet": sheet, "messages": _messages(proj)}


def _samples(rows: list[dict], key: str) -> list:
    head, _, tail = key.partition(".")
    out = []
    for row in rows:
        v = row.get(head)
        if head == "font_hits":
            out += v
        elif isinstance(v, list):
            out += [x.get(tail) for x in v] or [None]
        else:
            out.append(v.get(tail) if isinstance(v, dict) else None)
    return out


def _passes(v, op: str, thr) -> bool:
    if v is None:
        return False
    if op == "in":
        return thr[0] <= v <= thr[1]
    return {"le": v <= thr, "lt": v < thr, "ge": v >= thr}[op]


def _worst(values: list, op: str, thr):
    values = [v for v in values if v is not None]
    if not values:
        return None
    if op == "in":
        return max(values, key=lambda v: max(thr[0] - v, v - thr[1]))
    return min(values) if op == "ge" else max(values)


def _gate(samples: list, op: str, thr) -> dict:
    if op == "rate":
        hits = [bool(s) for s in samples]
        value = float(np.mean(hits)) if hits else None
        return {"value": value, "threshold": thr, "op": "ge", "samples": len(hits), "passed": value is not None and value >= thr}
    failed = sum(not _passes(s, op, thr) for s in samples)
    return {"value": _mean(samples), "worst": _worst(samples, op, thr), "threshold": list(thr) if isinstance(thr, tuple) else thr,
            "op": op, "samples": len(samples), "failed": failed, "passed": bool(samples) and failed == 0}


def synthetic_gates(rows: list[dict]) -> dict:
    return {name: _gate(_samples(rows, key), op, thr) for name, (key, op, thr) in SYNTHETIC_GATES.items()}


def eval_synthetic(out, n=6, *, renderer: Renderer, options: AnalyzeOptions | None = None, reuse: bool = False) -> dict:
    opts = options or AnalyzeOptions(refine=False, generate_3d=False)
    rows = []
    for i in range(n):
        log.info("eval-edits synthetic %s/%s", i + 1, n)
        rows.append(_eval_seed(Path(out), i + 1, PLATES[i % len(PLATES)], renderer, opts, reuse))
    return {"n": n, "rows": rows, "gates": synthetic_gates(rows)}


def _baseline(baseline, stem: str) -> dict:
    if not baseline:
        return {}
    data = json.loads(Path(baseline).read_text()) if not isinstance(baseline, dict) else baseline
    if isinstance(data.get("rows"), list):
        data = {Path(r["clip"]).stem: r for r in data["rows"]}
    return data.get("clips", data).get(stem, {})


def _render_l1(root: Path, clip: Path, renderer: Renderer, max_frames: int) -> float:
    """Same sampling as gate-m2-real's render check (every 10th frame from 0)."""
    if renderer == "browser":
        from ..gates import render_fidelity
        return render_fidelity(root, "s1", clip, max_frames=max_frames)["render_l1"]
    scene, _ = current_scene(root, "s1")
    idx = list(range(0, min(scene.frames, max_frames), 10))
    src = _source_frames(root, "s1", idx)
    got = render_frames(scene, scene_dir(root, "s1"), idx, renderer="numpy")
    return float(np.mean([np.abs(g.round().clip(0, 255) - src[f]).mean() / 255 for f, g in zip(idx, got)]))


def _clip_leak(scene: Scene, sd: Path, frames: Sequence[int]) -> dict:
    """Leak of every analysed layer against the analysed plate (clips have no true plate)."""
    plates = dict(zip(frames, render_frames(scene.model_copy(update={"elements": []}), sd, frames, renderer="numpy")))
    leak_px = opaque_px = 0.0
    corr = []
    for _, f, a, fg in layers(scene, sd, frames, renderer="numpy"):
        n = int((a > 0.5).sum())
        if n:
            layer = np.dstack([fg, a])
            leak_px += bg_leak_fraction(layer, plates[f]) * n
            opaque_px += n
            corr.append(leak_correlation(layer, plates[f]))
    return {"bg_leak_fraction": leak_px / opaque_px if opaque_px else None, "leak_correlation": _mean(corr), "against": "analysed plate"}


def eval_clip(clip, gold, out, *, max_frames=150, baseline=None, reuse=False, renderer: Renderer = "browser",
              options: AnalyzeOptions | None = None) -> dict:
    clip = Path(clip)
    titles = (json.loads(Path(gold).read_text()) if not isinstance(gold, dict) else gold)["titles"]
    cap = cv2.VideoCapture(str(clip))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or max_frames
    cap.release()
    n = min(total, max_frames)
    root = Path(out) / "clips" / clip.stem
    seconds = _analyse(clip, n, root, options or AnalyzeOptions(), make_ocr(None), reuse)
    scene, _ = current_scene(root, "s1")
    sd = scene_dir(root, "s1")
    integrity = title_integrity(scene, titles)
    colour = []
    for t, i in zip(titles, integrity):
        if t.get("role", "title") != "title":
            continue
        el = scene.element(i["element"]) if i["element"] else None
        got = el.canonical.color if el is not None else None
        colour.append({"text": t["text"], "gold": t["color"], "element": i["element"], "analysed": got,
                       "de": float(delta_e(srgb_to_lab(np.float32(hex_to_rgb8(got))), srgb_to_lab(np.float32(hex_to_rgb8(t["color"])))))
                       if got else None})
    gold_titles = [(t, i["element"]) for t, i in zip(titles, integrity) if t.get("role", "title") == "title"]
    edited = next(((t, e) for t, e in gold_titles if e), gold_titles[0] if gold_titles else ({"frame": n // 3}, None))
    title_id, settled = edited[1], int(edited[0]["frame"])   # the edited title's gold frame is the settled one
    frames = sorted({min(max(settled, 0), n - 1), n // 2, n - 1})
    res = edit_checks(root, "s1", title=title_id, hide=[title_id or _largest_sprite(scene)], frames=frames, renderer=renderer)
    base = _baseline(baseline, clip.stem)
    row = {"clip": clip.stem, "frames": frames, "analysed_frames": n, "seconds": round(seconds, 2), "vlm_calls": 0,
           "elements": len(scene.elements), "integrity": integrity, "colour": colour,
           "render_l1": _render_l1(root, clip, renderer, max_frames), "leak": _clip_leak(scene, sd, frames),
           "title": res["title"], "hide": res["hide"], "background": res["background"],
           "baseline": {k: base.get(k) for k in ("seconds", "render_l1")},
           "sheet": _sheet(res, frames, Path(out), f"clip-{clip.stem}"), "messages": _messages(root)}
    return row


def clip_gates(rows: list[dict]) -> dict:
    colour = [c["de"] for r in rows for c in r["colour"]]
    l1 = [(r["render_l1"], r["baseline"].get("render_l1")) for r in rows]
    secs = [(r["seconds"], r["baseline"].get("seconds")) for r in rows]
    gates = {"title_colour_de": _gate(colour, "lt", 10.0),
             "vlm_calls": _gate([r["vlm_calls"] for r in rows], "le", 0)}
    for name, pairs, limit in (("render_l1", l1, lambda b: b + 0.005), ("seconds", secs, lambda b: 1.5 * b)):
        known = [(v, limit(b)) for v, b in pairs if b is not None]
        gates[name] = ({"value": _mean(v for v, _ in known), "limits": [lim for _, lim in known], "samples": len(known),
                        "failed": sum(v > lim for v, lim in known), "passed": all(v <= lim for v, lim in known)}
                       if known else {"value": _mean(v for v, _ in pairs), "passed": None, "note": "no baseline"})
    return gates


def _fmt(v, nd=2) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.{nd}f}" if abs(v) >= 0.01 or v == 0 else f"{v:.4f}"
    return str(v)


def summary_markdown(m: dict) -> str:
    opts = m["options"]
    lines = [f"# keepframe eval-edits · {m['date']}", "",
             f"renderer `{m['renderer']}` · VLM calls {m['vlm_calls']} · passed {_fmt(m['passed'])}",
             "", "Analysis options — synthetic: " + ", ".join(f"{k}={v}" for k, v in opts["synthetic"].items()),
             "", "Analysis options — clips: " + ", ".join(f"{k}={v}" for k, v in opts["clips"].items()), ""]
    for group, gates in m["gates"].items():
        lines += [f"## {group} gates", "", "| gate | threshold | mean | worst | failed / samples | pass |", "|---|---|---|---|---|---|"]
        for name, g in gates.items():
            thr = g.get("threshold", g.get("limits", "baseline"))
            op = {"le": "≤", "lt": "<", "ge": "≥", "in": "∈"}.get(g.get("op"), "")
            lines.append(f"| {name} | {op} {thr} | {_fmt(g.get('value'), 3)} | {_fmt(g.get('worst'), 3)} | "
                         f"{g.get('failed', '—')} / {g.get('samples', '—')} | {_fmt(g.get('passed'))} |")
        lines.append("")
    rows = m.get("synthetic", {}).get("rows", [])
    if rows:
        lines += ["## synthetic scenes", "",
                  "| seed | plate | s | matched | title whole | title ΔE | font top-3 | α SAD | F ΔE int / edge | bg leak | leak slope | "
                  "plate ΔE under title / logo | title outside / smear / glyph | hide ΔE / hf / residue (truth ΔE) | halo | render L1 |",
                  "|" + "---|" * 16]
        for r in rows:
            t, a = r["title"] or {}, r["alpha"]
            hides = "; ".join(f"{h['element']}: {_fmt(h['mean_de'])} / {_fmt(h['hf_ratio'])} / {_fmt(h['residue_fraction'], 3)} ({_fmt(h['truth_de'])})"
                              for h in r["hide"])
            lines.append(f"| {r['seed']} | {r['plate']} | {r['seconds']:.0f} | {r['matched']}/{r['elements']['truth']} | "
                         f"{_fmt(r['integrity'][0]['whole'])} | {_fmt(r['title_colour_de'][0])} | {_fmt(r['font_top3'])} | {_fmt(a['sad'], 3)} | "
                         f"{_fmt(a['f_de_interior'])} / {_fmt(a['f_de_edge'])} | {_fmt(r['leak']['bg_leak_fraction'], 3)} | "
                         f"{_fmt(r['leak']['leak_correlation'])} | {_fmt(r['plate_de']['under_title'])} / {_fmt(r['plate_de']['under_logo'])} | "
                         f"{_fmt(t.get('outside_glyph_delta'))} / {_fmt(t.get('smear_score'))} / {_fmt(t.get('glyph_de'))} "
                         f"({t.get('status', '—')}) | {hides} | {_fmt(r['background'][0].get('halo_ring') if r['background'] else None)} | "
                         f"{_fmt(r['render_l1'], 4)} |")
        lines.append("")
    rows = m.get("clips", {}).get("rows", [])
    if rows:
        lines += ["## clips", "", "| clip | s | elements | titles whole | colour ΔE | render L1 | bg leak | title outside / smear / glyph | hide ΔE | halo |",
                  "|" + "---|" * 10]
        for r in rows:
            t = r["title"] or {}
            lines.append(f"| {r['clip']} | {r['seconds']:.0f} | {r['elements']} | {sum(i['whole'] for i in r['integrity'])}/{len(r['integrity'])} | "
                         f"{', '.join(_fmt(c['de']) for c in r['colour'])} | {_fmt(r['render_l1'], 4)} | {_fmt(r['leak']['bg_leak_fraction'], 3)} | "
                         f"{_fmt(t.get('outside_glyph_delta'))} / {_fmt(t.get('smear_score'))} / {_fmt(t.get('glyph_de'))} | "
                         f"{_fmt(r['hide'][0]['mean_de'] if r['hide'] else None)} | {_fmt(r['background'][0].get('halo_ring') if r['background'] else None)} |")
        lines.append("")
    return "\n".join(lines)


def eval_edits(clips_dir, gold_dir, out, *, max_frames=150, baseline=None, synthetic=6, reuse=False,
               renderer: Renderer = "browser", strict=False, only=None, refine: bool | None = None) -> dict:
    """Synthetic set (refine off unless asked) and/or every clip in `clips_dir` with gold titles in `gold_dir`
    (default analysis options, matching the baseline). Writes metrics.json, summary.md and sheets/ under `out`.
    `strict`: gates that could not be evaluated (no baseline) count as failures."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    syn_opts = AnalyzeOptions(refine=bool(refine), generate_3d=False)
    clip_opts = AnalyzeOptions() if refine is None else AnalyzeOptions(refine=refine)
    m: dict = {"date": date.today().isoformat(), "renderer": renderer, "vlm_calls": 0, "max_frames": max_frames, "strict": strict,
               "options": {"synthetic": asdict(syn_opts), "clips": asdict(clip_opts)}, "gates": {}}
    if synthetic:
        res = eval_synthetic(out, synthetic, renderer=renderer, options=syn_opts, reuse=reuse)
        m["synthetic"] = {"n": res["n"], "rows": res["rows"]}
        m["gates"]["synthetic"] = res["gates"]
    if clips_dir:
        rows, missing = [], []
        for clip in sorted(Path(clips_dir).glob("*.mp4")):
            if only and clip.stem not in only and clip.name not in only:
                continue
            gold = Path(gold_dir) / f"{clip.stem}.json"
            if not gold.exists():
                missing.append(clip.stem)
                continue
            log.info("eval-edits clip %s", clip.name)
            rows.append(eval_clip(clip, gold, out, max_frames=max_frames, baseline=baseline, reuse=reuse, renderer=renderer, options=clip_opts))
        m["clips"] = {"rows": rows, "missing_gold": missing}
        m["gates"]["clips"] = clip_gates(rows)
    verdicts = [g["passed"] for gates in m["gates"].values() for g in gates.values()]
    m["passed"] = all(v is True or (v is None and not strict) for v in verdicts)
    (out / "metrics.json").write_text(json.dumps(m, indent=2, default=float))
    (out / "summary.md").write_text(summary_markdown(m))
    return m
