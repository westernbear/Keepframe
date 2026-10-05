import numpy as np
import pytest

from keepframe.edit.agent import TEMPORAL_ELEMENT_MIN, TEMPORAL_MIN, _passed, _verification_error, edit
from keepframe.ir.schema import Background, Canonical, Element, FontGuess, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, init_project
from keepframe.ir.tracks import element_bbox
from keepframe.render.renderer import RenderResult
from keepframe.verify.similarity import temporal_by_id_detail
from keepframe.verify.verifier import verify


def _scene_with_three_movers():
    elements = [Element(id=f"fragment{i}", kind="sprite", visible=(0, 1),
                        canonical=Canonical(width=10, height=10)) for i in range(20)]
    for i in range(3):
        start, end = 10 + i * 10, 19 + i * 10
        elements.append(Element(id=f"moving{i}", kind="sprite", visible=(start, end),
                                canonical=Canonical(width=10, height=10),
                                tracks={"x": Track(keys=[Keyframe(t=start, v=0),
                                                         Keyframe(t=end, v=90)])}))
    return Scene(id="s1", size=(200, 100), fps=30, frames=40,
                 background=Background(), elements=elements)


def _render_probes(scene, directory):
    frames = list(range(scene.frames))
    return RenderResult(frames_dir=directory, frames=frames, hashes=[],
                        bboxes={el.id: [list(element_bbox(el, f)) for f in frames]
                                for el in scene.elements})


@pytest.mark.parametrize("damage", ["shift", "delete"])
def test_one_significant_motion_fails_even_when_average_passes(tmp_path, damage):
    reference = _scene_with_three_movers()
    out = reference.model_copy(deep=True)
    if damage == "shift":
        el = out.element("moving0")
        el.visible = (20, 29)
        for key in el.tracks["x"].keys:
            key.t += 10
    else:
        out.elements = [el for el in out.elements if el.id != "moving0"]
    report = verify(out, tmp_path, render_result=_render_probes(out, tmp_path),
                    reference=reference)

    assert report.passed
    assert report.temporal == pytest.approx(38 / 47)
    assert report.temporal > TEMPORAL_MIN
    assert not _passed(report)
    assert report.temporal_worst_element == "moving0"
    assert report.temporal_worst == 0.0
    error = _verification_error(report)
    assert "moving0" in error and "0.000 < 0.500" in error


def test_damage_to_fragment_below_ten_percent_still_passes(tmp_path):
    reference = _scene_with_three_movers()
    reference.element("fragment0").tracks["x"] = Track(keys=[Keyframe(t=0, v=0), Keyframe(t=1, v=10)])
    out = reference.model_copy(deep=True)
    out.elements = [el for el in out.elements if el.id != "fragment0"]
    report = verify(out, tmp_path, render_result=_render_probes(out, tmp_path),
                    reference=reference)

    assert report.temporal == pytest.approx(46 / 47)
    assert report.temporal_worst == 1.0
    assert report.temporal_worst_element.startswith("moving")
    assert _passed(report)


@pytest.mark.parametrize("pairs, average, worst", [
    (0, 1.0, ("main", 1.0)),
    (1, 18 / 19, ("main", 1.0)),
    (2, 0.9, ("fragment", 0.0)),
])
def test_floor_share_counts_only_consecutive_finite_reference_pairs(pairs, average, worst):
    main = np.c_[np.arange(19.), np.zeros(19)]
    fragment = np.array([[np.inf, 0.], [np.nan, np.nan], [0., 0.]])
    if pairs:
        fragment = np.vstack((fragment, [[1., 0.]]))
    if pairs == 2:
        fragment = np.vstack((fragment, [[np.nan, np.nan], [2., 0.], [3., 0.]]))
    score, detail = temporal_by_id_detail({"main": main, "fragment": fragment}, {"main": main})

    assert score == pytest.approx(average)
    assert detail == worst


def test_caller_can_raise_minimum_reference_share():
    main = np.c_[np.arange(19.), np.zeros(19)]
    fragment = np.c_[np.arange(3.), np.zeros(3)]
    score, worst = temporal_by_id_detail({"main": main, "fragment": fragment},
                                         {"main": main}, min_share=0.11)

    assert score == pytest.approx(0.9)
    assert worst == ("main", 1.0)


@pytest.mark.parametrize("speed_ratio, passed", [(0.4999, False), (0.5, True), (0.6, True)])
def test_element_floor_includes_exact_half_similarity(tmp_path, speed_ratio, passed):
    reference = _scene_with_three_movers()
    out = reference.model_copy(deep=True)
    out.element("moving0").tracks["x"].keys[-1].v *= speed_ratio
    report = verify(out, tmp_path, render_result=_render_probes(out, tmp_path),
                    reference=reference)

    assert TEMPORAL_ELEMENT_MIN == 0.5
    assert report.temporal > TEMPORAL_MIN
    assert report.temporal_worst_element == "moving0"
    assert report.temporal_worst == pytest.approx(speed_ratio)
    assert _passed(report) is passed
    assert ("moving0" in _verification_error(report)) is not passed


@pytest.mark.parametrize("reference", [None, "empty", "single_frame", "tiny_fragments"])
def test_no_significant_reference_evidence_has_no_floor(tmp_path, reference):
    scene = _scene_with_three_movers()
    if reference == "tiny_fragments":
        scene.elements = scene.elements[:20]
    else:
        scene.elements = [] if reference == "empty" else scene.elements[:1]
    if reference == "single_frame":
        scene.elements[0].visible = (0, 0)
    ref = None if reference is None else scene
    report = verify(scene, tmp_path, render_result=_render_probes(scene, tmp_path), reference=ref)

    assert report.temporal_worst is None
    assert report.temporal_worst_element is None
    assert _passed(report)


def test_reversed_motion_preserves_negative_floor_score():
    moving = np.c_[np.arange(4.), np.zeros(4)]
    score, worst = temporal_by_id_detail({"moving": moving}, {"moving": moving[::-1]})

    assert score == 0.0
    assert worst == ("moving", -1.0)


@pytest.fixture
def motion_project(tmp_path, monkeypatch):
    scene = _scene_with_three_movers()
    text = scene.element("moving0")
    text.kind = "text"
    text.canonical = Canonical(width=120, height=40, text="Hi", font=FontGuess(size_px=20))
    init_project(tmp_path, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    monkeypatch.setattr("keepframe.edit.agent.render",
                        lambda _html, out, directory: _render_probes(out, directory))
    return tmp_path, scene


def test_text_only_edit_keeps_significant_motion_and_passes(motion_project):
    root, before = motion_project
    result = edit(root, "s1", "Change the text", confirm=True,
                  intent={"targets": [{"element": "moving0", "property": "text", "value": "Hello"}]})

    assert result.status == "done"
    assert result.verify.temporal == result.verify.temporal_worst == 1.0
    out, version = current_scene(root, "s1")
    assert version.id == "v2"
    assert out.element("moving0").canonical.text == "Hello"
    assert [(el.visible, el.tracks) for el in out.elements] == [(el.visible, el.tracks) for el in before.elements]


@pytest.mark.parametrize("target", [
    {"element": "moving0", "property": "timing", "delay": 0.4},
    {"element": "moving0", "property": "timing", "speed": 2.0},
    {"property": "timing", "speed": 2.0},
])
def test_requested_retiming_uses_expected_reference_for_floor(motion_project, target):
    root, _ = motion_project
    result = edit(root, "s1", "Change the timing", confirm=True, intent={"targets": [target]})

    assert result.status == "done"
    assert result.verify.temporal == result.verify.temporal_worst == 1.0
    assert current_scene(root, "s1")[1].id == "v2"
