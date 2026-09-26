import pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.schema import Keyframe
from keepframe.analyze.constraints import extract_constraints
from keepframe.verify.verifier import verify


def test_verification_without_render_is_incomplete_and_fails(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    report = verify(scene, tmp_scene_dir)

    assert not report.layer_probe_complete
    assert not report.passed


def test_missing_bbox_probe_is_incomplete_and_fails(tmp_scene_dir):
    from keepframe.render.renderer import RenderResult

    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    frames = [0, 30, 59]
    report = verify(scene, tmp_scene_dir, render_result=RenderResult(
        frames_dir=tmp_scene_dir, frames=frames, hashes=[""] * len(frames), bboxes={}
    ))

    assert not report.layer_probe_complete
    assert not report.passed


@pytest.mark.browser
def test_saved_render_json_proves_layer_geometry(tmp_scene_dir):
    from keepframe.compose.composer import compose
    from keepframe.render.renderer import render, render_result_from_json

    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    rr = render(compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html"), scene, tmp_scene_dir / "r", frames=[0, 30, 59])
    restored = render_result_from_json(tmp_scene_dir / "r" / "render.json")

    report = verify(scene, tmp_scene_dir, render_result=restored)
    assert report.layer_probe_complete
    assert report.passed, report.layer_errors
def test_keep_predicates_gate(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    scene.constraints = [c.model_copy(update={"keep": True}) for c in extract_constraints(scene)]
    ok = verify(scene, tmp_scene_dir)
    assert ok.schema_ok and ok.keep_pass_rate == 1.0 and not ok.passed

    broken = scene.model_copy(deep=True)
    xs = broken.elements[0].tracks["x"].keys
    broken.elements[0].tracks["x"].keys = [Keyframe(t=xs[0].t, v=xs[-1].v), Keyframe(t=xs[-1].t, v=xs[0].v)]
    bad = verify(broken, tmp_scene_dir, reference=scene, reference_dir=tmp_scene_dir)
    assert bad.keep_pass_rate < 1.0 and not bad.passed
    assert bad.temporal is not None and bad.temporal < 1.0 and bad.appearance == pytest.approx(1.0)


def test_empty_scene_needs_no_bbox_probe(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    scene.elements = []

    report = verify(scene, tmp_scene_dir)

    assert report.layer_probe_complete
    assert report.passed

def test_missing_texture_fails_schema(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    scene.elements[0].canonical.texture = "assets/nope.png"
    r = verify(scene, tmp_scene_dir)
    assert not r.schema_ok and not r.passed and any("nope.png" in m for m in r.messages)


@pytest.mark.browser
def test_layer_check_against_render(tmp_scene_dir):
    from keepframe.compose.composer import compose
    from keepframe.render.renderer import render
    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    rr = render(compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html"), scene, tmp_scene_dir / "r", frames=[0, 30, 59])
    r = verify(scene, tmp_scene_dir, render_result=rr)
    assert r.layer_max_err_px <= 2.0 and r.passed, r.layer_errors
