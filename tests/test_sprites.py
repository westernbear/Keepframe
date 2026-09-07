import numpy as np, pytest
from refstudio.ir.synth import make_synthetic_scene
from refstudio.ir.tracks import eval_props
from refstudio.analyze.composite import composite_scene
from refstudio.analyze.background import foreground_mask
from refstudio.analyze.regions import build_palette, extract_regions
from refstudio.analyze.tracking import track_regions
from refstudio.analyze.sprites import sprite_props, z_order, RAW_COLS

def analyzed_tracks(scene, d):
    frames = np.stack([(composite_scene(scene, d, f) * 255).round().astype(np.uint8) for f in range(scene.frames)])
    bg = (0x10, 0x14, 0x18)
    fg = np.stack([foreground_mask(f, bg) for f in frames])
    pal = build_palette(frames, fg)
    tracks = track_regions([extract_regions(i, frames[i], fg[i], pal) for i in range(scene.frames)])
    return frames, bg, tracks

def test_props_match_golden_when_unoccluded(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=31, with_text=False, overlap=False)
    frames, bg, tracks = analyzed_tracks(scene, tmp_scene_dir)
    assert len(tracks) == len(scene.elements)
    for t in tracks:
        raw, canon, cf = sprite_props(t, frames, bg, scene.frames, 0)
        assert raw.shape == (scene.frames, len(RAW_COLS)) and canon.shape[2] == 4
        # find the golden element whose position at the canonical frame is nearest
        gold = min(scene.elements, key=lambda e: abs(eval_props(e, cf)["x"] - raw[cf, 0]) + abs(eval_props(e, cf)["y"] - raw[cf, 1]))
        errs = []
        for f in range(gold.visible[0], gold.visible[1] + 1):
            p = eval_props(gold, f)
            if p["opacity"] < 0.5 or np.isnan(raw[f, 0]):
                continue
            errs.append(abs(raw[f, 0] - p["x"]) + abs(raw[f, 1] - p["y"]))
            assert abs(raw[f, 2] - p["sx"]) < 0.08, (gold.id, f)
            assert abs(raw[f, 7] - p["opacity"]) < 0.15, (gold.id, f)
        assert np.median(errs) < 2.0, (gold.id, np.median(errs))

def test_z_order_from_overlap(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=32, with_text=False, overlap=True)
    frames, bg, tracks = analyzed_tracks(scene, tmp_scene_dir)
    z = z_order(tracks, frames, bg)
    assert set(z) == {t.id for t in tracks} and len(set(z.values())) == len(tracks)
