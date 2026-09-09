from __future__ import annotations
import json, pickle
from dataclasses import dataclass, asdict
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import Background, Canonical, Element, Keyframe, Project, Scene, Track, Version
from ..ir.store import current_scene, init_project, new_version, scene_dir as _scene_dir
from ..log import get
from ..progress import STAGES, report_stage
from .background import estimate_background, foreground_mask
from .constraints import extract_constraints
from .keyframes import fill_gaps, tracks_from_raw
from .regions import build_palette, extract_regions
from .report import element_confidence, reconstruction_error, write_report
from .semantics import assign_roles, group_by_motion
from .sprites import RAW_COLS, sprite_props, z_order
from .text import Ocr, apply_copy, ocr_frames, text_exclusion_mask, text_props, track_text
from .tracking import track_regions, _merge_adjacent_tracks, _trim_tail_crumbs
from .video import read_frames

log = get("keepframe.analyze")


@dataclass
class AnalyzeOptions:
    bg_override: str | None = None
    copy: list[str] | None = None
    ocr: bool = True
    refine: bool = True
    refine_iters: int = 200
    min_area: int = 30
    use_ecc: bool = True


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def _rgb(hexs: str):
    h = hexs.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _load_overrides(sd: Path) -> dict:
    p = sd / "stages" / "overrides.json"
    return json.loads(p.read_text()) if p.exists() else {"regions": [], "ids": {}, "merge": []}


def _pk(sd: Path, name: str, obj=None):
    p = sd / "stages" / f"{name}.pkl"
    if obj is None:
        return pickle.loads(p.read_bytes())
    p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(pickle.dumps(obj)); return obj


def _stage_text(frames, bg, opts, ocr, sd):
    boxes = [[] for _ in frames]; tracks = []; msg = None
    if opts.ocr:
        if ocr is None:
            try:
                from .text import RapidOcr
                ocr = RapidOcr()
            except Exception as e:  # rapidocr missing
                msg = f"text stage skipped: {e}"
                if "rapidocr_onnxruntime" in str(e):
                    msg += "; pip install -e '.[ocr]' (same python as keepframe)"
                log.info("%s", msg)
        if ocr is not None:
            boxes = ocr_frames(frames, ocr)
            tracks = track_text(boxes)
            log.info("text boxes=%s tracks=%s", sum(len(b) for b in boxes), len(tracks))
            if opts.copy:
                apply_copy(tracks, opts.copy)
    _pk(sd, "text", {"boxes": boxes, "tracks": tracks, "message": msg})
    return boxes, tracks, msg


def _stage_regions(frames, bg, boxes, opts, sd):
    fg = np.stack([foreground_mask(f, bg) for f in frames])
    pal = build_palette(frames, fg)
    ov = _load_overrides(sd)
    ov_by_frame: dict[int, list] = {}
    for o in ov.get("regions", []):
        m = cv2.imread(str(sd / o["mask"]), cv2.IMREAD_GRAYSCALE) > 127
        ov_by_frame.setdefault(o["frame"], []).append((m, int(o["label"])))
    n = len(frames)
    rbf = []
    for i in range(n):
        if i == 0 or i + 1 == n or (i + 1) % max(1, n // 10) == 0:
            report_stage("regions", f"{i + 1}/{n}")
        rbf.append(extract_regions(i, frames[i], fg[i], pal, min_area=opts.min_area, exclude_mask=text_exclusion_mask(boxes[i], fg[i].shape),
                                   overrides=ov_by_frame.get(i)))
    log.info("regions frames=%s labels=%s", n, sum(len(r) for r in rbf))
    return _pk(sd, "regions", rbf)


def _stage_tracking(rbf, sd):
    tracks = track_regions(rbf, cost_thr=2.0)
    ov = _load_overrides(sd)
    # overrides: forced region labels >= 1000 pin regions to an object id (label - 1000)
    for t in tracks:
        for f, r in list(t.regions.items()):
            if r.label >= 1000:
                target = r.label - 1000
                dst = next((u for u in tracks if u.id == target), None)
                if dst is not None and dst is not t:
                    dst.regions[f] = r; del t.regions[f]
    tracks = [t for t in tracks if t.regions]
    for t in tracks:
        _trim_tail_crumbs(t)
    tracks = [t for t in tracks if t.regions]
    tracks = _merge_adjacent_tracks(tracks)
    log.info("tracking objects=%s", len(tracks))
    return _pk(sd, "tracks", tracks)


def _stage_sprites(frames, bg, text_tracks, obj_tracks, opts, sd, n_frames):
    props = {}   # object_key -> dict(raw, canon, cf, kind, font, color)
    for t in text_tracks:
        raw, canon, cf, font, color = text_props(t, frames, bg, n_frames, 0)
        props[f"t{t.id}"] = {"raw": raw, "canon": canon, "cf": cf, "kind": "text", "text": t.text, "font": font, "color": color, "first": t.first, "last": t.last}
    z = z_order(obj_tracks, frames, bg)
    for t in obj_tracks:
        raw, canon, cf = sprite_props(t, frames, bg, n_frames, 0, use_ecc=opts.use_ecc)
        props[f"o{t.id}"] = {"raw": raw, "canon": canon, "cf": cf, "kind": "sprite", "z": z[t.id], "first": t.first, "last": t.last}
    ov = _load_overrides(sd)
    for a, b in ov.get("merge", []):   # merge object b into a (element ids resolved via ids.json at correction time)
        if a in props and b in props:
            pa, pb = props[a], props[b]
            m = np.isnan(pa["raw"][:, 0]) & ~np.isnan(pb["raw"][:, 0])
            pa["raw"][m] = pb["raw"][m]; pa["first"] = min(pa["first"], pb["first"]); pa["last"] = max(pa["last"], pb["last"])
            del props[b]
    if opts.refine and any(p["kind"] == "sprite" for p in props.values()):
        try:
            from .device import resolve_device
            from .refine import refine_affine, torch_available
            if torch_available():
                keys = [k for k, p in props.items() if p["kind"] == "sprite"]
                dev = resolve_device()
                log.info("refine start device=%s sprites=%s iters=%s", dev, len(keys), opts.refine_iters)
                report_stage("sprites", f"refine {len(keys)} sprites {dev}")
                refined = refine_affine(frames, bg, {k: props[k]["raw"] for k in keys}, {k: props[k]["canon"] for k in keys},
                                        {k: (0.5, 0.5) for k in keys}, {k: props[k]["z"] for k in keys},
                                        iters=opts.refine_iters, device=dev)
                for k in keys:
                    props[k]["raw"] = refined[k]
            else:
                log.info("refine skipped: torch not installed")
        except Exception as e:
            log.error("refine skipped: %s", e)
            props["_message"] = f"refine skipped: {e}"
    return _pk(sd, "props", props)


def _elements_from_props(props: dict, sd: Path, ids: dict) -> tuple[list[Element], dict[str, np.ndarray]]:
    keys = [k for k in props if not k.startswith("_")]
    keys.sort(key=lambda k: (props[k]["first"], float(np.nanmean(props[k]["raw"][:, 0]))))
    elements, raws = [], {}
    for k in keys:
        eid = ids.get(k) or f"e{len(ids) + 1}"
        ids[k] = eid
        p = props[k]
        raw_full = fill_gaps(p["raw"])
        valid = np.flatnonzero(~np.isnan(raw_full[:, 0]))
        first, last = int(valid[0]), int(valid[-1])
        tracks, fe = tracks_from_raw(raw_full[first:last + 1], first)
        (sd / "assets").mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(sd / "assets" / f"{eid}.png"), cv2.cvtColor(p["canon"], cv2.COLOR_RGBA2BGRA))
        np.savez_compressed(sd / "assets" / f"{eid}_raw.npz", raw=raw_full, first=first, cols=np.array(RAW_COLS))
        h, w = p["canon"].shape[:2]
        canonical = Canonical(width=w, height=h, texture=f"assets/{eid}.png",
                              text=p.get("text"), font=p.get("font"), color=p.get("color"))
        elements.append(Element(id=eid, kind=p["kind"], canonical=canonical, visible=(first, last), tracks=tracks,
                                z=Track(keys=[Keyframe(t=0, v=p.get("z", 0))]), raw=f"assets/{eid}_raw.npz", fit_error=fe))
        raws[eid] = raw_full
    return elements, raws


def _finish(sd: Path, scene: Scene, frames: np.ndarray, raws: dict, messages: list[str]) -> Scene:
    report_stage("semantics")
    assign_roles(scene.elements)
    scene.groups = group_by_motion(scene.elements, raws)
    report_stage("constraints")
    scene.constraints = extract_constraints(scene)
    report_stage("report")
    rec = reconstruction_error(scene, sd, frames, 0)
    conf = element_confidence(scene, sd, frames, 0)
    for e in scene.elements:
        e.confidence = conf[e.id]
    write_report(sd, {"reconstruction": rec, "confidence": conf, "messages": messages,
                      "elements": len(scene.elements), "fit_error_max_px": max([e.fit_error.max_px for e in scene.elements] or [0])})
    return scene


def analyze(video: Path, start: int, end: int, out_root: Path, options: AnalyzeOptions | None = None,
            ocr: Ocr | None = None) -> Project:
    opts = options or AnalyzeOptions()
    out_root = Path(out_root)
    log.info("pipeline start video=%s range=[%s,%s] out=%s", video, start, end, out_root)
    sd = _scene_dir(out_root, "s1")
    (sd / "stages").mkdir(parents=True, exist_ok=True)
    report_stage("frames")
    frames, fps = read_frames(video, start, end)
    np.save(sd / "stages" / "frames.npy", frames)
    n, H, W = frames.shape[:3]
    log.info("frames n=%s size=%sx%s fps=%s", n, W, H, fps)
    report_stage("background")
    bg, bconf = (_rgb(opts.bg_override), 1.0) if opts.bg_override else estimate_background(frames)
    (sd / "stages" / "background.json").write_text(json.dumps({"rgb": list(bg), "confidence": bconf}))
    log.info("background rgb=%s confidence=%s", list(bg), bconf)
    report_stage("text")
    boxes, text_tracks, msg = _stage_text(frames, bg, opts, ocr, sd)
    report_stage("regions")
    rbf = _stage_regions(frames, bg, boxes, opts, sd)
    report_stage("tracking")
    obj_tracks = _stage_tracking(rbf, sd)
    report_stage("sprites")
    props = _stage_sprites(frames, bg, text_tracks, obj_tracks, opts, sd, n)
    ids: dict = {}
    report_stage("keyframes")
    elements, raws = _elements_from_props(props, sd, ids)
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    scene = Scene(id="s1", size=(W, H), fps=fps, frames=n, background=Background(kind="color", value=_hex(bg), confidence=bconf), elements=elements)
    messages = [m for m in (msg, props.get("_message")) if m]
    scene = _finish(sd, scene, frames, raws, messages)
    (sd / "stages" / "options.json").write_text(json.dumps(asdict(opts)))
    project = init_project(out_root, {"file": str(video), "fps": fps, "size": [W, H], "mode": "range", "range": [start, end]}, scene)
    log.info("pipeline done scene=%s elements=%s frames=%s", scene.id, len(scene.elements), n)
    return project


def rerun(root: Path, scene_id: str, from_stage: str, note: str, options: AnalyzeOptions | None = None) -> Version:
    if from_stage not in STAGES:
        raise ValueError(from_stage)
    log.info("rerun scene=%s from=%s note=%s", scene_id, from_stage, note)
    root = Path(root); sd = _scene_dir(root, scene_id)
    opts = options or AnalyzeOptions(**json.loads((sd / "stages" / "options.json").read_text()))
    frames = np.load(sd / "stages" / "frames.npy")
    bgj = json.loads((sd / "stages" / "background.json").read_text()); bg = tuple(bgj["rgb"])
    n = len(frames)
    i = STAGES.index(from_stage)
    text = _pk(sd, "text"); boxes, text_tracks = text["boxes"], text["tracks"]
    if i <= STAGES.index("text"):
        report_stage("text")
        boxes, text_tracks, _ = _stage_text(frames, bg, opts, None, sd)
    if i <= STAGES.index("regions"):
        report_stage("regions")
        rbf = _stage_regions(frames, bg, boxes, opts, sd)
    else:
        rbf = _pk(sd, "regions")
    if i <= STAGES.index("tracking"):
        report_stage("tracking")
        obj_tracks = _stage_tracking(rbf, sd)
    else:
        obj_tracks = _pk(sd, "tracks")
    if i <= STAGES.index("sprites"):
        report_stage("sprites")
        props = _stage_sprites(frames, bg, text_tracks, obj_tracks, opts, sd, n)
    else:
        props = _pk(sd, "props")
    ids = json.loads((sd / "stages" / "ids.json").read_text())
    ids.update(_load_overrides(sd).get("ids", {}))
    report_stage("keyframes")
    elements, raws = _elements_from_props(props, sd, ids)
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    prev, _ = current_scene(root, scene_id)
    for e in elements:   # keep manual text edits across reruns
        try:
            old = prev.element(e.id)
            if old.provenance == "manual" and old.kind == "text":
                e.canonical.text, e.canonical.font, e.provenance = old.canonical.text, old.canonical.font, "manual"
        except KeyError:
            pass
    scene = Scene(id=scene_id, size=prev.size, fps=prev.fps, frames=n, background=prev.background, elements=elements)
    scene = _finish(sd, scene, frames, raws, [m for m in [props.get("_message")] if m])
    return new_version(root, scene_id, scene, note=note, auto=False)
