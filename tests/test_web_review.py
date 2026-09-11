import json
import re
from pathlib import Path
import numpy as np
import pytest
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
    assert "texture" in body["scene"]["elements"][0]["canonical"]


def test_review_bboxes_for_frame(tmp_path):
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
    srv = start(tmp_path / "ws")
    try:
        code, _, body = get(srv, f"/api/bboxes?project=p1&scene={scene.id}&frame=0")
    finally:
        srv.shutdown()
    assert code == 200
    data = json.loads(body)
    assert data["size"] == list(scene.size)
    visible = [el.id for el in scene.elements if el.visible[0] <= 0 <= el.visible[1]]
    assert visible and visible[0] in data["boxes"]
    box = data["boxes"][visible[0]]
    assert len(box) == 4 and box[2] > box[0] and box[3] > box[1]


def test_review_page_has_progress_bar():
    html = (Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "review.html").read_text(encoding="utf-8")
    assert 'id="review-progress"' in html
    assert 'role="progressbar"' in html
    assert "setProgress" in html


def test_review_page_refreshes_in_place():
    html = (Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "review.html").read_text(encoding="utf-8")
    assert "refreshState" in html
    assert "location.href = `/review" not in html


REVIEW_HTML = Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "review.html"
REVIEW_CSS = Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "css" / "app.css"


def test_review_play_button_toggles_pause_icon():
    html = REVIEW_HTML.read_text(encoding="utf-8")
    assert "function setPlaying" in html
    assert 'id="pause-icon"' in html
    assert 'id="play-icon"' in html
    assert "is-playing" in html
    css = REVIEW_CSS.read_text(encoding="utf-8")
    assert "#play-btn.is-playing #pause-icon" in css


def test_review_playing_does_not_reset_image_timer():
    html = REVIEW_HTML.read_text(encoding="utf-8")
    assert "if (playing && imgTimer)" in html


def test_review_timeline_playhead_spans_tracks_without_overflowing_ruler():
    html = REVIEW_HTML.read_text(encoding="utf-8")
    css = REVIEW_CSS.read_text(encoding="utf-8")
    assert 'id="track-playhead"' in html
    assert "timeline__body" in html
    assert ".timeline__ruler" in css
    assert re.search(r"\.timeline__ruler\s*\{[^}]*height:\s*72px", css, re.S)
    assert re.search(r"\.timeline__ruler\s*\{[^}]*overflow:\s*hidden", css, re.S)


def test_review_diff_microscope_controls():
    html = REVIEW_HTML.read_text(encoding="utf-8")
    css = REVIEW_CSS.read_text(encoding="utf-8")
    assert "drawOverlays" in html
    assert "fetchBboxes" in html
    assert 'id="element-filter"' in html
    assert 'id="transport-keys"' in html
    assert "mapBox" in html
    assert "#play-btn" in css
    assert re.search(r"#play-btn\s*\{[^}]*width:\s*44px", css, re.S)
    assert "keepSave.hidden" in html or "keep-save" in html and "hidden" in html


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


@pytest.mark.browser
def test_review_playback_updates_icon_frame_and_playhead(tmp_path):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    from keepframe.web.server import make_server
    import threading

    root = tmp_path / "p1"
    scene = make_synthetic_scene(tmp_path / "gold", seed=3, with_text=False, frames=24)
    init_project(
        root,
        {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 23]},
        scene,
    )
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "t", "status": "review"}))
    stages = scene_dir(root, scene.id) / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    frames = np.zeros((scene.frames, 36, 64, 3), np.uint8)
    for i in range(scene.frames):
        frames[i] = i
    np.save(stages / "frames.npy", frames)
    srv = make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"], chromium_sandbox=False)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"{base}/review?project=p1&scene={scene.id}")
            page.wait_for_function(
                "() => !document.getElementById('review-root').classList.contains('is-loading')",
                timeout=15000,
            )
            page.click("#play-btn")
            page.wait_for_function(
                """() => {
                  const src = document.getElementById('orig').src;
                  const frame = parseInt(document.getElementById('frame-num').textContent, 10);
                  const pause = document.getElementById('pause-icon');
                  const box = document.querySelector('#orig-overlay rect');
                  return frame > 2 && src.includes('/frame/orig/') && !src.includes('/frame/orig/0?') && pause && !pause.hidden && box;
                }""",
                timeout=8000,
            )
            metrics = page.evaluate(
                """() => {
                  const ruler = document.querySelector('.timeline__ruler').getBoundingClientRect();
                  const tracks = document.getElementById('timeline-tracks').getBoundingClientRect();
                  const head = document.getElementById('track-playhead').getBoundingClientRect();
                  return { overlap: ruler.bottom - tracks.top, headX: head.x, tracksX: tracks.x };
                }"""
            )
            browser.close()
    finally:
        srv.shutdown()
    assert metrics["overlap"] <= 1
    assert metrics["headX"] > metrics["tracksX"] + 270
