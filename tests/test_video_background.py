import numpy as np, pytest
from refstudio.ir.synth import make_synthetic_scene
from refstudio.analyze.video import read_frames, write_video, render_scene_video
from refstudio.analyze.background import estimate_background, foreground_mask
from refstudio.analyze.composite import composite_scene

def test_video_roundtrip_and_background(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=21, frames=12, with_text=False)
    vid = render_scene_video(scene, tmp_scene_dir, tmp_scene_dir / "v.mp4")
    frames, fps = read_frames(vid)
    assert frames.shape == (12, 360, 640, 3) and frames.dtype == np.uint8 and fps == pytest.approx(30, abs=0.01)
    ref = (composite_scene(scene, tmp_scene_dir, 5) * 255).round()
    assert np.abs(frames[5].astype(float) - ref).mean() < 2.0          # near-lossless
    bg, conf = estimate_background(frames)
    assert max(abs(bg[0] - 0x10), abs(bg[1] - 0x14), abs(bg[2] - 0x18)) <= 3 and conf > 0.5
    fg = foreground_mask(frames[5], bg)
    e = scene.elements[0]
    from refstudio.ir.tracks import element_bbox
    x0, y0, x1, y1 = [int(v) for v in element_bbox(e, 5)]
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    assert fg[cy, cx] and not fg[2, 2]

def test_read_frames_range_and_png_dir(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=21, frames=12, with_text=False)
    vid = render_scene_video(scene, tmp_scene_dir, tmp_scene_dir / "v.mp4")
    sub, _ = read_frames(vid, start=3, end=6)
    assert sub.shape[0] == 4
    import cv2
    d = tmp_scene_dir / "pngs"; d.mkdir()
    for i in range(3):
        cv2.imwrite(str(d / f"f_{i:05d}.png"), cv2.cvtColor(sub[i], cv2.COLOR_RGB2BGR))
    fr, fps = read_frames(d)
    assert fr.shape[0] == 3 and fps == 30.0
