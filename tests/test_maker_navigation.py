import threading

import pytest
pytest.importorskip("playwright")


@pytest.mark.browser
def test_maker_pages_share_localized_primary_navigation(tmp_path):
    from playwright.sync_api import sync_playwright
    from keepframe.web.server import make_server

    pages = ("/", "/library", "/new", "/analyze", "/review", "/agent")
    srv = make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"], chromium_sandbox=False)
            page = browser.new_page(viewport={"width": 375, "height": 800})
            for path in pages:
                page.goto(base + path, wait_until="networkidle")
                nav = page.locator(".maker-nav")
                links = nav.locator("a")
                hrefs = [links.nth(i).get_attribute("href") for i in range(links.count())]
                assert hrefs[:4] == ["/", "/library", "/new", "/agent"]
                active = nav.locator('[aria-current="page"]')
                if path in ("/", "/library", "/new", "/agent"):
                    assert active.get_attribute("href") == path
                else:
                    assert active.count() == 0
                if path == "/new":
                    assert page.locator("[data-file-input]").count() == 1
                    assert page.locator("[data-start-btn]").count() == 1
                elif path == "/analyze":
                    assert page.locator("[data-stage]").count() == 1
                elif path == "/review":
                    assert page.locator("#version-select").count() == 1
                    assert page.locator("#review-approve").count() == 1
                assert hrefs.count("/admin") == 1
                if path == "/agent":
                    assert any(href.startswith("/review?") for href in hrefs)
                assert [links.nth(i).inner_text().strip() for i in range(4)] == ["처음", "라이브러리", "새 레퍼런스", "에이전트"]
                assert nav.locator('a[href="/admin"]').inner_text().strip() == "관리"
                assert page.locator('a[href="/admin"]:visible').count() == 1
                assert page.locator("[data-lang-toggle]:visible").count() == 1
                page.locator("[data-lang-toggle]").click()
                assert nav.get_attribute("aria-label") == "Primary navigation"
                assert [links.nth(i).inner_text().strip() for i in range(4)] == ["Home", "Library", "New reference", "Agent"]
                assert nav.locator('a[href="/admin"]').inner_text().strip() == "Admin"
                page.locator("[data-lang-toggle]").click()
                assert nav.get_attribute("aria-label") == "주요 탐색"
            browser.close()
    finally:
        srv.shutdown()
        srv.server_close()
