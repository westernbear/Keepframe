from __future__ import annotations

import json
import os
import pickle
import shutil
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

import cv2
import numpy as np

from ..assets import AssetAPIError, AssetClient, validate_glb
from ..compose.composer import compose
from ..ir.schema import Background, Scene
from ..log import get
from ..render.renderer import load_frame, render
from .composite import composite_scene

log = get("keepframe.analyze")
ASSET_GEN_CAP = 2
_generation_requests: ContextVar[dict[str, int] | None] = ContextVar("keepframe_solid_generation_requests", default=None)


@contextmanager
def solid_generation_budget():
    """Share the two-request ceiling across all scenes in one analysis operation."""
    token = _generation_requests.set({"count": 0})
    try:
        yield
    finally:
        _generation_requests.reset(token)


def generate_solid_assets(scene: Scene, sd: Path, client) -> list[str]:
    """Reuse versioned models; generate at most two pending reference crops."""
    sd = Path(sd)
    candidates = [e for e in scene.elements if e.pending_asset == "3d"]
    messages = []
    requests = 0
    budget = _generation_requests.get()
    missing = 0
    for el in candidates:
        try:
            models = sorted((sd / "assets").glob(f"{el.id}.model[0-9]*.glb"),
                            key=lambda p: int(p.stem.rsplit("model", 1)[1]), reverse=True)
            if el.canonical.model:
                models.insert(0, sd / el.canonical.model)
            if models:
                path = models[0]
                validate_glb(path.read_bytes())
                messages.append(f"3D 재사용 {el.id}: {path.name}")
            elif client is None:
                missing += 1
                continue
            elif requests >= ASSET_GEN_CAP or (budget is not None and budget["count"] >= ASSET_GEN_CAP):
                messages.append(f"3D 생성 {el.id}: 생성 상한 2회")
                continue
            else:
                requests += 1
                if budget is not None:
                    budget["count"] += 1
                response = client.request(
                    task="generate", kind="3d",
                    prompt="Reconstruct this reference object as a 3D model. Preserve its shape, colours and texture.",
                    input_image=(sd / el.canonical.texture).read_bytes(),
                    size={"width": round(el.canonical.width), "height": round(el.canonical.height)},
                )
                if response.mime != "model/gltf-binary":
                    raise AssetAPIError("asset_mime_not_allowed")
                data = validate_glb(response.data)
                path = sd / "assets" / f"{el.id}.model1.glb"
                path.write_bytes(data)
                messages.append(f"3D 생성 {el.id}: {path.name}")
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
            related = {ids.get(key), *(ids.get(k) for k in p["fragments"])}
            manual = next((e for e in previous.elements if e.id in related and e.provenance == "manual" and e.canonical.model), None)
            if manual is not None:
                scene.element(ids[key]).canonical.model = manual.canonical.model
                scene.element(ids[key]).provenance = "manual"
    client = None
    if generate and os.environ.get("KEEPFRAME_ASSET_API_URL"):
        try:
            client = AssetClient(timeout=310)
        except AssetAPIError as exc:
            messages.append(f"3D 생성기 실패: {exc.code}")
    messages.extend(generate_solid_assets(scene, sd, client))
    if scene.background.kind == "image":
        plate = cv2.cvtColor(cv2.imread(str(sd / scene.background.value)), cv2.COLOR_BGR2RGB)
    else:
        value = scene.background.value.lstrip("#")
        plate = tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    reports = []
    for key, p in solids.items():
        eid = ids[key]
        try:
            el = scene.element(eid)
        except KeyError:  # UI opt-in may replace the measured objects.
            continue
        model_path = sd / el.canonical.model if el.canonical.model else None
        errors = solid_errors(frames, plate, {**p, "fps": scene.fps}, model_path)
        choice = choose_solid(errors)
        reports.append({"element": eid, **errors, "choice": choice})
        scores = ", ".join(f"{k}={v:.6f}" if v is not None else f"{k}=None" for k, v in errors.items())
        messages.append(f"3D 충실도 {eid}: {scores}; {choice}")
        if choice == "still":
            el.kind, el.pending_asset = "sprite", "3d"
        elif choice == "fragments":
            restored, fragment_raws = _elements_from_props(p["fragments"], sd, ids)
            scene.elements = [e for e in scene.elements if e.id != eid] + restored
            raws.pop(eid, None)
            raws.update(fragment_raws)
            if restored:
                largest = max(p["fragments"], key=lambda k: np.count_nonzero(p["fragments"][k]["canon"][..., 3]))
                pending = scene.element(ids[largest])
                pending.pending_asset = "3d"
                pending.canonical.model = el.canonical.model
                pending.provenance = el.provenance
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    return reports


def guard_reference_edit(edited, source_dir, candidate_dir, eid):
    """Use the same measured alternatives for an explicit regeneration of a known solid."""
    from .pipeline import _elements_from_props

    stages = source_dir / "stages"
    if not all((stages / name).is_file() for name in ("props.pkl", "ids.json", "frames.npy")):
        return edited, [], [], None
    props = pickle.loads((stages / "props.pkl").read_bytes())
    ids = json.loads((stages / "ids.json").read_text())
    match = next(((k, p) for k, p in props.items() if not k.startswith("_") and "fragments" in p and
                  (ids.get(k) == eid or any(ids.get(m) == eid for m in p["fragments"]))), None)
    if match is None:
        return edited, [], [], None
    key, p = match
    frames = np.load(stages / "frames.npy", mmap_mode="r")
    if edited.background.kind == "image":
        plate = cv2.cvtColor(cv2.imread(str(candidate_dir / edited.background.value)), cv2.COLOR_BGR2RGB)
    else:
        value = edited.background.value.lstrip("#")
        plate = tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    model = edited.element(eid)
    measured = {**p, "fps": edited.fps}
    if ids.get(key) != eid:
        member = next(k for k in p["fragments"] if ids.get(k) == eid)
        measured["model_props"] = p["fragments"][member]
    errors = solid_errors(frames, plate, measured, candidate_dir / model.canonical.model)
    choice = choose_solid(errors)
    related = {ids.get(key), *(ids.get(k) for k in p["fragments"])}
    retained = [e for e in edited.elements if e.id not in related]
    if choice == "model":
        replacement = [model]
    elif choice == "still":
        replacement, _ = _elements_from_props({key: p}, candidate_dir, ids)
        replacement[0].canonical.model = model.canonical.model
        replacement[0].pending_asset = "3d"
    else:
        replacement, _ = _elements_from_props(p["fragments"], candidate_dir, ids)
        if replacement:
            largest = max(p["fragments"], key=lambda k: np.count_nonzero(p["fragments"][k]["canon"][..., 3]))
            pending = next(e for e in replacement if e.id == ids[largest])
            pending.pending_asset = "3d"
            pending.canonical.model = model.canonical.model
    for el in replacement:
        el.provenance = "manual"
    edited.elements = retained + replacement
    scores = ", ".join(f"{k}={v:.6f}" if v is not None else f"{k}=None" for k, v in errors.items())
    message = f"3D 충실도 {ids[key]}: {scores}; {choice}"
    return edited, [{"element": ids[key], **errors, "choice": choice}], [message], ids


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
            shutil.copyfile(glb_path, sd / "assets" / "model.glb")
            if "model_props" in p:
                models, _ = _elements_from_props({"model": p["model_props"]}, sd, ids)
                model = models[0]
            else:
                model = still[0].model_copy(deep=True)
            model.kind, model.pending_asset = "3d", None
            model.canonical.model = "assets/model.glb"
            mini = scene.model_copy(update={"elements": [model]})
            try:
                html = compose(mini, sd, sd / "composition.html")
                result = render(html, mini, sd / "render", frames=samples, probe=False)
                errors["model"] = error(load_frame(result.frames_dir / f"f_{i:05d}.png") for i in range(len(samples)))
            except Exception as exc:
                log.warning("solid model render skipped: %s", type(exc).__name__)
    return errors
