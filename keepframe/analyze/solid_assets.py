from __future__ import annotations

import json
import os
import pickle
import shutil
import tempfile
from pathlib import Path
from typing import Literal

import cv2
import numpy as np

from ..assets import ASSET_GEN_CAP, AssetAPIError, AssetClient, validate_glb
from ..compose.composer import compose
from ..ir.schema import Background, Scene
from ..ir.tracks import element_bbox
from ..log import get
from ..render.renderer import load_frame, render
from .composite import composite_scene
from .keyframes import fill_gaps, tracks_from_raw

log = get("keepframe.analyze")


def _signature(first, box):
    return json.dumps([int(first), *(round(float(v)) for v in box)])


def _props_signature(p):
    first = p["first"]
    if p.get("boxes") is not None:
        box = p["boxes"][first]
    else:
        x, y, sx, sy = p["raw"][first, :4]
        h, w = p["canon"].shape[:2]
        box = (x - w * sx / 2, y - h * sy / 2, x + w * sx / 2, y + h * sy / 2)
    return _signature(first, box)


def _record_model(sd, model, signature):
    (sd / f"{model}.solid.json").write_text(json.dumps({"signature": signature}))


def _model_signature(sd, model):
    path = sd / f"{model}.solid.json"
    return json.loads(path.read_text())["signature"] if path.is_file() else None


def index_legacy_solid_models(sd):
    """Associate pre-signature GLBs using the old stages, before analysis overwrites them."""
    stages = sd / "stages"
    if not all((stages / name).is_file() for name in ("props.pkl", "ids.json")):
        return
    props = pickle.loads((stages / "props.pkl").read_bytes())
    ids = json.loads((stages / "ids.json").read_text())
    for key, p in props.items():
        if key.startswith("_") or "fragments" not in p:
            continue
        for related in (key, *p["fragments"]):
            if related not in ids:
                continue
            for path in (sd / "assets").glob(f"{ids[related]}.model[0-9]*.glb"):
                model = f"assets/{path.name}"
                if _model_signature(sd, model) is None:
                    _record_model(sd, model, _props_signature(p))


def _known_solid(sd, eid):
    stages = sd / "stages"
    if not all((stages / name).is_file() for name in ("props.pkl", "ids.json")):
        return None
    props = pickle.loads((stages / "props.pkl").read_bytes())
    ids = json.loads((stages / "ids.json").read_text())
    return next(((k, p, ids) for k, p in props.items() if not k.startswith("_") and "fragments" in p and
                 (ids.get(k) == eid or any(ids.get(m) == eid for m in p["fragments"]))), None)


def reference_crop(scene, sd, eid):
    """A restored fragment still references the entire measured solid."""
    known = _known_solid(sd, eid)
    if known:
        key, p, ids = known
        h, w = p["canon"].shape[:2]
        return f"assets/{ids[key]}.png", {"width": w, "height": h}
    c = scene.element(eid).canonical
    return c.texture, {"width": round(c.width), "height": round(c.height)}


def _plate(scene, sd):
    if scene.background.kind == "image":
        return cv2.cvtColor(cv2.imread(str(sd / scene.background.value)), cv2.COLOR_BGR2RGB)
    value = scene.background.value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _fidelity_message(eid, errors, choice):
    scores = ", ".join(f"{k}={v:.6f}" if v is not None else f"{k}=None" for k, v in errors.items())
    return f"3D 충실도 {eid}: {scores}; {choice}"


def _mark_largest(elements, p, ids, model):
    if not elements:
        return
    largest = max(p["fragments"], key=lambda k: np.count_nonzero(p["fragments"][k]["canon"][..., 3]))
    pending = next(e for e in elements if e.id == ids[largest])
    pending.pending_asset = "3d"
    pending.canonical.model = model.canonical.model
    pending.provenance = model.provenance


def _render_failure(messages, eid, model_path, errors):
    if model_path is not None and errors["model"] is None:
        messages.append(f"3D render failed {eid}: model_render_failed")


def generate_solid_assets(scene: Scene, sd: Path, client, *, signatures=None) -> list[str]:
    """Reuse models by solid signature; allow at most two attempts per solid."""
    sd = Path(sd)
    candidates = [e for e in scene.elements if e.pending_asset == "3d"]
    messages = []
    missing = 0
    for el in candidates:
        signature = (signatures or {}).get(el.id) or _signature(el.visible[0], element_bbox(el, el.visible[0]))
        try:
            models = sorted((p for p in (sd / "assets").glob("*.model[0-9]*.glb")
                             if _model_signature(sd, f"assets/{p.name}") == signature),
                            key=lambda p: (int(p.stem.rsplit("model", 1)[1]), p.name), reverse=True)
            if el.canonical.model and _model_signature(sd, el.canonical.model) == signature:
                models.insert(0, sd / el.canonical.model)
            if models:
                path = models[0]
                validate_glb(path.read_bytes())
                messages.append(f"3D 재사용 {el.id}: {path.name}")
            elif client is None:
                missing += 1
                continue
            else:
                for attempt in range(ASSET_GEN_CAP):
                    try:
                        response = client.request(
                            task="generate", kind="3d",
                            prompt="Reconstruct this reference object as a 3D model. Preserve its shape, colours and texture.",
                            input_image=(sd / el.canonical.texture).read_bytes(),
                            size={"width": round(el.canonical.width), "height": round(el.canonical.height)},
                        )
                        if response.mime != "model/gltf-binary":
                            raise AssetAPIError("asset_mime_not_allowed")
                        data = validate_glb(response.data)
                        n = 1
                        while (path := sd / "assets" / f"{el.id}.model{n}.glb").exists():
                            n += 1
                        path.write_bytes(data)
                        _record_model(sd, f"assets/{path.name}", signature)
                        messages.append(f"3D 생성 {el.id}: {path.name}")
                        break
                    except Exception as exc:
                        if attempt + 1 == ASSET_GEN_CAP:
                            raise
                        code = exc.code if isinstance(exc, AssetAPIError) else type(exc).__name__
                        messages.append(f"3D 생성 {el.id} 실패: {code}; 재시도")
            el.canonical.model = f"assets/{path.name}"
            el.kind = "3d"
            el.pending_asset = None
        except Exception as exc:
            # Report only a typed code: upstream exception strings may contain credentials.
            code = exc.code if isinstance(exc, AssetAPIError) else type(exc).__name__
            messages.append(f"3D 생성 {el.id} 실패: {code}")
            el.kind = "sprite"
            el.pending_asset = "3d"
            el.canonical.model = None
    if missing:
        reason = "생성 대기" if os.environ.get("KEEPFRAME_ASSET_API_URL") else "생성기 미설정"
        messages.append(f"3D 후보 {missing}개: {reason}")
    return messages


def finish_solid_assets(scene, sd, frames, props, ids, raws, messages, generate=True, previous=None):
    """Apply the fidelity decision before semantics/constraints see the final elements."""
    from .pipeline import _elements_from_props

    solids = {k: p for k, p in props.items() if not k.startswith("_") and "fragments" in p}
    if not solids:
        return []
    if previous is not None:
        for key, p in solids.items():
            if not any(e.id == ids[key] for e in scene.elements):
                continue
            manual = next((e for e in previous.elements if e.provenance == "manual" and e.canonical.model and
                           _model_signature(sd, e.canonical.model) == _props_signature(p)), None)
            if manual is not None:
                scene.element(ids[key]).canonical.model = manual.canonical.model
                scene.element(ids[key]).provenance = "manual"
    client = None
    if generate and os.environ.get("KEEPFRAME_ASSET_API_URL"):
        try:
            client = AssetClient(timeout=310)
        except AssetAPIError as exc:
            messages.append(f"3D 생성기 실패: {exc.code}")
    from ..progress import report_stage
    report_stage("keyframes", f"3D 생성/충실도 {len(solids)}개")
    messages.extend(generate_solid_assets(scene, sd, client, signatures={ids[k]: _props_signature(p) for k, p in solids.items()}))
    plate = _plate(scene, sd)
    reports = []
    for key, p in solids.items():
        eid = ids[key]
        try:
            el = scene.element(eid)
        except KeyError:  # UI opt-in may replace the measured objects.
            continue
        model_path = sd / el.canonical.model if el.canonical.model else None
        errors = solid_errors(frames, plate, {**p, "fps": scene.fps}, model_path)
        _render_failure(messages, eid, model_path, errors)
        choice = choose_solid(errors)
        reports.append({"element": eid, **errors, "choice": choice})
        messages.append(_fidelity_message(eid, errors, choice))
        if choice == "still":
            el.kind, el.pending_asset = "sprite", "3d"
        elif choice == "fragments":
            restored, fragment_raws = _elements_from_props(p["fragments"], sd, ids)
            scene.elements = [e for e in scene.elements if e.id != eid] + restored
            raws.pop(eid, None)
            raws.update(fragment_raws)
            _mark_largest(restored, p, ids, el)
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    return reports


def guard_reference_edit(edited, source_dir, candidate_dir, eid, previous=None):
    """Use the same measured alternatives for an explicit regeneration of a known solid."""
    from .pipeline import _elements_from_props

    stages = source_dir / "stages"
    if not all((stages / name).is_file() for name in ("props.pkl", "ids.json", "frames.npy")):
        return edited, [], [], None
    match = _known_solid(source_dir, eid)
    if match is None:
        return edited, [], [], None
    key, p, ids = match
    frames = np.load(stages / "frames.npy", mmap_mode="r")
    plate = _plate(edited, candidate_dir)
    model = edited.element(eid)
    measured = {**p, "fps": edited.fps}
    errors = solid_errors(frames, plate, measured, candidate_dir / model.canonical.model)
    choice = choose_solid(errors)
    _record_model(candidate_dir, model.canonical.model, _props_signature(p))
    related = {ids.get(key), *(ids.get(k) for k in p["fragments"])}
    if choice == "model":
        if ids.get(key) != eid:
            # Replace fragment geometry with the whole solid, keeping the current element's metadata.
            raw = fill_gaps(p["raw"])
            valid = np.flatnonzero(np.isfinite(raw[:, 0]))
            first, last = int(valid[0]), int(valid[-1])
            tracks, fe = tracks_from_raw(raw[first:last + 1], first)
            model.tracks = {**tracks, **{k: v for k, v in model.tracks.items() if k in {"opacity", "reveal"}}}
            model.visible, model.fit_error = (first, last), fe
            h, w = p["canon"].shape[:2]
            model.canonical.width, model.canonical.height = w, h
            model.canonical.texture = f"assets/{ids[key]}.png"
            model.raw = f"assets/{Path(model.canonical.model).stem}_raw.npz"
            from ..ir.schema import PROPS
            np.savez_compressed(candidate_dir / model.raw, raw=raw, first=first, cols=np.array(PROPS[:raw.shape[1]]))
        edited.elements = [e for e in edited.elements if e.id not in related or e.id == eid]
    elif choice == "still":
        model.kind, model.pending_asset = "sprite", "3d"
    else:
        if eid != ids[key]:
            model.kind, model.pending_asset = "sprite", "3d"
        existing = {e.id: e for e in edited.elements}
        old = {e.id: e for e in previous.elements} if previous is not None else {}
        missing = {}
        restored = []
        for member, fragment in p["fragments"].items():
            fid = ids.get(member)
            if fid in existing:
                restored.append(existing[fid])
            elif fid in old:
                restored.append(old[fid].model_copy(deep=True))
            else:
                missing[member] = fragment
        # Only genuinely missing fragments need analysis geometry; existing manual edits stay intact.
        created, _ = _elements_from_props(missing, candidate_dir, ids)
        for el in created:
            for field in ("role", "label", "caption", "confidence", "provenance"):
                setattr(el, field, getattr(model, field))
            el.canonical.color = model.canonical.color
        restored.extend(created)
        edited.elements = [e for e in edited.elements if e.id not in related] + restored
        _mark_largest(restored, p, ids, model)
    messages = []
    _render_failure(messages, ids[key], candidate_dir / model.canonical.model, errors)
    messages.append(_fidelity_message(ids[key], errors, choice))
    return edited, [{"element": ids[key], **errors, "choice": choice}], messages, ids


def choose_solid(e) -> Literal["model", "still", "fragments"]:
    best_alt = min(v for k, v in e.items() if k != "model" and v is not None)
    if e.get("model") is not None and e["model"] <= best_alt + 0.005:
        return "model"
    return "still" if e["still"] <= e["fragments"] + 0.005 else "fragments"


def solid_errors(frames, plate_or_bg, solid_props, glb_path: Path | None, sample_every=5) -> dict[str, float | None]:
    """Local RGB L1 for only this solid, sampled every fifth visible frame."""
    from .pipeline import _elements_from_props

    p = solid_props
    visible = np.flatnonzero(np.isfinite(p["raw"][:, 0]))
    samples = visible[::sample_every].tolist()
    if not samples:
        raise ValueError("solid has no visible frames")
    h, w = frames.shape[1:3]
    errors = {"fragments": None, "still": None, "model": None}
    with tempfile.TemporaryDirectory(prefix="keepframe-solid-") as temp:
        sd = Path(temp)
        (sd / "assets").mkdir()
        if isinstance(plate_or_bg, np.ndarray) and plate_or_bg.ndim == 3:
            cv2.imwrite(str(sd / "assets" / "plate.png"), cv2.cvtColor(plate_or_bg, cv2.COLOR_RGB2BGR))
            bg = Background(kind="image", value="assets/plate.png")
        else:
            bg = Background(value="#%02x%02x%02x" % tuple(int(v) for v in plate_or_bg))
        ids = {}
        fragments, _ = _elements_from_props(p["fragments"], sd, ids)
        still, _ = _elements_from_props({"solid": p}, sd, ids)
        scene = Scene(id="solid", size=(w, h), fps=p.get("fps", 30), frames=len(frames), background=bg, elements=still)
        boxes = p.get("boxes")

        def error(images):
            values = []
            for f, image in zip(samples, images):
                if boxes is not None:
                    x0, y0, x1, y1 = boxes[f]
                else:
                    x, y, sx, sy = p["raw"][f, :4]
                    ch, cw = p["canon"].shape[:2]
                    x0, y0, x1, y1 = round(x - cw * sx / 2), round(y - ch * sy / 2), round(x + cw * sx / 2), round(y + ch * sy / 2)
                x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
                if x1 > x0 and y1 > y0:
                    source = frames[f, y0:y1, x0:x1].astype(np.float32) / 255
                    values.append(float(np.mean(np.abs(source - image[y0:y1, x0:x1]))))
            return float(np.mean(values))

        for name, elements in (("fragments", fragments), ("still", still)):
            mini = scene.model_copy(update={"elements": elements})
            cache = {}
            errors[name] = error(composite_scene(mini, sd, f, cache) for f in samples)
        if glb_path is not None:
            model = still[0].model_copy(deep=True)
            model.kind, model.pending_asset = "3d", None
            model.canonical.model = "assets/model.glb"
            mini = scene.model_copy(update={"elements": [model]})
            try:
                shutil.copyfile(glb_path, sd / "assets" / "model.glb")
                html = compose(mini, sd, sd / "composition.html")
                result = render(html, mini, sd / "render", frames=samples, probe=False)
                errors["model"] = error(load_frame(result.frames_dir / f"f_{i:05d}.png") for i in range(len(samples)))
            except Exception as exc:
                log.warning("solid model render skipped: %s", type(exc).__name__)
    return errors
