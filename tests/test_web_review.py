import json
import re
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import numpy as np
import pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import init_project, scene_dir
from keepframe.web.workspace import load_meta
from tests.test_web_server import start, get

REVIEW_HTML = Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "review.html"
REVIEW_JS = Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "js" / "review.js"
REVIEW_PLAYBACK = Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "js" / "playback.js"
REVIEW_CSS = Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "css" / "app.css"


REVIEW_DIR = Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "js" / "review"


def review_src():
    review_modules = sorted(REVIEW_DIR.glob("*.js"))
    parts = [REVIEW_HTML, REVIEW_JS, REVIEW_PLAYBACK, *review_modules]
    return "\n".join(p.read_text(encoding="utf-8") for p in parts)



def _post(srv, path, payload):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    req = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


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
    assert body["status"] == "review"
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


def test_review_page_has_edit_form():
    html = review_src()
    assert 'id="edit-prompt"' in html
    assert "postEdit" in html
    assert 'data-i18n="review.editRun"' in html


def test_review_page_has_approve_button():
    html = review_src()
    assert 'id="review-approve"' in html
    assert "postApprove" in html
    assert "paintApprove" in html
    assert "goAgent" in html
    assert "review.openAgent" in html
    css = REVIEW_CSS.read_text(encoding="utf-8")
    assert ".review-empty #review-approve" in css


def test_approve_marks_project(tmp_path):
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
        missing = _post(srv, "/api/approve", {"scene": scene.id})
        unknown = _post(srv, "/api/approve", {"project": "missing", "scene": scene.id})
        code, body = _post(srv, "/api/approve", {"project": "p1", "scene": scene.id})
        again, again_body = _post(srv, "/api/approve", {"project": "p1", "scene": scene.id})
        st_code, _, state = get(srv, f"/api/state?project=p1&scene={scene.id}")
    finally:
        srv.shutdown()
    assert missing == (400, {"error": "project required"})
    assert unknown[0] == 404
    assert code == 200
    assert body["project"]["status"] == "approved"
    assert body["version"] == "v1"
    assert again == 200
    assert again_body["project"]["status"] == "approved"
    assert st_code == 200
    assert json.loads(state)["status"] == "approved"
    assert load_meta(tmp_path / "ws", "p1")["status"] == "approved"
    assert load_meta(tmp_path / "ws", "p1")["scene"] == scene.id


def test_review_page_has_progress_bar():
    html = review_src()
    assert 'id="review-progress"' in html
    assert 'role="progressbar"' in html
    assert "setProgress" in html


def test_review_page_refreshes_in_place():
    html = review_src()
    assert "refreshState" in html
    assert "location.href = `/review" not in html


def test_review_play_button_toggles_pause_icon():
    html = review_src()
    assert "function setPlaying" in html
    assert 'id="pause-icon"' in html
    assert 'id="play-icon"' in html
    assert "is-playing" in html
    css = REVIEW_CSS.read_text(encoding="utf-8")
    assert "#play-btn.is-playing #pause-icon" in css


def test_review_playing_does_not_reset_image_timer():
    html = review_src()
    assert "isSeekQueuedWhilePlaying" in html
    assert "createPreviewCache" in html
    assert "previews.wait" in html
    assert "waiting" in html


def test_review_timeline_playhead_spans_tracks_without_overflowing_ruler():
    html = review_src()
    css = REVIEW_CSS.read_text(encoding="utf-8")
    assert 'id="track-playhead"' in html
    assert "timeline__body" in html
    assert ".timeline__ruler" in css
    assert re.search(r"\.timeline__ruler\s*\{[^}]*height:\s*72px", css, re.S)
    assert re.search(r"\.timeline__ruler\s*\{[^}]*overflow:\s*hidden", css, re.S)


def test_review_diff_microscope_controls():
    html = review_src()
    css = REVIEW_CSS.read_text(encoding="utf-8")
    assert "drawOverlays" in html
    assert "fetchBboxes" in html
    assert 'id="element-filter"' in html
    assert 'id="transport-keys"' in html
    assert "mapBox" in html
    assert "#play-btn" in css
    assert re.search(r"#play-btn\s*\{[^}]*width:\s*44px", css, re.S)
    assert "keepSave.hidden" in html or "keep-save" in html and "hidden" in html


def test_review_frames_are_png_previews(tmp_path):
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
    for asset in (root / "gold" / "assets").glob("*.png"):
        target = scene_dir(root, scene.id) / "assets" / asset.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(asset.read_bytes())
    stages = scene_dir(root, scene.id) / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    np.save(stages / "frames.npy", np.zeros((scene.frames, 36, 64, 3), np.uint8))
    srv = start(tmp_path / "ws")
    try:
        orig = get(srv, f"/frame/orig/0?project=p1&scene={scene.id}")
        recon = get(srv, f"/frame/recon/0?project=p1&scene={scene.id}&v=v1")
    finally:
        srv.shutdown()
    assert orig[0] == recon[0] == 200
    assert orig[1] == recon[1] == "image/png"
    assert orig[2].startswith(b"\x89PNG\r\n\x1a\n")
    assert recon[2].startswith(b"\x89PNG\r\n\x1a\n")


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
    from keepframe.web.demo import ensure_demo_project
    import threading

    row = ensure_demo_project(tmp_path)
    srv = make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"], chromium_sandbox=False)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"{base}/review?project={row['id']}&scene={row['scene']}")
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
        srv.server_close()
    assert metrics["overlap"] <= 1
    assert metrics["headX"] > metrics["tracksX"] + 270
