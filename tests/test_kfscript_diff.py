"""Structural diff against round-trip truth (P2 Task 3)."""
from keepframe.ir.schema import Background, Canonical, Element, GlyphReveal, Keyframe, Scene, Track
from keepframe.kfscript.diff import diff


def _truth(*els, structure=()):
    return {"elements": [{"id": i, "kind": k, "text": text, "colour": "#ffffff" if text else None, "visible": list(vis),
                          "boxes": {str(f): list(box) for f in range(vis[0], vis[1] + 1)}} for i, k, box, vis, text in els],
            "structure": list(structure), "shots": [[0, 59]]}


def _el(id_, box, vis, text=None, colour="#ffffff", glyphs=False):
    x0, y0, x1, y1 = box
    can = Canonical(width=x1 - x0, height=y1 - y0, text=text, color=colour if text else None,
                    glyphs=GlyphReveal(x=[0.25, 0.75], delays=[0, 3]) if glyphs else None)
    return Element(id=id_, kind="text" if text else "sprite", canonical=can, visible=vis,
                   tracks={"x": Track(keys=[Keyframe(t=0, v=(x0 + x1) / 2)]), "y": Track(keys=[Keyframe(t=0, v=(y0 + y1) / 2)])})


def _scene(*els):
    return Scene(id="s1", size=(960, 540), fps=30, frames=60, background=Background(kind="color", value="#000000"), elements=list(els))


T = _truth(("title", "text", (100, 100, 500, 180), (0, 59), "Ship faster"), ("card", "shape", (600, 300, 900, 500), (10, 59), None),
           structure=[{"type": "typing", "targets": ["title"], "params": {}, "ir": "now"},
                      {"type": "stagger", "targets": ["title"], "params": {}, "ir": "P3"}])


def test_identity_perfect():
    d = diff(T, [(_scene(_el("e1", (100, 100, 500, 180), (0, 59), "Ship faster", glyphs=True), _el("e2", (600, 300, 900, 500), (10, 59))), 0)])
    assert d["matched_rate"] == 1 and d["text_ratio"] == 1 and d["colour_de"] == 0 and d["centre_px"] == 0 and d["visible_err"] == 0
    assert [r["match"] for r in d["rows"]] == ["e1", "e2"] and d["over_split"] == 0 and d["pieces_mean"] == 1
    assert d["structure"] == {"found": ["typing"], "missed": [], "unsupported": ["stagger"]}


def test_fragmentation_counted():
    thirds = [_el("a", (100, 100, 233, 180), (0, 59), "Ship"), _el("b", (233, 100, 366, 180), (0, 59), "fas"),
              _el("c", (366, 100, 500, 180), (0, 59), "ter")]
    d = diff(T, [(_scene(*thirds), 0)])
    title = d["rows"][0]
    assert title["fragments"] == 3 and d["over_split"] == 1 and title["match"] is None and d["matched_rate"] == 0
    assert title["text_ratio"] == 0 and d["structure"]["missed"] == ["typing"]


def test_unsupported_recorded():
    d = diff(T, [(_scene(), 0)])
    assert d["structure"]["unsupported"] == ["stagger"] and d["structure"]["missed"] == ["typing"] and d["matched"] == 0


def test_dl_ratio_and_split_scenes():
    # the analysis split the clip at frame 30: the second scene's frames are offset, the card is still matched
    first, second = _scene(_el("e1", (100, 100, 500, 180), (0, 29), "Ship faster")), _scene(_el("e2", (600, 300, 900, 500), (0, 29)))
    d = diff(T, [(first, 0), (second, 30)], view_chars=500, truth_chars=2000)
    assert d["dl_ratio"] == 0.25 and d["shots"] == {"truth": 1, "ir_scenes": 2, "boundaries": [30], "f1": 0.0}   # a split the truth has not
    card = d["rows"][1]
    assert card["match"] == "e2" and card["visible_err"] == 20   # IR 30–59 vs truth 10–59


def test_pop_and_drift_found_on_tracks():
    t = _truth(("mark", "image", (400, 200, 500, 300), (0, 59), None), ("url", "text", (300, 400, 600, 440), (0, 59), "www.x.com"),
               structure=[{"type": "pop", "targets": ["mark"], "params": {}, "ir": "now"},
                          {"type": "drift", "targets": ["url"], "params": {"axis": "y", "px": 24}, "ir": "now"}])
    mark, url = _el("e1", (400, 200, 500, 300), (0, 59)), _el("e2", (300, 400, 600, 440), (0, 59), "www.x.com")
    mark.tracks["sx"] = Track(keys=[Keyframe(t=0, v=0.1), Keyframe(t=10, v=1.0)])
    url.tracks["y"] = Track(keys=[Keyframe(t=0, v=405), Keyframe(t=30, v=420)])
    assert diff(t, [(_scene(mark, url), 0)])["structure"]["found"] == ["pop", "drift"]
    url.tracks["y"] = Track(keys=[Keyframe(t=0, v=420), Keyframe(t=30, v=405)])   # moves up: not the declared drift
    assert diff(t, [(_scene(mark, url), 0)])["structure"]["missed"] == ["drift"]


def test_shot_f1():
    from keepframe.kfscript.diff import shot_f1
    cut = {"shots": [[0, 44], [45, 89]], "transitions": [{"type": "cut", "frames": [45, 45]}]}
    assert shot_f1(cut, [45])["f1"] == 1 and shot_f1(cut, [46])["f1"] == 1 and shot_f1(cut, [48])["f1"] == 0
    assert shot_f1(cut, [])["f1"] == 0 and shot_f1(cut, [20, 45])["f1"] == 0.667
    wipe = {"shots": [[0, 43], [44, 89]], "transitions": [{"type": "wipe", "frames": [40, 48]}]}
    assert shot_f1(wipe, [48])["f1"] == 1 and shot_f1(wipe, [39])["f1"] == 1 and shot_f1(wipe, [51])["f1"] == 0
    assert shot_f1({"shots": [[0, 89]]}, [])["f1"] == 1


def test_build_and_shrink_found_on_tracks():
    t = _truth(("l1", "text", (100, 100, 500, 180), (0, 59), "Ship faster"),
               structure=[{"type": "build", "targets": ["l1"], "params": {"unit": "word"}, "ir": "now"},
                          {"type": "shrink", "targets": ["l1"], "params": {"scale": [1, 0.55]}, "ir": "now"}])
    grown = _el("e1", (100, 100, 500, 180), (0, 59), "Ship faster")   # the build read as a growing box: neither
    grown.tracks["sx"] = Track(keys=[Keyframe(t=0, v=0.4), Keyframe(t=20, v=1.0)])
    assert diff(t, [(_scene(grown), 0)])["structure"]["missed"] == ["build", "shrink"]
    typed = _el("e1", (100, 100, 500, 180), (0, 59), "Ship faster", glyphs=True)   # glyphs, then a shrink about its centre
    typed.tracks.update(sx=Track(keys=[Keyframe(t=0, v=1.0), Keyframe(t=40, v=1.0), Keyframe(t=55, v=0.6)]),
                        sy=Track(keys=[Keyframe(t=0, v=1.0), Keyframe(t=40, v=1.0), Keyframe(t=55, v=0.6)]))
    assert diff(t, [(_scene(typed), 0)])["structure"]["found"] == ["build", "shrink"]


def test_pieces_over_life():
    # the title is one element early and another late (a temporal split): one fragment at mid, two pieces over its life
    early, late = _el("a", (100, 100, 500, 180), (0, 20), "Ship faster", glyphs=True), _el("b", (100, 100, 500, 180), (21, 59), "Ship faster")
    d = diff(T, [(_scene(early, late), 0)])
    title = d["rows"][0]
    assert title["fragments"] == 1 and title["pieces"] == 2 and title["piece_ids"] == ["a", "b"]
    assert d["structure"]["found"] == ["typing"]   # the reveal lives on the early piece
