from keepframe.analyze.pipeline import AnalyzeOptions
from keepframe.analyze.video import render_scene_video
from keepframe.gates import m2_gate_real
from keepframe.ir.synth import make_synthetic_scene


def test_gate_real_caps_frames(tmp_path):
    clips = tmp_path / "clips"; clips.mkdir()
    gold = make_synthetic_scene(tmp_path / "gold", seed=3, with_text=False, frames=24)
    render_scene_video(gold, tmp_path / "gold", clips / "a.mp4")
    res = m2_gate_real(clips, tmp_path / "out", max_frames=12, options=AnalyzeOptions(ocr=False, refine=False))
    row = res["rows"][0]
    assert row["frames"] == 12
    assert row["live_action"] is None
    assert {"elements", "kinds", "mean_l1", "low_conf", "keep_on", "constraints", "seconds"} <= set(row)
