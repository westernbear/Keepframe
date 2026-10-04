from __future__ import annotations
import json, shutil, pickle
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import FontGuess, Version
from ..ir.store import current_scene, new_version, scene_dir
from ..analyze.background import PLATE_PATH, foreground_mask, foreground_mask_plate
from ..analyze.pipeline import rerun


def _overrides_path(sd: Path) -> Path:
    return sd / "stages" / "overrides.json"


def _load(sd: Path) -> dict:
    p = _overrides_path(sd)
    return json.loads(p.read_text()) if p.exists() else {"regions": [], "ids": {}, "merge": []}


def _save(sd: Path, ov: dict) -> None:
    _overrides_path(sd).write_text(json.dumps(ov, indent=2))


def _object_of(sd: Path, element_id: str) -> str:
    ids = json.loads((sd / "stages" / "ids.json").read_text())
    for key, eid in ids.items():
        if eid == element_id:
            return key
    raise KeyError(element_id)


def _object_num(key: str) -> int:
    if key.startswith("s"):
        raise ValueError("shape elements from OCR boxes cannot take region masks or partial ID reassignment; "
                         "use the text correction or reanalyze")
    if not key.startswith("o"):
        raise ValueError("region masks and partial ID reassignment require object tracks, not text or other elements")
    return int(key[1:])


def validate_correction_targets(root: Path, scene_id: str, op: str, args: dict) -> None:
    sd = scene_dir(root, scene_id)
    if op in {"mask", "bbox"}:
        key = _object_of(sd, args["object_id"])
        if op == "bbox" and key.startswith("t"):
            return
        _object_num(key)
    elif op == "reassign":
        scene, _ = current_scene(root, scene_id)
        frames = args["frames"]
        if frames[0] <= 0 and frames[1] >= scene.frames - 1:
            return
        for element_id in (args["from_id"], args["to_id"]):
            _object_num(_object_of(sd, element_id))


def edit_text(root: Path, scene_id: str, element_id: str, text: str | None = None, font: FontGuess | None = None,
              note: str = "edit text") -> Version:
    scene, _ = current_scene(root, scene_id)
    el = scene.element(element_id)
    if text is not None:
        el.canonical.text = text
    if font is not None:
        el.canonical.font = font
    el.provenance = "manual"
    return new_version(root, scene_id, scene, note=note, auto=False)


def set_region_mask(root: Path, scene_id: str, frame: int, mask_png: Path, object_id: str, note: str = "set region mask") -> Version:
    scene, _ = current_scene(root, scene_id)
    if not 0 <= frame < scene.frames:
        raise ValueError("frame out of range")
    sd = scene_dir(root, scene_id)
    key = _object_of(sd, object_id)
    object_num = _object_num(key)
    ov = _load(sd)
    n = len(ov["regions"]) + 1
    dst = sd / "stages" / f"ov_{n}.png"
    shutil.copy(mask_png, dst)
    ov["regions"].append({"frame": int(frame), "mask": f"stages/ov_{n}.png", "label": 1000 + object_num})
    _save(sd, ov)
    return rerun(root, scene_id, "regions", note=note)


def add_bbox_prompt(root: Path, scene_id: str, frame: int, bbox: tuple[int, int, int, int], object_id: str,
                    note: str = "bbox prompt") -> Version:
    scene, _ = current_scene(root, scene_id)  # migrate legacy observations before changing caches
    sd = scene_dir(root, scene_id)
    if not 0 <= frame < scene.frames or len(bbox) != 4:
        raise ValueError("invalid frame or box")
    w, h = scene.size
    x0, y0, x1, y1 = bbox
    x0, x1 = max(0, min(w, x0)), max(0, min(w, x1))
    y0, y1 = max(0, min(h, y0)), max(0, min(h, y1))
    if x1 <= x0 or y1 <= y0:
        raise ValueError("box must have positive area")
    bbox = (x0, y0, x1, y1)
    key = _object_of(sd, object_id)
    if scene.element(object_id).kind == "text":
        if not key.startswith("t"):
            raise ValueError("text boxes require a text track")
        path = sd / "stages/text.pkl"
        text = pickle.loads(path.read_bytes())
        track = next(t for t in text["tracks"] if t.id == int(key[1:]))
        if frame not in track.boxes:
            raise ValueError("text was not detected in this frame")
        track.boxes[frame].bbox = bbox
        track.boxes[frame].source = "manual_box"
        path.write_bytes(pickle.dumps(text))
        return rerun(root, scene_id, "regions", note=note)
    _object_num(key)
    frames = np.load(sd / "stages" / "frames.npy", mmap_mode="r")
    bgj = json.loads((sd / "stages" / "background.json").read_text())
    if bgj.get("plate", False):
        plate = cv2.imread(str(sd / PLATE_PATH), cv2.IMREAD_COLOR)
        if plate is None:
            raise FileNotFoundError(sd / PLATE_PATH)
        fg = foreground_mask_plate(frames[frame], cv2.cvtColor(plate, cv2.COLOR_BGR2RGB))
    else:
        fg = foreground_mask(frames[frame], tuple(bgj["rgb"]))
    m = np.zeros(frames.shape[1:3], np.uint8)
    x0, y0, x1, y1 = [max(0, v) for v in bbox]
    m[y0:y1, x0:x1] = fg[y0:y1, x0:x1] * 255
    tmp = sd / "stages" / "_bbox_tmp.png"
    cv2.imwrite(str(tmp), m)
    try:
        return set_region_mask(root, scene_id, frame, tmp, object_id, note=note)
    finally:
        tmp.unlink(missing_ok=True)


def reassign_id(root: Path, scene_id: str, frames: tuple[int, int], from_id: str, to_id: str, note: str = "reassign id") -> Version:
    sd = scene_dir(root, scene_id)
    scene, _ = current_scene(root, scene_id)
    ov = _load(sd)
    src, dst = _object_of(sd, from_id), _object_of(sd, to_id)
    if frames[0] <= 0 and frames[1] >= scene.frames - 1:
        ov["merge"].append([dst, src])
        _save(sd, ov)
        return rerun(root, scene_id, "sprites", note=note)
    src_num, dst_num = _object_num(src), _object_num(dst)
    tracks = __import__("pickle").loads((sd / "stages" / "tracks.pkl").read_bytes())
    t = next(t for t in tracks if t.id == src_num)
    for f in range(frames[0], frames[1] + 1):
        r = t.regions.get(f)
        if r is None:
            continue
        m = np.zeros((scene.size[1], scene.size[0]), np.uint8)
        x0, y0, x1, y1 = r.bbox
        m[y0:y1, x0:x1] = r.mask * 255
        n = len(ov["regions"]) + 1
        cv2.imwrite(str(sd / "stages" / f"ov_{n}.png"), m)
        ov["regions"].append({"frame": f, "mask": f"stages/ov_{n}.png", "label": 1000 + dst_num})
    _save(sd, ov)
    return rerun(root, scene_id, "regions", note=note)
