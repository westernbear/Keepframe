import json
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest
from pydantic import ValidationError

from keepframe.ir.schema import DEFAULTS, PROPS, Background, Canonical, Element, Keyframe, Scene, Track, dump, load_scene_json
from keepframe.ir.tracks import eval_props, eval_track
from tests.test_reveal_track import track
from tests.test_three import _triangle_glb


def spin_scene(tmp_path, prop="ry", kind="3d"):
    (tmp_path / "triangle.glb").write_bytes(_triangle_glb())
    texture = np.full((96, 96, 4), 255, np.uint8)
    cv2.imwrite(str(tmp_path / "texture.png"), texture)
    return Scene(id="spin", size=(128, 128), fps=10, frames=11, background=Background(), elements=[
        Element(id="model", kind=kind, canonical=Canonical(width=96, height=96, texture="texture.png",
                model="triangle.glb" if kind == "3d" else None), visible=(0, 10),
                tracks={"x": Track(keys=[Keyframe(t=0, v=64)]), "y": Track(keys=[Keyframe(t=0, v=64)]), prop: track(0, 90)})])


def test_ir_rotation_degrees_defaults_and_pending_asset_roundtrip(tmp_path):
    assert PROPS == ("x", "y", "sx", "sy", "rot", "skx", "sky", "opacity", "reveal", "rx", "ry")
    assert (DEFAULTS["reveal"], DEFAULTS["rx"], DEFAULTS["ry"]) == (1., 0., 0.)
    scene = spin_scene(tmp_path, kind="sprite")
    element = scene.elements[0]
    assert element.pending_asset is None
    element.pending_asset = "3d"
    assert load_scene_json(dump(scene)).elements[0].pending_asset == "3d"
    assert eval_props(element, 5)["ry"] == 45
    assert eval_props(element, 5)["rx"] == 0
    with pytest.raises(ValidationError):
        Element.model_validate({**element.model_dump(), "pending_asset": "sprite"})


def test_raw_rotation_columns_and_tolerances():
    from keepframe.analyze.keyframes import ERR, tracks_from_raw
    raw = np.tile([0., 0., 1., 1., 0., 0., 0., 1., 1., 0., 0.], (11, 1))
    raw[:, 9], raw[:, 10] = np.linspace(0, 90, 11), np.linspace(0, -360, 11)
    fitted, _ = tracks_from_raw(raw, 0)
    assert set(fitted) == {"rx", "ry"}
    assert eval_track(fitted["rx"], 5) == 45
    assert eval_track(fitted["ry"], 5) == -180
    assert ERR["rx"] == ERR["ry"] == 1.0


@pytest.mark.parametrize("prop", ["rx", "ry"])
@pytest.mark.browser
def test_three_rotation_changes_glb_frames_and_seeks_back_deterministically(tmp_path, prop):
    from keepframe.compose.composer import compose
    from keepframe.render.renderer import render
    scene = spin_scene(tmp_path, prop)
    result = render(compose(scene, tmp_path, tmp_path / "spin.html"), scene,
                    tmp_path / "spin", frames=[0, 5, 10, 0])
    assert len(set(result.hashes[:3])) == 3
    assert result.hashes[0] == result.hashes[3]
    assert len({tuple(b) for b in result.bboxes["model"]}) == 1
    scene.elements[0].tracks.pop(prop)
    static = render(compose(scene, tmp_path, tmp_path / "static.html"), scene,
                    tmp_path / "static", frames=[10])
    assert result.hashes[2] != static.hashes[0]


@pytest.mark.browser
def test_sprite_ignores_rotation_in_chromium(tmp_path):
    from keepframe.compose.composer import compose
    from keepframe.render.renderer import render
    scene = spin_scene(tmp_path, kind="sprite")
    scene.elements[0].tracks["rx"] = track(0, 90)
    result = render(compose(scene, tmp_path, tmp_path / "sprite.html"), scene,
                    tmp_path / "render", frames=[0, 5, 10])
    assert len(set(result.hashes)) == 1


@pytest.mark.parametrize("kind", ["sprite", "3d"])
def test_composite_and_lottie_use_static_texture_without_rotation(tmp_path, kind):
    from keepframe.analyze.composite import composite_scene
    from keepframe.render.lottie import write_lottie
    scene = spin_scene(tmp_path, kind=kind)
    assert np.array_equal(composite_scene(scene, tmp_path, 0), composite_scene(scene, tmp_path, 10))
    animation = json.loads(write_lottie(scene, tmp_path, tmp_path / "animation.json").read_text())
    layer = next(layer for layer in animation["layers"] if layer["nm"] == "model")
    assert layer["ty"] == 2 and layer["ddd"] == 0
    assert "rx" not in layer["ks"] and "ry" not in layer["ks"]
    warnings = json.loads((tmp_path / "animation.report.json").read_text())["warnings"]
    assert warnings == animation["meta"]["warnings"]
    assert bool(warnings) == (kind == "3d")
    if kind == "3d":
        assert "static" in warnings[0] and "model" in warnings[0]


def test_lottie_3d_requires_texture_instead_of_silently_dropping_model(tmp_path):
    from keepframe.render.lottie import preflight_lottie
    from keepframe.render.plan import PlanConflict
    scene = spin_scene(tmp_path)
    scene.elements[0].canonical.texture = None
    with pytest.raises(PlanConflict, match="texture fallback"):
        preflight_lottie(scene)


def test_lottie_final_3d_export_includes_warning_report(tmp_path):
    from keepframe.ir.store import init_project
    from keepframe.jobs.dispatch import run_job
    from keepframe.render.lottie import prepare_lottie_job
    from keepframe.render.plan import approve_render_plan, create_render_plan
    root = tmp_path / "p1"
    scene_dir = root / "scenes/s1"
    scene_dir.mkdir(parents=True)
    scene = spin_scene(scene_dir)
    scene.id = "s1"
    init_project(root, {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    (root / "meta.json").write_text(json.dumps({"id": "p1", "status": "approved", "scene": "s1", "version": "v1"}))
    plan = create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="lottie", mode="final")
    approve_render_plan(root, plan.id, digest=plan.digest, revision=0)
    result = run_job(prepare_lottie_job(root, plan.id))
    assert {"animation", "report"} == set(result)
    warning, = json.loads(Path(result["report"]).read_text())["warnings"]
    assert "3D element model" in warning and "rx/ry" in warning


@pytest.mark.parametrize("prop", ["rx", "ry"])
def test_spin_motion_constraints_and_brief(tmp_path, prop):
    from keepframe.analyze.constraints import apply_keep_preset, extract_constraints
    from keepframe.session.brief import scene_brief
    from keepframe.verify.matrix import extract_motions
    from keepframe.verify.predicates import build_context, eval_pred
    scene = spin_scene(tmp_path, prop)
    element = scene.elements[0]
    element.pending_asset = "3d"
    element.tracks[prop] = track(0, 360)
    motion, = extract_motions(scene)
    assert (motion.type, motion.mag, motion.dur) == ("spin", 360, 10)
    assert all(c.keep and eval_pred(c.pred, build_context(scene)) for c in apply_keep_preset(extract_constraints(scene), "content_only"))
    assert "spins 360°" in scene_brief(scene) and "(3D 후보)" in scene_brief(scene)
    assert 'sprites ignore' in scene_brief(scene)
    element.kind = "sprite"
    element.canonical.model = None
    assert extract_motions(scene) == []


def test_both_rotation_axes_keep_their_degree_changes_and_timing(tmp_path):
    from keepframe.verify.matrix import extract_motions
    scene = spin_scene(tmp_path)
    scene.elements[0].tracks["rx"] = track(0, -360, 2, 8)
    spins = extract_motions(scene)
    assert [(m.type, m.mag, m.start, m.end) for m in spins] == [
        ("spin", 90, 0, 10), ("spin", -360, 2, 8)]


def test_inspector_pending_3d_badge_is_bilingual_and_only_for_candidates(tmp_path):
    static = Path(__file__).parents[1] / "keepframe/web/static/js"
    ko, en = (static / "i18n.js").read_text().split("en: {", 1)
    assert '"review.pending3d": "3D 후보"' in ko
    assert '"review.pending3d": "3D candidate"' in en
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    source = (static / "review/inspector.js").read_text()
    source = source[source.index("export function attachInspector"):].replace("export function", "function", 1)
    script = tmp_path / "pending-badge.mjs"
    script.write_text('''import assert from 'node:assert/strict';
import vm from 'node:vm';
class Node {
  constructor(tag) { this.tagName=tag; this.children=[]; this.dataset={}; this.events={}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
  addEventListener(name, fn) { this.events[name]=fn; }
}
const item={id:'candidate',kind:'sprite',pending_asset:'3d',canonical:{},visible:[0,10]};
const root=new Node('section');
const ws={dom:{objectDetail:root},selectedId:item.id,versionId:'v1',
  state:{scene:{elements:[item]},project:{versions:[{id:'v1'}]}}};
const context=vm.createContext({document:{createElement:tag=>new Node(tag)},T:key=>key,ws});
vm.runInContext(''' + json.dumps(source) + '''+'\\nattachInspector(ws);',context);
const badges=()=>root.children.filter(child=>child.className?.includes('badge'));
ws.renderObjectDetail();
assert.equal(badges().length,1);
assert.equal(badges()[0].textContent,'review.pending3d');
item.pending_asset=null;
ws.renderObjectDetail();
assert.equal(badges().length,0);
''')
    result = subprocess.run([node, str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
