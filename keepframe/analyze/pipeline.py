from __future__ import annotations
import json, pickle, os, shutil, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import FONT_FAMILY_RE, Canonical, DEFAULTS, Element, Keyframe, PROPS, Project, Scene, TextStyle, TextureMeta, Track, UIModel, Version
from ..ir.store import current_scene, init_project_scenes, load_project, new_version, _save_project, scene_dir as _scene_dir
from ..review.overlay import snapshot_from_stages
from ..log import describe, get, scrub_paths
from ..progress import STAGES, report_stage
from . import matting, textstyle
from .background import PASS1_PATH, background_plate, estimate_background, foreground_mask, foreground_mask_plate, needs_plate, pass1_plate, rgb_to_lab
from .captions import MAX_TILES, caption_scene
from .constraints import DEFAULT_KEEP_PRESET, apply_keep_preset, carry_keep, extract_constraints
from .keyframes import fill_gaps, tracks_from_raw
from . import videoasset
from .movers import mover_cover, mover_frames, mover_props, mover_video
from .plate import FALLBACK_CONF, FRAMES_PATH, MOVERS_FAILED, PLATE_FAILED, PlateModel, background_for, build_plate, busy_plate, fallback_plate, load_plate, save_plate, write_video
from .regions import build_palette, extract_regions, merge_adjacent_regions
from .report import write_report
from .semantics import assign_roles, group_by_motion
from .sprites import sprite_props, z_order
from .solids import find_solids, solid_props
from .text import FULL_OPACITY_FAILED, FULL_OPACITY_NOTE, Ocr, apply_copy, merge_reveals, ocr_frames, reveal_exclusion_boxes, split_junk_text, text_exclusion_mask, text_props, track_text
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
            path = sd / PASS1_PATH
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
                msg = "text stage skipped (ocr_failed)"
                err = str(e)
                if "No module named 'rapidocr_onnxruntime'" in err:
                    msg += "; pip install -e '.[ocr]' (same python as keepframe)"
                elif "GraphOptimizationLevel" in err:
                    msg += "; pip uninstall -y onnxruntime onnxruntime-gpu && pip install 'onnxruntime-gpu>=1.19,<1.27'"
                log.warning("%s: %s", msg, describe(e, trace=True))
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


def _stage_plate(frames, fps, rbf, boxes, text_tracks, shape_tracks, obj_tracks, bg, bconf, pass1, opts, sd,
                 busy: dict | None = None) -> PlateModel:
    """`busy`: the decision of the busy pass (`_plate_stage`) when the regions were found again against its
    gradient: keys are then tried whatever the rank-2 share, and its numbers stay in the stats."""
    (sd / FRAMES_PATH).unlink(missing_ok=True)   # a stage cache: only a video plate writes it again
    try:
        model = build_plate(frames, rbf, boxes, text_tracks, shape_tracks, bg_rgb=bg, bconf=bconf,
                            bg_override=opts.bg_override, pass1=pass1, obj_tracks=obj_tracks, frames_out=sd / FRAMES_PATH,
                            try_keys=busy is not None)
        if busy is not None:
            model.stats.update(busy_pass1=busy["busy_pass1"], moved_de=busy["moved_de"], resegmented=busy["kind"])
        model = write_video(sd, model, fps)
    except Exception as e:   # plate v2 must never fail the analysis: keep pass 1 at lower confidence
        log.warning("plate v2 failed: %s", describe(e, trace=True))
        model = fallback_plate(pass1, bg, bconf, frames.shape[1:3], f"plate v2 skipped ({PLATE_FAILED})")
    _pk(sd, "movers", model.movers)   # before plate.json, which marks the plate stage complete
    save_plate(sd, model)
    return model


BUSY_JSON = "stages/busy.json"


def _busy_decision(sd: Path) -> dict | None:
    """The busy pass's decision an earlier run stored with the regions it found again (None: pass 1's regions)."""
    try:
        d = json.loads((sd / BUSY_JSON).read_text())
        return d if d.get("kind") == "gradient" and {"busy_pass1", "moved_de"} <= d.keys() else None
    except (OSError, ValueError, AttributeError):
        return None


def _plate_stage(frames, fps, rbf, boxes, text_tracks, shape_tracks, obj_tracks, bg, bconf, pass1, opts, sd, fresh=True):
    """The plate stage: (model, rbf, obj_tracks). `fresh`: `rbf` are pass 1's. When pass 1 missed a gradient that
    keeps changing (busy_plate), the regions and tracks are found again against it first, and that decision is
    stored (BUSY_JSON) for reruns that reuse those regions (`fresh` False). Only a fitted gradient wins (R57);
    anything failing there keeps pass 1's regions and tracks."""
    path = sd / BUSY_JSON
    if not fresh:
        busy = _busy_decision(sd)
    else:
        busy = None
        path.unlink(missing_ok=True)
        try:
            moving = None if opts.bg_override else busy_plate(frames, rbf, boxes, text_tracks, shape_tracks, bconf=bconf)
        except Exception:   # fail soft: pass 1's regions
            log.exception("busy plate failed")
            moving = None
        if moving is not None:
            t0 = time.perf_counter()
            report_stage("regions")
            try:
                again = _stage_regions(frames, bg, boxes, opts, sd, plate_at=moving.at, text_tracks=text_tracks,
                                       shape_tracks=shape_tracks)
                tracks = _stage_tracking(again, sd)
                rbf, obj_tracks = again, tracks
                busy = {"kind": moving.kind, "busy_pass1": moving.stats["busy_pass1"], "moved_de": moving.stats["moved_de"]}
                path.write_text(json.dumps(busy))
                log.info("plate busy regions again %.2fs objects=%s", time.perf_counter() - t0, len(obj_tracks))
            except Exception:   # fail soft: pass 1's regions and tracks, in the caches too
                log.exception("busy plate regions failed; pass 1 regions kept")
                _pk(sd, "regions", rbf)
                _pk(sd, "tracks", obj_tracks)
            report_stage("plate")
    model = _stage_plate(frames, fps, rbf, boxes, text_tracks, shape_tracks, obj_tracks, bg, bconf, pass1, opts, sd, busy)
    return model, rbf, obj_tracks


def _moving(model: PlateModel):
    """The per-frame plate of a background that moves (video or animated gradient), else None."""
    return model.at if model.frames is not None or len(model.gradient_keys) > 1 else None


def _cached_plate(sd: Path) -> PlateModel | None:
    """The plate stage's cache (plate.json + movers.pkl), or None when it must be rebuilt."""
    model = load_plate(sd)
    try:
        model.movers = _pk(sd, "movers") if model is not None else None
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError):
        return None
    return model if model is not None and isinstance(model.movers, list) else None


def _unclaimed(obj_tracks, shape_tracks, movers):
    """The object and shape tracks no mover claimed as its fragments."""
    objs = {i for m in movers for i in m.claimed}
    shapes = {i for m in movers for i in m.claimed_shapes}
    return [t for t in obj_tracks if t.id not in objs], [t for t in shape_tracks if t.id not in shapes]


def _mover_messages(props: dict, ids: dict) -> list[str]:
    out = []
    for k, p in props.items():
        if k.startswith("_") or not p.get("mover") or k not in ids:
            continue
        if not p.get("stable") and not p.get("video"):
            code = p.get("video_error")
            out.append(f"{ids[k]} animates in place; kept as a still sprite" + (f" ({code})" if code else ""))
        if p.get("synthetic"):
            out.append(f"{ids[k]}: {p['synthetic']} px of its texture under text are filled in (synthetic)")
    return out


# Fail-soft codes of the sprites stage (R52): report messages and stage data carry these, never exception text.
MOVER_DROPPED, TEXTURES_FAILED = "mover_dropped", "textures_failed"
LOW_TEXTURE_CONF = 0.05   # a matted texture at or below this confidence is reported (it still replaces the binary one)


def _full_opacity_failed(p: dict) -> bool:
    note = p.get("fade_note")   # older stages kept the exception text in the note
    return bool(p.get("full_opacity_error")) or (isinstance(note, str) and note.startswith("full-opacity frame not found"))


def _texture_messages(props: dict, ids: dict) -> list[str]:
    out = []
    for k, p in props.items():
        if k.startswith("_") or k not in ids or not isinstance(p, dict):
            continue
        meta = p.get("texture_meta") or {}
        if p.get("texture_error"):   # fixed text: older stages stored the exception text
            out.append(f"{ids[k]}: matted texture failed ({matting.TEXTURE_FAILED}); kept the binary texture")
        elif p.get("texture_note"):
            out.append(f"{ids[k]}: not matted, {p['texture_note']}; kept the binary texture")
        elif p.get("mover") and not p.get("stable") and not p.get("video") and p.get("texture_meta"):
            out.append(f"{ids[k]}: still texture matted from frame {p['cf']} only")
        elif meta.get("method") not in (None, "binary") and meta.get("confidence", 1.0) <= LOW_TEXTURE_CONF:
            out.append(f"{ids[k]}: matted texture at low confidence ({meta['confidence']:.2f}); it replaced the binary texture")
        if p.get("style_error"):   # fixed text: the stored value is a code (exception text in older stages)
            out.append(f"{ids[k]}: text style failed; kept the core-mask colour")
        if _full_opacity_failed(p):
            out.append(f"{ids[k]}: {FULL_OPACITY_NOTE}")
        elif p.get("fade_note"):
            out.append(f"{ids[k]}: {p['fade_note']}")
        if p.get("font_error") or p.get("font_skipped"):
            family = getattr(p.get("font"), "family_guess", None)
            family = family if isinstance(family, str) and FONT_FAMILY_RE.fullmatch(family) else "sans-serif"
            out.append(f"{ids[k]}: font match failed; kept {family}" if p.get("font_error")
                       else f"{ids[k]}: font not matched (scene work cap reached); kept {family}")
    return [scrub_paths(m) for m in out]   # report messages reach the browser: no server paths


def _fell_back(p: dict) -> bool:
    return any(p.get(name) for name in ("video_error", "texture_error", "texture_note", "style_error", "font_error",
                                        "font_skipped", "mover_dropped")) or _full_opacity_failed(p)


def fallback_ids(props: dict, ids: dict) -> set[str]:
    """The elements a fail-soft path left at today's behaviour, whose confidence drops (once) by FALLBACK_CONF: a
    video that could not be made, a texture not matted, a text style or font match that failed or was skipped, a
    full-opacity frame not found, the fragments of a dropped mover (and a solid made of them); every element when the
    textures phase failed as a whole, every text when the text style phase did."""
    textures_failed, style_failed = bool(props.get("_textures_message")), bool(props.get("_style_message"))
    out = set()
    for k, p in props.items():
        if k.startswith("_") or not isinstance(p, dict):
            continue
        fragments = p.get("fragments") or {}
        out |= {ids[fk] for fk, fp in fragments.items() if fk in ids and isinstance(fp, dict) and _fell_back(fp)}
        if k in ids and (_fell_back(p) or any(isinstance(fp, dict) and fp.get("mover_dropped") for fp in fragments.values())
                         or (textures_failed and not k.startswith("solid")) or (style_failed and p.get("kind") == "text")):
            out.add(ids[k])
    return out


def _font_registry(sd: Path):
    """The project's fonts (uploads + bundled) for a scene directory `<root>/scenes/<id>`; bundled only elsewhere."""
    from ..fonts.registry import FontRegistry
    sd = Path(sd)
    try:
        return FontRegistry.for_project(sd.parent.parent if sd.parent.name == "scenes" else None)
    except Exception as e:   # an unreadable font index: bundled fonts only
        log.warning("project fonts unusable (%s); bundled fonts only", describe(e))
        return FontRegistry()


def _pin_font(font, sd: Path, registry):
    """An uploaded face the analysis chose, copied into the scene's flat assets (`assets/font-<sha16>.<ext>`, which
    render plans pin) and named by `FontGuess.file`; the guess unchanged otherwise or on any error."""
    if font is None or font.source != "uploaded":
        return font
    from ..fonts.upload import pin_face
    try:
        face = registry.face(font.family_guess, font.weight)
        asset = pin_face(sd, face)
    except Exception as e:   # fail soft: the composer still finds the family in the project's registry
        log.warning("uploaded font %s not copied into the scene: %s", font.family_guess, scrub_paths(e))
        return font
    if asset is None:
        return font
    static = face.weight_range[0] == face.weight_range[1]
    return font.model_copy(update={"file": asset, "source": "uploaded",
                                   "postscript": face.postscript if static else font.postscript})


def _plate_inputs(model: PlateModel):
    """(bg, plate) for the stages after the plate: a flat colour has no plate image."""
    return model.rgb, (model.image if model.kind != "color" else None)


def _stage_regions(frames, bg, boxes, opts, sd, plate=None, text_tracks=None, shape_tracks=None, plate_at=None):
    """`plate_at(f)`: a per-frame plate (a background that moves), instead of `plate`."""
    reveal_boxes = reveal_exclusion_boxes((text_tracks or []) + (shape_tracks or []))
    plate_lab = rgb_to_lab(plate) if plate is not None else None
    fg = np.empty(frames.shape[:3], bool)   # filled in place: a list + np.stack held the masks twice
    for i, f in enumerate(frames):
        if plate_at is not None:
            fg[i] = foreground_mask_plate(f, plate_at(i))
        else:
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


def _stage_solids(frames, bg, boxes, obj_tracks, sd, plate=None, movers=(), notes=None, plate_at=None, dropped=None):
    """Solids (3D candidates) from the tracks no mover claimed; mover pixels are layers already, never a 3D
    candidate too. Returns (solids, movers): when the movers cannot be applied they are dropped with a note (and
    added to `dropped`: their fragments come back as sprites, at lower confidence).
    `plate_at(f)`: a per-frame plate (a background that moves), instead of `plate`."""
    plate_lab = rgb_to_lab(plate) if plate is not None else None
    shape = frames.shape[1:3]

    def mask(i, f):
        if plate_at is not None:
            return foreground_mask_plate(f, plate_at(i))
        return foreground_mask_plate(f, plate, plate_lab=plate_lab) if plate is not None else foreground_mask(f, bg)

    def run(movers):
        fg = (mask(i, f) for i, f in enumerate(frames))
        excluded = ((text_exclusion_mask(b, shape) | mover_cover(movers, f, shape)) if movers else text_exclusion_mask(b, shape)
                    for f, b in enumerate(boxes))
        return find_solids(frames, fg, _unclaimed(obj_tracks, [], movers)[0], excluded)

    try:
        solids = run(movers)
    except Exception as e:
        if not movers:
            raise
        log.warning("solids with movers failed; movers dropped: %s", describe(e, trace=True))
        if notes is not None:
            notes.append(f"movers dropped ({MOVERS_FAILED})")
        if dropped is not None:
            dropped.extend(movers)
        movers, solids = (), run(())
    return _pk(sd, "solids", solids), list(movers)


def _mover_videos(props: dict, movers, frames, plate, fps: float, sd: Path) -> None:
    """Unstable movers become video sprites (stages/m<id>.video.webm, copied into the assets with the element id);
    one whose video cannot be made keeps its still texture and records the code."""
    for m in movers:
        p = props.get(f"m{m.id}")
        if p is None:
            continue
        t0 = time.perf_counter()
        try:
            path, code = mover_video(m, p, frames, plate, fps, sd / "stages" / f"m{m.id}.video.webm")
        except Exception:   # fail soft: the still sprite
            log.exception("mover m%s video failed", m.id)
            path, code = None, "video_failed"
        if path is None:
            p["video_error"] = code
        else:
            p["video"] = f"stages/m{m.id}.video.webm"
        log.info("sprites mover m%s video %.2fs %s", m.id, time.perf_counter() - t0, code or "ok")


def _stage_sprites(frames, bg, text_tracks, shape_tracks, obj_tracks, opts, sd, n_frames, plate=None, solids=(), movers=(),
                   plate_model: PlateModel | None = None, fps: float | None = None, dropped=()):
    """`dropped`: movers an earlier stage dropped; their fragments, like those of a mover dropped here, are marked
    `mover_dropped`."""
    props = {}   # object_key -> dict(raw, canon, cf, kind, font, color)
    t0 = time.perf_counter()
    kept, failed = [], []
    behind = plate if plate is not None else np.full((*frames.shape[1:3], 3), bg, np.uint8)
    for m in movers:   # a mover that cannot become a sprite is dropped (its fragments come back), never the analysis
        try:
            props[f"m{m.id}"] = mover_props(m, frames, behind, n_frames)
            kept.append(m)
        except Exception as e:
            log.warning("mover m%s dropped: %s", m.id, describe(e, trace=True))
            failed.append(m)
    if failed:
        props["_movers_message"] = "; ".join(f"mover m{m.id} dropped ({MOVER_DROPPED})" for m in failed)
    fragments = {f"o{i}" for m in (*failed, *dropped) for i in m.claimed} \
        | {f"s{i}" for m in (*failed, *dropped) for i in m.claimed_shapes}
    obj_tracks, shape_tracks = _unclaimed(obj_tracks, shape_tracks, kept)
    if movers:
        log.info("sprites movers=%s kept=%s %.2fs", len(movers), len(kept), time.perf_counter() - t0)
        t0 = time.perf_counter()
    workers = max(1, min(8, os.cpu_count() or 4))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        jobs = [(t, is_text, []) for tracks, is_text in ((text_tracks, True), (shape_tracks, False)) for t in tracks]
        text_futs = {   # text that fades takes its canonical frame at full opacity (Task 9b)
            ex.submit(text_props, t, frames, bg, n_frames, 0, infer_font=is_text, plate=plate, full_opacity=is_text,
                      notes=notes): (t, is_text, notes)
            for t, is_text, notes in jobs
        }
        for fut, (t, is_text, notes) in text_futs.items():
            raw, canon, cf, font, color = fut.result()
            p = {"raw": raw, "canon": canon, "cf": cf, "kind": "text" if is_text else "sprite", "first": t.first, "last": t.last}
            if is_text:
                p.update(text=t.text, font=font, color=color)
            else:
                p["z"] = 0
            if notes:
                p["fade_note"] = "; ".join(notes)
            if FULL_OPACITY_NOTE in notes:
                p["full_opacity_error"] = FULL_OPACITY_FAILED
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
    for k in fragments & props.keys():   # a dropped mover's pieces are separate sprites again, less sure
        props[k]["mover_dropped"] = MOVER_DROPPED
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
    if opts.refine and any(p["kind"] == "sprite" and not p.get("mover") for p in props.values()):
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
                keys = [k for k, p in props.items() if p["kind"] == "sprite" and not p.get("mover")]   # movers hold their centroid track
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
            log.warning("refine skipped: %s", describe(e, trace=True))
            props["_message"] = "refine skipped (refine_failed)"
    # Textures v2: matted against the per-frame plate, after refine, before solids. A solid's fragments keep today's
    # textures: they are pieces of a turning surface, and the still/fragments fidelity choice compares like with like.
    report_stage("sprites", "textures")
    members = {f"o{mid}" for solid in solids for mid in solid.members}
    plate_at = plate_model if plate_model is not None else matting.as_plate(behind)
    videos = {f"m{m.id}": m for m in kept if not props[f"m{m.id}"].get("stable")} \
        if fps is not None and videoasset.ffmpeg_vp9_ok() else {}
    try:   # a video sprite under a layer is its own frame there, not its poster
        matting.matte_props(props, frames, plate_at, workers=workers, skip=members,
                            layers_at={k: mover_frames(m, frames, plate_at) for k, m in videos.items()})
    except Exception as e:   # fail soft: every element keeps its binary texture
        log.warning("textures v2 failed: %s", describe(e, trace=True))
        props["_textures_message"] = f"textures v2 skipped ({TEXTURES_FAILED})"
    # Text style: fill, gradient, fade and effects from the matted texture and the local plate (Task 9).
    report_stage("sprites", "text style")
    try:
        textstyle.style_props(props, frames, plate_model if plate_model is not None else matting.as_plate(behind), workers=workers,
                              fonts=_font_registry(sd))
    except Exception as e:   # fail soft: every text keeps its core-mask colour and no style
        log.warning("text style failed: %s", describe(e, trace=True))
        props["_style_message"] = f"text style skipped ({textstyle.STYLE_FAILED})"
    _mover_videos(props, list(videos.values()), frames, plate_at, fps, sd)
    for i, solid in enumerate(solids):
        p = solid_props(solid, frames)
        p["fragments"] = {k: props.pop(k) for mid in solid.members if (k := f"o{mid}") in props}
        p["z"] = max((v.get("z", 0) for v in p["fragments"].values()), default=0)
        props[f"solid{i + 1}"] = p
    return _pk(sd, "props", props)


def _write_replacing(path: Path, write) -> None:
    """`write(tmp)` into a temporary beside `path` (made by the writer: the usual file mode), then os.replace: a name
    that is a hard link (an edit candidate's view of the scene's assets) is replaced, never written through."""
    import threading
    tmp = path.with_name(f".{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp{path.suffix}")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _elements_from_props(props: dict, sd: Path, ids: dict) -> tuple[list[Element], dict[str, np.ndarray]]:
    keys = [k for k in props if not k.startswith("_")]
    keys.sort(key=lambda k: (props[k]["first"], float(np.nanmean(props[k]["raw"][:, 0]))))
    elements, raws = [], {}
    from ..fonts.upload import sweep_scene_assets
    sweep_scene_assets(sd)   # an interrupted font copy left behind
    registry = _font_registry(sd) if any(getattr(props[k].get("font"), "source", None) == "uploaded" for k in keys) else None
    for k in keys:
        eid = ids.get(k) or f"e{len(ids) + 1}"
        ids[k] = eid
        p = props[k]
        raw_full = fill_gaps(p["raw"])
        valid = np.flatnonzero(~np.isnan(raw_full[:, 0]))
        first, last = int(valid[0]), int(valid[-1])
        tracks, fe = tracks_from_raw(raw_full[first:last + 1], first)
        (sd / "assets").mkdir(parents=True, exist_ok=True)
        _write_replacing(sd / "assets" / f"{eid}.png",
                         lambda t: cv2.imwrite(str(t), cv2.cvtColor(p["canon"], cv2.COLOR_RGBA2BGRA)))
        _write_replacing(sd / "assets" / f"{eid}_raw.npz", lambda t: np.savez_compressed(
            t, raw=raw_full, first=first, cols=np.array(PROPS[:raw_full.shape[1]])))
        h, w = p["canon"].shape[:2]   # textures v2 pad the canonical by 2p; centre and anchor stay
        meta = p.get("texture_meta")
        video = None
        if p.get("video") and (sd / p["video"]).is_file():   # a video sprite: its WebM beside the poster
            video = f"assets/{eid}.video.webm"
            try:
                _write_replacing(sd / video, lambda t: shutil.copyfile(sd / p["video"], t))
            except OSError as e:   # fail soft: the still sprite, its code, lower confidence (props.pkl keeps the try)
                log.warning("video sprite %s not copied: %s", eid, describe(e))
                (sd / video).unlink(missing_ok=True)
                video = None
                p.pop("video")
                p["video_error"] = "video_failed"
        canonical = Canonical(width=w, height=h, texture=f"assets/{eid}.png", video=video,
                              text=p.get("text"), font=_pin_font(p.get("font"), sd, registry), color=p.get("color"),
                              style=TextStyle(**p["style"]) if p.get("style") else None,
                              texture_meta=TextureMeta(**meta) if meta else None)
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
            log.warning("captions skipped: %s", describe(e, trace=True))
            messages.append("captions skipped (captions_failed)")
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
    failed = fallback_ids(props or {}, ids or {})   # today's behaviour after a fail-soft path: less sure, once
    for e in scene.elements:
        e.confidence = conf[e.id] * (FALLBACK_CONF if e.id in failed else 1.0)
    write_report(sd, {"reconstruction": rec, "confidence": {e.id: e.confidence for e in scene.elements}, "messages": messages,
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
        message = "UI parse skipped (ui_parse_failed)"
        log.warning("%s scene=%s: %s", message, scene_id, describe(exc, trace=True))
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
    report_stage("plate")
    model, rbf, obj_tracks = _plate_stage(frames, fps, rbf, boxes, text_tracks, shape_tracks, obj_tracks, bg, bconf, plate, opts, sd)
    bg, plate = _plate_inputs(model)
    t = _stage_done("plate", t)
    report_stage("solids")
    notes: list[str] = []
    dropped: list = []
    solids, movers = _stage_solids(frames, bg, boxes, obj_tracks, sd, plate=plate, movers=model.movers, notes=notes,
                                   plate_at=_moving(model), dropped=dropped)
    t = _stage_done("solids", t)
    report_stage("sprites")
    props = _stage_sprites(frames, bg, text_tracks, shape_tracks, obj_tracks, opts, sd, n, plate=plate, solids=solids,
                           movers=movers, plate_model=model, fps=fps, dropped=dropped)
    t = _stage_done("sprites", t)
    ids_path = sd / "stages" / "ids.json"
    ids: dict = json.loads(ids_path.read_text()) if existing and ids_path.exists() else {}
    report_stage("keyframes")
    elements, raws = _elements_from_props(props, sd, ids)
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    scene = Scene(id=scene_id, size=(W, H), fps=fps, frames=n, background=background_for(model, sd), elements=elements)
    messages = [m for m in (msg, model.stats.get("message"), model.stats.get("movers_message"), *notes,
                            props.get("_message"), props.get("_movers_message"), props.get("_textures_message"),
                            props.get("_style_message")) if m]
    messages += _mover_messages(props, ids) + _texture_messages(props, ids)
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
            plate = pass1_plate(sd)
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
    model = _cached_plate(sd) if boundary > STAGES.index("plate") else None
    rebuilt = model is None   # rerun from the plate or earlier, or a project analysed before plate v2 / movers
    if rebuilt:
        report_stage("plate")
        model, rbf, obj_tracks = _plate_stage(frames, fps, rbf, boxes, text_tracks, shape_tracks, obj_tracks, bg, bconf, plate,
                                              opts, sd, fresh=boundary <= STAGES.index("regions"))
    bg, plate = _plate_inputs(model)
    t = _stage_done("plate", t)
    redo = rebuilt or not (sd / "stages" / "solids.pkl").exists()   # solids and sprites depend on the plate and movers
    notes: list[str] = []
    dropped: list = []
    movers = model.movers
    if boundary <= STAGES.index("solids") or redo:
        report_stage("solids")
        solids, movers = _stage_solids(frames, bg, boxes, obj_tracks, sd, plate=plate, movers=movers, notes=notes,
                                       plate_at=_moving(model), dropped=dropped)
    else:
        solids = _pk(sd, "solids")
    t = _stage_done("solids", t)
    if boundary <= STAGES.index("sprites") or redo:
        report_stage("sprites")
        props = _stage_sprites(frames, bg, text_tracks, shape_tracks, obj_tracks, opts, sd, n, plate=plate, solids=solids,
                               movers=movers, plate_model=model, fps=fps, dropped=dropped)
    else:
        props = _pk(sd, "props")
    t = _stage_done("sprites", t)
    ids = json.loads((sd / "stages" / "ids.json").read_text())
    ids.update(_load_overrides(sd).get("ids", {}))
    report_stage("keyframes")
    elements, raws = _elements_from_props(props, sd, ids)
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    scene = Scene(id=scene_id, size=(W, H), fps=fps, frames=n, background=background_for(model, sd), elements=elements)
    messages = [m for m in (msg, model.stats.get("message"), model.stats.get("movers_message"), *notes,
                            props.get("_message"), props.get("_movers_message"), props.get("_textures_message"),
                            props.get("_style_message")) if m]
    messages += _mover_messages(props, ids) + _texture_messages(props, ids)
    messages.append(f"3D 후보 {len(solids)}개")
    if opts.ui:
        scene = _apply_ui(frames, scene, sd, scene_id, messages=messages)
    for e in scene.elements:   # keep manual text edits across reruns
        try:
            old = prev.element(e.id)
            if old.provenance == "manual" and old.kind == "text":
                e.canonical.text, e.canonical.font, e.provenance = old.canonical.text, old.canonical.font, "manual"
                e.canonical.style = old.canonical.style
            e.label, e.caption = old.label, old.caption
        except KeyError:
            pass
    scene = _finish(sd, scene, frames, raws, messages, previous=prev, captioner=captioner,
                    props=props, ids=ids, generate_3d=False, kf_secs=time.perf_counter() - t)
    return new_version(root, scene_id, scene, note=note, auto=False, analysis_file=snapshot_from_stages(sd, scene))
