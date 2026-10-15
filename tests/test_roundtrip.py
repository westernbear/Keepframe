"""Round-trip harness (P2 Task 4)."""
import json

import pytest

from keepframe import cli
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.store import init_project, scene_dir


def test_cli_eval_roundtrip_args(monkeypatch, tmp_path):
    import keepframe.qa.roundtrip as rt
    got = {}
    monkeypatch.setattr(rt, "eval_roundtrip", lambda *a: got.setdefault("args", a) and {"passed": True})
    assert cli.main(["eval-roundtrip", "--out", str(tmp_path), "--kinds", "counter", "chat_ui", "--seeds", "2", "--reuse",
                     "--gold", "mk571=a.mp4,eval/kf/mk571", "--first-seed", "3"]) == 0
    assert got["args"] == (tmp_path, ("counter", "chat_ui"), 2, True, ["mk571=a.mp4,eval/kf/mk571"], 3)
    for bad in (["--kinds", "nope"], ["--gold", "mk571"]):
        with pytest.raises(SystemExit):
            cli.main(["eval-roundtrip", "--out", str(tmp_path), *bad])


def _fake_analyse(video, n_frames, root, opts, ocr, reuse):
    """The truth itself as the analysis: one static element per truth element at its mid-visible box."""
    truth = json.loads((video.parent / "truth.json").read_text())
    els = []
    for e in truth["elements"]:
        a, b = e["visible"]
        x0, y0, x1, y1 = e["boxes"][str((a + b) // 2)]
        els.append(Element(id=e["id"], kind="text" if e["kind"] == "text" else "sprite", visible=(a, b),
                           canonical=Canonical(width=x1 - x0, height=y1 - y0, text=e["text"] if e["kind"] == "text" else None, color=e["colour"]),
                           tracks={"x": Track(keys=[Keyframe(t=0, v=(x0 + x1) / 2)]), "y": Track(keys=[Keyframe(t=0, v=(y0 + y1) / 2)])}))
    scene = Scene(id="s1", size=tuple(truth["size"]), fps=truth["fps"], frames=n_frames, background=Background(kind="color", value="#101418"),
                  elements=els)
    if not (reuse and (root / "project.json").exists()):
        import shutil
        shutil.rmtree(root, ignore_errors=True)
        init_project(root, {"file": str(video), "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, n_frames - 1]}, scene)
        (scene_dir(root, "s1") / "report.json").write_text(json.dumps({"reconstruction": {"mean_l1": 0.05}}))
    return 1.0


@pytest.mark.browser
def test_fake_analysis_end_to_end(monkeypatch, tmp_path):
    import keepframe.qa.roundtrip as rt
    monkeypatch.setattr(rt, "_analyse", _fake_analyse)
    m = rt.eval_roundtrip(tmp_path, ("stepped_zoom",), 1)
    case = m["cases"][0]
    assert m["passed"] and case["kf_parity"] <= rt.ROUNDTRIP_GATES["kf_parity"] and case["l1_page"] < 0.01
    d = case["diff"]
    assert d["matched_rate"] == 1 and d["text_ratio"] == 1 and d["visible_err"] == 0 and d["structure"]["unsupported"] == ["steps"]
    assert (tmp_path / "summary.md").read_text().count("| stepped_zoom-1 |") == 1 and (tmp_path / "sheets" / "stepped_zoom-1.s1.png").exists()
    again = rt.eval_roundtrip(tmp_path, ("stepped_zoom",), 1, reuse=True)
    strip = lambda c: {k: v for k, v in c.items() if not k.endswith("_seconds")}
    assert strip(again["cases"][0]) == strip(case)


def test_ghost_de_sees_text_left_in_a_video_background(tmp_path):
    """B8 measuring stick: a background video that keeps a title's ghost inside its box scores higher than a clean one."""
    import cv2
    import numpy as np
    from keepframe.ir.schema import Background, Scene
    from keepframe.qa.roundtrip import ghost_de
    truth = {"elements": [{"id": "t", "kind": "text", "visible": [0, 7], "boxes": {str(f): [40, 40, 120, 70] for f in range(8)}}]}
    ramp = np.tile(np.linspace(30, 90, 160, dtype=np.uint8)[None, :, None], (120, 1, 3))
    scores = []
    for ghost in (False, True):
        root = tmp_path / str(ghost)
        (root / "scenes" / "s1" / "assets").mkdir(parents=True)
        w = cv2.VideoWriter(str(root / "scenes" / "s1" / "assets" / "bg.avi"), cv2.VideoWriter_fourcc(*"MJPG"), 30, (160, 120))
        for _ in range(8):
            f = ramp.copy()
            if ghost:
                f[48:62, 50:110] = 220
            w.write(f)
        w.release()
        scene = Scene(id="s1", size=(160, 120), fps=30, frames=8, background=Background(kind="video", value="assets/bg.avi"), elements=[])
        scores.append(ghost_de([(scene, 0)], root, truth))
    assert scores[0] < 3 < scores[1]
    assert ghost_de([(scene, 8)], root, truth) is None   # a scene (shot) the text never lives in measures nothing of it
