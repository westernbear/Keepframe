import pytest
from refstudio.ir.synth import make_synthetic_scene
from refstudio.ir.schema import Keyframe
from refstudio.analyze.constraints import extract_constraints
from refstudio.verify.verifier import verify


def test_keep_predicates_gate(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    scene.constraints = [c.model_copy(update={"keep": True}) for c in extract_constraints(scene)]
    ok = verify(scene, tmp_scene_dir)
    assert ok.schema_ok and ok.keep_pass_rate == 1.0 and ok.passed
    broken = scene.model_copy(deep=True)
    xs = broken.elements[0].tracks["x"].keys
    broken.elements[0].tracks["x"].keys = [Keyframe(t=xs[0].t, v=xs[-1].v), Keyframe(t=xs[-1].t, v=xs[0].v)]
    bad = verify(broken, tmp_scene_dir, reference=scene, reference_dir=tmp_scene_dir)
    assert bad.keep_pass_rate < 1.0 and not bad.passed
    assert bad.temporal is not None and bad.temporal < 1.0 and bad.appearance == pytest.approx(1.0)


def test_missing_texture_fails_schema(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    scene.elements[0].canonical.texture = "assets/nope.png"
    r = verify(scene, tmp_scene_dir)
    assert not r.schema_ok and not r.passed and any("nope.png" in m for m in r.messages)


@pytest.mark.browser
def test_layer_check_against_render(tmp_scene_dir):
    from refstudio.compose.composer import compose
    from refstudio.render.renderer import render
    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    rr = render(compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html"), scene, tmp_scene_dir / "r", frames=[0, 30, 59])
    r = verify(scene, tmp_scene_dir, render_result=rr)
    assert r.layer_max_err_px <= 2.0 and r.passed, r.layer_errors
