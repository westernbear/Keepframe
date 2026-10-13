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
    assert [r["match"] for r in d["rows"]] == ["e1", "e2"] and d["over_split"] == 0
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
    assert d["dl_ratio"] == 0.25 and d["shots"] == {"truth": 1, "ir_scenes": 2}
    card = d["rows"][1]
    assert card["match"] == "e2" and card["visible_err"] == 20   # IR 30–59 vs truth 10–59
