"""Task 12 (Stage B): the guard (D16) undoes a merged title that renders worse than its fragments: they come back under
their own keys, grouped (`text_fragments`) in the scene and the brief; an edit on a group member acts on the group."""
import copy, logging, re, time
import cv2, numpy as np, pytest
from keepframe.analyze import pipeline, titles
from keepframe.analyze.titles import MERGE_WORSE, guard_titles, text_groups
from keepframe.edit.agent import edit
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import Intent, Target, plan
from keepframe.edit.retime import retime_element
from keepframe.ir.schema import Background, Canonical, Element, FontGuess, Group, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, init_project
from keepframe.ir.synth import make_text_texture, make_texture
from keepframe.ir.tracks import element_bbox
from keepframe.render.renderer import RenderResult
from keepframe.session.brief import scene_brief
from tests.test_titles_merge import TITLE, Ocr, _analyse, _report, _split_scene, _stages


def _split(tmp):
    """test_titles_merge's split title ("Grand" + "Openin" + the missed "g" as a sprite), analysed; returns the root and
    what the guard was given (props copied before it ran)."""
    truth, video = _split_scene(tmp)
    seen = {}

    def spy(props, frames, plate, notes):
        seen.update(props=copy.deepcopy(props), frames=frames, plate=plate)
        guard_titles(props, frames, plate, notes)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "guard_titles", spy)
        root, _ = _analyse(tmp, truth, video, Ocr(truth, tmp / "truth", video, split=True, reread=TITLE))
    return root, seen


@pytest.fixture(scope="module")
def split(tmp_path_factory):
    return _split(tmp_path_factory.mktemp("guard"))


def test_guard_keeps_a_good_merge(split, caplog):
    root, seen = split
    props, _, scene = _stages(root)
    assert [k for k in props if k.startswith("title_")] == ["title_t1"]
    assert not any(isinstance(p, dict) and p.get("text_group") for p in props.values())
    assert not [g for g in scene.groups if g.reason == "text_fragments"]
    caplog.set_level(logging.INFO, "keepframe.analyze")
    again, notes = copy.deepcopy(seen["props"]), []
    guard_titles(again, seen["frames"], seen["plate"], notes)
    still, fragments = map(float, re.search(r"titles guard title_t1 still=([\d.]+) fragments=([\d.]+)", caplog.text).groups())
    assert still <= fragments and "title_t1" in again and notes == []
    with pytest.MonkeyPatch.context() as mp:   # fail soft: a guard that cannot measure keeps the merge, one coded note
        mp.setattr("keepframe.analyze.solid_assets.solid_errors", lambda *a: 1 / 0)
        guard_titles(again, seen["frames"], seen["plate"], notes)
    assert "title_t1" in again and notes == ["title guard failed (guard_failed)"]


def test_guard_undoes_a_misplaced_merge(split):
    _, seen = split
    props, notes = copy.deepcopy(seen["props"]), []
    p = props["title_t1"]
    fragments = copy.deepcopy(p["fragments"])
    p["raw"][:, 0] += p["canon"].shape[1] * np.nanmedian(p["raw"][:, 2]) / 4   # a quarter of its width off
    guard_titles(props, seen["frames"], seen["plate"], notes)
    assert "title_t1" not in props and notes == [] and len(fragments) == 3
    for k, f in fragments.items():   # back under their own keys, as measured, marked
        q = props[k]
        assert (q["merge_note"], q["text_group"]) == (MERGE_WORSE, "title_t1")
        assert np.array_equal(q["raw"], f["raw"], equal_nan=True) and np.array_equal(q["canon"], f["canon"])
    ids = {k: f"e{i}" for i, k in enumerate(props, 1) if not k.startswith("_")}
    assert text_groups(props, ids) == [Group(id="text1", members=[ids[k] for k in fragments], reason="text_fragments")]


def test_text_groups_reach_scene_and_brief(split, tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(titles, "TITLE_GUARD", -1.0)   # every merge renders "worse"
        root, _ = _split(tmp_path)
    props, ids, scene = _stages(root)
    restored = [k for k, p in props.items() if isinstance(p, dict) and p.get("text_group") == "title_t1"]
    assert not [k for k in props if k.startswith("title_")] and set(restored) == set(split[1]["props"]["title_t1"]["fragments"])
    members = [ids[k] for k in restored]
    assert scene.groups[0] == Group(id="text1", members=members, reason="text_fragments")
    assert f"group text1: {', '.join(members)} (text_fragments)" in scene_brief(scene)
    messages = _report(root)["messages"]
    assert all(f"{m}: title merge failed ({MERGE_WORSE}); kept as measured" in messages for m in members)
    assert set(members) <= pipeline.fallback_ids(props, ids)   # less sure, as a title left as its fragments


def test_guard_cost():
    """A 640 px title in two pieces sliding over 1280×720 × 60 frames: the guard measures it within 3 s and keeps the
    (exact) merge."""
    W, H, n, w, h = 1280, 720, 60, 640, 96
    canon = np.zeros((h, w, 4), np.uint8)
    for i in range(16):   # glyph-like bars
        canon[16:80, 8 + 40 * i:36 + 40 * i] = (240, 200, 40, 255)
    plate = np.full((H, W, 3), 30, np.uint8)
    frames = np.repeat(plate[None], n, 0)
    left = 320 + 2 * np.arange(n)
    for f, x in enumerate(left):
        frames[f, 300:300 + h, x:x + w][canon[..., 3] > 0] = canon[canon[..., 3] > 0][:, :3]

    def prop(c, x0, kind):
        raw = np.zeros((n, 8))
        raw[:, 0], raw[:, 1], raw[:, 2:4], raw[:, 7] = left + x0 + c.shape[1] / 2, 300 + h / 2, 1.0, 1.0
        return {"raw": raw, "canon": c, "cf": 30, "kind": kind, "first": 0, "last": n - 1, "text": "x" if kind == "text" else None}

    title = {**prop(canon, 0, "text"), "fragments": {"t1": prop(canon[:, :w // 2], 0, "text"), "o2": prop(canon[:, w // 2:], w // 2, "sprite")},
             "boxes": np.stack([left, np.full(n, 300), left + w, np.full(n, 300 + h)], 1).astype(float)}
    props, notes = {"title_t1": title}, []
    t0 = time.perf_counter()
    guard_titles(props, frames, plate, notes)
    assert time.perf_counter() - t0 <= 3.0 and list(props) == ["title_t1"] and notes == []


def _group_scene(sd):
    """A title in three pieces ("Grand", "Openin" and the missed "g", a sprite) sliding right, the guard's group, and a
    logo outside it."""
    (sd / "assets").mkdir(parents=True)
    make_texture(sd / "assets/g.png", "rect", 12, 20, (255, 255, 255))
    make_texture(sd / "assets/logo.png", "ellipse", 30, 30, (40, 200, 40))

    def piece(eid, kind, visible, x, w, text=None):
        if text:
            make_text_texture(sd / f"assets/{eid}.png", text, 24, (255, 255, 255))
        return Element(id=eid, kind=kind, visible=visible, canonical=Canonical(
            width=w, height=30, text=text, font=FontGuess(size_px=24) if text else None, color="#ffffff" if text else None,
            texture=f"assets/{eid}.png" if text else "assets/g.png"),
            tracks={"x": Track(keys=[Keyframe(t=visible[0], v=x), Keyframe(t=visible[1], v=x + 40)]), "y": Track(keys=[Keyframe(t=0, v=100.0)])})

    logo = Element(id="e4", kind="sprite", visible=(0, 39), canonical=Canonical(width=30, height=30, texture="assets/logo.png"),
                   tracks={"x": Track(keys=[Keyframe(t=0, v=40.0)]), "y": Track(keys=[Keyframe(t=0, v=40.0)])})
    return Scene(id="s1", size=(400, 200), fps=30, frames=40, background=Background(),
                 elements=[piece("e1", "text", (10, 39), 100.0, 80, "Grand"), piece("e2", "text", (14, 30), 190.0, 90, "Openin"),
                           piece("e3", "sprite", (10, 39), 245.0, 12), logo],
                 groups=[Group(id="text1", members=["e1", "e2", "e3"], reason="text_fragments")])


@pytest.fixture
def project(tmp_path, monkeypatch):
    """The group scene as a project; renders are probe boxes (no browser)."""
    root = tmp_path / "proj"
    scene = _group_scene(root / "scenes" / "s1")
    init_project(root, {"file": "ref.mp4", "fps": 30, "size": [400, 200]}, scene)

    def probes(_html, edited, out_dir):
        frames = list(range(edited.frames))
        return RenderResult(frames_dir=out_dir / "frames", frames=frames, hashes=[],
                            bboxes={el.id: [list(element_bbox(el, f)) for f in frames] for el in edited.elements})

    monkeypatch.setattr("keepframe.edit.agent.render", probes)
    return root, scene


def test_colour_edit_on_a_group_member_recolours_all(tmp_path):
    scene = _group_scene(tmp_path)
    out = apply_edit(scene, tmp_path, [Target(element="e3", property="color", value="#ff0000")], None, None)
    for eid in ("e1", "e2", "e3"):
        el = out.element(eid)
        tex = cv2.imread(str(tmp_path / el.canonical.texture), cv2.IMREAD_UNCHANGED)
        assert el.canonical.color == "#ff0000" and el.provenance == "manual"
        assert np.median(tex[tex[..., 3] > 200][:, :3], axis=0).tolist() == [0, 0, 255]   # BGR
    assert out.element("e4") == scene.element("e4") and out.groups == scene.groups


def test_retext_on_a_group_member_replaces_the_group(project):
    root, before = project
    result = edit(root, "s1", "문구를 바꿔줘", confirm=True, choices={"overflow": "expand_box"},
                  intent={"targets": [{"element": "e2", "property": "text", "value": "Fall Drop Sale"}]})
    assert result.status == "done", result.error
    edited, _ = current_scene(root, "s1")
    assert [e.id for e in edited.elements] == ["e1", "e4"] and edited.groups == []
    first = edited.element("e1")
    assert first.canonical.text == "Fall Drop Sale" and (first.visible, first.tracks) == (before.element("e1").visible, before.element("e1").tracks)


def test_timing_edit_on_a_group_member_moves_all(project):
    root, before = project
    result = edit(root, "s1", "속도를 바꿔줘", confirm=True,
                  intent={"targets": [{"element": "e3", "property": "timing", "speed": 2.0, "delay": 0.5}]})
    assert result.status == "done", result.error
    edited, _ = current_scene(root, "s1")
    for eid in ("e1", "e2", "e3"):   # one anchor, the group's first frame (10), then 15 frames later
        want = before.element(eid).model_copy(deep=True)
        retime_element(want, 2.0, 15, 10)
        got = edited.element(eid)
        assert np.abs(np.subtract(got.visible, want.visible)).max() <= 1
        assert [k.t for k in got.tracks["x"].keys] == pytest.approx([k.t for k in want.tracks["x"].keys], abs=1)
    assert edited.element("e4") == before.element("e4")
    built = plan(before, Intent(targets=[Target(element="e2", property="timing", delay=0.2)]))   # e2 ends early, e1 does not
    assert [(c.id, c.element) for c in built.conflicts] == [("timing_overflow", "e1")]
