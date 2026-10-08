from __future__ import annotations
import json, pickle, os, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import Background, Canonical, DEFAULTS, Element, Keyframe, PROPS, Project, Scene, Track, UIModel, Version
from ..ir.store import current_scene, init_project_scenes, load_project, new_version, _save_project, scene_dir as _scene_dir
from ..review.overlay import snapshot_from_stages
from ..log import get
from ..progress import STAGES, report_stage
from .background import PLATE_PATH, background_plate, estimate_background, foreground_mask, foreground_mask_plate, needs_plate, rgb_to_lab
from .captions import MAX_TILES, caption_scene
from .constraints import DEFAULT_KEEP_PRESET, apply_keep_preset, carry_keep, extract_constraints
from .keyframes import fill_gaps, tracks_from_raw
from .regions import build_palette, extract_regions, merge_adjacent_regions
from .report import write_report
from .semantics import assign_roles, group_by_motion
from .sprites import sprite_props, z_order
from .solids import find_solids, solid_props
from .text import Ocr, apply_copy, merge_reveals, ocr_frames, reveal_exclusion_boxes, split_junk_text, text_exclusion_mask, text_props, track_text
from .tracking import track_regions, _merge_adjacent_tracks, _trim_tail_crumbs
from .video import read_frames
from ..assets import AssetClient

log = get("keepframe.analyze")
try:
    import resource
except ImportError:   # Windows
    resource = None


def _rss_mb() -> tuple[float, float]:
    """Current and peak resident set size in MB."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 if resource else 0.0   # KiB on Linux
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2 ** 20, peak
    except (OSError, ValueError):
        return peak, peak


def _stage_done(name: str, t0: float) -> float:
    rss, peak = _rss_mb()
    now = time.perf_counter()
    log.info("stage %s done %.1fs rss %.0f MB peak %.0f MB", name, now - t0, rss, peak)
    return now


@dataclass
class AnalyzeOptions:
    bg_override: str | None = None
    copy: list[str] | None = None
    ocr: bool = True
    refine: bool = True
    refine_iters: int = 200
    min_area: int = 30
    use_ecc: bool = True
    ui: bool = False
    ocr_max_side: int | None = None
    generate_3d: bool = True


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
        obj = pickle.loads(p.read_bytes())
        if name != "solids":
            return obj
        changed = False
        for solid in obj:
            for f, (box, mask) in solid.frames.items():
                x0, y0, x1, y1 = box
                if mask.shape != (y1 - y0, x1 - x0):
                    solid.frames[f] = (box, mask[y0:y1, x0:x1].copy())
                    changed = True
        if not changed:
            return obj
    p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(pickle.dumps(obj)); return obj


def _stage_background(frames, opts, sd):
    bg, bconf = (_rgb(opts.bg_override), 1.0) if opts.bg_override else estimate_background(frames)
    plate = None
    if not opts.bg_override:
        candidate = background_plate(frames)
        if needs_plate(candidate, bg, bconf):
            plate = candidate
            path = sd / PLATE_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), cv2.cvtColor(plate, cv2.COLOR_RGB2BGR))
            bg = tuple(int(v) for v in plate.reshape(-1, 3).mean(0))   # Representative cache colour; measurements/refine use the local plate.
    (sd / "stages" / "background.json").write_text(json.dumps({"rgb": list(bg), "confidence": bconf, "plate": plate is not None}))
    return bg, bconf, plate


def _stage_text(frames, bg, opts, ocr, sd):
    boxes = [[] for _ in frames]; tracks = []; shape_tracks = []; msg = None
    if opts.ocr:
        if ocr is None:
            try:
                from .text import RapidOcr
                ocr = RapidOcr(max_side=opts.ocr_max_side)
            except Exception as e:  # rapidocr missing or broken onnxruntime
                msg = f"text stage skipped: {e}"
                err = str(e)
                if "No module named 'rapidocr_onnxruntime'" in err:
                    msg += "; pip install -e '.[ocr]' (same python as keepframe)"
                elif "GraphOptimizationLevel" in err:
                    msg += "; pip uninstall -y onnxruntime onnxruntime-gpu && pip install 'onnxruntime-gpu>=1.19,<1.27'"
                log.info("%s", msg)
        if ocr is not None:
            boxes = ocr_frames(frames, ocr)
            tracks = merge_reveals(track_text(boxes))
            if opts.copy:
                apply_copy(tracks, opts.copy)
            tracks, shape_tracks, n = split_junk_text(tracks)
            if n:
                msg = f"text tracks reclassified as shapes: {n}"
                log.info("%s", msg)
            log.info("text boxes=%s tracks=%s shapes=%s", sum(len(b) for b in boxes), len(tracks), len(shape_tracks))
    _pk(sd, "text", {"boxes": boxes, "tracks": tracks, "shape_tracks": shape_tracks, "message": msg})
    return boxes, tracks, shape_tracks, msg


def _stage_regions(frames, bg, boxes, opts, sd, plate=None, text_tracks=None, shape_tracks=None):
    reveal_boxes = reveal_exclusion_boxes((text_tracks or []) + (shape_tracks or []))
    plate_lab = rgb_to_lab(plate) if plate is not None else None
    fg = np.empty(frames.shape[:3], bool)   # filled in place: a list + np.stack held the masks twice
    for i, f in enumerate(frames):
        fg[i] = foreground_mask_plate(f, plate, plate_lab=plate_lab) if plate is not None else foreground_mask(f, bg)
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
        rbf.append(merge_adjacent_regions(i, frames[i], extract_regions(i, frames[i], fg[i], pal, min_area=opts.min_area, exclude_mask=text_exclusion_mask(boxes[i], fg[i].shape, extra_boxes=reveal_boxes.get(i, [])),
                                   overrides=ov_by_frame.get(i))))
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


def _stage_solids(frames, bg, boxes, obj_tracks, sd, plate=None):
    plate_lab = rgb_to_lab(plate) if plate is not None else None
    fg = (foreground_mask_plate(f, plate, plate_lab=plate_lab) if plate is not None else foreground_mask(f, bg) for f in frames)
    text_masks = (text_exclusion_mask(b, frames.shape[1:3]) for b in boxes)
    return _pk(sd, "solids", find_solids(frames, fg, obj_tracks, text_masks))


def _stage_sprites(frames, bg, text_tracks, shape_tracks, obj_tracks, opts, sd, n_frames, plate=None, solids=()):
    props = {}   # object_key -> dict(raw, canon, cf, kind, font, color)
    t0 = time.perf_counter()
    workers = max(1, min(8, os.cpu_count() or 4))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        text_futs = {
            ex.submit(text_props, t, frames, bg, n_frames, 0, infer_font=is_text, plate=plate): (t, is_text)
            for tracks, is_text in ((text_tracks, True), (shape_tracks, False)) for t in tracks
        }
        for fut, (t, is_text) in text_futs.items():
            raw, canon, cf, font, color = fut.result()
            p = {"raw": raw, "canon": canon, "cf": cf, "kind": "text" if is_text else "sprite", "first": t.first, "last": t.last}
            if is_text:
                p.update(text=t.text, font=font, color=color)
            else:
                p["z"] = 0
            props[f"{'t' if is_text else 's'}{t.id}"] = p
    log.info("sprites text_props tracks=%s shapes=%s %.2fs", len(text_tracks), len(shape_tracks), time.perf_counter() - t0)
    t0 = time.perf_counter()
    z = z_order(obj_tracks, frames, bg)
    log.info("sprites z_order objects=%s %.2fs", len(obj_tracks), time.perf_counter() - t0)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        # cv2 (moments, findTransformECC) releases the GIL, so tracks refine in parallel.
        obj_futs = {
            ex.submit(sprite_props, t, frames, bg, n_frames, 0, opts.use_ecc, plate=plate): t for t in obj_tracks
        }
        for fut, t in obj_futs.items():
            raw, canon, cf = fut.result()
            props[f"o{t.id}"] = {"raw": raw, "canon": canon, "cf": cf, "kind": "sprite", "z": z[t.id], "first": t.first, "last": t.last}
    log.info("sprites sprite_props objects=%s workers=%s ecc=%s %.2fs", len(obj_tracks), workers, opts.use_ecc,
             time.perf_counter() - t0)
    ov = _load_overrides(sd)
    for a, b in ov.get("merge", []):   # merge object b into a (element ids resolved via ids.json at correction time)
        if a in props and b in props:
            pa, pb = props[a], props[b]
            width = max(pa["raw"].shape[1], pb["raw"].shape[1])
            for p in (pa, pb):
                raw = p["raw"]
                if raw.shape[1] < width:
                    expanded = np.full((len(raw), width), np.nan)
                    expanded[:, :raw.shape[1]] = raw
                    expanded[~np.isnan(raw[:, 0]), raw.shape[1]:] = [DEFAULTS[prop] for prop in PROPS[raw.shape[1]:width]]
                    p["raw"] = expanded
            m = np.isnan(pa["raw"][:, 0]) & ~np.isnan(pb["raw"][:, 0])
            pa["raw"][m] = pb["raw"][m]; pa["first"] = min(pa["first"], pb["first"]); pa["last"] = max(pa["last"], pb["last"])
            del props[b]
    if opts.refine and any(p["kind"] == "sprite" for p in props.values()):
        try:
            from .device import resolve_device
            from .refine import refine_affine, torch_available
            if torch_available():
                import gc
                gc.collect()
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except ImportError:
                    pass
                keys = [k for k, p in props.items() if p["kind"] == "sprite"]
                dev = resolve_device()
                n, hh, ww = frames.shape[:3]
                log.info("refine start device=%s sprites=%s frames=%s %sx%s iters=%s",
                         dev, len(keys), n, hh, ww, opts.refine_iters)
                report_stage("sprites", f"refine {len(keys)} sprites {dev}")
                t0 = time.perf_counter()
                refined = refine_affine(frames, bg, {k: props[k]["raw"] for k in keys}, {k: props[k]["canon"] for k in keys},
                                        {k: (0.5, 0.5) for k in keys}, {k: props[k]["z"] for k in keys},
                                        iters=opts.refine_iters, device=dev, plate=plate)
                log.info("refine done device=%s sprites=%s frames=%s %.2fs", dev, len(keys), n,
                         time.perf_counter() - t0)
                for k in keys:
                    props[k]["raw"] = refined[k]
            else:
                log.info("refine skipped: torch not installed")
        except Exception as e:
            log.error("refine skipped: %s", e)
            props["_message"] = f"refine skipped: {e}"
    for i, solid in enumerate(solids):
        p = solid_props(solid, frames)
        p["fragments"] = {k: props.pop(k) for mid in solid.members if (k := f"o{mid}") in props}
        p["z"] = max((v.get("z", 0) for v in p["fragments"].values()), default=0)
        props[f"solid{i + 1}"] = p
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
        np.savez_compressed(sd / "assets" / f"{eid}_raw.npz", raw=raw_full, first=first, cols=np.array(PROPS[:raw_full.shape[1]]))
        h, w = p["canon"].shape[:2]
        canonical = Canonical(width=w, height=h, texture=f"assets/{eid}.png",
                              text=p.get("text"), font=p.get("font"), color=p.get("color"))
        elements.append(Element(id=eid, kind=p["kind"], canonical=canonical, visible=(first, last), tracks=tracks,
                                z=Track(keys=[Keyframe(t=0, v=p.get("z", 0))]), raw=f"assets/{eid}_raw.npz", fit_error=fe,
                                pending_asset=p.get("pending_asset")))
        raws[eid] = raw_full
    return elements, raws


def _finish(sd: Path, scene: Scene, frames: np.ndarray, raws: dict, messages: list[str], previous: Scene | None = None, captioner=None,
            props=None, ids=None, generate_3d=True, generation_skipped=None, kf_secs=0.0) -> Scene:
    from .solid_assets import finish_solid_assets
    t = time.perf_counter() - kf_secs
    solids_report = finish_solid_assets(scene, sd, frames, props, ids, raws, messages, generate=generate_3d,
                                        previous=previous, generation_skipped=generation_skipped) if props is not None else []
    t = _stage_done("keyframes", t)
    report_stage("semantics")
    assign_roles(scene.elements)
    if captioner is not None:
        try:
            count = caption_scene(scene, frames, captioner)
            log.info("captions elements=%s", count)
            sent = min(len(scene.elements), MAX_TILES)
            if count == 0 and scene.elements:
                messages.append("captions skipped: no usable captions")
            elif 0 < count < sent:
                messages.append(f"captions partial: {count}/{sent}")
        except Exception as e:  # captions are optional suggestions; analysis never fails on them
            messages.append(f"captions skipped: {type(e).__name__}: {e}"[:200])
    scene.groups = [] if scene.ui is not None else group_by_motion(scene.elements, raws)  # ponytail: parsed UI has no measured motion tracks.
    t = _stage_done("semantics", t)
    report_stage("constraints")
    scene.constraints = apply_keep_preset(extract_constraints(scene), DEFAULT_KEEP_PRESET)
    if previous is not None:
        scene.constraints = carry_keep(scene.constraints, previous.constraints)
    t = _stage_done("constraints", t)
    report_stage("report")
    from .report import reconstruction_and_confidence
    rec, conf = reconstruction_and_confidence(scene, sd, frames, 0)
    for e in scene.elements:
        e.confidence = conf[e.id]
    write_report(sd, {"reconstruction": rec, "confidence": conf, "messages": messages,
                      "solids": solids_report, "elements": len(scene.elements), "fit_error_max_px": max([e.fit_error.max_px for e in scene.elements] or [0])})
    _stage_done("report", t)
    return scene


def _parse_ui(frames: np.ndarray, scene: Scene, sd: Path) -> Scene:
    if not os.environ.get("KEEPFRAME_ASSET_API_URL"):
        return scene
    # Per frame pair, not two int16 copies of the stack; integer sums are exact in float64, so the means match.
    change = np.array([np.mean(np.abs(b.astype(np.int16) - a.astype(np.int16))) for a, b in zip(frames, frames[1:])])
    picks = [0, *([int(np.argmax(change)) + 1] if change.size else []), len(frames) - 1]
    selected = [frames[index] for index in dict.fromkeys(picks)]
    sheet = np.concatenate(selected, axis=1)
    ok, encoded = cv2.imencode(".png", cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR))
    if not ok:
        return scene
    response = AssetClient().request(
        task="parse",
        kind="ui",
        prompt=f"Parse these representative/state frames as keepframe.ui/1. Each tile is {scene.size[0]}x{scene.size[1]}; bbox coordinates must be relative to one tile. Preserve state frame ranges 0..{scene.frames - 1}.",
        input_image=encoded.tobytes(),
        size={"width": scene.size[0], "height": scene.size[1]},
    )
    ui = response.ui
    if ui is None or not ui.components:
        return scene
    height, width = frames[0].shape[:2]
    elements: list[Element] = []
    for index, component in enumerate(ui.components):
        x0, y0, x1, y1 = component.bbox
        x0, x1 = sorted((max(0, min(width, round(x0))), max(0, min(width, round(x1)))))
        y0, y1 = sorted((max(0, min(height, round(y0))), max(0, min(height, round(y1)))))
        if x1 <= x0 or y1 <= y0:
            continue
        texture = sd / "assets" / f"{component.id}.ui.png"
        texture.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(texture), cv2.cvtColor(frames[0, y0:y1, x0:x1], cv2.COLOR_RGB2BGRA))
        visible = (0, scene.frames - 1)
        if component.states:
            visible = (
                max(0, min(state.frames[0] for state in component.states)),
                min(scene.frames - 1, max(state.frames[1] for state in component.states)),
            )
        elements.append(Element(
            id=component.id,
            kind="ui",
            canonical=Canonical(width=x1 - x0, height=y1 - y0, texture=f"assets/{texture.name}"),
            visible=visible,
            tracks={
                "x": Track(keys=[Keyframe(t=0, v=(x0 + x1) / 2)]),
                "y": Track(keys=[Keyframe(t=0, v=(y0 + y1) / 2)]),
            },
            z=Track(keys=[Keyframe(t=0, v=index)]),
        ))
    if not elements:
        return scene
    parsed = scene.model_copy(update={"elements": elements, "ui": UIModel.model_validate(ui)})
    assign_roles(parsed.elements)
    parsed.constraints = apply_keep_preset(extract_constraints(parsed), DEFAULT_KEEP_PRESET)
    return parsed


def _apply_ui(frames: np.ndarray, scene: Scene, sd: Path, scene_id: str, messages: list[str] | None = None) -> Scene:
    try:
        return _parse_ui(frames, scene, sd)
    except Exception as exc:
        message = f"UI parse skipped: {type(exc).__name__}: {exc}"[:200]
        log.warning("%s scene=%s", message, scene_id)
        if messages is not None:
            messages.append(message)
        else:
            report = json.loads((sd / "report.json").read_text())
            report["messages"].append(message)
            write_report(sd, report)
    return scene


def analyze_scene_frames(
    frames: np.ndarray,
    fps: float,
    out_root: Path,
    scene_id: str,
    options: AnalyzeOptions | None = None,
    ocr: Ocr | None = None,
    captioner=None,
    *,
    _deferred_finish=None,
) -> Scene:
    opts = options or AnalyzeOptions()
    out_root = Path(out_root)
    existing = load_project(out_root) if (out_root / "project.json").exists() else None
    previous = current_scene(out_root, scene_id)[0] if existing and any(s.id == scene_id for s in existing.scenes) else None
    sd = _scene_dir(out_root, scene_id)
    from .solid_assets import index_legacy_solid_models
    index_legacy_solid_models(sd)
    (sd / "stages").mkdir(parents=True, exist_ok=True)
    overrides = sd / "stages" / "overrides.json"
    if not overrides.exists():
        overrides.write_text(json.dumps({"regions": [], "ids": {}, "merge": []}))
    np.save(sd / "stages" / "frames.npy", frames)
    n, H, W = frames.shape[:3]
    log.info("frames n=%s size=%sx%s fps=%s", n, W, H, fps)
    t = time.perf_counter()
    report_stage("background")
    bg, bconf, plate = _stage_background(frames, opts, sd)
    log.info("background rgb=%s confidence=%s", list(bg), bconf)
    t = _stage_done("background", t)
    report_stage("text")
    boxes, text_tracks, shape_tracks, msg = _stage_text(frames, bg, opts, ocr, sd)
    t = _stage_done("text", t)
    report_stage("regions")
    rbf = _stage_regions(frames, bg, boxes, opts, sd, plate=plate, text_tracks=text_tracks, shape_tracks=shape_tracks)
    t = _stage_done("regions", t)
    report_stage("tracking")
    obj_tracks = _stage_tracking(rbf, sd)
    t = _stage_done("tracking", t)
    report_stage("solids")
    solids = _stage_solids(frames, bg, boxes, obj_tracks, sd, plate=plate)
    t = _stage_done("solids", t)
    report_stage("sprites")
    props = _stage_sprites(frames, bg, text_tracks, shape_tracks, obj_tracks, opts, sd, n, plate=plate, solids=solids)
    t = _stage_done("sprites", t)
    ids_path = sd / "stages" / "ids.json"
    ids: dict = json.loads(ids_path.read_text()) if existing and ids_path.exists() else {}
    report_stage("keyframes")
    elements, raws = _elements_from_props(props, sd, ids)
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    scene = Scene(id=scene_id, size=(W, H), fps=fps, frames=n,
                  background=Background(kind="image", value=PLATE_PATH, confidence=bconf) if plate is not None else Background(kind="color", value=_hex(bg), confidence=bconf),
                  elements=elements)
    messages = [m for m in (msg, props.get("_message")) if m]
    messages.append(f"3D 후보 {len(solids)}개")
    if opts.ui:
        scene = _apply_ui(frames, scene, sd, scene_id, messages=messages)
    finish_args = dict(sd=sd, scene=scene, frames=frames, raws=raws, messages=messages, captioner=captioner,
                       props=props, ids=ids, generate_3d=opts.generate_3d, previous=previous, kf_secs=time.perf_counter() - t)
    if _deferred_finish is None:
        scene = _finish(**finish_args)
    else:
        _deferred_finish.append(finish_args)
    (sd / "stages" / "options.json").write_text(json.dumps(asdict(opts)))
    if _deferred_finish is None:
        log.info("pipeline done scene=%s elements=%s frames=%s", scene.id, len(scene.elements), n)
    return scene


def _normalized_text(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def _dhash(path: Path) -> int | None:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    small = cv2.resize(image, (9, 8), interpolation=cv2.INTER_AREA)
    bits = small[:, 1:] > small[:, :-1]
    value = 0
    for bit in bits.flat:
        value = (value << 1) | int(bit)
    return value


def link_adjacent_scenes(scenes: list[Scene], root: Path) -> list[dict]:
    links: list[dict] = []
    for left, right in zip(scenes, scenes[1:]):
        candidates: list[tuple[int, str, Element, Element]] = []
        for source in left.elements:
            for target in right.elements:
                source_text = _normalized_text(source.canonical.text)
                target_text = _normalized_text(target.canonical.text)
                if source_text and source_text == target_text:
                    candidates.append((0, "text", source, target))
                    continue
                source_texture, target_texture = source.canonical.texture, target.canonical.texture
                if not source_texture or not target_texture:
                    continue
                source_ratio = source.canonical.width / max(source.canonical.height, 1e-6)
                target_ratio = target.canonical.width / max(target.canonical.height, 1e-6)
                if abs(source_ratio - target_ratio) / max(source_ratio, target_ratio, 1e-6) > 0.1:
                    continue
                first = _dhash(_scene_dir(root, left.id) / source_texture)
                second = _dhash(_scene_dir(root, right.id) / target_texture)
                if first is not None and second is not None:
                    distance = (first ^ second).bit_count()
                    if distance <= 5:
                        candidates.append((distance, "dhash", source, target))
        used_left: set[str] = set()
        used_right: set[str] = set()
        for distance, reason, source, target in sorted(candidates, key=lambda row: (row[0], row[2].id, row[3].id)):
            if source.id in used_left or target.id in used_right:
                continue
            used_left.add(source.id)
            used_right.add(target.id)
            links.append(
                {
                    "from": {"scene": left.id, "element": source.id},
                    "to": {"scene": right.id, "element": target.id},
                    "reason": reason,
                    "distance": distance,
                }
            )
    return links


def analyze(
    video: Path,
    start: int,
    end: int,
    out_root: Path,
    options: AnalyzeOptions | None = None,
    ocr: Ocr | None = None,
    *,
    scenes: list[dict] | None = None,
    transitions: list[dict] | None = None,
    mode: str = "range",
    captioner=None,
) -> Project:
    opts = options or AnalyzeOptions()
    out_root = Path(out_root)
    log.info("pipeline start video=%s range=[%s,%s] out=%s", video, start, end, out_root)
    existing = load_project(out_root) if (out_root / "project.json").exists() else None
    t = time.perf_counter()
    frames, fps = read_frames(video, start, end)
    _stage_done("frames", t)
    layout = scenes or [{"id": "s1", "frames": [start, end]}]
    by_pair = {(item.get("from"), item.get("to")): item for item in transitions or []}
    analyzed: list[Scene] = []
    stored: list[tuple[Scene, tuple[int, int], dict | None]] = []
    deferred_finish = []
    for index, item in enumerate(layout):
        scene_id = str(item.get("id") or f"s{index + 1}")
        first, last = int(item["frames"][0]), int(item["frames"][1])
        local_first, local_last = first - start, last - start
        if local_first < 0 or local_last >= len(frames) or local_first > local_last:
            raise ValueError("scene range is outside analyzed frames")
        scene = analyze_scene_frames(frames[local_first : local_last + 1], fps, out_root, scene_id, opts, ocr,
                                     captioner=captioner, _deferred_finish=deferred_finish)
        analyzed.append(scene)
        next_id = str(layout[index + 1].get("id") or f"s{index + 2}") if index + 1 < len(layout) else None
        transition = by_pair.get((scene_id, next_id)) if next_id else None
        stored.append((scene, (first, last), transition))
    from .solid_assets import prepare_solid_assets
    skipped = prepare_solid_assets(deferred_finish, generate=opts.generate_3d)
    for job, pending in zip(deferred_finish, skipped):
        finished = _finish(**job, generation_skipped=pending)
        log.info("pipeline done scene=%s elements=%s frames=%s", finished.id, len(finished.elements), finished.frames)
    height, width = frames.shape[1:3]
    links = link_adjacent_scenes(analyzed, out_root)
    analysis_files = {scene.id: snapshot_from_stages(_scene_dir(out_root, scene.id), scene) for scene in analyzed}
    source = {"file": str(video), "fps": fps, "size": [width, height], "mode": mode, "range": [start, end] if mode == "range" else None}
    if existing:
        from ..ir.schema import SceneRef
        for scene in analyzed:
            new_version(out_root, scene.id, scene, note="reanalysis", analysis_file=analysis_files[scene.id])
        project = load_project(out_root)
        project.source.update(source)
        project.scenes = [SceneRef(id=scene.id, frames=frame_range, transition_out=transition) for scene, frame_range, transition in stored]
        project.links = links
        project.approved_scenes = {}
        _save_project(out_root, project)
    else:
        project = init_project_scenes(out_root, source, stored, links=links, analysis_files=analysis_files)
    return project


def rerun(root: Path, scene_id: str, from_stage: str, note: str, options: AnalyzeOptions | None = None, captioner=None) -> Version:
    if from_stage not in STAGES:
        raise ValueError(from_stage)
    log.info("rerun scene=%s from=%s note=%s", scene_id, from_stage, note)
    root = Path(root); sd = _scene_dir(root, scene_id)
    project = load_project(root)
    prev, _ = current_scene(root, scene_id)
    opts = options or AnalyzeOptions(**json.loads((sd / "stages" / "options.json").read_text()))
    boundary = STAGES.index(from_stage)
    t = time.perf_counter()
    if boundary <= STAGES.index("frames"):
        source = Path(project.source["file"])
        ref = next((item for item in project.scenes if item.id == scene_id), None)
        start, end = ref.frames if ref is not None else project.source.get("range", [0, None])
        frames, fps = read_frames(source, start, end)
        np.save(sd / "stages" / "frames.npy", frames)
    else:
        frames = np.load(sd / "stages" / "frames.npy")
        fps = prev.fps
    from .solid_assets import index_legacy_solid_models
    index_legacy_solid_models(sd)
    n, H, W = frames.shape[:3]
    t = _stage_done("frames", t)
    plate = None
    if boundary <= STAGES.index("background"):
        bg, bconf, plate = _stage_background(frames, opts, sd)
    else:
        bgj = json.loads((sd / "stages" / "background.json").read_text())
        bg, bconf = tuple(bgj["rgb"]), bgj.get("confidence", 1.0)
        if bgj.get("plate", False):
            plate = cv2.imread(str(sd / PLATE_PATH), cv2.IMREAD_COLOR)
            if plate is None:
                raise FileNotFoundError(sd / PLATE_PATH)
            plate = cv2.cvtColor(plate, cv2.COLOR_BGR2RGB)
    t = _stage_done("background", t)
    msg = None
    if boundary <= STAGES.index("text"):
        report_stage("text")
        boxes, text_tracks, shape_tracks, msg = _stage_text(frames, bg, opts, None, sd)
    else:
        text = _pk(sd, "text")
        boxes, text_tracks = text["boxes"], text["tracks"]
        shape_tracks = text.get("shape_tracks", [])
    t = _stage_done("text", t)
    if boundary <= STAGES.index("regions"):
        report_stage("regions")
        rbf = _stage_regions(frames, bg, boxes, opts, sd, plate=plate, text_tracks=text_tracks, shape_tracks=shape_tracks)
    else:
        rbf = _pk(sd, "regions")
    t = _stage_done("regions", t)
    if boundary <= STAGES.index("tracking"):
        report_stage("tracking")
        obj_tracks = _stage_tracking(rbf, sd)
    else:
        obj_tracks = _pk(sd, "tracks")
    t = _stage_done("tracking", t)
    missing_solids = not (sd / "stages" / "solids.pkl").exists()
    if boundary <= STAGES.index("solids") or missing_solids:
        report_stage("solids")
        solids = _stage_solids(frames, bg, boxes, obj_tracks, sd, plate=plate)
    else:
        solids = _pk(sd, "solids")
    t = _stage_done("solids", t)
    if boundary <= STAGES.index("sprites") or missing_solids:
        report_stage("sprites")
        props = _stage_sprites(frames, bg, text_tracks, shape_tracks, obj_tracks, opts, sd, n, plate=plate, solids=solids)
    else:
        props = _pk(sd, "props")
    t = _stage_done("sprites", t)
    ids = json.loads((sd / "stages" / "ids.json").read_text())
    ids.update(_load_overrides(sd).get("ids", {}))
    report_stage("keyframes")
    elements, raws = _elements_from_props(props, sd, ids)
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    scene = Scene(id=scene_id, size=(W, H), fps=fps, frames=n,
                  background=Background(kind="image", value=PLATE_PATH, confidence=bconf) if plate is not None else Background(kind="color", value=_hex(bg), confidence=bconf),
                  elements=elements)
    messages = [m for m in (msg, props.get("_message")) if m]
    messages.append(f"3D 후보 {len(solids)}개")
    if opts.ui:
        scene = _apply_ui(frames, scene, sd, scene_id, messages=messages)
    for e in scene.elements:   # keep manual text edits across reruns
        try:
            old = prev.element(e.id)
            if old.provenance == "manual" and old.kind == "text":
                e.canonical.text, e.canonical.font, e.provenance = old.canonical.text, old.canonical.font, "manual"
            e.label, e.caption = old.label, old.caption
        except KeyError:
            pass
    scene = _finish(sd, scene, frames, raws, messages, previous=prev, captioner=captioner,
                    props=props, ids=ids, generate_3d=False, kf_secs=time.perf_counter() - t)
    return new_version(root, scene_id, scene, note=note, auto=False, analysis_file=snapshot_from_stages(sd, scene))
