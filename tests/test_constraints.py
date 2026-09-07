from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.schema import Keyframe
from keepframe.analyze.constraints import extract_constraints
from keepframe.verify.predicates import build_context, eval_pred


def test_extracted_constraints_hold_on_source_scene(tmp_scene_dir):
    for seed in range(1, 6):
        scene = make_synthetic_scene(tmp_scene_dir / str(seed), seed=seed)
        cs = extract_constraints(scene)
        assert cs and all(c.keep is False for c in cs)
        ctx = build_context(scene)
        failing = [c.pred for c in cs if not eval_pred(c.pred, ctx)]
        assert failing == [], (seed, failing)


def test_reversed_direction_breaks_dir_constraint(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=2, with_text=False)
    cs = [c for c in extract_constraints(scene) if c.pred.startswith("dir(m_e1_1")]
    assert cs
    flipped = scene.model_copy(deep=True)
    for prop in ("x", "y"):   # reverse the whole displacement so the direction vector flips
        ks = flipped.elements[0].tracks[prop].keys
        v0, v1 = ks[0].v, ks[-1].v
        flipped.elements[0].tracks[prop].keys = [Keyframe(t=ks[0].t, v=v1), Keyframe(t=ks[-1].t, v=v0)]
    assert eval_pred(cs[0].pred, build_context(flipped)) is False
