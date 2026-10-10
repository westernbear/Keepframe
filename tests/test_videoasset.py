import numpy as np, pytest
from keepframe.analyze import videoasset
from keepframe.analyze.videoasset import VideoReader, encode_webm, fill_video_plate
from keepframe.ir.colour import delta_e, srgb8_to_lab

pytestmark = pytest.mark.skipif(not videoasset.ffmpeg_vp9_ok(), reason="ffmpeg with libvpx-vp9 required")


def _rgba_clip(n=8, w=97, h=61):
    """A soft-edged disc moving over a transparent field (odd size: chroma rounding)."""
    yy, xx = np.mgrid[:h, :w].astype(np.float64)
    out = np.zeros((n, h, w, 4), np.uint8)
    for f in range(n):
        d = np.hypot(xx - (30 + 4 * f), yy - 30)
        a = np.clip(22 - d, 0, 1)   # 1 px soft rim
        out[f, ..., 0] = 40 + 3 * xx
        out[f, ..., 1] = 200 - 2 * yy
        out[f, ..., 2] = 90 + 10 * f
        out[f, ..., 3] = np.rint(a * 255)
    return out


def test_webm_rgba_roundtrip(tmp_path):
    clip = _rgba_clip()
    out = encode_webm(clip, 30.0, tmp_path / "a.webm", alpha=True)
    assert out.is_file() and out.read_bytes()[:4] == b"\x1a\x45\xdf\xa3"   # EBML: a WebM file
    with VideoReader(out, (97, 61), alpha=True) as r:
        got = np.stack([r.frame(f) for f in range(len(clip))])
    assert got.shape == clip.shape and got.dtype == np.uint8
    a, b = clip[..., 3] / 255.0, got[..., 3] / 255.0
    assert np.abs(a - b).mean() <= 0.03
    inner = clip[..., 3] == 255
    de = delta_e(srgb8_to_lab(got[..., :3][inner]), srgb8_to_lab(clip[..., :3][inner]))
    assert np.percentile(de, 95) <= 4


def test_reader_sequential_and_backward_seek(tmp_path):
    n = 12
    clip = np.zeros((n, 32, 48, 3), np.uint8)
    for f in range(n):
        clip[f] = (20 * f, 255 - 20 * f, 128)
    path = encode_webm(iter(clip), 25.0, tmp_path / "c.webm", alpha=False)   # any iterable of frames
    near = lambda img, f: np.abs(img[16, 24].astype(int) - clip[f, 16, 24]).max() <= 3
    with VideoReader(path, (48, 32), alpha=False) as r:
        assert all(near(r.frame(f), f) for f in range(n)) and r.starts == 1   # one decoder, read in order
        assert near(r.frame(9), 9) and r.starts == 1                            # recent frames come from the cache
        assert near(r.frame(1), 1) and r.starts == 2                            # backward past the cache: restart
        assert near(r.frame(2), 2) and r.starts == 2                            # then sequential again
        assert near(r.frame(n + 5), n - 1)                                      # past the end: the last frame
    assert r.frame(4).shape == (32, 48, 3)                                     # reopens after close


def test_fill_video_plate_takes_the_nearest_uncovered_frame(tmp_path):
    n, H, W = 7, 4, 5
    frames = np.zeros((n, H, W, 3), np.uint8)
    for f in range(n):
        frames[f] = 10 * f
    occ = np.zeros((n, H, W), bool)
    occ[1:5, 0, 0] = True          # covered in frames 1..4: 1 → 0, 2 → 0 (tie: earlier), 3 → 5, 4 → 5
    occ[:, 1, 1] = True            # never uncovered: the hole fill
    hole = np.full((H, W, 3), 77, np.uint8)
    out = fill_video_plate(frames, occ, hole, tmp_path / "p.npy", band=2)
    assert out.shape == frames.shape and out.dtype == np.uint8
    assert [int(out[f, 0, 0, 0]) for f in range(n)] == [0, 0, 0, 50, 50, 50, 60]
    assert (out[:, 1, 1] == 77).all() and (out[:, 2, 3, 0] == 10 * np.arange(n)).all()
    assert np.array_equal(np.load(tmp_path / "p.npy", mmap_mode="r"), out)


def _bars(n, w, h, alpha=False):
    """Frame f is one flat colour (and, with alpha, opaque on its left half only)."""
    out = np.zeros((n, h, w, 4 if alpha else 3), np.uint8)
    for f in range(n):
        out[f, ..., :3] = ((37 * f) % 256, 255 - (23 * f) % 256, (90 + 41 * f) % 256)
        if alpha:
            out[f, :, : w // 2, 3] = 255
    return out


def _video_scene(tmp_path, n=6):
    from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
    import cv2
    (tmp_path / "assets").mkdir(exist_ok=True)
    encode_webm(_bars(n, 64, 48), 30.0, tmp_path / "assets" / "background.webm", alpha=False)
    encode_webm(_bars(n, 20, 16, alpha=True), 30.0, tmp_path / "assets" / "e1.video.webm", alpha=True)
    cv2.imwrite(str(tmp_path / "assets" / "background.png"), np.full((48, 64, 3), 9, np.uint8))
    poster = np.full((16, 20, 4), 200, np.uint8)
    poster[..., 3] = 255
    cv2.imwrite(str(tmp_path / "assets" / "e1.png"), poster)
    el = Element(id="e1", kind="sprite", visible=(2, n - 1), z=Track(keys=[Keyframe(t=0, v=-1)]),
                 canonical=Canonical(width=20, height=16, texture="assets/e1.png", video="assets/e1.video.webm"),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=20.0)]), "y": Track(keys=[Keyframe(t=0, v=20.0)])})
    return Scene(id="v", size=(64, 48), fps=30.0, frames=n, elements=[el],
                 background=Background(kind="video", value="assets/background.webm", poster="assets/background.png"))


def test_compositor_plays_video_background_and_sprite(tmp_path):
    from keepframe.analyze.composite import composite_scene
    scene, cache = _video_scene(tmp_path), {}
    bars = _bars(6, 64, 48)
    for f in (0, 4, 1, 5, 3):   # out of order: the readers seek back
        img = composite_scene(scene, tmp_path, f, cache) * 255
        assert np.abs(img[2, 2] - bars[f, 0, 0]).max() <= 3                    # the plate is frame f of the video
        if f >= 2:   # the sprite's frame f − start, opaque on its left half (x 10..20), clear on its right
            assert np.abs(img[20, 14] - bars[f - 2, 0, 0]).max() <= 6   # crf 30 on a 20×16 clip
            assert np.abs(img[20, 26] - bars[f, 0, 0]).max() <= 3


def test_compositor_falls_back_to_poster_and_texture(tmp_path):
    from keepframe.analyze.composite import composite_scene
    scene = _video_scene(tmp_path)
    (tmp_path / "assets" / "background.webm").write_bytes(b"not a video")
    (tmp_path / "assets" / "e1.video.webm").write_bytes(b"not a video")
    img = composite_scene(scene, tmp_path, 3) * 255
    assert np.abs(img[2, 2] - 9).max() <= 1 and np.abs(img[20, 26] - 200).max() <= 1


def _chromium(scene, root, frames, out):
    from keepframe.compose.composer import compose
    from keepframe.render.renderer import load_frame, render
    res = render(compose(scene, root, root / "c.html"), scene, out, frames=frames, probe=False)
    return res, [load_frame(res.frames_dir / f"f_{i:05d}.png") * 255 for i in range(len(frames))]


@pytest.mark.browser
def test_video_frames_exact_in_chromium(tmp_path):
    from keepframe.analyze.composite import composite_scene
    n = 24
    scene = _video_scene(tmp_path, n)
    bars = _bars(n, 64, 48)
    frames = [0, 1, 17, n - 1, 1, 0]   # and back again
    _, imgs = _chromium(scene, tmp_path, frames, tmp_path / "r")
    for f, img in zip(frames, imgs):
        assert np.abs(img[2, 2] - bars[f, 0, 0]).max() <= 3, f                  # the background's frame f, exactly
        if f >= 2:
            assert np.abs(img[20, 14] - bars[f - 2, 0, 0]).max() <= 6, f        # the sprite's frame f − start
            assert np.abs(img[20, 26] - bars[f, 0, 0]).max() <= 3, f            # its clear half shows the plate
        ref = composite_scene(scene, tmp_path, f) * 255
        assert np.abs(img[2:46, 2:62] - ref[2:46, 2:62]).max() <= 6, f           # numpy and Chromium agree


@pytest.mark.browser
def test_render_deterministic_with_video(tmp_path):
    scene = _video_scene(tmp_path, 24)
    a, _ = _chromium(scene, tmp_path, [0, 17, 1, 23, 5, 5], tmp_path / "a")
    b, _ = _chromium(scene, tmp_path, [23, 5, 0, 1, 17, 0], tmp_path / "b")
    ha, hb = dict(zip(a.frames, a.hashes)), dict(zip(b.frames, b.hashes))
    assert ha == hb and len(set(ha.values())) == 5


def test_colour_edit_on_a_video_sprite_draws_the_edited_still(tmp_path):
    """A recoloured video sprite is its recoloured poster: the clip is dropped (Keepframe never recolours video)."""
    from keepframe.analyze.composite import composite_scene
    from keepframe.edit.apply import apply_edit
    from keepframe.edit.intent import Target
    scene = _video_scene(tmp_path)
    out = apply_edit(scene, tmp_path, [Target(element="e1", property="color", value="#ff0000")], {}, None)
    el = out.element("e1")
    assert el.canonical.video is None and el.canonical.texture != "assets/e1.png"
    assert scene.element("e1").canonical.video == "assets/e1.video.webm"   # the stored scene is untouched
    img = composite_scene(out, tmp_path, 3) * 255
    assert np.abs(img[20, 26] - (200, 0, 0)).max() <= 2                     # the tinted poster, all over its box


def test_encode_capped_reencodes_smaller_then_gives_up(tmp_path, monkeypatch):
    clip = _bars(6, 64, 48)
    crfs = []
    real = videoasset.encode_webm
    monkeypatch.setattr(videoasset, "encode_webm", lambda *a, crf, **k: crfs.append(crf) or real(*a, crf=crf, **k))
    path, code = videoasset.encode_capped(lambda: iter(clip), 30.0, tmp_path / "a.webm", alpha=False)
    assert (path, code, crfs) == (tmp_path / "a.webm", None, [30])
    monkeypatch.setattr(videoasset, "MAX_BYTES", 10)   # over the cap at any quality
    path, code = videoasset.encode_capped(lambda: iter(clip), 30.0, tmp_path / "b.webm", alpha=False)
    assert (path, code, crfs[1:]) == (None, "video_too_large", [30, 36]) and not (tmp_path / "b.webm").exists()
    path, code = videoasset.encode_capped(lambda: iter([np.zeros((4, 4), np.uint8)]), 30.0, tmp_path / "c.webm", alpha=False)
    assert (path, code) == (None, "encode_failed") and not list(tmp_path.glob("*c.webm*"))


@pytest.mark.browser
def test_video_seek_that_never_lands_fails_with_a_code(tmp_path):
    """R58: a seek whose `seeked` never comes ends in a timeout, and the renderer stops with a code instead of
    waiting forever."""
    from keepframe.compose.composer import compose
    from keepframe.render.renderer import RenderError, render
    scene = _video_scene(tmp_path)
    html = compose(scene, tmp_path, tmp_path / "c.html")
    stub = ("<script>window.__seekTimeoutMs = 300; for (const v of document.querySelectorAll('video'))"
            " Object.defineProperty(v, 'currentTime', {get() { return 0; }, set(t) {}});</script></body>")
    html.write_text(html.read_text().replace("</body>", stub))
    with pytest.raises(RenderError) as e:
        render(html, scene, tmp_path / "r", frames=[3], probe=False)
    assert e.value.code == "video_seek_failed" and str(e.value) == "video_seek_failed"


def _retimed(tmp_path, n=12, speed=2.0):
    from keepframe.edit.retime import retime_scene
    scene = _video_scene(tmp_path, n)
    retime_scene(scene, speed)
    return scene


def test_compositor_plays_retimed_video_at_its_rate(tmp_path):
    """Final review: after a scene speed ×2 the plate shows source frame 2f and the sprite 2(f − start)."""
    from keepframe.analyze.composite import composite_scene
    n = 12
    scene = _retimed(tmp_path, n)
    bars = _bars(n, 64, 48)
    assert (scene.frames, scene.element("e1").visible) == (7, (1, 6))
    cache = {}
    for f in (0, 3, 6, 2):
        img = composite_scene(scene, tmp_path, f, cache) * 255
        assert np.abs(img[2, 2] - bars[min(2 * f, n - 1), 0, 0]).max() <= 3, f
        if f >= 1:
            assert np.abs(img[20, 14] - bars[2 * (f - 1), 0, 0]).max() <= 6, f


def test_video_source_frame_matches_ae_time_stretch():
    """Final review N1: AE shows source frame floor(offset · rate) (time stretch 100 / rate, no half-frame offset);
    numpy and the template pick the same frame at every rate (identical to offset at rate 1)."""
    import math
    from keepframe.analyze.composite import video_source_frame
    for rate in (0.5, 0.75, 1.0, 1.5, 2.0, 1 / 3):
        assert [video_source_frame(d, rate) for d in range(24)] == [math.floor(d * rate + 1e-9) for d in range(24)], rate
    assert video_source_frame(-3, 2.0) == 0


def test_compositor_plays_slowed_video_at_its_rate(tmp_path):
    """×0.5: the plate shows source frame floor(f / 2), the sprite floor((f − start) / 2), as AE does."""
    from keepframe.analyze.composite import composite_scene
    n = 12
    scene = _retimed(tmp_path, n, 0.5)
    bars = _bars(n, 64, 48)
    assert (scene.frames, scene.element("e1").visible) == (23, (4, 22))
    cache = {}
    for f in (1, 3, 9, 22, 4, 5):
        img = composite_scene(scene, tmp_path, f, cache) * 255
        assert np.abs(img[2, 2] - bars[f // 2, 0, 0]).max() <= 3, f
        if f >= 4:
            assert np.abs(img[20, 14] - bars[(f - 4) // 2, 0, 0]).max() <= 6, f


@pytest.mark.browser
@pytest.mark.parametrize("speed, frames", [(2.0, [0, 1, 3, 5, 2]), (0.5, [1, 3, 9, 22, 5])])
def test_retimed_video_frames_match_in_chromium(tmp_path, speed, frames):
    import math
    from keepframe.analyze.composite import composite_scene
    n = 12
    scene = _retimed(tmp_path, n, speed)
    bars = _bars(n, 64, 48)
    start = scene.element("e1").visible[0]
    src = lambda d: min(n - 1, math.floor(d * speed + 1e-9))
    _, imgs = _chromium(scene, tmp_path, frames, tmp_path / "r")
    for f, img in zip(frames, imgs):
        assert np.abs(img[2, 2] - bars[src(f), 0, 0]).max() <= 3, f                # the plate at the edited speed
        if f >= start:
            assert np.abs(img[20, 14] - bars[src(f - start), 0, 0]).max() <= 6, f  # so is the sprite
        ref = composite_scene(scene, tmp_path, f) * 255
        assert np.abs(img[2:46, 2:62] - ref[2:46, 2:62]).max() <= 6, f
