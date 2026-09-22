import pytest

pytest.importorskip("playwright")

@pytest.mark.browser
def test_library_and_review_render(tmp_path):
    from playwright.sync_api import sync_playwright
    from keepframe.web.demo import ensure_demo_project
    from keepframe.web.server import make_server
    from tests.test_web_ingest import _mp4
    import threading

    row = ensure_demo_project(tmp_path)
    video = tmp_path / "upload.mp4"
    _mp4(video)
    srv = make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(base + "/")
            assert page.locator(".land-hero").count() == 1
            page.goto(base + "/library")
            page.wait_for_selector("#project-list .row")
            page.goto(base + "/new")
            page.locator("[data-file-input]").set_input_files(video)
            page.wait_for_selector('[data-mode="range"]', state="visible")
            page.goto(f"{base}/review?project={row['id']}&scene={row['scene']}")
            page.wait_for_selector("#orig", state="visible")
            page.wait_for_selector("#recon", state="visible")
            page.wait_for_function("() => ['orig', 'recon'].every(id => document.getElementById(id).naturalWidth > 0)")
            browser.close()
    finally:
        srv.shutdown()
        srv.server_close()
