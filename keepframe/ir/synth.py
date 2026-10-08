from __future__ import annotations
import random
from pathlib import Path
from typing import Literal, Sequence
import cv2
import numpy as np
from .colour import delta_e, hex_to_rgb8, rgb8_to_hex, srgb_to_lab
from .schema import Background, Canonical, Element, FontGuess, Gradient, GradientKey, GradientStop, Keyframe, Scene, Track
from .tracks import PRESET_EASES

PALETTE = [(239, 71, 111), (255, 209, 102), (6, 214, 160), (17, 138, 178), (7, 59, 76), (255, 255, 255)]


def make_spinning_sphere_video(frames: int = 30, size: tuple[int, int] = (240, 180),
                               radius: float = 48, deg_per_frame: float = 6,
                               translation: tuple[float, float] = (1, 0.5)) -> np.ndarray:
    """Orthographic striped sphere with a known yaw and independent translation (RGB)."""
    w, h = size
    yy, xx = np.mgrid[:h, :w]
    video = np.empty((frames, h, w, 3), np.uint8)
    for f in range(frames):
        x = xx - (w / 2 - translation[0] * (frames - 1) / 2 + translation[0] * f)
        y = yy - (h / 2 - translation[1] * (frames - 1) / 2 + translation[1] * f)
        z = np.sqrt(np.maximum(radius ** 2 - x ** 2 - y ** 2, 0))
        mask = x ** 2 + y ** 2 <= radius ** 2
        lon = np.arctan2(x, z) - np.radians(deg_per_frame * f)
        lat = np.arcsin(np.clip(y / radius, -1, 1))
        stripes = np.sin(8 * lon + 0.5 * np.sin(10 * lat))
        texture = 120 + 75 * stripes + 30 * np.cos(12 * lat)
        video[f] = (16, 20, 24)
        video[f][mask] = np.stack([texture, texture * 0.85, texture * 0.65], axis=-1)[mask].clip(0, 255).astype(np.uint8)
    return video


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
            text = rng.choice(["Launch", "Faster", "Keepframe", "New"])
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


# --- reference scenes with exact ground truth (Stage A eval) ---------------------------------------------

REFERENCE_FONTS = ("DejaVu Sans", "Liberation Serif", "Liberation Sans", "DejaVu Serif")
TITLES = ("Summer Sale", "A Weekend Away", "Big Launch", "New Arrivals", "Grand Opening", "Fresh Picks", "City Lights",
          "Open Studio")
SUBTITLES = ("Up to 50% off", "This Friday only", "Limited edition", "Join us today")
TITLE_COLOURS = ((171, 0, 4), (20, 1, 138), (240, 235, 254), (51, 49, 56), (255, 209, 102), (255, 255, 255), (17, 138, 178))
PLATE_COLOURS = ("#1d2733", "#f4efe6", "#2b1b3d", "#0f3d3e", "#e8d5b7", "#20304a")
GRADIENTS = (("#f3d9b1", "#c06c84"), ("#0f2027", "#2c5364"), ("#ffecd2", "#fcb69f"), ("#141e30", "#243b55"),
             ("#355c7d", "#6c5b7b", "#c06c84"), ("#e0eafc", "#cfdef3", "#a1c4fd"), ("#fbd3e9", "#bb377d"))


def _supersampled(w: int, h: int, alpha, colour, s: int = 4) -> np.ndarray:
    """BGRA texture with true anti-aliased edges: shapes drawn at s×, averaged premultiplied."""
    a = np.zeros((h * s, w * s), np.uint8)
    c = np.zeros((h * s, w * s, 3), np.uint8)
    alpha(a, s)
    colour(c, s)
    af = a.astype(np.float32) / 255
    A = cv2.resize(af, (w, h), interpolation=cv2.INTER_AREA)
    P = cv2.resize(c.astype(np.float32) * af[..., None], (w, h), interpolation=cv2.INTER_AREA)
    C = np.where(A[..., None] > 1e-4, P / np.maximum(A, 1e-4)[..., None], 0)
    return np.dstack([C[..., ::-1], A * 255]).round().clip(0, 255).astype(np.uint8)


def _shape_texture(path: Path, shape: str, w: int, h: int, rgb, accent) -> None:
    """Disc, ring, pill or rect with an accent spot; "stripes" is a striped disc (the mover)."""
    def alpha(a, s):
        W, H = w * s, h * s
        if shape in ("disc", "ring", "stripes"):
            cv2.ellipse(a, (W // 2, H // 2), (W // 2 - s, H // 2 - s), 0, 0, 360, 255, -1, cv2.LINE_AA)
            if shape == "ring":
                cv2.ellipse(a, (W // 2, H // 2), (W // 4, H // 4), 0, 0, 360, 0, -1, cv2.LINE_AA)
            return
        r = min(W, H) // (2 if shape == "pill" else 6)   # rounded rectangle
        cv2.rectangle(a, (s + r, s), (W - s - r, H - s), 255, -1)
        cv2.rectangle(a, (s, s + r), (W - s, H - s - r), 255, -1)
        for cx, cy in ((s + r, s + r), (W - s - r, s + r), (s + r, H - s - r), (W - s - r, H - s - r)):
            cv2.circle(a, (cx, cy), r, 255, -1, cv2.LINE_AA)

    def colour(c, s):
        c[:] = rgb
        if shape == "stripes":
            for x in range(0, c.shape[1], 6 * s):
                c[:, x:x + 3 * s] = accent
        else:
            cv2.circle(c, (c.shape[1] // 3, c.shape[0] // 3), min(c.shape[:2]) // 4, tuple(int(v) for v in accent), -1, cv2.LINE_AA)

    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), _supersampled(w, h, alpha, colour))


def _gradient(rng: random.Random, stops: tuple[str, ...]) -> Gradient:
    if rng.random() < 0.6:
        return Gradient(kind="linear", angle=round(rng.uniform(0, 360), 1),
                        stops=[GradientStop(offset=i / (len(stops) - 1), color=c) for i, c in enumerate(stops)])
    return Gradient(kind="radial", center=(rng.choice([0.15, 0.85]), rng.choice([0.2, 0.8])), radius=round(rng.uniform(1.0, 1.4), 2),
                    stops=[GradientStop(offset=i / (len(stops) - 1), color=c) for i, c in enumerate(stops)])


def _reference_plate(root: Path, rng: random.Random, plate: str, frames: int, W: int, H: int) -> tuple[Background, np.ndarray]:
    """The background and its mean colours, one per key (for picking readable title colours)."""
    if plate == "flat":
        value = rng.choice(PLATE_COLOURS)
        return Background(kind="color", value=value), np.array([hex_to_rgb8(value)], np.float64)
    if plate in ("gradient", "animated"):
        stops = rng.choice(GRADIENTS)
        g = _gradient(rng, stops)
        if plate == "gradient":
            return Background(kind="gradient", gradient=g), np.array([hex_to_rgb8(c) for c in stops], np.float64).mean(0, keepdims=True)
        other = rng.choice([s for s in GRADIENTS if len(s) == len(stops) and s != stops])
        g1 = g.model_copy(update={"angle": (g.angle + rng.uniform(30, 90)) % 360,
                                  "stops": [GradientStop(offset=s.offset, color=c) for s, c in zip(g.stops, other)]})
        mean = np.stack([np.array([hex_to_rgb8(c) for c in cs], np.float64).mean(0) for cs in (stops, other)])
        return Background(kind="gradient", gradient=g, gradient_keys=[GradientKey(t=0, gradient=g), GradientKey(t=frames - 1, gradient=g1)]), mean
    if plate != "image":
        raise ValueError(f"unknown plate {plate!r}")
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    img = np.empty((H, W, 3), np.float32)
    img[:] = hex_to_rgb8(rng.choice(PLATE_COLOURS))
    for _ in range(3):   # broad colour blobs plus fine grain: a textured, but locally smooth, picture
        cx, cy, s = rng.uniform(0, W), rng.uniform(0, H), rng.uniform(0.25, 0.45) * W
        g = np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * s * s))[..., None] * rng.uniform(0.5, 0.9)
        img = img * (1 - g) + np.float32(rng.choice(PALETTE)) * g
    img += np.random.default_rng(rng.getrandbits(32)).normal(0, 2.0, img.shape).astype(np.float32)
    img = img.round().clip(0, 255).astype(np.uint8)
    path = root / "assets" / "plate.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    return Background(kind="image", value="assets/plate.png"), img.reshape(-1, 3).mean(0, keepdims=True)


def _readable(rng: random.Random, plate_rgb: np.ndarray) -> tuple[int, int, int]:
    lab = srgb_to_lab(plate_rgb)
    for c in rng.sample(TITLE_COLOURS, len(TITLE_COLOURS)):
        if float(delta_e(srgb_to_lab(np.array(c, np.float64)), lab).min()) >= 50:
            return c
    return (255, 255, 255) if lab[:, 0].mean() < 50 else (0, 0, 0)


def _title(root: Path, rng: random.Random, eid: str, text: str, size_px: int, family: str, rgb, frames: int,
           x: float, y: float, start: int, z: int) -> Element:
    from ..edit.textraster import render_lines
    img = render_lines([text], size_px, rgb, family)
    path = root / "assets" / f"{eid}.png"
    if img is None:   # no fontconfig: Hershey glyphs
        w, h = make_text_texture(path, text, size_px, rgb)
        family = "sans-serif"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), img)
        h, w = img.shape[:2]
    intro = max(2, round(frames * rng.uniform(0.12, 0.2)))
    dy = rng.choice([18.0, -18.0, 24.0])
    tracks = {"x": Track(keys=[Keyframe(t=0, v=round(x, 2))]),
              "y": Track(keys=[Keyframe(t=start, v=round(y + dy, 2), ease=PRESET_EASES["out_cubic"]), Keyframe(t=start + intro, v=round(y, 2))]),
              "opacity": Track(keys=[Keyframe(t=start, v=0.0, ease=PRESET_EASES["out_quad"]), Keyframe(t=start + intro, v=1.0)])}
    canonical = Canonical(width=w, height=h, texture=f"assets/{eid}.png", text=text, color=rgb8_to_hex(rgb),
                          font=FontGuess(family_guess=family, weight=400, size_px=size_px, source="system"))
    return Element(id=eid, kind="text", role="text", canonical=canonical, visible=(start, frames - 1), tracks=tracks,
                   z=Track(keys=[Keyframe(t=0, v=z)]))


def make_reference_scene(root: Path, seed: int, *, plate: Literal["flat", "gradient", "animated", "image"] = "gradient",
                         n_sprites: int = 3, n_titles: int = 1, mover: bool = False, logo: bool = True,
                         fonts: Sequence[str] | None = None, frames: int = 60, size: tuple[int, int] = (640, 360),
                         fps: float = 30.0) -> Scene:
    """A motion-graphics reference with known layers: a plate (flat, gradient, animated gradient or picture),
    anti-aliased sprites, a static logo that always covers the same spot, an optional big mover behind the
    title, and titles in named fonts that ease in and then hold still for most of the shot."""
    from ..edit.textraster import resolve_families
    root = Path(root)
    rng = random.Random(seed)
    W, H = size
    background, plate_rgb = _reference_plate(root, rng, plate, frames, W, H)
    if fonts is None:
        fonts = [f for f in REFERENCE_FONTS if f.casefold() in {n.casefold() for n in resolve_families(f)}] or ["sans-serif"]
    elements: list[Element] = []
    if mover:
        m = rng.randint(100, 140)
        _shape_texture(root / "assets" / "mover.png", "stripes", m, m, rng.choice(PALETTE[:4]), rng.choice(PALETTE[2:]))
        y = rng.uniform(0.45, 0.7) * H
        elements.append(Element(id="mover", kind="sprite", canonical=Canonical(width=m, height=m, texture="assets/mover.png"),
                                visible=(0, frames - 1), z=Track(keys=[Keyframe(t=0, v=1)]),
                                tracks={"x": Track(keys=[Keyframe(t=0, v=round(0.15 * W, 2)), Keyframe(t=frames - 1, v=round(0.85 * W, 2))]),
                                        "y": Track(keys=[Keyframe(t=0, v=round(y, 2))]),
                                        "rot": Track(keys=[Keyframe(t=0, v=0.0), Keyframe(t=frames - 1, v=rng.choice([-60.0, 60.0]))])}))
    for i in range(1, n_sprites + 1):
        eid = f"s{i}"
        w, h = rng.randint(30, 90), rng.randint(30, 90)
        shape = rng.choice(["disc", "rect", "ring", "pill"])
        _shape_texture(root / "assets" / f"{eid}.png", shape, w, h, rng.choice(PALETTE), rng.choice(PALETTE))
        tracks = {"x": _track(rng, frames, rng.uniform(0.1, 0.9) * W, rng.uniform(0.1, 0.9) * W, rng.randint(2, 3)),
                  "y": _track(rng, frames, rng.uniform(0.15, 0.85) * H, rng.uniform(0.15, 0.85) * H, 2)}
        if rng.random() < 0.5:
            tracks["rot"] = _track(rng, frames, 0.0, rng.choice([-45.0, 30.0, 90.0]), 2)
        first = rng.choice([0, 0, rng.randint(0, frames // 3)])
        elements.append(Element(id=eid, kind="sprite", canonical=Canonical(width=w, height=h, texture=f"assets/{eid}.png"),
                                visible=(first, frames - 1), tracks=tracks, z=Track(keys=[Keyframe(t=0, v=2 + i if i > 1 else 9)])))
    texts = rng.sample(TITLES, 1) + rng.sample(SUBTITLES, max(0, n_titles - 1))
    x, y = W / 2 + rng.uniform(-0.08, 0.08) * W, rng.uniform(0.35, 0.5) * H
    start = rng.randint(0, max(0, frames // 15))
    for i, text in enumerate(texts):
        size_px = rng.randint(44, 56) if i == 0 else rng.randint(22, 28)
        elements.append(_title(root, rng, f"title{i + 1}", text, size_px, fonts[(seed + i) % len(fonts)], _readable(rng, plate_rgb),
                               frames, x, y + (0 if i == 0 else 1.6 * size_px), start + (0 if i == 0 else frames // 10), 8))
    if logo:
        _shape_texture(root / "assets" / "logo.png", "disc", 44, 44, rng.choice(PALETTE), rng.choice(PALETTE))
        lx, ly = rng.choice([(W - 40, 38), (40, 38), (W - 40, H - 38), (40, H - 38)])
        elements.append(Element(id="logo", kind="sprite", canonical=Canonical(width=44, height=44, texture="assets/logo.png"),
                                visible=(0, frames - 1), z=Track(keys=[Keyframe(t=0, v=12)]),
                                tracks={"x": Track(keys=[Keyframe(t=0, v=float(lx))]), "y": Track(keys=[Keyframe(t=0, v=float(ly))])}))
    return Scene(id=f"ref{seed}", size=size, fps=fps, frames=frames, background=background, elements=elements)


def render_frames(scene: Scene, root: Path, frames: Sequence[int], *, renderer: Literal["numpy", "browser"] = "numpy") -> list[np.ndarray]:
    """RGB float32 0..255 per frame: the numpy compositor (unrounded) or Chromium screenshots."""
    if renderer == "numpy":
        from ..analyze.composite import composite_scene
        cache: dict = {}
        return [composite_scene(scene, root, f, cache) * np.float32(255) for f in frames]
    if renderer != "browser":
        raise ValueError(f"unknown renderer {renderer!r}")
    import tempfile
    from ..compose.composer import compose
    from ..render.renderer import load_frame, render
    with tempfile.TemporaryDirectory(prefix="keepframe-frames-") as td:
        res = render(compose(scene, root, Path(td) / "composition.html"), scene, Path(td) / "render", frames=list(frames), probe=False)
        return [load_frame(res.frames_dir / f"f_{i:05d}.png") * np.float32(255) for i in range(len(res.frames))]


def render_reference(scene: Scene, root: Path, out: Path, *, renderer: Literal["numpy", "browser"] = "numpy") -> Path:
    """The reference clip (H.264 yuv444p crf 8) as the analysis will see it."""
    from ..analyze.video import render_scene_video, write_video
    if renderer == "numpy":
        return render_scene_video(scene, root, out)
    frames = render_frames(scene, root, range(scene.frames), renderer=renderer)
    return write_video(np.stack([f.round().clip(0, 255).astype(np.uint8) for f in frames]), scene.fps, out)


def _alone(scene: Scene, el: Element | None, colour: str) -> Scene:
    return scene.model_copy(update={"elements": [el] if el is not None else [], "background": Background(kind="color", value=colour)})


def layers(scene: Scene, root: Path, frames: Sequence[int], *, renderer: Literal["numpy", "browser"] = "numpy"):
    """Yield (element id, frame, α, F) from a black/white double render of each element on its own:
    α = 1 − mean_c(I_w − I_b)/255, F = I_b/α where α > 0.02 (F in 0..255, 0 elsewhere)."""
    for el in scene.elements:
        black = render_frames(_alone(scene, el, "#000000"), root, frames, renderer=renderer)
        white = render_frames(_alone(scene, el, "#ffffff"), root, frames, renderer=renderer)
        for f, b, w in zip(frames, black, white):
            a = np.clip(1.0 - (w - b).mean(-1) / 255.0, 0.0, 1.0).astype(np.float32)
            fg = np.where(a[..., None] > 0.02, b / np.maximum(a, 1e-6)[..., None], 0).clip(0, 255).astype(np.float32)
            yield el.id, f, a, fg


def ground_truth(scene: Scene, root: Path, frames: Sequence[int], *, renderer: Literal["numpy", "browser"] = "numpy"
                 ) -> dict[str, dict[int, tuple[np.ndarray, np.ndarray]]]:
    out: dict[str, dict[int, tuple[np.ndarray, np.ndarray]]] = {e.id: {} for e in scene.elements}
    for eid, f, a, fg in layers(scene, root, frames, renderer=renderer):
        out[eid][f] = (a, fg)
    return out


def true_plate(scene: Scene, root: Path, f: int, *, renderer: Literal["numpy", "browser"] = "numpy") -> np.ndarray:
    """The background alone at frame f (RGB float32 0..255)."""
    return render_frames(scene.model_copy(update={"elements": []}), root, [f], renderer=renderer)[0]
