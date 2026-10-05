"""Immutable, versioned observations, independent of reconstruction transforms."""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import tempfile
from pathlib import Path

import cv2
import numpy as np


def _publish(path: Path, data: bytes) -> None:
    """Publish complete content-addressed files without overwriting existing data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        name = Path(tmp.name)
    try:
        try:
            os.link(name, path)
        except FileExistsError:
            pass
    finally:
        name.unlink()


def _freeze_frames(sd: Path) -> str:
    source = sd / "stages/frames.npy"
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    rel = f"analysis/sources/{digest.hexdigest()}.npy"
    dst = sd / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        with tempfile.NamedTemporaryFile(dir=dst.parent, delete=False) as tmp:
            name = Path(tmp.name)
        try:
            shutil.copyfile(source, name)
            try:
                os.link(name, dst)
            except FileExistsError:
                pass
        finally:
            name.unlink()
    return rel


def mask_region(mask: np.ndarray, bbox) -> dict | None:
    """All contour rings, including holes, islands and disconnected components."""
    x, y, _, _ = map(int, bbox)
    binary = np.ascontiguousarray(mask, dtype=np.uint8)
    if not binary.any():
        return None
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    rings = []
    for contour, relation in zip(contours, hierarchy[0]):
        points = contour.reshape(-1, 2) + [x, y]
        rings.append({"points": points.tolist(), "parent": int(relation[3])})
    yy, xx = np.nonzero(binary)
    return {"bbox": [int(xx.min()) + x, int(yy.min()) + y, int(xx.max()) + x + 1, int(yy.max()) + y + 1],
            "rings": rings, "source": "mask"}


def box_region(bbox, source="ocr") -> dict:
    x0, y0, x1, y1 = map(int, bbox)
    return {"bbox": [x0, y0, x1, y1], "source": source,
            "rings": [{"points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "parent": -1}]}


def intervals(frames) -> list[list[int]]:
    out = []
    for f in sorted(set(frames)):
        if out and f == out[-1][1] + 1:
            out[-1][1] = f
        else:
            out.append([f, f])
    return out


def save_snapshot(sd: Path, payload: dict) -> str:
    """Return a project-relative manifest reference. Equal observations deduplicate."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    digest = hashlib.sha256(raw).hexdigest()
    directory = sd / "analysis" / digest
    manifest = {k: v for k, v in payload.items() if k != "frames"}
    manifest["snapshot"] = digest
    for f, observations in enumerate(payload["frames"]):
        _publish(directory / f"{f}.json", json.dumps(observations, ensure_ascii=False).encode())
    _publish(directory / "manifest.json", json.dumps(manifest, ensure_ascii=False).encode())
    return f"scenes/{sd.name}/analysis/{digest}/manifest.json"


def snapshot_from_stages(sd: Path, scene) -> str | None:
    stages = sd / "stages"
    if not all((stages / name).is_file() for name in ("ids.json", "frames.npy", "tracks.pkl", "text.pkl")):
        return None
    ids = json.loads((stages / "ids.json").read_text())
    overrides = stages / "overrides.json"
    ov = json.loads(overrides.read_text()) if overrides.exists() else {}
    aliases = {b: a for a, b in ov.get("merge", [])}
    elements = {e.id: e for e in scene.elements}
    observations = [{} for _ in range(scene.frames)]
    seen: dict[str, list[int]] = {}

    def target(key):
        visited = set()
        while key in aliases and key not in visited:
            visited.add(key)
            key = aliases[key]
        eid = ids.get(key)
        return eid if eid in elements else None

    def add(eid, f, region, ocr=None):
        if eid is None or region is None or not 0 <= f < scene.frames:
            return
        obj = observations[f].setdefault(eid, {"id": eid, "kind": elements[eid].kind, "regions": [], "ocr": []})
        obj["regions"].append(region)
        if ocr is not None:
            obj["ocr"].append(ocr)
        seen.setdefault(eid, []).append(f)

    for track in pickle.loads((stages / "tracks.pkl").read_bytes()):
        for f, region in track.regions.items():
            add(target(f"o{track.id}"), f, mask_region(region.mask, region.bbox))
    solids_path = stages / "solids.pkl"
    if solids_path.is_file():
        for i, solid in enumerate(pickle.loads(solids_path.read_bytes())):
            for f, (bbox, mask) in solid.frames.items():
                x0, y0, x1, y1 = bbox
                add(target(f"solid{i + 1}"), f, mask_region(mask[y0:y1, x0:x1], bbox))
    text = pickle.loads((stages / "text.pkl").read_bytes())
    for track in text["tracks"]:
        for f, box in track.boxes.items():
            add(target(f"t{track.id}"), f, box_region(box.bbox, getattr(box, "source", "ocr")),
                {"text": box.text, "confidence": float(box.conf) if box.conf is not None else None, "bbox": list(box.bbox)})
    payload = {"schema": "keepframe.analysis/1", "size": list(scene.size), "frame_count": scene.frames,
               "frames_file": _freeze_frames(sd),
               "objects": [{"id": eid, "kind": elements[eid].kind, "intervals": intervals(fs)} for eid, fs in seen.items()],
               "frames": [list(frame.values()) for frame in observations]}
    return save_snapshot(sd, payload)


def read_manifest(root: Path, analysis_file: str) -> dict:
    return json.loads((root / analysis_file).read_text())


def frame_overlay(root: Path, scene, version, frame: int) -> dict:
    if not 0 <= frame < scene.frames:
        raise IndexError("frame out of range")
    result = {"scene": scene.id, "version": version.id, "frame": frame, "size": list(scene.size),
              "available": bool(version.analysis_file), "snapshot": None, "objects": []}
    if not version.analysis_file:
        return result
    manifest = read_manifest(root, version.analysis_file)
    result.update(size=manifest["size"], snapshot=manifest["snapshot"])
    result["objects"] = json.loads((root / version.analysis_file).with_name(f"{frame}.json").read_text())
    return result
