import hashlib, json, os
from pathlib import Path
import numpy as np, pytest
from keepframe.analyze.pipeline import AnalyzeOptions, analyze
from keepframe.analyze.video import render_scene_video
from keepframe.ir.schema import Background, Gradient, GradientStop
from keepframe.ir.synth import make_synthetic_scene

GOLDEN = Path(__file__).parent / "golden" / "analysis_fingerprint.json"
TIMING = {"seconds", "elapsed", "timings"}


def _strip(o):
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in TIMING}
    return [_strip(v) for v in o] if isinstance(o, list) else o


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _file_sha(p: Path) -> str:
    if p.suffix == ".json":
        return _sha(json.dumps(_strip(json.loads(p.read_text())), sort_keys=True).encode())
    if p.suffix == ".npz":   # zip members carry write times; hash the arrays
        with np.load(p) as z:
            return _sha(b"".join(f"{k}{z[k].dtype}{z[k].shape}".encode() + z[k].tobytes() for k in sorted(z.files)))
    return _sha(p.read_bytes())


def fingerprint(root: Path) -> dict[str, str]:
    """scene.json, report.json, assets, analysis snapshots and json/npy stage caches of every scene."""
    return {p.relative_to(root).as_posix(): _file_sha(p) for p in sorted((root / "scenes").rglob("*"))
            if p.is_file() and p.suffix != ".pkl"}


def _clips(tmp: Path) -> list[tuple[Path, list[dict] | None]]:
    plain = make_synthetic_scene(tmp / "plain", seed=7, frames=40, with_text=False, overlap=True)
    graded = make_synthetic_scene(tmp / "graded", seed=8, frames=40, with_text=False, overlap=True)
    graded.background = Background(kind="gradient", gradient=Gradient(angle=90, stops=[
        GradientStop(offset=0, color="#1a2a3a"), GradientStop(offset=1, color="#c8b090")]))   # plate path
    return [(render_scene_video(plain, tmp / "plain", tmp / "plain.mp4"),
             [{"id": "s1", "frames": [0, 19]}, {"id": "s2", "frames": [20, 39]}]),
            (render_scene_video(graded, tmp / "graded", tmp / "graded.mp4"), None)]


def test_analysis_output_unchanged_by_memory_work(tmp_path):
    clips = _clips(tmp_path)
    got = {"inputs": {v.name: _sha(v.read_bytes()) for v, _ in clips}, "projects": {}}
    for video, scenes in clips:
        root = tmp_path / f"{video.stem}-proj"
        analyze(video, 0, 39, root, AnalyzeOptions(ocr=False, refine=False), scenes=scenes)
        got["projects"][video.stem] = fingerprint(root)
    if os.environ.get("KEEPFRAME_WRITE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    want = json.loads(GOLDEN.read_text())
    if got["inputs"] != want["inputs"]:
        pytest.skip("synthetic clips encode differently here (ffmpeg build); golden not comparable")
    assert got["projects"] == want["projects"]


def test_read_frames_peak_is_one_stack(tmp_path):
    import tracemalloc
    from keepframe.analyze.video import read_frames
    scene = make_synthetic_scene(tmp_path / "s", seed=3, frames=60, with_text=False)
    video = render_scene_video(scene, tmp_path / "s", tmp_path / "s.mp4")
    tracemalloc.start()
    try:
        frames, _ = read_frames(video, 0, 59)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert frames.shape == (60, 360, 640, 3)
    assert peak <= 1.3 * frames.nbytes


def test_stage_log_has_rss(tmp_path, caplog):
    import logging, re
    from keepframe.analyze.pipeline import rerun
    from keepframe.progress import STAGES
    scene = make_synthetic_scene(tmp_path / "s", seed=5, frames=12, with_text=False)
    video = render_scene_video(scene, tmp_path / "s", tmp_path / "s.mp4")
    line = re.compile(r"stage (\w+) done \d+\.\ds rss \d+ MB peak \d+ MB$")
    caplog.set_level(logging.INFO, logger="keepframe.analyze")
    analyze(video, 0, 11, tmp_path / "proj", AnalyzeOptions(ocr=False, refine=False))
    done = [m.group(1) for r in caplog.records if (m := line.match(r.getMessage()))]
    assert done == list(STAGES)
    caplog.clear()
    rerun(tmp_path / "proj", "s1", "regions", note="rerun")
    assert [m.group(1) for r in caplog.records if (m := line.match(r.getMessage()))] == list(STAGES)
