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


def test_gate_real_counts_every_scene(tmp_path, monkeypatch):
    """A clip the analysis splits at a cut reports all its scenes, not only s1."""
    import keepframe.analyze.pipeline as pipeline
    clips = tmp_path / "clips"; clips.mkdir()
    gold = make_synthetic_scene(tmp_path / "gold", seed=3, with_text=False, frames=24)
    render_scene_video(gold, tmp_path / "gold", clips / "a.mp4")
    real = pipeline.analyze
    monkeypatch.setattr(pipeline, "analyze", lambda v, a, b, root, opts, **k: real(
        v, a, b, root, opts, scenes=[{"id": "s1", "frames": [0, 9]}, {"id": "s2", "frames": [10, b]}]))
    row = m2_gate_real(clips, tmp_path / "out", max_frames=24, options=AnalyzeOptions(ocr=False, refine=False))["rows"][0]
    assert row["scenes"] == [["s1", 0, 9], ["s2", 10, 23]] and row["frames"] == 24
