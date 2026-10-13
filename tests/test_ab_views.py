import json

from scripts import ab_views
from keepframe.ir.store import init_project
from keepframe.ir.synth import make_reference_scene


def test_auto_gold_from_ir(tmp_scene_dir):
    scene = make_reference_scene(tmp_scene_dir, 9, plate="flat", title="stagger")
    qs = ab_views.auto_questions(scene)
    kinds = {q["kind"] for q in qs}
    assert {"yesno", "idset", "text"} <= kinds and all(q["cat"] == "auto" for q in qs)
    glyph = next(q for q in qs if "glyph by glyph" in q["q"])
    assert glyph["gold"] == [e.id for e in scene.elements if e.canonical.glyphs] != []


def test_scoring():
    s = ab_views.score_one
    assert s({"kind": "yesno", "gold": "yes"}, " Yes ") == 1
    assert s({"kind": "number", "gold": 1.0, "tol": 0.07}, "1.05") == 1 and s({"kind": "number", "gold": 1.0, "tol": 0.07}, 1.2) == 0
    assert s({"kind": "colour", "gold": "#ff0000"}, "#fe0101") == 1 and s({"kind": "colour", "gold": "#ff0000"}, "#0000ff") == 0
    assert s({"kind": "idset", "gold": ["e1", "e2"]}, ["e1"]) == 0.5 and s({"kind": "idset", "gold": []}, []) == 1
    assert s({"kind": "edit", "gold": ["e6"], "property": "color"}, {"element": "e6", "property": "color"}) == 1
    assert s({"kind": "edit", "gold": [None], "property": "background"}, [{"element": None, "property": "background"}]) == 1
    assert s({"kind": "text", "gold": "Ask.And move forward."}, "ask. and move forward.") == 1
    assert s({"kind": "id", "gold": ["e3"]}, None) == 0


def test_packets_blind(tmp_path):
    root, out = tmp_path / "kf", tmp_path / "ab"
    scene = make_reference_scene(tmp_path / "src", 9, plate="flat", title="stagger").model_copy(update={"id": "s1"})
    init_project(root / "clipA", {"file": "x.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range",
                                  "range": [0, scene.frames - 1]}, scene)
    import shutil
    shutil.copytree(tmp_path / "src" / "assets", root / "clipA" / "scenes" / "s1" / "assets")
    out.mkdir()
    (out / "read_gold.json").write_text(json.dumps({"clipA": {"read": [{"q": "Which id is the title?", "ids": ["e1"]}],
                                                              "edit": [{"q": "make it red", "ids": ["e1"], "property": "color"}]}}))
    index = ab_views.packets(root, out)
    assert sorted(m["view"] for m in index.values()) == ["brief", "kfscript"]
    for name in index:
        assert "brief" not in name and "kfscript" not in name
        text = (out / "packets" / f"{name}.md").read_text()
        assert "Q1:" in text and "Edit request: make it red" in text and "View:" not in text
    (out / "answers").mkdir()
    for name in index:
        (out / "answers" / f"{name}.json").write_text("{}")
    res = ab_views.score(out)
    assert set(res["accuracy"]) >= {"brief/auto", "kfscript/edit"} and res["switch_default_to_kfscript"] is False


def test_edit_scoring_accepts_several_targets():
    q = {"kind": "edit", "gold": ["e64", "e65"], "property": "font"}
    assert ab_views.score_one(q, {"element": ["e64", "e65"], "property": "font"}) == 1
    assert ab_views.score_one(q, [{"element": "e64", "property": "font"}, {"element": "e65", "property": "font"}]) == 1
    assert ab_views.score_one(q, [{"element": "e64", "property": "font"}, {"element": "e9", "property": "font"}]) == 0


def test_synth_gold_from_roundtrip(tmp_path):
    (tmp_path / "cases" / "logo_sting-1").mkdir(parents=True)
    (tmp_path / "cases" / "logo_sting-1" / "truth.json").write_text(json.dumps({"elements": [
        {"id": "bg", "role": "background", "kind": "shape"}, {"id": "name", "role": "brand_name", "kind": "text"},
        {"id": "b0", "role": "bubble", "kind": "shape"}, {"id": "b1", "role": "bubble", "kind": "shape"},
        {"id": "url", "role": "url", "kind": "text"}]}))
    rows = [{"id": "bg", "fragment_ids": ["e0"]}, {"id": "name", "fragment_ids": ["e3", "e4"]}, {"id": "b0", "fragment_ids": ["e5"]},
            {"id": "b1", "fragment_ids": ["e6"]}, {"id": "url", "fragment_ids": []}]
    (tmp_path / "metrics.json").write_text(json.dumps({"cases": [{"name": "logo_sting-1", "kind": "logo_sting", "diff": {"rows": rows}},
                                                                 {"name": "mk571", "kind": "gold", "diff": {"rows": []}}]}))
    g = ab_views.synth_gold(tmp_path)
    assert set(g) == {"_note", "logo_sting-1"}   # gold rows keep their hand-written read gold
    assert g["logo_sting-1"]["read"] == [{"q": "Which element id(s) make up the brand name (text)? (list)", "ids": ["e3", "e4"], "set": True}]
    assert g["logo_sting-1"]["edit"] == [{"q": "make the brand name red", "ids": ["e3", "e4"], "property": "color"}]
