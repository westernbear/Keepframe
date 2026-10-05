import base64
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import numpy as np
import pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import current_scene, init_project, init_project_scenes, load_project, new_version, scene_dir
from keepframe.web.workspace import load_meta
from tests.test_web_server import start, get
from tests.test_corrections import ocr_shape_project

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
    req.add_header("Origin", f"http://127.0.0.1:{srv.server_address[1]}")
    try:
        with urlopen(req) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def test_review_scene_exposes_current_font_for_correction_preview(tmp_path):
    from keepframe.web.server import _slim_scene

    scene = make_synthetic_scene(tmp_path / "gold", seed=11, frames=4)
    text = next(el for el in scene.elements if el.kind == "text")
    slim = next(el for el in _slim_scene(scene)["elements"] if el["id"] == text.id)
    assert slim["canonical"]["font"] == text.canonical.font.model_dump(
        include={"family_guess", "weight", "size_px"},
    )


@pytest.mark.parametrize("family", ["x;color:red", "Noto_Sans.Regular", "x,y"])
def test_text_correction_api_rejects_invalid_family(tmp_path, monkeypatch, family):
    root = tmp_path / "ws" / "p1"
    scene = make_synthetic_scene(root / "gold", seed=11, frames=4)
    init_project(root, {"file": "ref.mp4"}, scene)
    (root / "meta.json").write_text(json.dumps({"id": "p1", "status": "review"}))
    calls = []
    monkeypatch.setattr("keepframe.web.server.corrections.edit_text", lambda *args, **kw: calls.append((args, kw)))
    srv = start(tmp_path / "ws")
    try:
        code, body = _post(srv, "/api/correct", {"project": "p1", "scene": scene.id, "op": "text",
                                              "args": {"element_id": scene.elements[0].id,
                                                       "font": {"family_guess": family}}})
    finally:
        srv.shutdown()
        srv.server_close()
    assert code == 400
    assert "font family" in body["error"]
    assert calls == []
    assert len(load_project(root).versions) == 1
    assert current_scene(root, scene.id)[0] == scene


@pytest.mark.parametrize("op,field", [
    ("text", "element_id"), ("mask", "object_id"), ("bbox", "object_id"),
    ("reassign", "from_id"), ("reassign", "to_id"),
])
def test_correction_api_checks_ids_in_requested_scene_version(tmp_path, monkeypatch, op, field):
    root = tmp_path / "ws" / "p1"
    scene = make_synthetic_scene(root / "gold", seed=11, frames=4)
    init_project(root, {"file": "ref.mp4"}, scene)
    old_id = scene.elements[0].id
    latest = scene.model_copy(deep=True)
    latest.elements[0].id = "latest-only"
    new_version(root, scene.id, latest, note="different elements")
    calls = []
    monkeypatch.setattr("keepframe.web.server.ReviewState.run_correction", lambda *args: calls.append(args))
    before = (root / "project.json").read_bytes()
    srv = start(tmp_path / "ws")
    try:
        for version, missing in [("v1", "latest-only"), ("v2", old_id)]:
            args = {"version": version, field: missing}
            code, body = _post(srv, "/api/correct", {
                "project": "p1", "scene": scene.id, "op": op, "args": args,
            })
            assert code == 400, body
            assert missing in body["error"] and "unknown" in body["error"]
        # A valid historical ID reaches the existing stale-version conflict, even
        # though that ID no longer exists in the latest scene.
        code, body = _post(srv, "/api/correct", {
            "project": "p1", "scene": scene.id, "op": op,
            "args": {"version": "v1", field: old_id},
        })
        assert code == 409, body
        assert calls == []
        assert json.loads(get(srv, f"/api/state?project=p1&scene={scene.id}")[2])["job"]["status"] == "idle"
    finally:
        srv.shutdown()
        srv.server_close()
    assert (root / "project.json").read_bytes() == before


@pytest.mark.parametrize("op", ["mask", "bbox", "reassign_from", "reassign_to"])
def test_shape_correction_api_returns_400_without_mutation(ocr_shape_project, op):
    root = ocr_shape_project
    sd = scene_dir(root, "s1")
    stages = sd / "stages"
    ids = json.loads((stages / "ids.json").read_text())
    args = {"object_id": ids["s2"], "frame": 2}
    if op == "mask":
        import cv2
        ok, png = cv2.imencode(".png", np.full((96, 144), 255, np.uint8))
        assert ok
        args["mask_png_base64"] = base64.b64encode(png).decode("ascii")
    elif op == "bbox":
        args["bbox"] = [98, 52, 126, 80]
    else:
        args = {"frames": [2, 3], "from_id": ids["s2"], "to_id": ids["o2"]}
        if op == "reassign_to":
            args["from_id"], args["to_id"] = args["to_id"], args["from_id"]
    before = {p.name: p.read_bytes() for p in stages.iterdir() if p.is_file()}
    project_before = (root / "project.json").read_bytes()
    scene, version = current_scene(root, "s1")
    srv = start(root.parent)
    try:
        code, body = _post(srv, "/api/correct", {"project": "p1", "scene": "s1",
                                              "op": "reassign" if op.startswith("reassign") else op,
                                              "args": args})
        state = json.loads(get(srv, "/api/state?project=p1&scene=s1")[2])
    finally:
        srv.shutdown()
        srv.server_close()
    assert code == 400 and "shape elements from OCR boxes" in body["error"]
    assert state["job"]["status"] == "idle"
    assert {p.name: p.read_bytes() for p in stages.iterdir() if p.is_file()} == before
    assert (root / "project.json").read_bytes() == project_before
    assert current_scene(root, "s1") == (scene, version)
    assert list(sd.glob("scene.v*.json")) == [sd / "scene.v1.json"]


@pytest.mark.parametrize("op", ["mask", "bbox", "reassign"])
def test_object_correction_api_still_accepts_and_reruns(ocr_shape_project, monkeypatch, op):
    import cv2
    import threading
    from keepframe.review import corrections

    root = ocr_shape_project
    stages = scene_dir(root, "s1") / "stages"
    ids = json.loads((stages / "ids.json").read_text())
    finished = threading.Event()
    real_rerun = corrections.rerun

    def observed_rerun(*args, **kwargs):
        result = real_rerun(*args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(corrections, "rerun", observed_rerun)
    args = {"object_id": ids["o2"], "frame": 2}
    if op == "mask":
        pixels = np.zeros((96, 144), np.uint8)
        pixels[52:80, 50:75] = 255
        ok, png = cv2.imencode(".png", pixels)
        assert ok
        args["mask_png_base64"] = base64.b64encode(png).decode("ascii")
    elif op == "bbox":
        args["bbox"] = [50, 52, 75, 80]
    else:
        args = {"frames": [2, 3], "from_id": ids["o1"], "to_id": ids["o2"]}
    srv = start(root.parent)
    try:
        code, _ = _post(srv, "/api/correct", {"project": "p1", "scene": "s1", "op": op, "args": args})
        assert code == 202 and finished.wait(5)
    finally:
        srv.shutdown()
        srv.server_close()
    assert current_scene(root, "s1")[1].id == "v2"
    overrides = json.loads((stages / "overrides.json").read_text())
    assert overrides["regions"] and all(r["label"] == 1002 for r in overrides["regions"])


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
    assert set(body["project"].keys()) == {"versions", "scenes", "links", "approved_scenes"}
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


def test_multiscene_project_is_approved_only_after_every_scene(tmp_path):
    root = tmp_path / "ws" / "p1"
    first = make_synthetic_scene(root / "scenes" / "s1", seed=21, with_text=False, frames=6).model_copy(update={"id": "s1"})
    second = make_synthetic_scene(root / "scenes" / "s2", seed=22, with_text=False, frames=6).model_copy(update={"id": "s2"})
    init_project_scenes(root, {"file": "ref.mp4", "fps": 30, "size": list(first.size)}, [(first, (0, 5), {"transition": "cut"}), (second, (6, 11), None)])
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "multi", "status": "review", "scene": "s1", "version": "v1"}))
    srv = start(tmp_path / "ws")
    try:
        first_code, first_body = _post(srv, "/api/approve", {"project": "p1", "scene": "s1", "v": "v1"})
        second_code, second_body = _post(srv, "/api/approve", {"project": "p1", "scene": "s2", "v": "v1"})
    finally:
        srv.shutdown()
    assert first_code == 200 and first_body["project"]["status"] == "review"
    assert second_code == 200 and second_body["project"]["status"] == "approved"
    assert second_body["project"]["approved_scenes"] == {"s1": "v1", "s2": "v1"}


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


def test_review_analysis_layer_controls():
    html = review_src()
    css = REVIEW_CSS.read_text(encoding="utf-8")
    assert "drawOverlays" in html
    assert "fetchAnalysisOverlay" in html
    assert "fetchBboxes" not in html
    assert 'id="recon"' not in html
    assert 'id="peak-back"' not in html
    assert 'id="overlay-opacity"' in html
    assert 'id="region-mode"' in html
    assert 'id="element-filter"' in html
    assert 'id="transport-keys"' in html
    assert "positionOverlay" in html
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
                  const box = document.querySelector('#orig-overlay path');
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
