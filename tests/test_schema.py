import pytest
from pydantic import ValidationError
from keepframe.ir.schema import (Keyframe, Track, Element, Canonical, Scene, Background,
                                 Constraint, Project, SceneRef, Version, dump, load_scene_json, PROPS, DEFAULTS)

def make_scene():
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=100, height=50, texture="assets/e1.png"),
                 visible=(0, 59), tracks={"x": Track(keys=[Keyframe(t=0, v=0, ease=(0.2, 0, 0, 1)), Keyframe(t=30, v=200)])})
    return Scene(id="s1", size=(640, 360), fps=30, frames=60, background=Background(value="#101418"),
                 elements=[el], constraints=[Constraint(pred="type(m_e1_1,'translation')", keep=True)])

def test_roundtrip_json_uses_schema_alias():
    s = make_scene()
    text = dump(s)
    assert '"schema": "keepframe.scene/1"' in text
    s2 = load_scene_json(text)
    assert s2 == s
    assert s2.element("e1").tracks["x"].keys[0].ease == (0.2, 0, 0, 1)

def test_track_requires_sorted_unique_times():
    with pytest.raises(ValidationError):
        Track(keys=[Keyframe(t=10, v=1), Keyframe(t=5, v=0)])
    with pytest.raises(ValidationError):
        Track(keys=[Keyframe(t=5, v=1), Keyframe(t=5, v=0)])

def test_unknown_track_property_rejected():
    with pytest.raises(ValidationError):
        Element(id="e", kind="sprite", canonical=Canonical(width=1, height=1), visible=(0, 0),
                tracks={"bogus": Track(keys=[Keyframe(t=0, v=0)])})

def test_defaults_cover_all_props():
    assert set(DEFAULTS) == set(PROPS)

def test_project_roundtrip():
    p = Project(source={"file": "ref.mp4", "fps": 30, "size": [640, 360], "mode": "range", "range": [0, 59]},
                scenes=[SceneRef(id="s1", frames=(0, 59))],
                versions=[Version(id="v1", parent=None, note="initial", scene_file="scenes/s1/scene.v1.json")])
    text = dump(p)
    assert '"schema": "keepframe.project/1"' in text
