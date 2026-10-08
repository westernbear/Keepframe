import json, re
from pathlib import Path
import cv2, numpy as np, pytest
from keepframe.analyze import pipeline
from keepframe.analyze.background import PASS1_PATH, PLATE_PATH, background_plate
from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames, rerun
from keepframe.analyze.plate import PLATE_JSON, SYNTH_PATH
from keepframe.ir.colour import delta_e, srgb_to_lab
from keepframe.ir.store import current_scene, init_project, scene_dir
from keepframe.ir.synth import make_reference_scene, render_frames, true_plate
from keepframe.progress import STAGES
from keepframe.review.corrections import add_bbox_prompt

OPTS = AnalyzeOptions(ocr=False, refine=False, use_ecc=False)
STATIC = Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static"


def _logo_clip(n=12, h=320, w=640, logo=True):
    ramp = np.linspace(0, 255, w, dtype=np.uint8)
    frames = np.repeat(np.stack([np.stack([ramp, ramp[::-1], np.full(w, 80, np.uint8)], -1)] * h)[None], n, 0).copy()
    if logo:
        frames[:, 80:120, 320:360] = (250, 20, 20)   # static logo: never shows the plate behind it
    for i in range(n):
        frames[i, 20:36, 5 + i * 8: 21 + i * 8] = (255, 255, 255)
    return frames


@pytest.fixture
def logo_project(tmp_path):
    frames = _logo_clip()
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", OPTS)
    init_project(tmp_path, {"file": "ref.mp4", "fps": 30, "size": [640, 320]}, scene)
    return tmp_path, scene, frames


def test_stage_order_has_plate_between_tracking_and_solids():
    assert STAGES.index("tracking") + 1 == STAGES.index("plate") == STAGES.index("solids") - 1
    js = (STATIC / "js" / "analyze.js").read_text()
    assert json.loads(re.search(r"const PIPELINE = (\[.*?\]);", js).group(1)) == list(STAGES)
    assert 'bg: ["background", "plate"]' in js and 'plate: "analyze.step.bg"' in js


def test_analysis_writes_pass1_and_v2_plate(logo_project):
    root, scene, frames = logo_project
    sd = scene_dir(root, "s1")
    pass1 = cv2.cvtColor(cv2.imread(str(sd / PASS1_PATH)), cv2.COLOR_BGR2RGB)
    assert np.array_equal(pass1, background_plate(frames))
    plate = cv2.cvtColor(cv2.imread(str(sd / PLATE_PATH)), cv2.COLOR_BGR2RGB)
    assert np.abs(plate.astype(int) - frames[0].astype(int))[200:].max() == 0   # exact where the plate shows
    pj = json.loads((sd / PLATE_JSON).read_text())
    assert (pj["version"], pj["kind"], pj["synthetic"]) == (2, "image", True)
    assert [h["method"] for h in pj["stats"]["holes"]] == ["poly"]
    synthetic = cv2.imread(str(sd / SYNTH_PATH), cv2.IMREAD_GRAYSCALE) > 127
    # the hole is the logo plus pass 1's halo around it (a region too), dilated
    assert synthetic[80:120, 320:360].all() and synthetic.sum() == pj["stats"]["holes"][0]["area"] < 82 * 99
    assert scene.background.kind == "image" and scene.background.value == PLATE_PATH
    assert scene.background.synthetic == SYNTH_PATH and scene.background.confidence == pj["confidence"]
    assert pj["confidence"] == pytest.approx(json.loads((sd / "stages" / "background.json").read_text())["confidence"]
                                             * (1 - 0.5 * synthetic.mean()))
    ramp = np.linspace(0, 255, 640, dtype=np.uint8)
    want = np.stack([ramp, ramp[::-1], np.full(640, 80, np.uint8)], -1)[320:360].astype(int)
    assert np.abs(plate[80:120, 320:360].astype(int) - want[None]).max() <= 2   # the logo is gone, the ramp continues


def test_plate_stage_cached_and_rerun_from_sprites_reuses_it(logo_project, monkeypatch):
    root, scene, _ = logo_project
    sd = scene_dir(root, "s1")
    cached = {p: (sd / p).read_bytes() for p in (PLATE_JSON, PLATE_PATH, SYNTH_PATH)}
    monkeypatch.setattr(pipeline, "build_plate", lambda *a, **k: pytest.fail("rebuilt the cached plate"))
    rerun(root, "s1", "sprites", note="cached plate")
    assert current_scene(root, "s1")[0].background == scene.background
    assert {p: (sd / p).read_bytes() for p in cached} == cached


def test_rerun_from_regions_rebuilds_plate(logo_project, monkeypatch):
    root, scene, _ = logo_project
    sd = scene_dir(root, "s1")
    before = (sd / PLATE_PATH).read_bytes()
    calls = []
    real = pipeline.build_plate
    monkeypatch.setattr(pipeline, "build_plate", lambda *a, **k: calls.append(1) or real(*a, **k))
    rerun(root, "s1", "regions", note="regions")
    assert len(calls) == 1
    rerun(root, "s1", "plate", note="plate")
    assert len(calls) == 2
    rerun(root, "s1", "solids", note="solids")
    assert len(calls) == 2
    assert current_scene(root, "s1")[0].background == scene.background
    assert (sd / PLATE_PATH).read_bytes() == before


def test_legacy_project_without_plate_json_rebuilds_on_rerun_from_sprites(logo_project):
    root, scene, _ = logo_project
    sd = scene_dir(root, "s1")
    v2, p1 = (sd / PLATE_PATH).read_bytes(), (sd / PASS1_PATH).read_bytes()
    (sd / PLATE_JSON).unlink()
    (sd / SYNTH_PATH).unlink()
    (sd / PASS1_PATH).unlink()
    (sd / PLATE_PATH).write_bytes(p1)   # before plate v2, assets/background.png held pass 1
    rerun(root, "s1", "sprites", note="legacy")
    assert (sd / PASS1_PATH).read_bytes() == p1
    assert (sd / PLATE_PATH).read_bytes() == v2
    assert json.loads((sd / PLATE_JSON).read_text())["version"] == 2
    assert current_scene(root, "s1")[0].background == scene.background


def _square_mask(sd):
    overrides = json.loads((sd / "stages" / "overrides.json").read_text())
    return cv2.imread(str(sd / overrides["regions"][-1]["mask"]), cv2.IMREAD_GRAYSCALE)


def test_corrections_bbox_prompt_uses_pass1_plate(tmp_path):
    scene = analyze_scene_frames(_logo_clip(logo=False), 30, tmp_path, "s1", OPTS)
    init_project(tmp_path, {"file": "ref.mp4", "fps": 30, "size": [640, 320]}, scene)
    root, sd = tmp_path, scene_dir(tmp_path, "s1")
    square = scene.elements[0]
    assert len(scene.elements) == 1 and (sd / PASS1_PATH).exists()
    cv2.imwrite(str(sd / PLATE_PATH), np.full((320, 640, 3), 255, np.uint8))   # pass 2 must not be used
    add_bbox_prompt(root, "s1", 5, (35, 10, 75, 45), square.id)
    mask = _square_mask(sd)
    assert 200 <= np.count_nonzero(mask) <= 320 and np.all(mask[20:36, 45:61] == 255)
    p1 = (sd / PASS1_PATH).read_bytes()
    (sd / PASS1_PATH).unlink()
    (sd / PLATE_PATH).write_bytes(p1)   # legacy layout
    add_bbox_prompt(root, "s1", 6, (43, 10, 83, 45), square.id)
    mask = _square_mask(sd)
    assert 200 <= np.count_nonzero(mask) <= 320 and np.all(mask[20:36, 53:69] == 255)
    assert (sd / PASS1_PATH).read_bytes() == p1


def test_plate_v2_failure_falls_back_to_pass1(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("plate exploded")

    monkeypatch.setattr(pipeline, "build_plate", boom)
    frames = _logo_clip()
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", OPTS)
    sd = scene_dir(tmp_path, "s1")
    bgj = json.loads((sd / "stages" / "background.json").read_text())
    assert scene.background.kind == "image" and scene.background.synthetic is None
    assert scene.background.confidence == pytest.approx(0.5 * bgj["confidence"])
    assert np.array_equal(cv2.cvtColor(cv2.imread(str(sd / PLATE_PATH)), cv2.COLOR_BGR2RGB), background_plate(frames))
    assert any("plate v2 skipped: RuntimeError: plate exploded" in m
               for m in json.loads((sd / "report.json").read_text())["messages"])


def test_ig2_like_smear_removed(tmp_path):
    ref = make_reference_scene(tmp_path / "ref", 2, plate="gradient", n_sprites=1, mover=True, logo=True, frames=40)
    frames = np.stack([f.round().clip(0, 255).astype(np.uint8) for f in render_frames(ref, tmp_path / "ref", range(ref.frames))])
    truth = srgb_to_lab(true_plate(ref, tmp_path / "ref", 0))
    smear = delta_e(srgb_to_lab(background_plate(frames)), truth)
    assert np.percentile(smear, 99) > 10   # pass 1 holds the title, logo and mover
    scene = analyze_scene_frames(frames, 30, tmp_path / "proj", "s1", OPTS)
    sd = scene_dir(tmp_path / "proj", "s1")
    assert scene.background.kind == "image"
    plate = cv2.cvtColor(cv2.imread(str(sd / PLATE_PATH)), cv2.COLOR_BGR2RGB)
    assert np.percentile(delta_e(srgb_to_lab(plate), truth), 95) < 1.5
