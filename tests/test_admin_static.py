from pathlib import Path

import re
import pytest

STATIC = Path("keepframe/web/static")
FORBIDDEN = ("cdn.tailwindcss.com", "fonts.googleapis.com", "lh3.googleusercontent.com", "material-symbols")
PAGES = ("admin-login.html", "admin-tenants.html", "admin-queue.html", "admin-quarantine.html", "admin-audit.html", "admin-llm.html")


def static_src(*names):
    return "\n".join((STATIC / name).read_text(encoding="utf-8") for name in names)


def test_admin_pages_offline():
    for name in PAGES:
        text = (STATIC / name).read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{name} {bad}"
        assert "/static/css/app.css" in text



def test_admin_pages_keep_local_assets_and_navigation_contracts():
    for name in PAGES:
        text = (STATIC / name).read_text(encoding="utf-8")
        for asset in re.findall(r'<(?:script|img|link)\b[^>]*(?:src|href)="([^\"]+)"', text, flags=re.I):
            assert asset.startswith("/static/"), (name, asset)
            assert (STATIC / asset.removeprefix("/static/").split("?", 1)[0]).is_file(), (name, asset)
        assert not re.search(r'(?:src|href)="https?://', text, flags=re.I), name

    for name in ("admin-queue.html", "admin-quarantine.html", "admin-audit.html", "admin-llm.html"):
        text = (STATIC / name).read_text(encoding="utf-8")
        for path in ("/admin/tenants", "/admin/queue", "/admin/quarantine", "/admin/audit", "/admin/llm"):
            assert f'href="{path}"' in text, (name, path)


def test_admin_pages_keep_operation_policy_visible():
    for name in ("admin-tenants.html", "admin-queue.html", "admin-quarantine.html", "admin-audit.html", "admin-llm.html"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert 'data-lang-toggle' in text, name
        assert "scene editor" not in text.lower(), name
    policy = static_src("admin-tenants.html", "admin-quarantine.html")
    assert "admin.policy" in policy and "admin.queue.retries" in static_src("admin-queue.html")
    assert "재시도 상한 4회" in static_src("js/i18n.js") and "에셋 생성 2회" in static_src("js/i18n.js")
    assert "admin.audit.appendOnly" in static_src("admin-audit.html")
    assert "<video" not in static_src("admin-quarantine.html").lower()


@pytest.mark.browser
def test_admin_navigation_is_reachable_and_dense_data_scrolls_on_mobile(tmp_path):
    pytest.importorskip("playwright")
    from keepframe.admin.auth import MemoryAuth
    from keepframe.admin.memory import MemoryAdmin
    from keepframe.web.server import make_server
    from playwright.sync_api import sync_playwright
    import threading

    auth = MemoryAuth({"operator@example.test": "safe-test-password"})
    session_id = auth.login("operator@example.test", "safe-test-password")
    server = make_server(tmp_path, port=0, admin=True, admin_svc=MemoryAdmin(), admin_auth=auth)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"], chromium_sandbox=False)
            page = browser.new_page(viewport={"width": 375, "height": 812})
            page.context.add_cookies([{"name": "keepframe_admin", "value": session_id, "url": base}])
            page.goto(base + "/admin")
            assert page.get_by_label("이메일").is_visible()
            assert page.get_by_label("비밀번호").is_visible()
            assert page.locator("#email").evaluate("el => el.getBoundingClientRect().height >= 44")
            assert page.locator("#password").evaluate("el => el.getBoundingClientRect().height >= 44")
            for route in ("/admin/tenants", "/admin/queue", "/admin/quarantine", "/admin/audit", "/admin/llm"):
                page.goto(base + route)
                page.wait_for_load_state("networkidle")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), route
                for label in ("테넌트", "작업 큐", "검역", "감사 로그", "LLM"):
                    assert page.get_by_role("link", name=label).count() == 1, (route, label)
                assert page.locator("nav a").evaluate_all("links => links.every(el => { const r=el.getBoundingClientRect(); return r.width >= 44 && r.height >= 44 && r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight; })"), route
                assert page.locator("nav a").last.evaluate("el => { el.focus(); const style=getComputedStyle(el); return style.outlineStyle !== 'none' && style.outlineWidth !== '0px'; }"), route
            for route, table in (
                ("/admin/queue", ".data-table"),
                ("/admin/audit", ".audit-table"),
            ):
                page.goto(base + route)
                page.wait_for_load_state("networkidle")
                assert page.locator(table).evaluate("el => { const r=el.parentElement; return r.scrollWidth >= r.clientWidth && ['auto', 'scroll'].includes(getComputedStyle(r).overflowX); }"), route
            for route, selector in (("/admin/tenants", "#tenant-rows"), ("/admin/quarantine", ".quarantine-rows-scroll")):
                page.goto(base + route)
                page.wait_for_load_state("networkidle")
                assert page.locator(selector).is_visible(), route
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.browser
def test_admin_data_rows_support_keyboard_selection(tmp_path):
    pytest.importorskip("playwright")
    from keepframe.admin.auth import MemoryAuth
    from keepframe.admin.memory import MemoryAdmin
    from keepframe.web.server import make_server
    from playwright.sync_api import sync_playwright
    import threading

    auth = MemoryAuth({"operator@example.test": "safe-test-password"})
    session_id = auth.login("operator@example.test", "safe-test-password")
    server = make_server(tmp_path, port=0, admin=True, admin_svc=MemoryAdmin(), admin_auth=auth)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"], chromium_sandbox=False)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.context.add_cookies([{"name": "keepframe_admin", "value": session_id, "url": base}])
            for route, selector, selected_class in (
                ("/admin/tenants", ".admin-table-row", "admin-table-row--selected"),
                ("/admin/queue", "#job-rows tr", "row-selected"),
                ("/admin/quarantine", ".quarantine-row", "quarantine-row--selected"),
            ):
                page.goto(base + route)
                row = page.locator(selector).first
                assert row.evaluate("el => el.tabIndex >= 0"), route
                row.focus()
                row.press("Enter")
                page.wait_for_timeout(30)
                if selected_class not in (row.get_attribute("class") or ""):
                    assert page.locator(selector).first.get_attribute("class") and selected_class in page.locator(selector).first.get_attribute("class"), route
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


def test_no_maker_cta_or_fake_version():
    for name in PAGES:
        text = (STATIC / name).read_text(encoding="utf-8")
        assert "+ New Project" not in text
        assert "v 3.4.0" not in text and "V 0.4.0" not in text


def test_pages_call_admin_api():
    tenants = static_src("admin-tenants.html", "js/admin-tenants.js")
    queue = static_src("admin-queue.html", "js/admin-queue.js")
    quarantine = static_src("admin-quarantine.html", "js/admin-quarantine.js")
    audit = static_src("admin-audit.html", "js/admin-audit.js")
    assert "/admin/api/tenants" in tenants
    assert "/admin/api/jobs" in queue
    assert "/admin/api/quarantine" in quarantine
    assert "<video" not in quarantine.lower()
    assert "/admin/api/audit" in audit
    assert "delete" not in audit.lower()


MAKER_PAGES = (
    "landing.html",
    "library.html",
    "ingest.html",
    "analyze.html",
    "review.html",
    "agent.html",
)


def test_maker_pages_link_admin():
    for name in MAKER_PAGES:
        text = (STATIC / name).read_text(encoding="utf-8")
        assert 'href="/admin"' in text, name
        assert 'data-i18n="admin.manage"' in text, name


def test_login_page_returns_to_maker_without_seed_credential_disclosure():
    text = (STATIC / "admin-login.html").read_text(encoding="utf-8")
    assert 'href="/"' in text
    assert "admin.localSeed" not in text
    assert "mina@keepframe.app" not in text and "dev-admin" not in text


def test_llm_page_has_oauth_fields():
    text = static_src("admin-llm.html", "js/admin-llm.js")
    assert 'id="auth"' in text
    assert 'value="oauth"' in text
    assert 'id="clientId"' in text
    assert 'id="tokenUrl"' in text
    assert 'id="tenantId"' in text
    assert 'id="chatgptConnect"' in text
    assert "/admin/api/llm/oauth/start" in text
    assert 'id="chatgptOAuth"' in text
    assert "chatgptOAuth.hidden" in text
    assert "applyProviderDefaults" in text
    assert 'id="modelOptions"' in text
    assert "/admin/api/llm/models" in text
    assert "default_base_url" in text
