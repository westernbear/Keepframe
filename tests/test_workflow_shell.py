import json
import threading
from pathlib import Path

import pytest



ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"


def test_workflow_stepper_is_mounted_on_each_workflow_page():
    # step state and placement are checked by the browser tests below and frontend/src/lib/workflow.test.ts
    for page in ("ingest", "analyze", "review"):
        tsx = (SRC / "pages" / page / "index.tsx").read_text(encoding="utf-8")
        assert "import { WorkflowStepper } from '@/components/app/workflow-stepper'" in tsx
        assert tsx.count("<WorkflowStepper ") == 1
    korean, english = (json.loads((SRC / "i18n" / f"{lang}.json").read_text(encoding="utf-8")) for lang in ("ko", "en"))
    for key in ("workflow.ingest", "workflow.analyze", "workflow.review", "workflow.status.ingest", "workflow.status.analyze", "workflow.status.review", "workflow.title"):
        assert korean[key]
        assert english[key]


@pytest.mark.browser
def test_analyze_reload_does_not_post_again_after_explicit_ingest_start(tmp_path):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from keepframe.web.server import make_server
    from tests.test_web_ingest import _mp4

    video = tmp_path / "upload.mp4"
    _mp4(video)
    server = make_server(tmp_path, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"], chromium_sandbox=False)
            page = browser.new_page(viewport={"width": 1024, "height": 768})
            analyze_posts = []
            page.add_init_script("""
                const nativeFetch = window.fetch.bind(window);
                window.fetch = (input, init = {}) => {
                  const url = typeof input === 'string' ? input : input.url;
                  if (url.endsWith('/api/estimate')) {
                    return nativeFetch(input, init).then(async response => {
                      const body = await response.json();
                      body.seconds = 60;
                      body.scene_count = 1;
                      body.seconds_per_scene = 60;
                      body.confirm_token = 'range-confirmed';
                      return new Response(JSON.stringify(body), {status: response.status, headers: {'content-type': 'application/json'}});
                    });
                  }
                  return nativeFetch(input, init);
                };
            """)
            job = {"id": "jui", "project_id": "", "status": "queued", "stage": "frames", "eta_s": 60}
            project = {"id": "", "title": "upload", "status": "analyzing", "job_id": "jui"}

            def start_job(route):
                analyze_posts.append(route.request.url)
                project["id"] = route.request.post_data_json["project_id"]
                job["project_id"] = project["id"]
                route.fulfill(status=202, content_type="application/json", body=json.dumps({"job": job}))

            def project_detail(route):
                path = route.request.url.split("?", 1)[0].removeprefix(base)
                if route.request.method == "GET" and path.startswith("/api/projects/") and path.count("/") == 3:
                    project["id"] = path.removeprefix("/api/projects/")
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"project": project}))
                else:
                    route.continue_()


            page.route("**/api/analyze", start_job)
            page.route("**/api/projects/*", project_detail)
            page.route("**/api/jobs/jui", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(job)))
            page.goto(f"{base}/new")
            page.locator("[data-file-input]").set_input_files(video)
            page.wait_for_function("() => document.querySelector('[data-video-name]').textContent === 'upload'")
            page.locator('[data-mode="range"]').click()
            page.wait_for_function("() => !document.querySelector('[data-start-btn]').disabled")
            page.locator("[data-start-btn]").click()
            page.wait_for_url("**/analyze?*")
            page.wait_for_function("() => document.querySelector('[data-stage]').textContent !== '—'")
            assert len(analyze_posts) == 1
            page.wait_for_function("() => document.querySelector('[data-workflow-project]').textContent !== ''")
            assert page.locator("[data-workflow-project]").inner_text() == "upload"
            assert len(analyze_posts) == 1
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.browser
def test_workflow_stepper_tracks_page_and_review_failure_state(tmp_path):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from keepframe.web.demo import ensure_demo_project
    from keepframe.web.server import make_server

    demo = ensure_demo_project(tmp_path)
    server = make_server(tmp_path, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"], chromium_sandbox=False)
            page = browser.new_page(viewport={"width": 375, "height": 800})
            page.add_init_script("localStorage.removeItem('keepframe.lang')")
            analyze_posts = []
            page.route("**/api/analyze", lambda route: (analyze_posts.append(route.request.url), route.fulfill(status=404, content_type="application/json", body=json.dumps({"error": "not found"}))))
            page.route("**/api/state**", lambda route: route.continue_() if "project=missing-project" not in route.request.url else route.fulfill(status=404, content_type="application/json", body=json.dumps({"error": "not found"})))

            page.goto(f"{base}/new")
            steps = page.locator("[data-workflow-step]")
            assert steps.count() == 3
            assert [steps.nth(i).get_attribute("data-workflow-step") for i in range(3)] == ["ingest", "analyze", "review"]
            assert page.locator('[data-workflow-step="ingest"][aria-current="step"]').count() == 1
            assert page.locator("[data-workflow-step][aria-current=step]").count() == 1
            assert page.locator('[data-workflow-step][data-complete="true"]').count() == 0
            assert page.locator("[data-workflow-project]").inner_text() == "프로젝트 없음"
            page.locator("[data-lang-toggle]").click()
            assert page.locator("[data-workflow-project]").inner_text() == "No project selected"
            page.locator("[data-lang-toggle]").click()

            page.goto(f"{base}/analyze?job=missing-project&token=already-confirmed")
            page.wait_for_function("() => document.querySelector('[data-status]').hidden === false")
            assert page.locator('[data-workflow-step="analyze"][aria-current="step"]').count() == 1
            assert page.locator('[data-workflow-step][data-complete="true"]').count() == 0
            assert analyze_posts == []

            page.goto(f"{base}/review?project={demo['id']}&scene={demo['scene']}")
            page.wait_for_function("() => !document.getElementById('review-root').classList.contains('is-loading')")
            steps = page.locator("[data-workflow-step]")
            assert steps.count() == 3
            assert page.locator('[data-workflow-step="review"][aria-current="step"]').count() == 1
            assert page.locator('[data-workflow-step="ingest"][data-complete="true"]').count() == 1
            assert page.locator('[data-workflow-step="analyze"][data-complete="true"]').count() == 1
            page.locator("[data-lang-toggle]").click()
            assert [steps.nth(i).locator("[data-i18n]").inner_text().strip() for i in range(3)] == ["Upload", "Analyze", "Review"]

            page.goto(f"{base}/review?project=missing-project&scene=s1")
            page.wait_for_function("() => !document.getElementById('review-root').classList.contains('is-loading')")
            assert page.locator("#job-banner").is_visible()
            assert page.locator('[data-workflow-step][aria-current="step"]').count() == 0
            assert page.locator('[data-workflow-step][data-complete="true"]').count() == 0
            assert page.locator('#review-missing a[href="/library"]').count() == 1

            page.goto(f"{base}/review")
            assert page.locator("#review-root.is-empty").count() == 1
            assert page.locator('#review-missing a[href="/library"]').is_visible()
            assert page.locator('[data-workflow-step][aria-current="step"]').count() == 0
            assert page.locator('[data-workflow-step][data-complete="true"]').count() == 0
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
