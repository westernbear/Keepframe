import time
import cv2, numpy as np, pytest
from keepframe.analyze.background import background_plate, estimate_background
from keepframe.analyze.plate import (build_plate, fill_holes, find_holes, masked_median, occupancy, sample_frames)
from keepframe.analyze.regions import Region
from keepframe.analyze.text import TextBox
from keepframe.ir.colour import delta_e, srgb_to_lab
from keepframe.ir.schema import Keyframe, Track
from keepframe.ir.synth import ground_truth, make_reference_scene, render_frames, true_plate


def _region(f, mask):
    ys, xs = np.nonzero(mask)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    return Region(frame=f, label=1, color=(0.0, 0.0, 0.0), bbox=(x0, y0, x1, y1), area=int(mask.sum()),
                  centroid=(float(xs.mean()), float(ys.mean())), mask=mask[y0:y1, x0:x1].copy())


def _render(scene, root):
    return np.stack([f.round().clip(0, 255).astype(np.uint8) for f in render_frames(scene, root, range(scene.frames))])


def _truth_regions(scene, root, ids):
    rbf = [[] for _ in range(scene.frames)]
    gt = ground_truth(scene.model_copy(update={"elements": [e for e in scene.elements if e.id in ids]}), root, range(scene.frames))
    for per in gt.values():
        for f, (a, _) in per.items():
            if (a > 0.02).any():
                rbf[f].append(_region(f, a > 0.02))
    return rbf, gt


def _de(a, b):
    return delta_e(srgb_to_lab(np.asarray(a, np.float64)), srgb_to_lab(np.asarray(b, np.float64)))


def _dilated(mask, px=8):
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
    return cv2.dilate(mask.astype(np.uint8), k) > 0


def _held_title_scene(root, frames=40, start=12):
    scene = make_reference_scene(root, 1, plate="gradient", n_sprites=0, logo=False, frames=frames)
    title = scene.element("title1")
    title.visible = (start, frames - 1)
    title.tracks = {"x": title.tracks["x"], "y": Track(keys=[Keyframe(t=0, v=title.tracks["y"].keys[-1].v)])}
    return scene


def test_sample_frames_spread_over_the_shot():
    assert sample_frames(30) == list(range(30))
    s = sample_frames(100)
    assert len(s) == 48 and s[0] == 0 and s[-1] == 99 and s == sorted(set(s))
    assert sample_frames(200, 10) == [round(v) for v in np.linspace(0, 199, 10)]


def test_occupancy_paints_regions_ocr_boxes_and_reveals_then_dilates():
    mask = np.zeros((40, 60), bool)
    mask[10:14, 10:14] = True
    rbf = [[_region(0, mask)], []]
    boxes = [[], [TextBox(frame=1, text="hi", bbox=(30, 20, 34, 24), conf=1.0)]]
    occ = occupancy((40, 60), rbf, boxes, {1: [(0, 0, 2, 2)]}, [0, 1], dilate_px=0)
    assert occ.shape == (2, 40, 60) and occ.dtype == bool
    assert np.array_equal(occ[0], mask)
    want = np.zeros((40, 60), bool)
    want[18:26, 28:36] = True   # OCR box + 2 px
    want[0:2, 0:2] = True
    assert np.array_equal(occ[1], want)
    grown = occupancy((40, 60), rbf, boxes, {}, [0])[0]
    assert np.array_equal(grown, _dilated(mask, 8))


def test_occupancy_fills_enclosed_interiors_up_to_a_quarter_of_the_frame():
    def ring(y0, y1, x0, x1, h=100, w=100):
        m = np.zeros((h, w), bool)
        m[y0:y1, x0:x1] = True
        m[y0 + 2:y1 - 2, x0 + 2:x1 - 2] = False
        return m

    small, big, open_ = ring(10, 50, 10, 60), ring(5, 95, 5, 95), ring(60, 104, 50, 90)   # open_: no bottom edge
    occ = occupancy((100, 100), [[_region(0, small)], [_region(1, big)], [_region(2, open_)]], [[], [], []], {}, [0, 1, 2],
                    dilate_px=0)
    assert occ[0][10:50, 10:60].all() and occ[0].sum() == 40 * 50   # enclosed, 46×36 < 25 % → filled
    assert np.array_equal(occ[1], big)                               # 86×86 > 25 % → left open
    assert np.array_equal(occ[2], open_)                             # its inside reaches the frame edge → open


def test_masked_median_ignores_title_present_in_70pct_of_frames(tmp_path):
    scene = _held_title_scene(tmp_path)
    frames = _render(scene, tmp_path)
    n, H, W = frames.shape[:3]
    truth = true_plate(scene, tmp_path, 0)
    rbf, gt = _truth_regions(scene, tmp_path, {"title1"})
    present = sum((gt["title1"][f][0] > 0.02).any() for f in range(n))
    assert present == 28 and present / n == 0.7
    under = gt["title1"][n - 1][0] > 0.5
    assert np.percentile(_de(background_plate(frames), truth)[under], 95) > 15   # pass 1 holds the title
    sample = sample_frames(n)
    plate, counts = masked_median(frames, sample, occupancy((H, W), rbf, [[] for _ in range(n)], {}, sample))
    assert plate.dtype == np.uint8 and counts.min() == 12 and counts.max() == n
    assert np.percentile(_de(plate, truth), 95) < 1.0
    assert np.percentile(_de(plate, truth)[under], 95) < 1.0


def test_masked_median_matches_numpy_median_and_counts():
    rng = np.random.default_rng(3)
    frames = rng.integers(0, 256, (9, 70, 5, 3), dtype=np.uint8)
    occ = rng.random((5, 70, 5)) < 0.4
    occ[:, 0, 0] = True
    sample = [0, 2, 4, 6, 8]
    plate, counts = masked_median(frames, sample, occ, band=16)
    assert np.array_equal(counts, (~occ).sum(0))
    assert counts[0, 0] == 0 and plate[0, 0].tolist() == [0, 0, 0]
    for y, x in [(1, 1), (33, 4), (69, 2)]:
        v = frames[sample, y, x][~occ[:, y, x]].astype(np.float64)
        assert np.array_equal(plate[y, x], np.floor(np.median(v, 0) + 0.5).astype(np.uint8))


def _logo_scene(root, frames=24, plate="gradient", seed=1):
    scene = make_reference_scene(root, seed, plate=plate, n_sprites=0, logo=True, frames=frames)
    return scene.model_copy(update={"elements": [e for e in scene.elements if e.id == "logo"]})


def test_always_covered_logo_hole_poly_on_gradient(tmp_path):
    scene = _logo_scene(tmp_path)
    assert scene.background.kind == "gradient"
    frames = _render(scene, tmp_path)
    n = len(frames)
    rbf, gt = _truth_regions(scene, tmp_path, {"logo"})
    bg, conf = estimate_background(frames)
    model = build_plate(frames, rbf, [[] for _ in range(n)], [], [], bg_rgb=bg, bconf=conf, bg_override=None,
                        pass1=background_plate(frames))
    assert model.kind == "gradient"   # D4; the hole stats and the synthetic mask stay
    holes = model.stats["holes"]
    assert [h["method"] for h in holes] == ["poly"] and holes[0]["rms"] <= 2.0
    want = _dilated(gt["logo"][0][0] > 0.02)
    iou = (model.synthetic & want).sum() / (model.synthetic | want).sum()
    assert iou >= 0.95
    truth = true_plate(scene, tmp_path, 0)
    assert np.percentile(_de(model.image, truth)[model.synthetic], 95) < 1.0
    assert np.percentile(_de(model.image, truth), 95) < 1.0
    frac = model.synthetic.mean()
    assert model.stats["synthetic_fraction"] == pytest.approx(frac)
    assert model.confidence == pytest.approx(conf * (1 - 0.5 * frac))


def test_textured_hole_uses_inpaint_and_lowers_confidence():
    rng = np.random.default_rng(5)
    tex = cv2.resize(rng.integers(40, 220, (15, 20, 3), dtype=np.uint8), (160, 120), interpolation=cv2.INTER_NEAREST)
    frames = np.repeat(tex[None], 12, 0)
    logo = np.zeros((120, 160), bool)
    logo[40:70, 60:100] = True
    frames[:, logo] = (250, 250, 250)
    rbf = [[_region(f, logo)] for f in range(12)]
    model = build_plate(frames, rbf, [[] for _ in range(12)], [], [], bg_rgb=(128, 128, 128), bconf=0.8,
                        bg_override=None, pass1=background_plate(frames))
    assert model.kind == "image"
    assert [h["method"] for h in model.stats["holes"]] == ["inpaint"]
    assert np.array_equal(model.synthetic, _dilated(logo))
    assert not np.any(np.all(model.image[model.synthetic] == 250, axis=-1))
    assert model.confidence == pytest.approx(0.8 * (1 - 0.5 * model.synthetic.mean())) and model.confidence < 0.8
    assert np.array_equal(model.image[~model.synthetic], tex[~model.synthetic])


def test_hole_check_uses_all_frames_not_only_samples():
    n = 100
    sample = sample_frames(n)
    away = [f for f in range(n) if f not in sample][:3]
    yy, xx = np.mgrid[0:64, 0:96]
    plate = np.stack([xx * 2, yy * 3, np.full_like(xx, 90)], -1).astype(np.uint8)
    frames = np.repeat(plate[None], n, 0)
    square = np.zeros((64, 96), bool)
    square[20:36, 30:50] = True
    rbf = []
    for f in range(n):
        if f not in away:
            frames[f, square] = (255, 0, 0)
        rbf.append([] if f in away else [_region(f, square)])
    occ = occupancy((64, 96), rbf, [[] for _ in range(n)], {}, sample)
    med, counts = masked_median(frames, sample, occ)
    assert (counts[square] == 0).all()
    holes, uncovered = find_holes(counts, frames, rbf, [[] for _ in range(n)], {})
    assert not holes.any()
    filled, synthetic, info = fill_holes(med, holes, uncovered)
    assert not synthetic.any() and info == []
    assert np.array_equal(filled, plate)
    model = build_plate(frames, rbf, [[] for _ in range(n)], [], [], bg_rgb=(0, 0, 0), bconf=0.5, bg_override=None,
                        pass1=plate)
    assert model.stats["holes"] == [] and model.confidence == 0.5
    assert np.array_equal(model.image, plate)


def test_flat_plate_with_covered_logo_is_color_and_override_skips_work(monkeypatch):
    frames = np.full((10, 60, 80, 3), (90, 70, 140), np.uint8)
    logo = np.zeros((60, 80), bool)
    logo[10:30, 20:40] = True
    frames[:, logo] = (250, 20, 20)
    rbf = [[_region(f, logo)] for f in range(10)]
    model = build_plate(frames, rbf, [[] for _ in range(10)], [], [], bg_rgb=(90, 70, 140), bconf=0.9,
                        bg_override=None, pass1=None)
    assert model.kind == "color" and model.rgb == (90, 70, 140) and model.confidence == 0.9
    assert model.synthetic is None and model.stats["p95_flat_de"] < 2.0
    from keepframe.analyze import plate as plate_mod
    monkeypatch.setattr(plate_mod, "masked_median", lambda *a, **k: pytest.fail("override computed a plate"))
    over = build_plate(frames, rbf, [[] for _ in range(10)], [], [], bg_rgb=(1, 2, 3), bconf=1.0,
                       bg_override="#112233", pass1=None)
    assert (over.kind, over.rgb, over.confidence) == ("color", (17, 34, 51), 1.0)


def test_plate_v2_speed():
    n, H, W = 48, 1080, 1920
    yy, xx = np.mgrid[0:H, 0:W]
    base = np.stack([xx * 255 // W, yy * 255 // H, np.full_like(xx, 120)], -1).astype(np.uint8)
    frames = np.repeat(base[None], n, 0)
    noise = np.random.default_rng(0).integers(0, 3, (H, W, 3), dtype=np.uint8)
    frames += noise
    boxes = []
    for f in range(n):
        row = []
        for k in range(10):
            x0 = 100 + 170 * k + (f * 9 if k % 2 else 0)
            y0 = 80 + 90 * k
            row.append(TextBox(frame=f, text="t", bbox=(x0, y0, x0 + 160, y0 + 60), conf=1.0))
            frames[f, y0:y0 + 60, x0:x0 + 160] = (250, 250, 250)
        boxes.append(row)
    rbf = [[] for _ in range(n)]
    t = time.perf_counter()
    model = build_plate(frames, rbf, boxes, [], [], bg_rgb=(128, 128, 120), bconf=0.2, bg_override=None, pass1=base)
    took = time.perf_counter() - t
    assert model.kind == "image" and len(model.stats["holes"]) == 5
    assert took < 8.0, took
