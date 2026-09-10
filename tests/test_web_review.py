import json
from pathlib import Path
import numpy as np
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import init_project, scene_dir
from tests.test_web_server import start, get


def test_state_from_synthetic(tmp_path):
    root = tmp_path / "ws" / "p1"
    scene = make_synthetic_scene(root / "gold", seed=11, with_text=False)
    init_project(
        root,
        {
            "file": "ref.mp4",
            "fps": scene.fps,
            "size": list(scene.size),
            "mode": "range",
            "range": [0, scene.frames - 1],
        },
        scene,
    )
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "t", "status": "review"}))
    (scene_dir(root, scene.id) / "report.json").write_text(
        json.dumps({"reconstruction": {"per_frame_l1": [0.02, 0.05, 0.2, 0.01] * 50}})
    )
    srv = start(tmp_path / "ws")
    import urllib.request

    url = f"http://127.0.0.1:{srv.server_address[1]}/api/state?project=p1&scene={scene.id}"
    with urllib.request.urlopen(url) as r:
        body = json.loads(r.read())
    srv.shutdown()
    assert body["scene"]["id"] == scene.id
    assert body["version"]["id"] == "v1"
    assert "tracks" not in body["scene"]["elements"][0]
    rec = body["report"]["reconstruction"]
    assert "per_frame_l1" not in rec
    assert len(rec["l1_bins"]) <= 200 and len(rec["l1_bins"]) == len(rec["l1_hot"])
    assert rec["l1_max"] == 0.2 and rec["l1_peaks"] and max(rec["l1_peaks"]) < 200
    assert set(body["project"].keys()) == {"versions"}
    assert body["project"]["versions"][0]["id"] == "v1"
    assert body["project"]["versions"][0]["scene_file"].startswith(f"scenes/{scene.id}/")


def test_review_page_has_progress_bar():
    html = (Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "review.html").read_text(encoding="utf-8")
    assert 'id="review-progress"' in html
    assert 'role="progressbar"' in html
    assert "setProgress" in html


def test_review_page_refreshes_in_place():
    html = (Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "review.html").read_text(encoding="utf-8")
    assert "refreshState" in html
    assert "location.href = `/review" not in html


def test_review_orig_frame_is_jpeg_preview(tmp_path):
    root = tmp_path / "ws" / "p1"
    scene = make_synthetic_scene(root / "gold", seed=11, with_text=False)
    init_project(
        root,
        {
            "file": "ref.mp4",
            "fps": scene.fps,
            "size": list(scene.size),
            "mode": "range",
            "range": [0, scene.frames - 1],
        },
        scene,
    )
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "t", "status": "review"}))
    stages = scene_dir(root, scene.id) / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    np.save(stages / "frames.npy", np.zeros((scene.frames, 36, 64, 3), np.uint8))
    srv = start(tmp_path / "ws")
    try:
        code, ctype, body = get(srv, f"/frame/orig/0?project=p1&scene={scene.id}")
    finally:
        srv.shutdown()
    assert code == 200
    assert ctype == "image/jpeg"
    assert body[:2] == b"\xff\xd8"


def test_review_frame_has_cache_control(tmp_path):
    import urllib.request

    root = tmp_path / "ws" / "p1"
    scene = make_synthetic_scene(root / "gold", seed=11, with_text=False)
    init_project(
        root,
        {
            "file": "ref.mp4",
            "fps": scene.fps,
            "size": list(scene.size),
            "mode": "range",
            "range": [0, scene.frames - 1],
        },
        scene,
    )
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "t", "status": "review"}))
    stages = scene_dir(root, scene.id) / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    np.save(stages / "frames.npy", np.zeros((scene.frames, 36, 64, 3), np.uint8))
    srv = start(tmp_path / "ws")
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/frame/orig/0?project=p1&scene={scene.id}"
        with urllib.request.urlopen(url) as r:
            cache = r.headers.get("cache-control")
    finally:
        srv.shutdown()
    assert cache and "max-age" in cache
