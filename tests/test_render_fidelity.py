import json

import cv2
import numpy as np
import pytest

from keepframe import gates
from keepframe.analyze.pipeline import AnalyzeOptions
from keepframe.analyze.video import render_scene_video
from keepframe.compose.composer import compose
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.store import init_project, new_version, scene_dir
from keepframe.ir.synth import make_texture
from keepframe.render.renderer import render


def _project(tmp_path):
    root = tmp_path / "project"
    scene = Scene(
        id="animation", size=(200, 120), fps=30, frames=21,
        background=Background(value="#101418"), elements=[
            Element(id="card", kind="sprite", visible=(0, 20),
                    canonical=Canonical(width=60, height=40, texture="assets/card.png"),
                    tracks={
                        "x": Track(keys=[Keyframe(t=0, v=60), Keyframe(t=20, v=140)]),
                        "y": Track(keys=[Keyframe(t=0, v=60)]),
                    }),
        ],
    )
    init_project(root, {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    sd = scene_dir(root, scene.id)
    make_texture(sd / "assets/card.png", "rect", 60, 40, (239, 71, 111))
    return root, scene, sd


@pytest.mark.browser
def test_render_fidelity_is_low_for_own_video_and_detects_twenty_pixel_shift(tmp_path):
    root, scene, sd = _project(tmp_path)
    html = compose(scene, sd, tmp_path / "source.html")
    clip = render(html, scene, tmp_path / "source-render", mp4=True).mp4
    reference = gates.render_fidelity(root, scene.id, clip)
    assert reference["render_l1"] < 0.01
    assert reference["frames"] == [0, 10, 20]

    shifted = scene.model_copy(deep=True)
    for key in shifted.element("card").tracks["x"].keys:
        key.v += 20
    new_version(root, scene.id, shifted, note="shift x by 20px")
    changed = gates.render_fidelity(root, scene.id, clip)
    assert changed["render_l1"] > reference["render_l1"]


@pytest.mark.browser
def test_real_gate_render_check_is_optional_and_preserves_frame_limit(tmp_path):
    root, scene, sd = _project(tmp_path)
    html = compose(scene, sd, tmp_path / "source.html")
    clip = render(html, scene, tmp_path / "source-render", mp4=True).mp4
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "animation.mp4").write_bytes(clip.read_bytes())
    options = AnalyzeOptions(ocr=False, refine=False)

    default = gates.m2_gate_real(clips, tmp_path / "unchecked", max_frames=11, options=options)
    assert default["rows"][0]["render_l1"] is None
    assert default["rows"][0]["render_frames"] is None
    checked = gates.m2_gate_real(clips, tmp_path / "checked", max_frames=11, options=options, render_check=True)
    row = checked["rows"][0]
    assert row["frames"] == 11
    assert 0 <= row["render_l1"] <= 1
    assert row["render_frames"] == 2


@pytest.mark.parametrize("error", [
    RuntimeError("Chromium launch failed"),
    FileNotFoundError("unreadable clip"),
    ValueError("no source frames to compare: " + "x" * 250),
])
def test_real_gate_continues_after_render_check_failure(tmp_path, monkeypatch, error):
    _, scene, sd = _project(tmp_path)
    clips = tmp_path / "clips"
    clips.mkdir()
    first = render_scene_video(scene, sd, clips / "a-failed.mp4")
    (clips / "b-good.mp4").write_bytes(first.read_bytes())
    first.with_suffix(".gt.json").write_text(json.dumps({"elements": []}))

    def render_check(root, scene_id, clip, sample_every=10, max_frames=150):
        if clip.name == "a-failed.mp4":
            raise error
        return {"render_l1": 0.125, "frames": [0, 10]}

    monkeypatch.setattr(gates, "render_fidelity", render_check)
    out = tmp_path / "out"
    result = gates.m2_gate_real(clips, out, max_frames=11, options=AnalyzeOptions(ocr=False, refine=False),
                                render_check=True)

    assert result["clips"] == 2
    failed, good = result["rows"]
    assert [row["clip"] for row in result["rows"]] == ["a-failed.mp4", "b-good.mp4"]
    assert failed["render_l1"] is None and failed["render_frames"] is None
    report = json.loads((scene_dir(out / "a-failed", "s1") / "report.json").read_text())
    assert failed["messages"] == report["messages"] + [f"render check failed: {type(error).__name__}: {error}"[:200]]
    assert len(failed["messages"][-1]) <= 200
    assert failed["pos_err_px"] is None
    assert good["render_l1"] == 0.125 and good["render_frames"] == 2
    assert not any(message.startswith("render check failed:") for message in good["messages"])


def test_real_gate_cli_accepts_render_check(tmp_path, capsys):
    import json
    from keepframe.cli import main

    assert main(["gate-m2-real", "--clips", str(tmp_path / "clips"), "--out", str(tmp_path / "out"),
                 "--max-frames", "11", "--render-check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"clips": 0, "max_frames": 11, "rows": []}


@pytest.mark.browser
@pytest.mark.parametrize("source_frames, expected_frames, expected_reads", [(30, [0, 5, 10], 11), (3, [0], 4)])
def test_render_fidelity_streams_only_to_last_sample_and_resizes_render(
    tmp_path, monkeypatch, source_frames, expected_frames, expected_reads,
):
    root, scene, _ = _project(tmp_path)
    scene.background.value = "#ffffff"
    scene.elements = []
    new_version(root, scene.id, scene, note="white background")
    clip = tmp_path / "black.avi"
    writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"MJPG"), 30, (400, 240))
    assert writer.isOpened()
    try:
        for _ in range(source_frames):
            writer.write(np.zeros((240, 400, 3), dtype=np.uint8))
    finally:
        writer.release()

    open_capture = cv2.VideoCapture
    captures = []

    class SequentialCapture:
        def __init__(self, path):
            self.capture = open_capture(path)
            self.reads = 0
            self.released = False
            captures.append(self)

        def isOpened(self):
            return self.capture.isOpened()

        def read(self):
            self.reads += 1
            return self.capture.read()

        def set(self, *args):
            pytest.fail("source frames must be read sequentially without seeking")

        def release(self):
            self.released = True
            self.capture.release()

    monkeypatch.setattr(cv2, "VideoCapture", SequentialCapture)
    result = gates.render_fidelity(root, scene.id, clip, sample_every=5, max_frames=12)
    assert result["render_l1"] == pytest.approx(1.0)
    assert result["frames"] == expected_frames
    assert len(captures) == 1 and captures[0].reads == expected_reads and captures[0].released


@pytest.mark.parametrize("kwargs", [{"sample_every": 0}, {"sample_every": -1}, {"max_frames": 0}])
def test_render_fidelity_rejects_empty_sampling_ranges(tmp_path, kwargs):
    with pytest.raises(ValueError, match="positive"):
        gates.render_fidelity(tmp_path / "missing", "s1", tmp_path / "missing.mp4", **kwargs)


def test_render_fidelity_rejects_unreadable_clip_before_rendering(tmp_path):
    root, scene, _ = _project(tmp_path)
    with pytest.raises(FileNotFoundError):
        gates.render_fidelity(root, scene.id, tmp_path / "missing.mp4")
