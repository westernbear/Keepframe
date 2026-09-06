from __future__ import annotations
import random
from pathlib import Path
import cv2
import numpy as np
from .schema import Background, Canonical, Element, FontGuess, Keyframe, Scene, Track
from .tracks import PRESET_EASES

PALETTE = [(239, 71, 111), (255, 209, 102), (6, 214, 160), (17, 138, 178), (7, 59, 76), (255, 255, 255)]


def make_texture(path: Path, shape: str, w: int, h: int, color: tuple[int, int, int]) -> None:
    img = np.zeros((h, w, 4), np.uint8)
    bgr = (color[2], color[1], color[0])
    if shape == "rect":
        cv2.rectangle(img, (0, 0), (w - 1, h - 1), (*bgr, 255), -1)
    else:
        cv2.ellipse(img, (w // 2, h // 2), (w // 2 - 1, h // 2 - 1), 0, 0, 360, (*bgr, 255), -1)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)


def make_text_texture(path: Path, text: str, size_px: int, color: tuple[int, int, int]) -> tuple[int, int]:
    scale = size_px / 22.0  # Hershey simplex cap height ≈ 22 px at scale 1
    thick = max(1, int(round(scale * 2)))
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
    w, h = tw + 4, th + base + 4
    img = np.zeros((h, w, 4), np.uint8)
    cv2.putText(img, text, (2, th + 2), cv2.FONT_HERSHEY_SIMPLEX, scale, (color[2], color[1], color[0], 255), thick, cv2.LINE_8)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)
    return w, h


def _track(rng: random.Random, frames: int, v0: float, v1: float, nkeys: int) -> Track:
    ts = sorted(rng.sample(range(1, frames - 1), nkeys - 2)) if nkeys > 2 else []
    ts = [0] + ts + [frames - 1]
    vals = [v0] + [v0 + (v1 - v0) * rng.random() for _ in ts[1:-1]] + [v1]
    keys = []
    for i, (t, v) in enumerate(zip(ts, vals)):
        ease = PRESET_EASES[rng.choice(list(PRESET_EASES))] if i < len(ts) - 1 else None
        keys.append(Keyframe(t=t, v=round(v, 2), ease=ease))
    return Track(keys=keys)


def make_synthetic_scene(scene_root: Path, seed: int, n_elements: int = 4, frames: int = 60,
                         size: tuple[int, int] = (640, 360), fps: float = 30.0, with_text: bool = True,
                         overlap: bool = True) -> Scene:
    rng = random.Random(seed)
    W, H = size
    elements: list[Element] = []
    n = n_elements + (1 if with_text else 0)
    for i in range(1, n + 1):
        eid = f"e{i}"
        color = PALETTE[(i - 1) % len(PALETTE)]
        is_text = with_text and i == n
        if is_text:
            text = rng.choice(["Launch", "Faster", "Ref Studio", "New"])
            tw, th = make_text_texture(scene_root / "assets" / f"{eid}.png", text, 40, color)
            canonical = Canonical(width=tw, height=th, texture=f"assets/{eid}.png", text=text,
                                  font=FontGuess(family_guess="sans-serif", weight=700, size_px=40),
                                  color="#%02x%02x%02x" % color)
        else:
            w, h = rng.randint(40, 160), rng.randint(40, 120)
            make_texture(scene_root / "assets" / f"{eid}.png", rng.choice(["rect", "ellipse"]), w, h, color)
            canonical = Canonical(width=w, height=h, texture=f"assets/{eid}.png")
        cx0, cx1 = rng.uniform(0.15, 0.85) * W, rng.uniform(0.15, 0.85) * W
        cy0, cy1 = rng.uniform(0.2, 0.8) * H, rng.uniform(0.2, 0.8) * H
        if overlap and i > 1:  # make it cross the previous element's path
            cx1 = elements[-1].tracks["x"].keys[0].v
            cy1 = elements[-1].tracks["y"].keys[0].v
        tracks = {"x": _track(rng, frames, cx0, cx1, rng.randint(2, 4)),
                  "y": _track(rng, frames, cy0, cy1, rng.randint(2, 3))}
        if rng.random() < 0.5:
            tracks["rot"] = _track(rng, frames, 0.0, rng.choice([-45.0, 30.0, 90.0]), 2)
        if rng.random() < 0.5:
            s = rng.uniform(0.6, 1.6)
            tracks["sx"] = _track(rng, frames, 1.0, s, 2)
            tracks["sy"] = _track(rng, frames, 1.0, s, 2)
        if rng.random() < 0.5:
            tracks["opacity"] = Track(keys=[Keyframe(t=0, v=0.0, ease=PRESET_EASES["out_quad"]),
                                            Keyframe(t=rng.randint(6, 18), v=1.0)])
        first = rng.choice([0, 0, rng.randint(0, frames // 3)])
        elements.append(Element(id=eid, kind="text" if is_text else "sprite", role="text" if is_text else "secondary",
                                canonical=canonical, visible=(first, frames - 1), tracks=tracks,
                                z=Track(keys=[Keyframe(t=0, v=i)])))
    return Scene(id=f"synth{seed}", size=size, fps=fps, frames=frames,
                 background=Background(kind="color", value="#101418"), elements=elements)
