"""Round-trip reference pages (P2 Tasks 1–2)."""
import json

import numpy as np
import pytest

from keepframe.kfscript.synth import KINDS, make_template, render_template


def test_templates_deterministic():
    for k in KINDS:
        a, b = make_template(k, 1), make_template(k, 1)
        assert a.doc == b.doc and a.truth == b.truth, k
    assert any(make_template(k, 1).doc != make_template(k, 2).doc for k in KINDS)


def test_truth_levels():
    for k in KINDS:
        t = make_template(k, 1)
        ids = [e["id"] for e in t.truth["elements"]]
        assert len(ids) == len(set(ids)) and all(f'id="{i}"' in t.doc for i in ids), k
        for e in t.truth["elements"]:
            assert e["kind"] in {"text", "shape", "image"} and 0 <= e["visible"][0] <= e["visible"][1] < t.frames, (k, e)
            assert e["kind"] != "text" or e["text"], (k, e)
        for s in t.truth["structure"]:
            assert s["ir"] in {"now", "P3"} and set(s["targets"]) <= set(ids), (k, s)
        assert t.truth["kind"] == k and t.truth["shots"][0][0] == 0 and t.truth["shots"][-1][1] == t.frames - 1


@pytest.mark.browser
def test_every_kind_renders(tmp_path):
    from keepframe.ir.schema import Background, Canonical, Element, Scene
    from keepframe.render.renderer import load_frame, render
    for k in KINDS:
        t = make_template(k, 1)
        page = tmp_path / f"{k}.html"
        page.write_text(t.doc)
        ids = [e["id"] for e in t.truth["elements"]]
        stub = Scene(id=k, size=t.size, fps=t.fps, frames=t.frames, background=Background(kind="color", value="#000000"),
                     elements=[Element(id=i, kind="sprite", visible=(0, t.frames - 1), canonical=Canonical(width=1, height=1)) for i in ids])
        res = render(page, stub, tmp_path / k, frames=[t.frames // 2])
        assert load_frame(res.frames_dir / "f_00000.png").std() > 0.02, k   # the middle frame is not blank
        assert all(len(res.bboxes[i]) == 1 for i in ids), k


@pytest.mark.browser
def test_render_template_mp4_and_boxes(tmp_path):
    from keepframe.analyze.video import read_frames
    mp4, truth = render_template("stepped_zoom", 1, tmp_path)
    t = json.loads(truth.read_text())
    frames, _ = read_frames(mp4)
    assert len(frames) == t["frames"] == 90 and frames[0].shape == (540, 960, 3)
    for e in t["elements"]:
        a, b = e["visible"]
        assert sorted(map(int, e["boxes"])) == list(range(a, b + 1))
        assert all(0 <= x0 < x1 <= 960 and 0 <= y0 < y1 <= 540 for x0, y0, x1, y1 in e["boxes"].values())
    word = next(e for e in t["elements"] if e["id"] == "word")
    w = lambda f: word["boxes"][str(f)][2] - word["boxes"][str(f)][0]
    assert w(12) < w(24) < w(5)   # the stepped zoom holds: big, small, settle
    assert np.abs(frames[5].astype(float) - frames[12].astype(float)).mean() > 1
