import json, re, subprocess
import cv2, numpy as np, pytest
from keepframe.analyze import plate as plate_mod
from keepframe.analyze.background import PLATE_PATH
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames
from keepframe.analyze.plate import build_plate, load_plate
from keepframe.analyze.regions import Region
from keepframe.analyze.video import read_frames
from keepframe.ir.colour import delta_e, hex_to_rgb8, srgb_to_lab
from keepframe.ir.gradient import gradient_at, render_gradient
from keepframe.ir.schema import Background, Gradient, GradientKey, GradientStop
from keepframe.ir.store import scene_dir
from keepframe.ir.synth import make_reference_scene, render_frames, true_plate

OPTS = AnalyzeOptions(ocr=False, refine=False, use_ecc=False)
MESSAGE = re.compile(r"^background animates \(p95 ΔE \d+\.\d\); kept as a still image$")


def _de(a, b):
    return delta_e(srgb_to_lab(np.asarray(a, np.float64)), srgb_to_lab(np.asarray(b, np.float64)))


def _region(f, mask):
    ys, xs = np.nonzero(mask)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    return Region(frame=f, label=1, color=(0.0, 0.0, 0.0), bbox=(x0, y0, x1, y1), area=int(mask.sum()),
                  centroid=(float(xs.mean()), float(ys.mean())), mask=mask[y0:y1, x0:x1].copy())


def _with_square(plates):
    """A white square crossing each plate, with its region; returns (frames, rbf)."""
    frames = np.array(plates, np.uint8)
    n, H, W = frames.shape[:3]
    rbf = []
    for f in range(n):
        m = np.zeros((H, W), bool)
        x = 4 + f * (W - 40) // max(1, n - 1)
        m[H // 6:H // 6 + 24, x:x + 24] = True
        frames[f, m] = (255, 255, 255)
        rbf.append([_region(f, m)])
    return frames, rbf


def _build(frames, rbf=None):
    n = len(frames)
    return build_plate(frames, rbf or [[] for _ in range(n)], [[] for _ in range(n)], [], [], bg_rgb=(0, 0, 0), bconf=0.6,
                       bg_override=None, pass1=frames[0])


def _stops_lab(g):
    return srgb_to_lab(np.array([hex_to_rgb8(s.color) for s in g.stops], np.float64))


def _aligned(g, truth):
    """g with its angle/stops flipped when it describes the same linear gradient the other way round."""
    if g.kind == "linear" and abs((g.angle - truth.angle) % 360 - 180) < 10:
        return g.model_copy(update={"angle": (g.angle + 180) % 360,
                                    "stops": [GradientStop(offset=1 - s.offset, color=s.color) for s in g.stops[::-1]]})
    return g


def _angle_err(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def test_flat_is_color():
    frames, rbf = _with_square([np.full((90, 160, 3), (40, 60, 90), np.uint8)] * 12)
    model = _build(frames, rbf)
    assert model.kind == "color" and model.rgb == (40, 60, 90) and model.gradient is None
    assert model.stats["temporal_p95_de"] < 1.5


def test_linear_two_stop_is_gradient():
    truth = Gradient(kind="linear", angle=37.0, stops=[GradientStop(offset=0, color="#f3d9b1"), GradientStop(offset=1, color="#c06c84")])
    frames, rbf = _with_square([render_gradient(truth, 320, 180)] * 16)
    model = _build(frames, rbf)
    assert model.kind == "gradient" and not model.gradient_keys
    g = _aligned(model.gradient, truth)
    assert g.kind == "linear" and _angle_err(g.angle, truth.angle) <= 1.0
    assert len(g.stops) == 2 and delta_e(_stops_lab(g), _stops_lab(truth)).max() < 1.5
    assert model.stats["gradient_p95_de"] <= 1.5
    assert np.array_equal(model.image, render_gradient(model.gradient, 320, 180))   # the plate is what the scene draws
    assert np.array_equal(model.at(7), model.image)


def _dominant_gradient_clip(n=12, h=320, w=640):
    frames = np.full((n, h, w, 3), (90, 70, 140), np.uint8)
    start = h * 3 // 5
    ramp = np.linspace((90, 70, 140), (180, 180, 200), h - start).astype(np.uint8)
    frames[:, start:] = ramp[None, :, None, :]
    clean = frames[0].copy()
    rbf = []
    for i in range(n):
        frames[i, 20:36, 5 + i * 8:21 + i * 8] = (255, 255, 255)
        m = np.zeros((h, w), bool)
        m[20:36, 5 + i * 8:21 + i * 8] = True
        rbf.append([_region(i, m)])
    return frames, rbf, clean


def test_dominant_gradient_is_three_stop_gradient():
    frames, rbf, clean = _dominant_gradient_clip()
    model = _build(frames, rbf)
    assert model.kind == "gradient"
    g = model.gradient
    if abs(g.angle % 360) < 10 or abs(g.angle % 360 - 360) < 10:   # bottom → top: flip to top → bottom
        g = _aligned(g, Gradient(angle=180, stops=g.stops))
    assert g.kind == "linear" and _angle_err(g.angle, 180) <= 1.0 and len(g.stops) == 3
    assert g.stops[1].offset == pytest.approx(0.6, abs=0.02)
    assert delta_e(_stops_lab(g)[:2], srgb_to_lab(np.array([(90, 70, 140)] * 2, np.float64))).max() < 1.5
    assert np.percentile(_de(render_gradient(g, 640, 320), clean), 95) <= 1.5


def test_radial_gradient():
    truth = Gradient(kind="radial", center=(0.15, 0.8), radius=1.2,
                     stops=[GradientStop(offset=0, color="#355c7d"), GradientStop(offset=0.5, color="#6c5b7b"),
                            GradientStop(offset=1, color="#c06c84")])
    frames, rbf = _with_square([render_gradient(truth, 320, 180)] * 12)
    model = _build(frames, rbf)
    assert model.kind == "gradient" and model.gradient.kind == "radial"
    cx, cy = model.gradient.center
    assert abs(cx - 0.15) <= 0.03 and abs(cy - 0.8) <= 0.03
    assert np.percentile(_de(model.image, render_gradient(truth, 320, 180)), 95) <= 1.5


def test_photo_texture_is_image(tmp_path):
    scene = make_reference_scene(tmp_path, 4, plate="image", n_sprites=0, n_titles=0, logo=False, frames=12, size=(320, 180))
    pic = true_plate(scene, tmp_path, 0).round().clip(0, 255).astype(np.uint8)
    frames, rbf = _with_square([pic] * 12)
    model = _build(frames, rbf)
    assert model.kind == "image" and model.gradient is None
    assert not model.stats.get("video_candidate") and "message" not in model.stats


def test_compression_noise_still_static(tmp_path):
    truth = Gradient(kind="linear", angle=120.0, stops=[GradientStop(offset=0, color="#0f2027"), GradientStop(offset=1, color="#2c5364")])
    clean = render_gradient(truth, 640, 360).astype(np.float32)
    rng = np.random.default_rng(0)
    for i in range(30):
        cv2.imwrite(str(tmp_path / f"f_{i:05d}.png"),
                    cv2.cvtColor((clean + rng.normal(0, 1.5, clean.shape)).round().clip(0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR))
    out = tmp_path / "noisy.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "30", "-i", str(tmp_path / "f_%05d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", str(out)], check=True)
    frames, _ = read_frames(out)
    assert frames.shape == (30, 360, 640, 3)
    model = _build(frames)
    assert model.stats["temporal_p95_de"] < 1.5   # static: the noise and the codec do not read as motion
    assert model.kind in ("gradient", "image") and not model.gradient_keys
    assert not model.stats.get("video_candidate") and "message" not in model.stats


def test_drifting_gradient_is_animated_gradient():
    n, W, H = 24, 320, 180
    g0 = Gradient(kind="linear", angle=60.0, stops=[GradientStop(offset=0, color="#141e30"), GradientStop(offset=1, color="#243b55")])
    g1 = Gradient(kind="linear", angle=80.0, stops=[GradientStop(offset=0, color="#355c7d"), GradientStop(offset=1, color="#c06c84")])
    bg = Background(kind="gradient", gradient=g0, gradient_keys=[GradientKey(t=0, gradient=g0), GradientKey(t=n - 1, gradient=g1)])
    truth = [render_gradient(gradient_at(bg, f), W, H) for f in range(n)]
    frames, rbf = _with_square(truth)
    model = _build(frames, rbf)
    assert model.kind == "gradient" and len(model.gradient_keys) >= 2
    assert model.stats["temporal_p95_de"] >= 1.5 and model.stats["rank2_ratio"] >= 0.95
    ts = [k.t for k in model.gradient_keys]
    assert ts == sorted(ts) and ts[0] == 0 and ts[-1] == n - 1
    for f in range(n):
        assert np.percentile(_de(model.at(f), truth[f]), 95) < 3, f
    assert np.array_equal(model.crop(5, (10, 20, 40, 50)), model.at(5)[20:50, 10:40])


def _scrolling_texture(n=16, W=160, H=96, amp=16, speed=3):
    """A soft texture drifting sideways: too faint for pass 1 to call it foreground, too busy for a gradient."""
    rng = np.random.default_rng(9)
    noise = cv2.GaussianBlur(rng.normal(0, 1, (H, W + speed * n)).astype(np.float32), (0, 0), 14)
    tex = np.float32([120, 130, 150]) + amp * (noise / np.abs(noise).max())[..., None] * np.float32([1.0, 1.0, 0.8])
    return np.stack([tex[:, speed * f:speed * f + W] for f in range(n)]).round().clip(0, 255).astype(np.uint8)


def test_textured_motion_is_video_candidate_kept_as_image_with_message(tmp_path):
    frames = _scrolling_texture()
    model = _build(frames)
    assert model.kind == "image" and model.gradient is None and model.stats["video_candidate"] is True
    assert MESSAGE.match(model.stats["message"]), model.stats["message"]
    assert model.at(3) is model.image
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", OPTS)
    assert scene.background.kind == "image"
    messages = json.loads((scene_dir(tmp_path, "s1") / "report.json").read_text())["messages"]
    assert any(MESSAGE.match(m) for m in messages), messages


def test_classify_failure_keeps_still_kind_with_message(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("fit exploded")

    monkeypatch.setattr(plate_mod, "fit_gradient", boom)
    truth = Gradient(kind="linear", angle=90.0, stops=[GradientStop(offset=0, color="#000000"), GradientStop(offset=1, color="#ffffff")])
    frames, rbf = _with_square([render_gradient(truth, 160, 90)] * 8)
    model = _build(frames, rbf)
    assert model.kind == "image" and model.confidence < 0.6
    assert model.stats["message"] == "plate classification skipped: RuntimeError: fit exploded"


def test_gradient_reaches_scene_and_composites(tmp_path):
    ref = make_reference_scene(tmp_path / "ref", 3, plate="gradient", n_sprites=2, frames=24)
    assert ref.background.kind == "gradient"
    frames = np.stack([f.round().clip(0, 255).astype(np.uint8) for f in render_frames(ref, tmp_path / "ref", range(ref.frames))])
    scene = analyze_scene_frames(frames, 30, tmp_path / "proj", "s1", OPTS)
    sd = scene_dir(tmp_path / "proj", "s1")
    bg = scene.background
    assert bg.kind == "gradient" and bg.gradient is not None and bg.poster == PLATE_PATH and bg.synthetic is None
    assert (sd / PLATE_PATH).is_file()
    alone = scene.model_copy(update={"elements": []})
    got = composite_scene(alone, sd, 0)
    want = true_plate(ref, tmp_path / "ref", 0) / 255.0
    assert np.abs(got - want).mean() < 0.01
    cached = load_plate(sd)
    assert cached.kind == "gradient" and cached.gradient == bg.gradient and cached.gradient_keys == bg.gradient_keys
