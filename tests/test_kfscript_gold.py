"""mk571 gold truth and its hand-written page (P2 Task 5)."""
import json
from pathlib import Path

import pytest

from keepframe.qa.roundtrip import GOLD, REPO

TRUTH = json.loads((GOLD / "mk571.truth.json").read_text())


def test_gold_mk571_truth_ids():
    page = (REPO / TRUTH["page"]).read_text()
    ids = [e["id"] for e in TRUTH["elements"]]
    assert all(f'id="{i}"' in page for i in ids) and "window.__bbox" in page
    assert all(set(s["targets"]) <= set(ids) and s["ir"] in {"now", "P3"} for s in TRUTH["structure"])
    assert TRUTH["size"] == [1920, 1080] and TRUTH["frames"] == 482


@pytest.mark.browser
def test_gold_mk571_bbox(tmp_path):
    from keepframe.ir.schema import Background, Canonical, Element, Scene
    from keepframe.render.renderer import render
    ids = [e["id"] for e in TRUTH["elements"]]
    stub = Scene(id="g", size=(1920, 1080), fps=TRUTH["fps"], frames=482, background=Background(kind="color", value="#000000"),
                 elements=[Element(id=i, kind="sprite", visible=(0, 481), canonical=Canonical(width=1, height=1)) for i in ids])
    b = render(REPO / TRUTH["page"], stub, tmp_path, frames=[100, 300]).bboxes
    x0, y0, x1, y1 = b["frame"][1]   # settled: 1012 × 298 about the centre (gold-reads.md)
    assert abs((x1 - x0) - 1012) < 3 and abs((y1 - y0) - 298) < 3 and abs((x0 + x1) / 2 - 959.5) < 1
    assert b["frame"][0][2] - b["frame"][0][0] == pytest.approx(241, abs=3)   # before the stretch
    assert b["url"][1][1] > y1 and b["name"][1][0] > x0                     # url below the frame, name inside it
