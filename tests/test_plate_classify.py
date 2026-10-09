import json, re, subprocess
import cv2, numpy as np, pytest
from keepframe.analyze import plate as plate_mod
from keepframe.analyze.background import PLATE_PATH
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames, rerun
from keepframe.analyze.plate import build_plate, load_plate
from keepframe.analyze.regions import Region
from keepframe.analyze.video import read_frames
from keepframe.ir.colour import delta_e, hex_to_rgb8, srgb_to_lab
from keepframe.ir.gradient import gradient_at, render_gradient
from keepframe.ir.schema import Background, Gradient, GradientKey, GradientStop
from keepframe.ir.store import init_project, scene_dir
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


_RAMP = Gradient(kind="linear", angle=120.0, stops=[GradientStop(offset=0, color="#3a6186"), GradientStop(offset=1, color="#89253e")])


def test_fine_grain_720p_stays_image():
    """A ≤ 160 px fit averages grain away (fit p95 ≈ 1.0); the ≤ 640 px bound keeps it a picture (R19)."""
    clean = render_gradient(_RAMP, 1280, 720).astype(np.float32)
    grain = (clean + np.random.default_rng(1).normal(0, 4, clean.shape)).round().clip(0, 255).astype(np.uint8)
    model = _build(np.repeat(grain[None], 4, 0))
    assert model.stats["gradient_p95_de"] <= 1.5 and model.stats["gradient_full_p95_de"] > 3.0
    assert model.kind == "image" and np.array_equal(model.image, grain)


def test_two_pixel_checker_stays_image():
    yy, xx = np.mgrid[0:360, 0:640]
    chk = ((xx // 2 + yy // 2) % 2 * 2 - 1)[..., None].astype(np.float32)
    plate = (render_gradient(_RAMP, 640, 360).astype(np.float32) + 12 * chk).round().clip(0, 255).astype(np.uint8)
    model = _build(np.repeat(plate[None], 4, 0))
    assert model.stats["gradient_p95_de"] <= 1.5 and model.stats["gradient_full_p95_de"] > 3.0
    assert model.kind == "image" and np.array_equal(model.image, plate)


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


def _texture_with_square(n=24, W=160, H=96, step=16):
    """_scrolling_texture drifting 1 px a frame with an opaque square crossing it fast: (frames, plates)."""
    plates = _scrolling_texture(n, W, H, amp=24, speed=1)
    frames = plates.copy()
    for f in range(n):
        x = 4 + (f * step) % (W - 28)
        frames[f, 36:60, x:x + 24] = (250, 120, 30)
    return frames, plates


@pytest.mark.skipif(not plate_mod.videoasset.ffmpeg_vp9_ok(), reason="ffmpeg with libvpx-vp9 required")
def test_textured_motion_background_is_video_plate(tmp_path):
    from keepframe.analyze.videoasset import VideoReader
    frames, plates = _texture_with_square()
    n, H, W = frames.shape[:3]
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", OPTS)
    sd = scene_dir(tmp_path, "s1")
    bg = scene.background
    assert (bg.kind, bg.value, bg.poster) == ("video", "assets/background.webm", PLATE_PATH)
    assert (sd / bg.value).is_file() and (sd / PLATE_PATH).is_file()
    per_frame = np.load(sd / "stages" / "plate_frames.npy", mmap_mode="r")
    assert per_frame.shape == frames.shape
    messages = json.loads((sd / "report.json").read_text())["messages"]
    assert not any("background animates" in m for m in messages), messages
    assert any(e.kind == "sprite" for e in scene.elements)   # the square is a layer, not part of the plate
    under = (frames != plates).any(-1)
    with VideoReader(sd / bg.value, (W, H)) as video:
        for f in range(n):
            for plate in (per_frame[f], video.frame(f)):   # the stage cache and what renders play
                assert np.percentile(_de(plate[under[f]], plates[f][under[f]]), 95) <= 3, f
    cached = load_plate(sd)
    assert cached.kind == "video" and np.array_equal(cached.at(5), per_frame[5])


def test_no_ffmpeg_falls_back_with_message(tmp_path, monkeypatch):
    monkeypatch.setattr(plate_mod.videoasset, "ffmpeg_vp9_ok", lambda: False)
    frames, _ = _texture_with_square()
    model = _build(frames)
    assert model.kind == "image" and model.gradient is None and model.stats["video_candidate"] is True
    assert MESSAGE.match(model.stats["message"]), model.stats["message"]
    assert model.at(3) is model.image
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", OPTS)
    sd = scene_dir(tmp_path, "s1")
    assert scene.background.kind == "image" and not (sd / "assets" / "background.webm").exists()
    assert not (sd / "stages" / "plate_frames.npy").exists()
    messages = json.loads((sd / "report.json").read_text())["messages"]
    assert any(MESSAGE.match(m) for m in messages), messages


def test_busy_animated_gradient_is_found_again_not_full_frame_sprites(tmp_path):
    """Eval seed 3: a gradient turning from navy to pink. Pass 1's still plate misses it everywhere, so its regions
    cover most of every frame; the plate stage fits the moving gradient robustly, finds the layers again against it
    and ends as an animated gradient, without full-frame sprites copying the background."""
    ref = make_reference_scene(tmp_path / "ref", 3, plate="animated", n_titles=2, frames=24)   # 640×360
    frames = np.stack([f.round().clip(0, 255).astype(np.uint8) for f in render_frames(ref, tmp_path / "ref", range(ref.frames))])
    scene = analyze_scene_frames(frames, 30, tmp_path / "proj", "s1", OPTS)
    sd = scene_dir(tmp_path / "proj", "s1")
    bg = scene.background
    assert bg.kind == "gradient" and len(bg.gradient_keys) >= 2, bg.kind
    assert load_plate(sd).stats.get("busy_pass1", 0) > 0.5
    for f in (0, 12, 23):
        assert np.percentile(_de(render_gradient(gradient_at(bg, f), 640, 360), true_plate(ref, tmp_path / "ref", f)), 95) < 3, f
    area = [e.canonical.width * e.canonical.height / (640 * 360) for e in scene.elements]
    assert max(area) < 0.1 and sum(area) < 0.4, area   # layers (titles in pieces: no OCR here), no plate copies
    # R58: a rerun that reuses the regions found again classifies the same way (the busy decision is stored)
    assert json.loads((sd / "stages" / "busy.json").read_text())["kind"] == "gradient"
    init_project(tmp_path / "proj", {"file": "ref.mp4", "fps": 30, "size": [640, 360]}, scene)
    for stage in ("plate", "tracking"):
        rerun(tmp_path / "proj", "s1", stage, "again")
        again = load_plate(sd)
        assert again.kind == "gradient" and len(again.gradient_keys) >= 2, stage
        assert again.stats["resegmented"] == "gradient" and again.stats["busy_pass1"] > 0.5, stage


def _best_iou(truth, scene) -> float:
    """The best mean bbox IoU (over shared visible frames) of an analysed element with a truth element."""
    from keepframe.ir.tracks import element_bbox

    def iou(a, b):
        w, h = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
        i = max(0.0, w) * max(0.0, h)
        return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i)

    best = 0.0
    for e in scene.elements:
        fs = range(max(truth.visible[0], e.visible[0]), min(truth.visible[1], e.visible[1]) + 1)
        if len(fs):
            best = max(best, float(np.mean([iou(element_bbox(truth, f), element_bbox(e, f)) for f in fs])))
    return best


class _TruthOcr:
    """Fake OCR reading a reference's titles where they are drawn at ≥ 50 % opacity (frames known by content)."""

    def __init__(self, ref, frames):
        import hashlib
        self.key = lambda img: hashlib.sha1(np.ascontiguousarray(img).tobytes()).hexdigest()
        self.index = {self.key(f): i for i, f in enumerate(frames)}
        self.titles = [e for e in ref.elements if e.kind == "text"]

    def __call__(self, frame):
        from keepframe.ir.tracks import element_bbox, eval_props
        f = self.index.get(self.key(frame))
        if f is None:
            return []
        return [(t.canonical.text, tuple(int(round(v)) for v in element_bbox(t, f)), 0.99) for t in self.titles
                if t.visible[0] <= f <= t.visible[1] and eval_props(t, f)["opacity"] >= 0.5]


def test_busy_pass_without_a_gradient_keeps_tracked_layers(tmp_path, monkeypatch):
    """R57: the busy pass wins only with a fitted gradient. Its gate firing (forced) on a seed-3-like clip whose
    gradient turns gently, and no gradient fitting (forced): pass 1's regions stand, so the sprites and the logo it
    tracked stay separate elements and nothing joins the plate."""
    from keepframe.ir.colour import rgb8_to_hex
    monkeypatch.setattr(plate_mod, "BUSY", 0.0)
    monkeypatch.setattr(plate_mod, "_animated_keys", lambda *a, **k: ([], 0.0))
    ref = make_reference_scene(tmp_path / "ref", 3, plate="animated", n_titles=2, frames=16)
    g0 = ref.background.gradient_keys[0].gradient
    g1 = g0.model_copy(update={"angle": g0.angle + 20, "stops": [
        s.model_copy(update={"color": rgb8_to_hex(np.clip(np.add(hex_to_rgb8(s.color), 18), 0, 255))}) for s in g0.stops]})
    ref.background = ref.background.model_copy(update={"gradient_keys": [GradientKey(t=0, gradient=g0), GradientKey(t=15, gradient=g1)]})
    frames = np.stack([f.round().clip(0, 255).astype(np.uint8) for f in render_frames(ref, tmp_path / "ref", range(ref.frames))])
    scene = analyze_scene_frames(frames, 30, tmp_path / "proj", "s1", AnalyzeOptions(refine=False, use_ecc=False, generate_3d=False),
                                 ocr=_TruthOcr(ref, frames))
    sd = scene_dir(tmp_path / "proj", "s1")
    assert "resegmented" not in load_plate(sd).stats and not (sd / "stages" / "busy.json").exists()
    found = {t.id: round(_best_iou(t, scene), 2) for t in ref.elements}
    for eid in ("s1", "s2", "logo", "title1", "title2"):   # what pass 1 separates on this clip (s3 it never does)
        assert found[eid] >= 0.5, found


def test_busy_regions_failure_keeps_pass1_regions(tmp_path, monkeypatch):
    """R58: finding the regions again against the busy gradient failing keeps pass 1's regions and tracks (and their
    caches); the analysis goes on and stores no busy decision."""
    import pickle
    from keepframe.analyze import pipeline
    real = pipeline._stage_regions

    def regions(*a, plate_at=None, **k):
        if plate_at is not None:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(pipeline, "_stage_regions", regions)
    ref = make_reference_scene(tmp_path / "ref", 3, plate="animated", n_titles=2, frames=16)
    frames = np.stack([f.round().clip(0, 255).astype(np.uint8) for f in render_frames(ref, tmp_path / "ref", range(ref.frames))])
    scene = analyze_scene_frames(frames, 30, tmp_path / "proj", "s1", OPTS)
    sd = scene_dir(tmp_path / "proj", "s1")
    stats = load_plate(sd).stats
    assert scene.elements and "resegmented" not in stats and not (sd / "stages" / "busy.json").exists()
    rbf = pickle.loads((sd / "stages" / "regions.pkl").read_bytes())
    cover = np.mean([sum(r.area for r in regions) / (640 * 360) for regions in rbf])
    assert cover > 0.5 and pickle.loads((sd / "stages" / "tracks.pkl").read_bytes())   # pass 1's regions, cached


def test_video_plate_failure_lowers_confidence(tmp_path, monkeypatch):
    """R58: a video plate that fails keeps the still image with its code, at lower confidence than the still a
    machine without VP9 keeps."""
    frames, _ = _texture_with_square()
    n = len(frames)
    rbf = [[] for _ in range(n)]
    build = lambda: build_plate(frames, rbf, [[] for _ in range(n)], [], [], bg_rgb=(0, 0, 0), bconf=0.6, bg_override=None,
                                pass1=frames[0], frames_out=tmp_path / "plate_frames.npy")
    monkeypatch.setattr(plate_mod.videoasset, "ffmpeg_vp9_ok", lambda: False)
    still = build()
    monkeypatch.setattr(plate_mod.videoasset, "ffmpeg_vp9_ok", lambda: True)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(plate_mod.videoasset, "fill_video_plate", boom)
    failed = build()
    assert failed.kind == "image" and failed.stats["message"].endswith("kept as a still image (video_failed)")
    assert failed.confidence == pytest.approx(still.confidence * plate_mod.FALLBACK_CONF)
    assert not (tmp_path / "plate_frames.npy").exists()


def test_video_encode_failure_lowers_confidence(tmp_path, monkeypatch):
    model = plate_mod.PlateModel("video", np.zeros((8, 8, 3), np.uint8), (0, 0, 0), None, 0.8, {"temporal_p95_de": 4.2},
                                 frames=np.zeros((3, 8, 8, 3), np.uint8))
    (tmp_path / "stages").mkdir()
    (tmp_path / plate_mod.FRAMES_PATH).write_bytes(b"x")
    monkeypatch.setattr(plate_mod.videoasset, "encode_capped", lambda *a, **k: (None, "video_too_large"))
    out = plate_mod.write_video(tmp_path, model, 30.0)
    assert (out.kind, out.frames, out.confidence) == ("image", None, pytest.approx(0.8 * plate_mod.FALLBACK_CONF))
    assert out.stats["message"] == "background animates (p95 ΔE 4.2); kept as a still image (video_too_large)"
    assert not (tmp_path / plate_mod.FRAMES_PATH).exists()
