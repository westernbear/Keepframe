import json, pickle
import cv2, numpy as np, pytest
from keepframe.analyze.background import background_plate, estimate_background, foreground_mask_plate
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames, rerun
from keepframe.compose.composer import compose
from keepframe.edit.agent import edit
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, init_project, scene_dir


@pytest.fixture(autouse=True)
def stable_background_estimation():
    cv2.setRNGSeed(0)


def _gradient_clip(n=12, h=60, w=120):
    ramp = np.linspace(0, 255, w, dtype=np.uint8)
    frames = np.repeat(np.stack([np.stack([ramp, ramp[::-1], np.full(w, 80, np.uint8)], -1)] * h)[None], n, 0).copy()
    for i in range(n):
        frames[i, 20:36, 5 + i * 8: 21 + i * 8] = (255, 255, 255)   # one moving square
    return frames


def test_plate_isolates_the_moving_square():
    frames = _gradient_clip()
    _, conf = estimate_background(frames)
    assert conf < 0.30
    fg = foreground_mask_plate(frames[5], background_plate(frames))
    assert 200 <= fg.sum() <= 320   # ~16x16 square, not the gradient


def test_image_background_is_composed_and_composited(tmp_path):
    plate = _gradient_clip()[0]
    (tmp_path / "assets").mkdir()
    cv2.imwrite(str(tmp_path / "assets" / "background.png"), cv2.cvtColor(plate, cv2.COLOR_RGB2BGR))
    scene = Scene(id="s1", size=(120, 60), fps=30, frames=1, background=Background(kind="image", value="assets/background.png"), elements=[])
    html = compose(scene, tmp_path, tmp_path / "c.html").read_text()
    assert 'url("data:image/png;base64,' in html
    canvas = composite_scene(scene, tmp_path, 0)
    assert abs(canvas[30, 10] - plate[30, 10] / 255.0).max() < 0.02


def test_image_background_is_resized_cached_and_under_elements(tmp_path, monkeypatch):
    from keepframe.analyze import composite

    plate = _gradient_clip()[0]
    (tmp_path / "assets").mkdir()
    cv2.imwrite(str(tmp_path / "assets" / "background.png"), cv2.cvtColor(plate, cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(tmp_path / "assets" / "e1.png"), np.full((2, 2, 4), 255, np.uint8))
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=2, height=2, anchor=(0, 0), texture="assets/e1.png"),
                 visible=(0, 0), tracks={"opacity": Track(keys=[Keyframe(t=0, v=0.5)])})
    scene = Scene(id="s1", size=(60, 30), fps=30, frames=2,
                  background=Background(kind="image", value="assets/background.png"), elements=[el])
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
    frames = _gradient_clip()
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", AnalyzeOptions(ocr=False, refine=False, use_ecc=False))
    init_project(tmp_path, {"file": "ref.mp4", "fps": 30, "size": [120, 60]}, scene)
    return tmp_path, scene, frames


def test_analyze_stores_plate_without_gradient_elements(analyzed_plate):
    root, scene, frames = analyzed_plate
    sd = scene_dir(root, "s1")
    plate = background_plate(frames)
    bgj = json.loads((sd / "stages" / "background.json").read_text())
    assert scene.background == Background(kind="image", value="assets/background.png", confidence=bgj["confidence"])
    assert bgj["plate"] is True and bgj["confidence"] < 0.30
    assert bgj["rgb"] == [int(v) for v in plate.reshape(-1, 3).mean(0)]
    saved = cv2.cvtColor(cv2.imread(str(sd / "assets" / "background.png")), cv2.COLOR_BGR2RGB)
    assert np.array_equal(saved, plate)
    assert len(scene.elements) == 1
    assert all(el.canonical.texture != scene.background.value for el in scene.elements)
    regions = pickle.loads((sd / "stages" / "regions.pkl").read_bytes())
    assert [sum(r.area for r in rs) for rs in regions] == [256] * len(frames)


@pytest.mark.parametrize("stage", ["background", "text", "regions", "keyframes"])
def test_rerun_preserves_plate_and_region_masks(analyzed_plate, monkeypatch, stage):
    root, scene, frames = analyzed_plate
    sd = scene_dir(root, "s1")
    original_plate = (sd / "assets" / "background.png").read_bytes()
    if stage == "background":
        cv2.setRNGSeed(0)
    else:
        monkeypatch.setattr("keepframe.analyze.pipeline.background_plate", lambda _: pytest.fail("recomputed cached plate"))
    version = rerun(root, "s1", stage, note="plate regression")
    updated, _ = current_scene(root, "s1")
    assert version.id == "v2"
    assert updated.background.kind == "image" and updated.background.value == scene.background.value
    assert updated.background.confidence < 0.30
    assert updated.background.confidence == json.loads((sd / "stages" / "background.json").read_text())["confidence"]
    assert len(updated.elements) == 1
    assert (sd / "assets" / "background.png").read_bytes() == original_plate
    regions = pickle.loads((sd / "stages" / "regions.pkl").read_bytes())
    assert [sum(r.area for r in rs) for rs in regions] == [256] * len(frames)


def test_background_override_disables_plate_on_analysis_and_rerun(tmp_path, analyzed_plate):
    opts = AnalyzeOptions(bg_override="#112233", ocr=False, refine=False, use_ecc=False)
    scene = analyze_scene_frames(_gradient_clip(), 30, tmp_path / "override", "s1", opts)
    sd = scene_dir(tmp_path / "override", "s1")
    assert scene.background == Background(kind="color", value="#112233", confidence=1.0)
    assert json.loads((sd / "stages" / "background.json").read_text())["plate"] is False
    assert not (sd / "assets" / "background.png").exists()
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
    assert scene.elements == [] and not (sd / "assets" / "background.png").exists()
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
    before = (sd / "assets" / "background.png").read_bytes()
    assets = {p.name for p in (sd / "assets").iterdir()}
    composed = []

    def candidate_compose(edited, directory, out):
        assert edited.background == scene.background
        assert (directory / edited.background.value).read_bytes() == before
        assert all(el.canonical.texture != edited.background.value for el in edited.elements)
        result = compose(edited, directory, out)
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
    assert (sd / "assets" / "background.png").read_bytes() == before
    assert {p.name for p in (sd / "assets").iterdir()} - assets == {updated.elements[0].canonical.texture.split("/")[-1]}
