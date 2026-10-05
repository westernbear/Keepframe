import json

import cv2
import numpy as np
import pytest

from keepframe.analyze.keyframes import ERR, fill_gaps, tracks_from_raw
from keepframe.ir.schema import Background, Canonical, Element, FontGuess, Keyframe, Scene, Track
from keepframe.ir.tracks import element_bbox, eval_props, eval_track


def track(start, end, first=0, last=10):
    return Track(keys=[Keyframe(t=first, v=start), Keyframe(t=last, v=end)])


def reveal_scene(tmp_path, text="MMMMMMMM"):
    # A real text raster supplies the composite renderer's canonical texture.
    from keepframe.edit.textraster import render_lines
    font = FontGuess(family_guess="DejaVu Sans", size_px=20)
    texture = render_lines([text], font.size_px, (255, 255, 255), font.family_guess)
    height, width = texture.shape[:2]
    cv2.imwrite(str(tmp_path / "text.png"), texture)
    element = Element(id="title", kind="text", canonical=Canonical(
        width=width, height=height, anchor=(0, 0), texture="text.png", text=text,
        font=font, color="#ffffff"), visible=(0, 10),
        tracks={"x": Track(keys=[Keyframe(t=0, v=10)]), "reveal": track(0, 1)})
    return Scene(id="reveal", size=(600, 64), fps=10, frames=11,
                 background=Background(), elements=[element])


def test_reveal_defaults_interpolation_and_bbox():
    element = Element(id="text", kind="text", canonical=Canonical(width=100, height=20),
                      visible=(0, 10), tracks={"reveal": track(0, 1)})
    assert eval_props(element, 5)["reveal"] == 0.5
    assert element_bbox(element, 0) == element_bbox(element, 10) == (-50, -10, 50, 10)
    element.tracks.clear()
    assert eval_props(element, 5)["reveal"] == 1.0


@pytest.mark.parametrize("columns", [8, 9, 11])
def test_raw_columns_are_backward_compatible_and_nan_means_default(columns):
    raw = np.tile([0., 0., 1., 1., 0., 0., 0., 1., np.nan, np.nan, np.nan][:columns], (11, 1))
    raw[:, 0] = np.linspace(10, 30, 11)
    fitted, error = tracks_from_raw(raw, 3)
    assert set(fitted) == {"x"}
    assert fitted["x"].keys[-1].t == 13
    assert error.max_px <= 2
    if columns >= 9:
        raw[:, 8] = np.linspace(0, 1, 11)
        fitted, _ = tracks_from_raw(raw, 0)
        assert eval_track(fitted["reveal"], 5) == pytest.approx(0.5)
        assert ERR["reveal"] == 0.01


def test_fill_gaps_preserves_all_nan_and_interpolates_each_observed_column():
    raw = np.full((7, 11), np.nan)
    raw[1:6, 0] = [1, 2, 3, 4, 5]
    raw[1, 8], raw[5, 8] = 0, 1
    filled = fill_gaps(raw)
    assert filled[3, 8] == 0.5
    assert np.isnan(filled[:, 9:]).all()
    assert np.isnan(filled[[0, 6], 8]).all()


def test_composite_reveal_clips_left_half_without_mutating_texture_cache(tmp_path):
    from keepframe.analyze.composite import composite_scene
    scene = reveal_scene(tmp_path)
    cache = {}
    half = composite_scene(scene, tmp_path, 5, cache)
    full = composite_scene(scene, tmp_path, 10, cache)
    edge = 10 + round(scene.elements[0].canonical.width / 2)
    assert np.count_nonzero(half[:, 10:edge]) > 0
    assert not np.count_nonzero(half[:, edge + 1:])
    assert np.count_nonzero(full[:, edge + 1:]) > 0
    assert not np.count_nonzero(composite_scene(scene, tmp_path, 0, cache))


@pytest.mark.browser
def test_chromium_reveal_clips_pixels_and_preserves_layer_probe(tmp_path):
    from keepframe.compose.composer import compose
    from keepframe.render.renderer import load_frame, render
    scene = reveal_scene(tmp_path)
    result = render(compose(scene, tmp_path, tmp_path / "reveal.html"), scene,
                    tmp_path / "render", frames=[0, 5, 10, 5])
    half = load_frame(result.frames_dir / "f_00001.png")
    full = load_frame(result.frames_dir / "f_00002.png")
    edge = 10 + round(scene.elements[0].canonical.width / 2)
    assert np.count_nonzero(half[:, :edge]) > 0
    assert not np.count_nonzero(half[:, edge + 1:])
    assert np.count_nonzero(full[:, edge + 1:]) > 0
    assert result.hashes[1] == result.hashes[3]
    assert len({tuple(bbox) for bbox in result.bboxes["title"]}) == 1


@pytest.mark.browser
def test_reveal_survives_longer_copy(tmp_path):
    from keepframe.analyze.composite import composite_scene
    from keepframe.compose.composer import compose
    from keepframe.edit.apply import apply_edit
    from keepframe.edit.intent import Target
    from keepframe.render.renderer import render
    original = reveal_scene(tmp_path)
    scene = apply_edit(original, tmp_path, [Target(element="title", property="text", value="MMMMMMMM" * 3)], {"overflow": "expand_box"}, None)
    assert scene.elements[0].tracks["reveal"] == original.elements[0].tracks["reveal"]
    assert scene.elements[0].canonical.width > original.elements[0].canonical.width * 2.9
    full = composite_scene(scene, tmp_path, 10)
    element = scene.elements[0]
    element.tracks.pop("reveal")
    assert np.array_equal(full, composite_scene(scene, tmp_path, 10))
    control = render(compose(scene, tmp_path, tmp_path / "control.html"), scene,
                     tmp_path / "control", frames=[10])
    element.tracks["reveal"] = track(0, 1)
    revealed = render(compose(scene, tmp_path, tmp_path / "long.html"), scene,
                      tmp_path / "long", frames=[10])
    assert revealed.hashes == control.hashes


def test_lottie_reveal_exports_animated_rectangle_with_track_easing(tmp_path):
    from keepframe.ir.tracks import PRESET_EASES
    from keepframe.render.lottie import write_lottie
    scene = reveal_scene(tmp_path)
    scene.elements[0].tracks["reveal"].keys[0].ease = PRESET_EASES["out_quad"]
    animation = json.loads(write_lottie(scene, tmp_path, tmp_path / "animation.json").read_text())
    layer = next(layer for layer in animation["layers"] if layer["nm"] == "title")
    mask = layer["masksProperties"][0]
    assert mask["mode"] == "a" and mask["pt"]["a"] == 1
    keys = mask["pt"]["k"]
    assert [key["t"] for key in keys] == [0, 10]
    assert keys[0]["s"][0]["v"][1][0] == 0
    assert keys[-1]["s"][0]["v"][1][0] == scene.elements[0].canonical.width
    assert keys[0]["o"]["x"] == [0.5]


def test_ae_reports_reveal_gap_until_mapping_lands(tmp_path):
    from keepframe.after_effects.compatibility import analyze_ae_compatibility
    from tests.test_ae_compatibility import _capabilities
    scene = reveal_scene(tmp_path)
    assert "reveal" in {issue.semantic_key for issue in analyze_ae_compatibility(scene, _capabilities())}
    scene.elements[0].tracks["reveal"] = Track(keys=[Keyframe(t=0, v=1)])
    assert "reveal" not in {issue.semantic_key for issue in analyze_ae_compatibility(scene, _capabilities())}


def test_reveal_motion_constraints_content_only_and_scene_brief(tmp_path):
    from keepframe.analyze.constraints import apply_keep_preset, extract_constraints
    from keepframe.session.brief import scene_brief
    from keepframe.verify.matrix import extract_motions
    from keepframe.verify.predicates import build_context, eval_pred
    scene = reveal_scene(tmp_path)
    scene.fps, scene.frames = 10, 12
    scene.elements[0].visible = (0, 11)
    scene.elements[0].tracks["reveal"] = track(0, 1, 2, 9)
    motion, = extract_motions(scene)
    assert (motion.type, motion.start, motion.end, motion.mag, motion.dur) == ("reveal", 2, 9, 1.0, 7)
    constraints = apply_keep_preset(extract_constraints(scene), "content_only")
    assert {c.pred for c in constraints if c.keep} == {"type(m_title_1,'reveal')", "mag(m_title_1,1.0)", "dur(m_title_1,7)"}
    assert all(eval_pred(c.pred, build_context(scene)) for c in constraints)
    assert "reveals left→right 0.20s–0.90s" in scene_brief(scene)
    scene.elements[0].tracks["reveal"] = track(0, 1, 2, 5)
    assert not eval_pred("dur(m_title_1,7)", build_context(scene))


@pytest.mark.parametrize("kind", ["sprite", "text"])
@pytest.mark.browser
def test_lottie_reveal_mask_renders_half_and_final_pixels(tmp_path, kind):
    from keepframe.render.lottie import compose_lottie_player, write_lottie
    from keepframe.render.renderer import load_frame, render
    scene = reveal_scene(tmp_path)
    element = scene.elements[0]
    element.kind = kind
    element.tracks["y"] = Track(keys=[Keyframe(t=0, v=25)])
    animation = json.loads(write_lottie(scene, tmp_path, tmp_path / "animation.json").read_text())
    result = render(compose_lottie_player(animation, tmp_path / "lottie.html"), scene,
                    tmp_path / "lottie", frames=[0, 5, 10], probe=False)
    half = load_frame(result.frames_dir / "f_00001.png")
    full = load_frame(result.frames_dir / "f_00002.png")
    edge = 10 + round(element.canonical.width / 2)
    assert not np.count_nonzero(load_frame(result.frames_dir / "f_00000.png"))
    assert np.count_nonzero(half[:, :edge]) > 0
    assert not np.count_nonzero(half[:, edge + 1:])
    assert np.count_nonzero(full[:, edge + 1:]) > 0
    element.tracks.pop("reveal")
    control_animation = json.loads(write_lottie(scene, tmp_path, tmp_path / "control.json").read_text())
    control = render(compose_lottie_player(control_animation, tmp_path / "control.html"), scene,
                     tmp_path / "control", frames=[10], probe=False)
    assert result.hashes[2] == control.hashes[0]
