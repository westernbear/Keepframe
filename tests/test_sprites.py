import cv2
import numpy as np, pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.tracks import eval_props
from keepframe.analyze.composite import composite_scene, load_texture
from keepframe.analyze.background import foreground_mask
from keepframe.analyze.regions import Region, build_palette, extract_regions
from keepframe.analyze.tracking import track_regions
from keepframe.analyze.sprites import RAW_COLS, props_from_moments, refine_ecc, sprite_props, z_order

def analyzed_tracks(scene, d):
    frames = np.stack([(composite_scene(scene, d, f) * 255).round().astype(np.uint8) for f in range(scene.frames)])
    bg = (0x10, 0x14, 0x18)
    fg = np.stack([foreground_mask(f, bg) for f in frames])
    pal = build_palette(frames, fg)
    tracks = track_regions([extract_regions(i, frames[i], fg[i], pal) for i in range(scene.frames)])
    return frames, bg, tracks


def test_public_sprite_adapters_restore_consumer_call_shapes():
    region = Region(0, 1, (255.0, 0.0, 0.0), (2, 3, 6, 7), 16, (3.5, 4.5), np.ones((4, 4), bool))
    canon = np.zeros((4, 4, 4), np.uint8)
    canon[..., 0] = 255
    canon[..., 3] = 255
    props = props_from_moments(region, canon)
    assert props["x"] == 4.0 and props["y"] == 5.0
    assert props["sx"] == props["sy"] == 1.0


def test_public_refine_ecc_returns_init_when_real_ecc_rejects_constant_image():
    region = Region(0, 1, (255.0, 0.0, 0.0), (2, 2, 6, 6), 16, (3.5, 3.5), np.ones((4, 4), bool))
    frame = np.zeros((10, 10, 3), np.uint8)
    canon = np.zeros((4, 4, 4), np.uint8)
    canon[..., 3] = 255
    init = {"x": 4.0, "y": 4.0, "sx": 1.0, "sy": 1.0, "rot": 0.0, "skx": 0.0, "sky": 0.0, "opacity": 0.7}
    props = refine_ecc(frame, region, canon, (0.5, 0.5), init)
    assert props["x"] == init["x"] and props["y"] == init["y"]
    assert props["sx"] == init["sx"] and props["sy"] == init["sy"]
    assert props["opacity"] == init["opacity"]


def _track_from_regions(regions):
    from keepframe.analyze.tracking import ObjectTrack

    return ObjectTrack(id=1, regions={r.frame: r for r in regions})


def _region(frame, mask, x=10, y=10):
    ys, xs = np.nonzero(mask)
    bbox = (x, y, x + mask.shape[1], y + mask.shape[0])
    return Region(frame, 1, (255.0, 209.0, 102.0), bbox, int(mask.sum()),
                  (x + float(xs.mean()), y + float(ys.mean())), mask)


def test_sprite_scale_uses_canonical_frame_not_partial_first_observation():
    frames = np.zeros((2, 32, 32, 3), np.uint8)
    frames[:, 10:20, 10:20] = (255, 209, 102)
    partial = np.zeros((8, 8), bool); partial[:] = True
    full = np.ones((10, 10), bool)
    track = _track_from_regions([_region(0, partial), _region(1, full)])

    raw, _, cf = sprite_props(track, frames, (0, 0, 0), 2, 0, use_ecc=False)

    assert cf == 1
    assert raw[cf, 2] == pytest.approx(1.0)
    assert raw[cf, 3] == pytest.approx(1.0)


def test_sprite_rotation_keeps_observed_pose_beyond_one_point_three_degrees():
    frames = np.zeros((2, 64, 64, 3), np.uint8)
    regions = []
    for f, angle in enumerate((0, 20)):
        mask = np.zeros((32, 32), np.uint8)
        cv2.ellipse(mask, (16, 16), (12, 4), angle, 0, 360, 1, -1)
        x, y = 16, 16
        frames[f, y:y + 32, x:x + 32] = (255, 209, 102)
        regions.append(_region(f, mask.astype(bool), x, y))
    track = _track_from_regions(regions)

    raw, _, _ = sprite_props(track, frames, (0, 0, 0), 2, 0, use_ecc=False)

    assert abs(raw[1, 4] - raw[0, 4]) > 10.0

def test_props_match_golden_when_unoccluded(tmp_scene_dir):
    source = make_synthetic_scene(tmp_scene_dir, seed=31, with_text=False, overlap=False)
    for gold in source.elements:
        # Separate layers: their animated paths can cross even with overlap=False.
        scene = source.model_copy(update={"elements": [gold]})
        frames, bg, tracks = analyzed_tracks(scene, tmp_scene_dir)
        assert len(tracks) == 1
        raw, canon, cf = sprite_props(tracks[0], frames, bg, scene.frames, 0)
        gold_texture = load_texture(tmp_scene_dir / gold.canonical.texture)
        gold_area = np.count_nonzero(gold_texture[..., 3] > 0.5)
        canon_area = np.count_nonzero(canon[..., 3] > 127)
        errs = []
        for f in range(gold.visible[0], gold.visible[1] + 1):
            p = eval_props(gold, f)
            if p["opacity"] < 0.5 or np.isnan(raw[f, 0]):
                continue
            errs.append(abs(raw[f, 0] - p["x"]) + abs(raw[f, 1] - p["y"]))
            # Different canonical crops have different native sizes.
            rendered_size = np.sqrt(canon_area * raw[f, 2] * raw[f, 3])
            golden_size = np.sqrt(gold_area * p["sx"] * p["sy"])
            assert abs(rendered_size / golden_size - 1.0) < 0.08, (gold.id, f)
            assert abs(raw[f, 7] - p["opacity"]) < 0.15, (gold.id, f)
        assert np.median(errs) < 2.0, (gold.id, np.median(errs))

def test_z_order_from_overlap(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=32, with_text=False, overlap=True)
    frames, bg, tracks = analyzed_tracks(scene, tmp_scene_dir)
    z = z_order(tracks, frames, bg)
    assert set(z) == {t.id for t in tracks} and len(set(z.values())) == len(tracks)
