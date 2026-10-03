from __future__ import annotations
import json, pickle, os, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import Background, Canonical, Element, Keyframe, Project, Scene, Track, UIModel, Version
from ..ir.store import current_scene, init_project_scenes, load_project, new_version, _save_project, scene_dir as _scene_dir
from ..review.overlay import snapshot_from_stages
from ..log import get
from ..progress import STAGES, report_stage
from .background import estimate_background, foreground_mask
from .constraints import DEFAULT_KEEP_PRESET, apply_keep_preset, carry_keep, extract_constraints
from .keyframes import fill_gaps, tracks_from_raw
from .regions import build_palette, extract_regions
from .report import element_confidence, reconstruction_error, write_report
from .semantics import assign_roles, group_by_motion
from .sprites import RAW_COLS, sprite_props, z_order
from .text import Ocr, apply_copy, ocr_frames, text_exclusion_mask, text_props, track_text
from .tracking import track_regions, _merge_adjacent_tracks, _trim_tail_crumbs
from .video import read_frames
from ..assets import AssetAPIError, AssetClient

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
    t0 = time.perf_counter()
    workers = max(1, min(8, os.cpu_count() or 4))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        text_futs = {
            ex.submit(text_props, t, frames, bg, n_frames, 0): t for t in text_tracks
        }
        for fut, t in text_futs.items():
            raw, canon, cf, font, color = fut.result()
            props[f"t{t.id}"] = {"raw": raw, "canon": canon, "cf": cf, "kind": "text", "text": t.text, "font": font, "color": color, "first": t.first, "last": t.last}
    log.info("sprites text_props tracks=%s %.2fs", len(text_tracks), time.perf_counter() - t0)
    t0 = time.perf_counter()
    z = z_order(obj_tracks, frames, bg)
    log.info("sprites z_order objects=%s %.2fs", len(obj_tracks), time.perf_counter() - t0)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        # cv2 (moments, findTransformECC) releases the GIL, so tracks refine in parallel.
        obj_futs = {
            ex.submit(sprite_props, t, frames, bg, n_frames, 0, opts.use_ecc): t for t in obj_tracks
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
                                        iters=opts.refine_iters, device=dev)
                log.info("refine done device=%s sprites=%s frames=%s %.2fs", dev, len(keys), n,
                         time.perf_counter() - t0)
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


def _finish(sd: Path, scene: Scene, frames: np.ndarray, raws: dict, messages: list[str], previous: Scene | None = None) -> Scene:
    report_stage("semantics")
    assign_roles(scene.elements)
    scene.groups = group_by_motion(scene.elements, raws)
    report_stage("constraints")
    scene.constraints = apply_keep_preset(extract_constraints(scene), DEFAULT_KEEP_PRESET)
    if previous is not None:
        scene.constraints = carry_keep(scene.constraints, previous.constraints)
    report_stage("report")
    rec = reconstruction_error(scene, sd, frames, 0)
    conf = element_confidence(scene, sd, frames, 0)
    for e in scene.elements:
        e.confidence = conf[e.id]
    write_report(sd, {"reconstruction": rec, "confidence": conf, "messages": messages,
                      "elements": len(scene.elements), "fit_error_max_px": max([e.fit_error.max_px for e in scene.elements] or [0])})
    return scene


def _parse_ui(frames: np.ndarray, scene: Scene, sd: Path) -> Scene:
    if not os.environ.get("KEEPFRAME_ASSET_API_URL"):
        return scene
    change = np.mean(np.abs(frames[1:].astype(np.int16) - frames[:-1].astype(np.int16)), axis=(1, 2, 3)) if len(frames) > 1 else np.array([])
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


def analyze_scene_frames(
    frames: np.ndarray,
    fps: float,
    out_root: Path,
    scene_id: str,
    options: AnalyzeOptions | None = None,
    ocr: Ocr | None = None,
) -> Scene:
    opts = options or AnalyzeOptions()
    out_root = Path(out_root)
    existing = load_project(out_root) if (out_root / "project.json").exists() else None
    sd = _scene_dir(out_root, scene_id)
    (sd / "stages").mkdir(parents=True, exist_ok=True)
    overrides = sd / "stages" / "overrides.json"
    if not overrides.exists():
        overrides.write_text(json.dumps({"regions": [], "ids": {}, "merge": []}))
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
    ids_path = sd / "stages" / "ids.json"
    ids: dict = json.loads(ids_path.read_text()) if existing and ids_path.exists() else {}
    report_stage("keyframes")
    elements, raws = _elements_from_props(props, sd, ids)
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    scene = Scene(id=scene_id, size=(W, H), fps=fps, frames=n, background=Background(kind="color", value=_hex(bg), confidence=bconf), elements=elements)
    messages = [m for m in (msg, props.get("_message")) if m]
    scene = _finish(sd, scene, frames, raws, messages)
    try:
        scene = _parse_ui(frames, scene, sd)
    except AssetAPIError as exc:
        log.warning("UI parse skipped scene=%s code=%s", scene_id, exc.code)
    (sd / "stages" / "options.json").write_text(json.dumps(asdict(opts)))
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
) -> Project:
    opts = options or AnalyzeOptions()
    out_root = Path(out_root)
    log.info("pipeline start video=%s range=[%s,%s] out=%s", video, start, end, out_root)
    existing = load_project(out_root) if (out_root / "project.json").exists() else None
    frames, fps = read_frames(video, start, end)
    layout = scenes or [{"id": "s1", "frames": [start, end]}]
    by_pair = {(item.get("from"), item.get("to")): item for item in transitions or []}
    analyzed: list[Scene] = []
    stored: list[tuple[Scene, tuple[int, int], dict | None]] = []
    for index, item in enumerate(layout):
        scene_id = str(item.get("id") or f"s{index + 1}")
        first, last = int(item["frames"][0]), int(item["frames"][1])
        local_first, local_last = first - start, last - start
        if local_first < 0 or local_last >= len(frames) or local_first > local_last:
            raise ValueError("scene range is outside analyzed frames")
        scene = analyze_scene_frames(frames[local_first : local_last + 1], fps, out_root, scene_id, opts, ocr)
        analyzed.append(scene)
        next_id = str(layout[index + 1].get("id") or f"s{index + 2}") if index + 1 < len(layout) else None
        transition = by_pair.get((scene_id, next_id)) if next_id else None
        stored.append((scene, (first, last), transition))
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


def rerun(root: Path, scene_id: str, from_stage: str, note: str, options: AnalyzeOptions | None = None) -> Version:
    if from_stage not in STAGES:
        raise ValueError(from_stage)
    log.info("rerun scene=%s from=%s note=%s", scene_id, from_stage, note)
    root = Path(root); sd = _scene_dir(root, scene_id)
    project = load_project(root)
    prev, _ = current_scene(root, scene_id)
    opts = options or AnalyzeOptions(**json.loads((sd / "stages" / "options.json").read_text()))
    boundary = STAGES.index(from_stage)

    if boundary <= STAGES.index("frames"):
        source = Path(project.source["file"])
        ref = next((item for item in project.scenes if item.id == scene_id), None)
        start, end = ref.frames if ref is not None else project.source.get("range", [0, None])
        frames, fps = read_frames(source, start, end)
        np.save(sd / "stages" / "frames.npy", frames)
    else:
        frames = np.load(sd / "stages" / "frames.npy")
        fps = prev.fps
    n, H, W = frames.shape[:3]

    if boundary <= STAGES.index("background"):
        bg, bconf = (_rgb(opts.bg_override), 1.0) if opts.bg_override else estimate_background(frames)
        (sd / "stages" / "background.json").write_text(json.dumps({"rgb": list(bg), "confidence": bconf}))
    else:
        bgj = json.loads((sd / "stages" / "background.json").read_text())
        bg, bconf = tuple(bgj["rgb"]), bgj.get("confidence", 1.0)

    if boundary <= STAGES.index("text"):
        report_stage("text")
        boxes, text_tracks, _ = _stage_text(frames, bg, opts, None, sd)
    else:
        text = _pk(sd, "text")
        boxes, text_tracks = text["boxes"], text["tracks"]
    if boundary <= STAGES.index("regions"):
        report_stage("regions")
        rbf = _stage_regions(frames, bg, boxes, opts, sd)
    else:
        rbf = _pk(sd, "regions")
    if boundary <= STAGES.index("tracking"):
        report_stage("tracking")
        obj_tracks = _stage_tracking(rbf, sd)
    else:
        obj_tracks = _pk(sd, "tracks")
    if boundary <= STAGES.index("sprites"):
        report_stage("sprites")
        props = _stage_sprites(frames, bg, text_tracks, obj_tracks, opts, sd, n)
    else:
        props = _pk(sd, "props")
    ids = json.loads((sd / "stages" / "ids.json").read_text())
    ids.update(_load_overrides(sd).get("ids", {}))
    report_stage("keyframes")
    elements, raws = _elements_from_props(props, sd, ids)
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    for e in elements:   # keep manual text edits across reruns
        try:
            old = prev.element(e.id)
            if old.provenance == "manual" and old.kind == "text":
                e.canonical.text, e.canonical.font, e.provenance = old.canonical.text, old.canonical.font, "manual"
        except KeyError:
            pass
    scene = Scene(id=scene_id, size=(W, H), fps=fps, frames=n,
                  background=Background(kind="color", value=_hex(bg), confidence=bconf), elements=elements)
    scene = _finish(sd, scene, frames, raws, [m for m in [props.get("_message")] if m], previous=prev)
    return new_version(root, scene_id, scene, note=note, auto=False, analysis_file=snapshot_from_stages(sd, scene))
