import pytest
from pathlib import Path

pytest.importorskip("playwright")

@pytest.mark.browser
def test_library_and_review_render(tmp_path):
    from playwright.sync_api import sync_playwright
    from keepframe.ir.synth import make_synthetic_scene
    from keepframe.ir.store import init_project
    from keepframe.web.server import make_server
    import json, threading

    root = tmp_path / "p1"
    scene = make_synthetic_scene(tmp_path / "gold", seed=3, with_text=False, frames=12)
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene)
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "Smoke", "status": "review"}))
    srv = make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with sync_playwright() as p:
            page = p.chromium.launch().new_page(viewport={"width": 1280, "height": 800})
            page.goto(base + "/")
            assert page.locator(".land-hero").count() == 1
            page.goto(base + "/library")
            assert page.locator("body").count() == 1
            page.goto(base + "/review?project=p1&scene=" + scene.id)
            page.wait_for_selector("#orig, [data-pane=orig]")
    finally:
        srv.shutdown()
