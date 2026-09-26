import json
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

@pytest.mark.browser
def test_library_routes_uploaded_and_analyzing_projects(tmp_path):
    import threading
    from keepframe.web.server import make_server
    from keepframe.web.workspace import create_project, write_meta
    from tests.test_web_ingest import _mp4
    from playwright.sync_api import sync_playwright

    video = tmp_path / "upload.mp4"
    _mp4(video)
    uploaded = create_project(tmp_path, "Uploaded", video, "range", (0, 9))
    with_job = create_project(tmp_path, "Analyzing job", video, "range", (0, 9))
    without_job = create_project(tmp_path, "Analyzing without job", video, "range", (0, 9))
    write_meta(tmp_path, with_job["id"], status="analyzing", job_id="j123")
    write_meta(tmp_path, without_job["id"], status="analyzing")
    srv = make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.route("**/api/analyze", lambda route: route.fulfill(status=202, content_type="application/json", body=json.dumps({"job": {"id": "j123", "status": "queued"}})))
            page.goto(base + "/library")
            page.get_by_text("Uploaded", exact=True).click()
            page.wait_for_load_state("domcontentloaded")
            assert page.url.endswith(f"/new?project={uploaded['id']}")
            page.wait_for_function("() => document.querySelector('[data-video-name]').textContent === 'Uploaded'")
            page.goto(base + "/library")
            page.get_by_text("Analyzing job", exact=True).click()
            page.wait_for_url(f"**/analyze?job={with_job['id']}")
            page.goto(base + "/library")
            page.get_by_text("Analyzing without job", exact=True).click()
            page.wait_for_url(f"**/review?project={without_job['id']}&scene=s1")
            browser.close()
    finally:
        srv.shutdown()
        srv.server_close()

@pytest.mark.browser
def test_library_routes_all_project_statuses(tmp_path):
    import threading
    from keepframe.web.server import make_server
    from keepframe.web.workspace import create_project, write_meta
    from tests.test_web_ingest import _mp4
    from playwright.sync_api import sync_playwright

    video = tmp_path / "upload.mp4"
    _mp4(video)
    uploaded = create_project(tmp_path, "Uploaded", video, "range", (0, 9))
    job = create_project(tmp_path, "Analyzing job", video, "range", (0, 9))
    no_job = create_project(tmp_path, "Analyzing without job", video, "range", (0, 9))
    review = create_project(tmp_path, "Review", video, "range", (0, 9))
    approved = create_project(tmp_path, "Approved", video, "range", (0, 9))
    rejected = create_project(tmp_path, "Rejected", video, "range", (0, 9))
    write_meta(tmp_path, job["id"], status="analyzing", job_id="j123")
    write_meta(tmp_path, no_job["id"], status="analyzing")
    write_meta(tmp_path, review["id"], status="review", scene="s1")
    write_meta(tmp_path, approved["id"], status="approved", scene="s1", version="v2")
    write_meta(tmp_path, rejected["id"], status="rejected", reason="Live-action")
    srv = make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            routes = [
                ("Uploaded", f"/new?project={uploaded['id']}"),
                ("Analyzing job", f"/analyze?job={job['id']}"),
                ("Analyzing without job", f"/review?project={no_job['id']}&scene=s1"),
                ("Review", f"/review?project={review['id']}&scene=s1"),
                ("Approved", f"/agent?project={approved['id']}&scene=s1&v=v2"),
            ]
            for title, path in routes:
                page.goto(base + "/library")
                page.get_by_text(title, exact=True).click()
                page.wait_for_load_state("domcontentloaded")
                assert page.url.endswith(path), (title, page.url)
            page.goto(base + "/library")
            assert page.get_by_text("Live-action", exact=True).count() == 1
            browser.close()
    finally:
        srv.shutdown()
        srv.server_close()

@pytest.mark.browser
def test_ingest_submits_the_latest_displayed_estimate_snapshot(tmp_path):
    import json
    import threading
    from keepframe.web.server import make_server
    from tests.test_web_ingest import _mp4
    from playwright.sync_api import sync_playwright

    video = tmp_path / "upload.mp4"
    _mp4(video)
    srv = make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with sync_playwright() as p:
            errors = []
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script("""
                const nativeFetch = window.fetch.bind(window);
                window.__estimateRequests = [];
                window.fetch = (input, init = {}) => {
                  const url = typeof input === 'string' ? input : input.url;
                  if (url.endsWith('/api/estimate')) {
                    const body = JSON.parse(init.body);
                    window.__estimateRequests.push(body);
                    const delay = body.mode === 'range' && body.start === 0 ? 700 : 20;
                    const result = {scene_count: 1, seconds: 180, seconds_per_scene: 180, note: 'estimate', confirm_token: `token-${body.mode}-${body.start}-${body.end}`};
                    return new Promise(resolve => setTimeout(() => resolve(new Response(JSON.stringify(result), {status: 200, headers: {'content-type': 'application/json'}})), delay));
                  }
                  return nativeFetch(input, init);
                };
            """)
            fake_job = {"id": "jfake", "project_id": "", "status": "queued", "stage": "frames", "eta_s": 180}
            page.route("**/api/analyze", lambda route: route.fulfill(status=202, content_type="application/json", body=json.dumps({"job": fake_job})))
            page.goto(base + "/new")
            page.locator("[data-file-input]").set_input_files(video)
            page.wait_for_function("() => document.querySelector('[data-video-name]').textContent === 'upload'")
            page.locator('[data-mode="range"]').click()
            page.wait_for_function("() => window.__estimateRequests.filter(x => x.mode === 'range').length === 1")
            page.locator("[data-start]").fill("2")
            page.locator("[data-start]").dispatch_event("change")
            page.wait_for_function("() => window.__estimateRequests.some(x => x.mode === 'range' && x.start === 2)")
            page.wait_for_timeout(800)
            assert "8프레임" in page.locator("[data-range-meta]").inner_text()
            assert not page.locator("[data-start-btn]").is_disabled()
            page.locator("[data-start-btn]").dispatch_event("click")
            page.wait_for_function("() => location.pathname === '/analyze'")
            params = page.evaluate("Object.fromEntries(new URLSearchParams(location.search))")
            assert params["mode"] == "range"
            assert params["start"] == "2" and params["end"] == "9"
            assert params["token"] == "token-range-2-9"
            browser.close()
    finally:
        srv.shutdown()
        srv.server_close()

@pytest.mark.browser
def test_full_video_confirmation_is_invalidated_by_mode_and_range_changes(tmp_path):
    import threading
    from keepframe.web.server import make_server
    from tests.test_web_ingest import _mp4
    from playwright.sync_api import sync_playwright

    video = tmp_path / "upload.mp4"
    _mp4(video)
    srv = make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.route("**/api/analyze", lambda route: route.fulfill(status=202, content_type="application/json", body='{"job":{"id":"jfake","status":"queued","stage":"frames","eta_s":180}}'))
            page.add_init_script("""
                const nativeFetch = window.fetch.bind(window);
                window.__estimates = JSON.parse(sessionStorage.getItem('estimate-log') || '[]');
                window.fetch = async (input, init = {}) => {
                  const url = typeof input === 'string' ? input : input.url;
                  if (url.endsWith('/api/estimate')) {
                    const body = JSON.parse(init.body);
                    const token = `token-${window.__estimates.length}-${body.mode}-${body.start}-${body.end}`;
                    window.__estimates.push({body, token});
                    sessionStorage.setItem('estimate-log', JSON.stringify(window.__estimates));
                    return new Response(JSON.stringify({scene_count: 2, seconds: 360, seconds_per_scene: 180, note: 'estimate', confirm_token: token}), {status: 200, headers: {'content-type': 'application/json'}});
                  }
                  return nativeFetch(input, init);
                };
            """)
            page.goto(f"http://127.0.0.1:{srv.server_address[1]}/new")
            page.locator("[data-file-input]").set_input_files(video)
            page.wait_for_function("() => window.__estimates.length === 1 && document.querySelector('[data-confirm-btn]').offsetParent !== null")
            page.locator("[data-confirm-btn]").click()
            assert not page.locator("[data-start-btn]").is_disabled()
            page.locator('[data-mode="range"]').click()
            page.wait_for_function("() => window.__estimates.length === 2")
            page.locator("[data-start]").fill("2")
            page.locator("[data-start]").dispatch_event("change")
            page.wait_for_function("() => window.__estimates.length === 3")
            page.locator('[data-mode="full"]').click()
            assert page.locator("[data-start-btn]").is_disabled()
            page.wait_for_function("() => window.__estimates.length >= 4 && document.querySelector('[data-confirm-btn]').offsetParent !== null", timeout=5000)
            page.locator("[data-start-btn]").dispatch_event("click")
            assert page.url.endswith("/new")
            page.locator("[data-confirm-btn]").click()
            assert not page.locator("[data-start-btn]").is_disabled()
            page.locator("[data-start-btn]").dispatch_event("click")
            page.wait_for_function("() => location.pathname === '/analyze'")
            params = page.evaluate("Object.fromEntries(new URLSearchParams(location.search))")
            latest = page.evaluate("JSON.parse(sessionStorage.getItem('estimate-log')).at(-1)")
            assert params["mode"] == "full" and params["token"] == latest["token"]
            assert int(params["start"]) == latest["body"]["start"] and int(params["end"]) == latest["body"]["end"]
            browser.close()
    finally:
        srv.shutdown()
        srv.server_close()
