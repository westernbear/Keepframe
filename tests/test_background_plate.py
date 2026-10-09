import json, pickle
import cv2, numpy as np, pytest
from keepframe.analyze.background import PASS1_PATH, PLATE_PATH, background_plate, estimate_background, foreground_mask_plate, needs_plate
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames, rerun
from keepframe.compose.composer import compose
from keepframe.edit.agent import edit
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, init_project, scene_dir
from keepframe.review.corrections import add_bbox_prompt


def _ramp(h, w, picture=False):
    """A horizontal ramp (a linear gradient); picture=True adds a vertical blue ramp no single gradient fits."""
    ramp = np.linspace(0, 255, w, dtype=np.uint8)
    out = np.stack([np.stack([ramp, ramp[::-1], np.full(w, 80, np.uint8)], -1)] * h)
    if picture:
        out[..., 2] = np.linspace(40, 140, h, dtype=np.uint8)[:, None]
    return out


def _gradient_clip(n=12, h=60, w=120, picture=False):
    frames = np.repeat(_ramp(h, w, picture)[None], n, 0).copy()
    for i in range(n):
        frames[i, 20:36, 5 + i * 8: 21 + i * 8] = (255, 255, 255)   # one moving square
    return frames


def test_plate_isolates_the_moving_square():
    frames = _gradient_clip(h=320, w=640)
    bg, conf = estimate_background(frames)
    assert conf < 0.30
    plate = background_plate(frames)
    assert needs_plate(plate, bg, conf)
    fg = foreground_mask_plate(frames[5], plate)
    assert 200 <= fg.sum() <= 320   # ~16x16 square, not the gradient


def test_small_gradient_keeps_square_separate_from_edge_residuals():
    frames = _gradient_clip()
    bg, conf = estimate_background(frames)
    plate = background_plate(frames)
    assert needs_plate(plate, bg, conf)
    fg = foreground_mask_plate(frames[5], plate)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(fg.astype(np.uint8))
    square = labels[28, 53]
    assert square > 0
    assert stats[square, cv2.CC_STAT_AREA] == 256
    assert tuple(stats[square, :4]) == (45, 20, 16, 16)


def test_estimate_background_is_deterministic():
    frames = _gradient_clip()
    estimates = [estimate_background(frames) for _ in range(10)]
    assert estimates == [estimates[0]] * 10


def _dominant_gradient_clip(n=12, h=320, w=640):
    frames = np.full((n, h, w, 3), (90, 70, 140), np.uint8)
    start = h * 3 // 5
    ramp = np.linspace((90, 70, 140), (180, 180, 200), h - start).astype(np.uint8)
    frames[:, start:] = ramp[None, :, None, :]
    for i in range(n):
        frames[i, 20:36, 5 + i * 8:21 + i * 8] = (255, 255, 255)
    return frames


def test_dominant_gradient_uses_plate_on_analysis_and_rerun(tmp_path):
    frames = _dominant_gradient_clip()
    bg, conf = estimate_background(frames)
    assert conf > 0.50
    assert needs_plate(background_plate(frames), bg, conf)
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", AnalyzeOptions(ocr=False, refine=False, use_ecc=False))
    sd = scene_dir(tmp_path, "s1")
    bgj = json.loads((sd / "stages" / "background.json").read_text())
    assert scene.background.kind == "gradient" and bgj["plate"] is True   # D4: a 3-stop gradient, editable
    assert len(scene.background.gradient.stops) == 3 and scene.background.poster == PLATE_PATH
    assert bgj["confidence"] == conf and len(scene.elements) == 1
    regions = pickle.loads((sd / "stages" / "regions.pkl").read_bytes())
    assert [sum(r.area for r in rs) for rs in regions] == [256] * len(frames)
    init_project(tmp_path, {"file": "ref.mp4", "fps": 30, "size": [640, 320]}, scene)
    rerun(tmp_path, "s1", "background", note="dominant gradient")
    assert current_scene(tmp_path, "s1")[0].background == scene.background
    assert json.loads((sd / "stages" / "background.json").read_text()) == bgj
    regions = pickle.loads((sd / "stages" / "regions.pkl").read_bytes())
    assert [sum(r.area for r in rs) for rs in regions] == [256] * len(frames)


def test_flat_background_with_static_logo_stays_color(tmp_path):
    frames = np.full((12, 320, 640, 3), (90, 70, 140), np.uint8)
    frames[:, 80:120, 320:360] = (250, 20, 20)
    bg, conf = estimate_background(frames)
    assert conf > 0.50
    assert not needs_plate(background_plate(frames), bg, conf)
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", AnalyzeOptions(ocr=False, refine=False, use_ecc=False))
    sd = scene_dir(tmp_path, "s1")
    assert scene.background.kind == "color" and len(scene.elements) == 1
    assert json.loads((sd / "stages" / "background.json").read_text())["plate"] is False
    assert not (sd / PLATE_PATH).exists()
    regions = pickle.loads((sd / "stages" / "regions.pkl").read_bytes())
    assert all(len(rs) == 1 and rs[0].bbox == (320, 80, 360, 120) and rs[0].area == 1600 for rs in regions)


def test_flat_background_with_large_static_central_sprite_stays_color(tmp_path):
    from keepframe.analyze.background import foreground_mask

    frames = np.full((12, 320, 640, 3), (90, 70, 140), np.uint8)
    frames[:, 80:240, 200:440] = (250, 20, 20)
    bg, conf = estimate_background(frames)
    plate = background_plate(frames)
    assert conf > 0.50 and foreground_mask(plate, bg).mean() > 0.10
    assert not needs_plate(plate, bg, conf)
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", AnalyzeOptions(ocr=False, refine=False, use_ecc=False))
    sd = scene_dir(tmp_path, "s1")
    assert scene.background.kind == "color" and len(scene.elements) == 1
    assert json.loads((sd / "stages" / "background.json").read_text())["plate"] is False
    assert not (sd / PLATE_PATH).exists()
    regions = pickle.loads((sd / "stages" / "regions.pkl").read_bytes())
    assert all(len(rs) == 1 and rs[0].bbox == (200, 80, 440, 240) and rs[0].area == 38400 for rs in regions)


def test_gradient_with_static_sharp_logo_preserves_foreground_region(tmp_path):
    frames = _gradient_clip(h=320, w=640)
    frames[:, 80:120, 320:360] = (250, 20, 20)
    plate = background_plate(frames)
    fg = foreground_mask_plate(frames[5], plate)
    assert fg[80:120, 320:360].sum() == 1600
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", AnalyzeOptions(ocr=False, refine=False, use_ecc=False))
    sd = scene_dir(tmp_path, "s1")
    assert scene.background.kind == "gradient"   # the ramp behind the logo is an editable gradient (D4)
    assert json.loads((sd / "stages" / "background.json").read_text())["plate"] is True
    regions = pickle.loads((sd / "stages" / "regions.pkl").read_bytes())
    assert all(any(r.bbox == (320, 80, 360, 120) and r.area == 1600 for r in rs) for rs in regions)
    ids = json.loads((sd / "stages" / "ids.json").read_text())
    tracks = pickle.loads((sd / "stages" / "tracks.pkl").read_bytes())
    logo = next(t for t in tracks if all(r.bbox == (320, 80, 360, 120) for r in t.regions.values()))
    assert scene.element(ids[f"o{logo.id}"]).kind == "sprite"


@pytest.mark.parametrize("h,w", [(10, 14), (64, 96), (96, 64)])
def test_needs_plate_thresholds(h, w):
    from keepframe.analyze.background import PLATE_CONF_MAX, PLATE_NONUNIFORM, PLATE_RING

    assert PLATE_CONF_MAX == 0.30 and PLATE_NONUNIFORM == 0.10 and PLATE_RING == 0.08
    plate = np.zeros((h, w, 3), np.uint8)
    assert needs_plate(plate, (0, 0, 0), 0.29)
    assert not needs_plate(plate, (0, 0, 0), 0.30)
    b = max(2, int(0.08 * min(h, w)))
    plate[b:-b, b:-b] = 255
    assert not needs_plate(plate, (0, 0, 0), 0.90)
    ring = np.ones((h, w), bool)
    ring[b:-b, b:-b] = False
    rows, cols = np.nonzero(ring)
    count = len(rows) // 10
    assert count / len(rows) == 0.10
    plate[rows[:count], cols[:count]] = 255
    assert not needs_plate(plate, (0, 0, 0), 0.90)
    plate[rows[count], cols[count]] = 255
    assert needs_plate(plate, (0, 0, 0), 0.90)


@pytest.mark.parametrize("h,w", [(1, 1), (2, 3), (3, 8)])
def test_needs_plate_ring_covers_tiny_frames(h, w):
    plate = np.zeros((h, w, 3), np.uint8)
    assert not needs_plate(plate, (0, 0, 0), 0.30)
    plate[:] = 255
    assert needs_plate(plate, (0, 0, 0), 0.30)


def test_region_stage_converts_plate_to_lab_once(tmp_path, monkeypatch):
    from keepframe.analyze import background, pipeline

    frames = _gradient_clip(h=320, w=640)
    plate = background_plate(frames)
    conversions = []
    rgb_to_lab = background.rgb_to_lab

    def counted_lab(img):
        if img is plate:
            conversions.append(img)
        return rgb_to_lab(img)

    monkeypatch.setattr(background, "rgb_to_lab", counted_lab)
    monkeypatch.setattr(pipeline, "rgb_to_lab", counted_lab, raising=False)
    regions = pipeline._stage_regions(frames, (127, 127, 80), [[] for _ in frames], AnalyzeOptions(), tmp_path, plate=plate)
    assert len(conversions) == 1
    assert [sum(r.area for r in rs) for rs in regions] == [256] * len(frames)


def test_image_background_is_composed_and_composited(tmp_path):
    plate = _gradient_clip()[0]
    (tmp_path / "assets").mkdir()
    cv2.imwrite(str(tmp_path / PLATE_PATH), cv2.cvtColor(plate, cv2.COLOR_RGB2BGR))
    scene = Scene(id="s1", size=(120, 60), fps=30, frames=1, background=Background(kind="image", value=PLATE_PATH), elements=[])
    html = compose(scene, tmp_path, tmp_path / "c.html").read_text()
    assert 'url("data:image/png;base64,' in html
    canvas = composite_scene(scene, tmp_path, 0)
    assert abs(canvas[30, 10] - plate[30, 10] / 255.0).max() < 0.02


def test_image_background_is_resized_cached_and_under_elements(tmp_path, monkeypatch):
    from keepframe.analyze import composite

    plate = _gradient_clip()[0]
    (tmp_path / "assets").mkdir()
    cv2.imwrite(str(tmp_path / PLATE_PATH), cv2.cvtColor(plate, cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(tmp_path / "assets" / "e1.png"), np.full((2, 2, 4), 255, np.uint8))
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=2, height=2, anchor=(0, 0), texture="assets/e1.png"),
                 visible=(0, 0), tracks={"opacity": Track(keys=[Keyframe(t=0, v=0.5)])})
    scene = Scene(id="s1", size=(60, 30), fps=30, frames=2,
                  background=Background(kind="image", value=PLATE_PATH), elements=[el])
    loads = []
    load = composite.load_texture

    def counted_load(path):
        loads.append(path.name)
        return load(path)

    monkeypatch.setattr(composite, "load_texture", counted_load)
    cache = {}
    expected = cv2.resize(plate.astype(np.float32) / 255.0, scene.size)
    first = composite_scene(scene, tmp_path, 0, cache)
    second = composite_scene(scene, tmp_path, 1, cache)
    assert first.shape == (30, 60, 3) and first.dtype == np.float32
    assert np.allclose(first[:2, :2], expected[:2, :2] * 0.5 + 0.5)
    assert np.allclose(first[2:], expected[2:])
    assert np.allclose(second, expected)
    assert loads == ["background.png", "e1.png"]


@pytest.fixture
def analyzed_plate(tmp_path):
    frames = _gradient_clip(h=320, w=640, picture=True)   # a picture: the image-plate path
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", AnalyzeOptions(ocr=False, refine=False, use_ecc=False))
    init_project(tmp_path, {"file": "ref.mp4", "fps": 30, "size": [640, 320]}, scene)
    return tmp_path, scene, frames


def test_analyze_stores_plate_without_gradient_elements(analyzed_plate):
    root, scene, frames = analyzed_plate
    sd = scene_dir(root, "s1")
    plate = background_plate(frames)
    bgj = json.loads((sd / "stages" / "background.json").read_text())
    assert scene.background == Background(kind="image", value=PLATE_PATH, confidence=bgj["confidence"])
    assert bgj["plate"] is True and bgj["confidence"] < 0.30
    assert bgj["rgb"] == [int(v) for v in plate.reshape(-1, 3).mean(0)]
    assert np.array_equal(cv2.cvtColor(cv2.imread(str(sd / PASS1_PATH)), cv2.COLOR_BGR2RGB), plate)   # pass 1
    clean = _ramp(320, 640, picture=True)
    assert np.array_equal(cv2.cvtColor(cv2.imread(str(sd / PLATE_PATH)), cv2.COLOR_BGR2RGB), clean)   # pass 2
    assert len(scene.elements) == 1
    assert all(el.canonical.texture != scene.background.value for el in scene.elements)
    regions = pickle.loads((sd / "stages" / "regions.pkl").read_bytes())
    assert [sum(r.area for r in rs) for rs in regions] == [256] * len(frames)


@pytest.mark.parametrize("stage", ["background", "text", "regions", "keyframes"])
def test_rerun_preserves_plate_and_region_masks(analyzed_plate, monkeypatch, stage):
    root, scene, frames = analyzed_plate
    sd = scene_dir(root, "s1")
    original_plate = (sd / PLATE_PATH).read_bytes()
    if stage != "background":
        monkeypatch.setattr("keepframe.analyze.pipeline.background_plate", lambda _: pytest.fail("recomputed cached plate"))
    version = rerun(root, "s1", stage, note="plate regression")
    updated, _ = current_scene(root, "s1")
    assert version.id == "v2"
    assert updated.background.kind == "image" and updated.background.value == scene.background.value
    assert updated.background.confidence < 0.30
    assert updated.background.confidence == json.loads((sd / "stages" / "background.json").read_text())["confidence"]
    assert len(updated.elements) == 1
    assert (sd / PLATE_PATH).read_bytes() == original_plate
    regions = pickle.loads((sd / "stages" / "regions.pkl").read_bytes())
    assert [sum(r.area for r in rs) for rs in regions] == [256] * len(frames)


def test_bbox_prompt_uses_plate_to_isolate_square(analyzed_plate):
    root, scene, _ = analyzed_plate
    sd = scene_dir(root, "s1")
    version = add_bbox_prompt(root, "s1", 5, (35, 10, 75, 45), scene.elements[0].id)
    assert version.id == "v2"
    overrides = json.loads((sd / "stages" / "overrides.json").read_text())
    mask = cv2.imread(str(sd / overrides["regions"][-1]["mask"]), cv2.IMREAD_GRAYSCALE)
    assert 200 <= np.count_nonzero(mask) <= 320
    assert np.all(mask[20:36, 45:61] == 255)
    assert not mask[:10].any() and not mask[45:].any()


def test_background_override_disables_plate_on_analysis_and_rerun(tmp_path, analyzed_plate):
    opts = AnalyzeOptions(bg_override="#112233", ocr=False, refine=False, use_ecc=False)
    scene = analyze_scene_frames(_gradient_clip(), 30, tmp_path / "override", "s1", opts)
    sd = scene_dir(tmp_path / "override", "s1")
    assert scene.background == Background(kind="color", value="#112233", confidence=1.0)
    assert json.loads((sd / "stages" / "background.json").read_text())["plate"] is False
    assert not (sd / PLATE_PATH).exists()
    root, _, _ = analyzed_plate
    rerun(root, "s1", "background", note="solid override", options=opts)
    assert current_scene(root, "s1")[0].background == scene.background
    assert json.loads((scene_dir(root, "s1") / "stages" / "background.json").read_text())["plate"] is False
    rerun(root, "s1", "keyframes", note="cached solid override", options=opts)
    assert current_scene(root, "s1")[0].background == scene.background


def test_solid_background_and_legacy_cache_stay_color(tmp_path):
    frames = np.full((12, 60, 120, 3), (17, 34, 51), np.uint8)
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", AnalyzeOptions(ocr=False, refine=False))
    sd = scene_dir(tmp_path, "s1")
    assert scene.background.kind == "color" and scene.background.value == "#112233"
    assert scene.background.confidence >= 0.30
    assert scene.elements == [] and not (sd / PLATE_PATH).exists()
    bgj = json.loads((sd / "stages" / "background.json").read_text())
    assert bgj.pop("plate") is False
    (sd / "stages" / "background.json").write_text(json.dumps(bgj))
    init_project(tmp_path, {"file": "ref.mp4", "fps": 30, "size": [120, 60]}, scene)
    rerun(tmp_path, "s1", "regions", note="legacy cache")
    assert current_scene(tmp_path, "s1")[0].background == scene.background


def test_edit_preserves_plate_and_promotes_only_element_assets(analyzed_plate, monkeypatch):
    from keepframe.verify.verifier import VerifyReport

    root, scene, _ = analyzed_plate
    sd = scene_dir(root, "s1")
    before = (sd / PLATE_PATH).read_bytes()
    assets = {p.name for p in (sd / "assets").iterdir()}
    composed = []

    def candidate_compose(edited, directory, out, **kw):
        assert edited.background == scene.background
        assert (directory / edited.background.value).read_bytes() == before
        assert all(el.canonical.texture != edited.background.value for el in edited.elements)
        result = compose(edited, directory, out, **kw)
        assert 'url("data:image/png;base64,' in result.read_text()
        composed.append(result)
        return result

    monkeypatch.setattr("keepframe.edit.agent.compose", candidate_compose)
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_args, **_kwargs: VerifyReport(schema_ok=True, passed=True))
    attachment = cv2.imencode(".png", np.full((16, 16, 4), 255, np.uint8))[1].tobytes()
    result = edit(root, "s1", "이미지 교체", confirm=True, attachment=attachment,
                  intent={"targets": [{"element": scene.elements[0].id, "property": "texture", "value": "attachment"}]})
    assert result.status == "done" and result.version.id == "v2" and len(composed) == 1
    updated, _ = current_scene(root, "s1")
    assert updated.background == scene.background and len(updated.elements) == 1
    assert (sd / PLATE_PATH).read_bytes() == before
    assert {p.name for p in (sd / "assets").iterdir()} - assets == {updated.elements[0].canonical.texture.split("/")[-1]}
