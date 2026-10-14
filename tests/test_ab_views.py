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
    assert s({"kind": "point", "gold": [100, 50], "tol": 8}, [104, 47]) == 1 and s({"kind": "point", "gold": [100, 50], "tol": 8}, "[120, 50]") == 0
    assert s({"kind": "point", "gold": [100, 50], "tol": 8}, "x=99.5, y=52") == 1


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
    box = lambda *b: {"visible": [0, 0], "boxes": {"0": list(b)}}
    (tmp_path / "cases" / "logo_sting-1" / "truth.json").write_text(json.dumps({"elements": [
        {"id": "bg", "role": "background", "kind": "shape", **box(0, 0, 960, 540)},
        {"id": "frame", "role": "frame", "kind": "shape", **box(100, 100, 800, 300)},
        {"id": "name", "role": "brand_name", "kind": "text", **box(200, 150, 700, 250)},
        {"id": "b0", "role": "bubble", "kind": "shape", **box(0, 400, 100, 450)}, {"id": "b1", "role": "bubble", "kind": "shape", **box(0, 460, 100, 500)},
        {"id": "url", "role": "url", "kind": "text", **box(300, 350, 600, 380)}]}))
    rows = [{"id": "bg", "piece_ids": ["e0"]}, {"id": "frame", "piece_ids": ["e2", "e3", "e4"]}, {"id": "name", "piece_ids": ["e3", "e4"]},
            {"id": "b0", "piece_ids": ["e5"]}, {"id": "b1", "piece_ids": ["e6"]}, {"id": "url", "piece_ids": []}]
    (tmp_path / "metrics.json").write_text(json.dumps({"cases": [{"name": "logo_sting-1", "kind": "logo_sting", "diff": {"rows": rows}},
                                                                 {"name": "mk571", "kind": "gold", "diff": {"rows": []}}]}))
    g = ab_views.synth_gold(tmp_path)
    assert set(g) == {"_note", "logo_sting-1"}   # gold rows keep their hand-written read gold
    assert g["logo_sting-1"]["read"] == [{"q": "Which element id(s) make up the frame (shape)? (list)", "ids": ["e2"], "set": True},
                                         {"q": "Which element id(s) make up the brand name (text)? (list)", "ids": ["e3", "e4"], "set": True}]
    assert g["logo_sting-1"]["edit"] == [{"q": "make the frame red", "ids": ["e2"], "property": "color"},
                                         {"q": "change the brand name text to 'Hello'", "ids": ["e3", "e4"], "property": "text"},
                                         {"q": "make the frame appear 0.5 s later", "ids": ["e2"], "property": "timing"}]


def test_motion_questions_from_ir(tmp_scene_dir):
    scene = make_reference_scene(tmp_scene_dir, 4, plate="flat", mover=True, logo=True)
    qs = ab_views.motion_questions(scene)
    assert {q["cat"] for q in qs} == {"motion"} and {"number", "id", "point"} <= {q["kind"] for q in qs}
    entrance = next(q for q in qs if "first appear" in q["q"])
    el = scene.element(entrance["q"].split()[6])
    assert entrance["gold"] == el.visible[0] / scene.fps


def test_paired_ci_resamples_clips():
    """Two clips, kf-script right on every question of one and level on the other: +50 points; the interval spans the
    clip spread (0 to +100), not the question count."""
    rows = [{"clip": c, "view": v, "q": f"Q{i}", "cat": "auto", "score": float(v == "kfscript" or c == "b")}
            for c in ("a", "b") for v in ab_views.VIEWS for i in range(10)]
    ci = ab_views.paired_ci(rows, {"auto"})
    assert ci["mean"] == 50 and ci["lo"] == 0 and ci["hi"] == 100 and ci["clips"] == 2 and ci["n"] == 20
    assert ab_views.paired_ci(rows, {"edit"})["n"] == 0
    # the scenes of one clip are one cluster: a:s1 and a:s2 resample together
    split = [{**r, "clip": f"a:s{1 + int(r['q'][1:]) % 2}"} if r["clip"] == "a" else r for r in rows]
    assert ab_views.paired_ci(split, {"auto"})["clips"] == 2
    assert ab_views.paired_ci(split, {"auto"}, by_scene=True)["clips"] == 3   # real clips: each scene its own cluster


def test_real_gold_from_read_boxes(tmp_path):
    """Read boxes measured on source frames name roles; the IR ids inside each (≥ 60 % of their own box) at that frame
    answer them, each id owned by the smallest box holding it."""
    from keepframe.ir.tracks import element_bbox
    scene = make_reference_scene(tmp_path / "src", 9, plate="flat", title="stagger").model_copy(update={"id": "s1"})
    init_project(tmp_path / "kf" / "clipA", {"file": "x.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range",
                                             "range": [0, scene.frames - 1]}, scene)
    f = scene.frames - 1
    on = [e for e in scene.elements if e.visible[0] <= f <= e.visible[1]]
    small = [round(v) for v in element_bbox(on[0], f, scene.size)]
    big = [0, 0, scene.size[0], scene.size[1]]
    boxes = [{"clip": "clipA", "frame": f, "role": "title", "kind": "text", "box": [small[0] - 2, small[1] - 2, small[2] + 2, small[3] + 2]},
             {"clip": "clipA", "frame": f, "role": "whole frame", "kind": "shape", "box": big}]
    gold = ab_views.real_gold(tmp_path / "kf", boxes)["clipA:s1"]
    title, frame = (r["ids"] for r in gold["read"])
    assert on[0].id in title and on[0].id not in frame and set(title) | set(frame) == {e.id for e in on}
    assert [e["property"] for e in gold["edit"]] == ["text", "timing", "color"]
