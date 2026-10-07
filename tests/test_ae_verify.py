import json
from pathlib import Path

import cv2
import numpy as np
import pytest

import keepframe.ae.verify as verifier
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.render.renderer import RenderResult, frame_hash


def _scene(*elements, size=(20, 16), frames=60):
    return Scene(id="s1", size=size, fps=30, frames=frames,
                 background=Background(value="#102030"), elements=list(elements))


def _png(path, image):
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), image)
    return path


def _stub_render(monkeypatch, images, bboxes=None):
    work_paths = []

    def compose(scene, scene_dir, out_html):
        out_html.write_text("synthetic composition")
        work_paths.append(out_html)
        return out_html

    def render(html, scene, out_dir, *, frames, probe):
        assert html.is_file()
        assert frames == sorted(images)
        assert probe is True
        frames_dir = out_dir / "frames"
        hashes = []
        for i, frame in enumerate(frames):
            path = _png(frames_dir / f"f_{i:05d}.png", images[frame])
            hashes.append(frame_hash(path.read_bytes()))
        work_paths.append(out_dir)
        return RenderResult(frames_dir=frames_dir, frames=frames, hashes=hashes,
                            bboxes=bboxes or {el.id: [] for el in scene.elements})

    monkeypatch.setattr(verifier, "compose", compose)
    monkeypatch.setattr(verifier, "render", render)
    return work_paths


def test_sample_frames_spreads_evenly_with_both_ends():
    from keepframe.ae.verify import sample_frames

    assert sample_frames(150) == sorted(set(sample_frames(150)))
    assert sample_frames(150)[0] == 0
    assert sample_frames(150)[-1] == 149
    assert len(sample_frames(150)) == 16
    assert sample_frames(5) == [0, 1, 2, 3, 4]
    assert sample_frames(1) == [0]
    assert sample_frames(0) == []
    assert sample_frames(10, n=3) == [0, 4, 9]


def test_compare_frames_l1_and_mask():
    a = np.zeros((10, 10, 3), np.uint8)
    b = a.copy()
    b[:5] = 255
    l1, diff = verifier.compare_frames(a, b)
    assert l1 == pytest.approx(0.5)
    assert diff.shape == a.shape and diff.dtype == np.uint8
    assert np.array_equal(diff, b)
    assert verifier.compare_frames(a, b, [(0, 0, 10, 5)])[0] == 0.0
    assert not a.any()


def test_compare_frames_avoids_uint8_wraparound_and_amplifies_diff():
    a = np.array([[[255, 20, 0]]], np.uint8)
    b = np.array([[[0, 0, 80]]], np.uint8)
    expected = (255 + 20 + 80) / (3 * 255)
    for first, second in [(a, b), (b, a)]:
        l1, diff = verifier.compare_frames(first, second)
        assert l1 == pytest.approx(expected)
        assert diff.tolist() == [[[255, 80, 255]]]


@pytest.mark.parametrize("masks, expected", [
    ([(0, 0, 2, 4), (1, 0, 3, 4)], 1.0),
    ([(-20, -20, 20, 20)], 0.0),
    ([(-20, -20, -1, -1)], 1.0),
    ([(5, 5, 10, 10), (3, 3, 1, 1)], 1.0),
])
def test_compare_frames_uses_unmasked_pixel_count_and_clips_masks(masks, expected):
    a = np.zeros((4, 4, 3), np.uint8)
    b = np.full_like(a, 255)
    l1, diff = verifier.compare_frames(a, b, masks)
    assert l1 == expected
    assert np.array_equal(diff, b)


@pytest.mark.parametrize("other", [
    np.zeros((2, 1, 3), np.uint8),
    np.zeros((2, 2), np.uint8),
    np.zeros((2, 2, 3), np.float32),
])
def test_compare_frames_rejects_incompatible_images(other):
    with pytest.raises(ValueError, match="uint8"):
        verifier.compare_frames(np.zeros((2, 2, 3), np.uint8), other)


@pytest.mark.parametrize("larger", [True, False])
def test_ae_frames_are_resized_to_scene_size(tmp_path, monkeypatch, larger):
    if larger:
        ae = np.zeros((6, 6, 3), np.uint8)
        ae[1, 1] = [0, 90, 180]
        kf = np.zeros((2, 2, 3), np.uint8)
        kf[0, 0] = [0, 10, 20]
    else:
        ae = np.repeat(np.array([[0, 80], [160, 240]], np.uint8)[..., None], 3, axis=2)
        kf = np.repeat(np.array([
            [0, 20, 60, 80], [40, 60, 100, 120],
            [120, 140, 180, 200], [160, 180, 220, 240],
        ], np.uint8)[..., None], 3, axis=2)
    _stub_render(monkeypatch, {7: kf})
    scene = _scene(size=(kf.shape[1], kf.shape[0]))
    report = verifier.verify(scene, tmp_path, {7: _png(tmp_path / "ae.png", ae)}, tmp_path / "report")
    assert report["passed"] and report["mean"] == report["max"] == 0.0
    assert np.array_equal(cv2.imread(str(tmp_path / "report/f0007_ae.png")), kf)


def test_pass_rule_and_worst_three(tmp_path, monkeypatch):
    black = np.zeros((10, 10, 3), np.uint8)
    ae_frames, images = {}, {}
    for frame, count in reversed(list(enumerate([0, 1, 6, 2, 3]))):
        image = black.copy()
        image.reshape(-1, 3)[:count] = 255
        ae_frames[frame] = _png(tmp_path / f"ae-{frame}.png", image)
        images[frame] = black
    work_paths = _stub_render(monkeypatch, images)
    out = tmp_path / "report"
    report = verifier.verify(_scene(size=(10, 10)), tmp_path, ae_frames, out)
    assert set(report) == {"passed", "mean", "max", "thresholds", "frames", "worst", "notes", "masked"}
    assert report["passed"] is False
    assert report["mean"] == pytest.approx(0.024)
    assert report["max"] == pytest.approx(0.06)
    assert report["thresholds"] == {"mean": 0.02, "frame": 0.05}
    assert [row["frame"] for row in report["frames"]] == [0, 1, 2, 3, 4]
    assert [row["l1"] for row in report["frames"]] == pytest.approx([0, 0.01, 0.06, 0.02, 0.03])
    assert [row["frame"] for row in report["worst"]] == [2, 4, 3]
    assert report["notes"] == report["masked"] == []
    expected_names = set()
    for row in report["worst"]:
        frame = row["frame"]
        assert set(row) == {"frame", "l1", "ae", "kf", "diff"}
        assert row["l1"] == report["frames"][frame]["l1"]
        for kind in ("ae", "kf", "diff"):
            name = f"f{frame:04d}_{kind}.png"
            assert row[kind] == name
            expected_names.add(name)
            expected = black if kind == "kf" else cv2.imread(str(ae_frames[frame]))
            assert np.array_equal(cv2.imread(str(out / name)), expected)
    assert {p.name for p in out.iterdir()} == expected_names
    assert all(not path.exists() for path in work_paths)
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("counts, passed, mean, maximum", [
    ([1, 0, 5], True, 0.02, 0.05),
    ([3], False, 0.03, 0.03),
    ([0, 0, 6, 0, 0], False, 0.012, 0.06),
])
def test_verify_requires_both_thresholds_and_accepts_equality(
    tmp_path, monkeypatch, counts, passed, mean, maximum,
):
    black = np.zeros((10, 10, 3), np.uint8)
    ae_frames = {}
    for frame, count in enumerate(counts):
        image = black.copy()
        image.reshape(-1, 3)[:count] = 255
        ae_frames[frame] = _png(tmp_path / f"ae-{frame}.png", image)
    _stub_render(monkeypatch, {frame: black for frame in ae_frames})
    report = verifier.verify(_scene(size=(10, 10)), tmp_path, ae_frames, tmp_path / "report")
    assert report["passed"] is passed
    assert report["mean"] == pytest.approx(mean)
    assert report["max"] == pytest.approx(maximum)
    assert len(report["worst"]) == min(3, len(counts))


def test_sparse_frames_use_render_order_and_stable_ties(tmp_path, monkeypatch):
    images = {10042: np.full((2, 2, 3), 200, np.uint8),
              42: np.full((2, 2, 3), 100, np.uint8), 2: np.zeros((2, 2, 3), np.uint8)}
    ae_frames = {frame: _png(tmp_path / f"ae-{frame}.png", image) for frame, image in images.items()}
    _stub_render(monkeypatch, images)
    out = tmp_path / "report"
    report = verifier.verify(_scene(size=(2, 2), frames=10043), tmp_path, ae_frames, out)
    assert report["passed"] and report["mean"] == 0.0
    assert report["frames"] == [{"frame": frame, "l1": 0.0} for frame in [2, 42, 10042]]
    assert [row["frame"] for row in report["worst"]] == [2, 42, 10042]
    assert report["worst"][-1]["ae"] == "f10042_ae.png"
    assert np.array_equal(cv2.imread(str(out / "f10042_kf.png")), images[10042])


@pytest.mark.parametrize("ae_level", [64, 0], ids=["substituted", "missing-text"])
def test_substituted_text_is_masked_and_noted(tmp_path, monkeypatch, ae_level):
    text = Element(id="t1", kind="text", canonical=Canonical(width=2, height=2, text="Title"), visible=(0, 59))
    kf = np.zeros((16, 20, 3), np.uint8)
    kf[3:13, 5:15] = 128
    ae_frames = {}
    for frame, level in [(3, 96), (17, ae_level)]:
        image = kf.copy()
        image[3:13, 5:15] = level
        ae_frames[frame] = _png(tmp_path / f"ae-{frame}.png", image)
    _stub_render(monkeypatch, {3: kf, 17: kf}, {"t1": [[9, 7, 11, 9], [9, 7, 11, 9]]})
    note = "e1 · title: Arial Bold instead of Pretendard"
    report = verifier.verify(_scene(text), tmp_path, ae_frames, tmp_path / "masked", masked={"t1": note})
    region_l1 = (128 - ae_level) / 255
    assert report["passed"] and report["mean"] == report["max"] == 0.0
    assert report["masked"] == [{"id": "t1", "worst_l1": pytest.approx(region_l1)}]
    assert report["notes"] == [f"{note} — text region differs by {region_l1 * 100:.1f}%"]
    assert cv2.imread(str(tmp_path / "masked/f0017_diff.png"))[3:13, 5:15].any()
    unmasked = verifier.verify(_scene(text), tmp_path, ae_frames, tmp_path / "unmasked")
    assert unmasked["passed"] is False


def test_text_mask_rounds_outward_clips_and_preserves_outside_errors(tmp_path, monkeypatch):
    text = Element(id="t1", kind="text", canonical=Canonical(width=3.4, height=2.2), visible=(0, 59))
    black = np.zeros((10, 10, 3), np.uint8)
    ae = black.copy()
    ae[:8, :7] = 255
    ae_path = _png(tmp_path / "ae.png", ae)
    _stub_render(monkeypatch, {0: black}, {"t1": [[-1.2, 1.2, 2.2, 3.4]]})
    out = tmp_path / "report"
    scene = _scene(text, size=(10, 10))
    report = verifier.verify(scene, tmp_path, {0: ae_path}, out, masked={"t1": "substituted"})
    assert report["passed"] and report["mean"] == 0.0
    assert report["masked"] == [{"id": "t1", "worst_l1": 1.0}]
    ae[8, 7] = 255
    _png(ae_path, ae)
    report = verifier.verify(scene, tmp_path, {0: ae_path}, out, masked={"t1": "substituted"})
    assert report["passed"] is False
    assert report["mean"] == pytest.approx(1 / 44)


@pytest.mark.parametrize("bbox", [
    [4, 4, 4, 5], [4, 4, 5, 4],
    [-2, 2, -0.1, 4], [10.1, 2, 12, 4], [2, 10.1, 4, 12],
])
def test_empty_or_off_frame_text_boxes_are_skipped(tmp_path, monkeypatch, bbox):
    text = Element(id="t1", kind="text", canonical=Canonical(width=2, height=2), visible=(0, 59))
    black = np.zeros((10, 10, 3), np.uint8)
    ae = _png(tmp_path / "ae.png", np.full_like(black, 255))
    _stub_render(monkeypatch, {0: black}, {"t1": [bbox]})
    report = verifier.verify(_scene(text, size=(10, 10)), tmp_path, {0: ae}, tmp_path / "report",
                             masked={"t1": "substituted"})
    assert report["passed"] is False and report["mean"] == 1.0
    assert report["masked"] == [{"id": "t1", "worst_l1": 0.0}]
    assert report["notes"] == ["substituted — text region differs by 0.0%"]


@pytest.mark.parametrize("missing", [True, False], ids=["missing", "not-image"])
def test_missing_or_unreadable_ae_frame_raises_without_path(tmp_path, monkeypatch, missing):
    path = tmp_path / "invalid.png"
    if not missing:
        path.write_text("not an image")
    _stub_render(monkeypatch, {42: np.zeros((16, 20, 3), np.uint8)})
    with pytest.raises(ValueError) as error:
        verifier.verify(_scene(), tmp_path, {42: path}, tmp_path / "report")
    assert str(error.value) == "frame 42 from AE is missing or not an image"


def test_verify_rejects_empty_ae_frames(tmp_path):
    with pytest.raises(ValueError, match="no AE frames"):
        verifier.verify(_scene(), tmp_path, {}, tmp_path / "report")


@pytest.mark.parametrize("frame", [-1, 60])
def test_verify_rejects_frames_outside_scene(tmp_path, frame):
    image = _png(tmp_path / "ae.png", np.zeros((16, 20, 3), np.uint8))
    with pytest.raises(ValueError, match="outside the scene"):
        verifier.verify(_scene(), tmp_path, {frame: image}, tmp_path / "report")


def test_missing_keepframe_image_is_visible_error(tmp_path, monkeypatch):
    black = np.zeros((16, 20, 3), np.uint8)
    _stub_render(monkeypatch, {0: black})
    render = verifier.render

    def missing_image(*args, **kwargs):
        result = render(*args, **kwargs)
        (result.frames_dir / "f_00000.png").unlink()
        return result

    monkeypatch.setattr(verifier, "render", missing_image)
    ae = _png(tmp_path / "ae.png", black)
    with pytest.raises(ValueError, match="frame 0 from Keepframe is missing or not an image"):
        verifier.verify(_scene(), tmp_path, {0: ae}, tmp_path / "report")


def test_failed_report_image_write_is_visible_error(tmp_path, monkeypatch):
    black = np.zeros((16, 20, 3), np.uint8)
    ae = _png(tmp_path / "ae.png", black)
    _stub_render(monkeypatch, {0: black})
    out = tmp_path / "report"
    imwrite = cv2.imwrite

    def failed_write(path, image):
        return False if Path(path).parent == out else imwrite(path, image)

    monkeypatch.setattr(cv2, "imwrite", failed_write)
    with pytest.raises(OSError, match="could not write f0000_ae.png"):
        verifier.verify(_scene(), tmp_path, {0: ae}, out)


@pytest.mark.browser
def test_browser_own_render_passes_and_missing_sprite_fails(tmp_path):
    sprite = Element(id="sprite", kind="sprite", visible=(0, 9),
                     canonical=Canonical(width=24, height=16, texture="assets/sprite.png"),
                     tracks={"x": Track(keys=[Keyframe(t=0, v=20), Keyframe(t=9, v=44)]),
                             "y": Track(keys=[Keyframe(t=0, v=18)])})
    scene = _scene(sprite, size=(64, 36), frames=10)
    _png(tmp_path / "assets/sprite.png", np.full((16, 24, 3), 255, np.uint8))
    html = verifier.compose(scene, tmp_path, tmp_path / "composition.html")
    reference = verifier.render(html, scene, tmp_path / "reference", frames=list(range(10)), probe=True)
    ae_frames = {frame: reference.frames_dir / f"f_{i:05d}.png" for i, frame in enumerate(reference.frames)}
    report = verifier.verify(scene, tmp_path, ae_frames, tmp_path / "same")
    assert report["passed"] and report["mean"] == report["max"] == 0.0
    image = cv2.imread(str(ae_frames[6]))
    assert np.all(image[10:26, 24:48] == 255)
    image[10:26, 24:48] = [0x30, 0x20, 0x10]
    _png(ae_frames[6], image)
    report = verifier.verify(scene, tmp_path, ae_frames, tmp_path / "changed")
    assert report["passed"] is False
    assert report["worst"][0]["frame"] == 6
    assert report["max"] > 0.05 and report["mean"] < 0.02
    assert all(row["l1"] == 0.0 for row in report["frames"] if row["frame"] != 6)
