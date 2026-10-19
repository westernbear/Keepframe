from pathlib import Path

import json
import re
import pytest

STATIC = Path("keepframe/web/static")
SRC = Path("frontend/src")
ADMIN = SRC / "pages/admin"
FORBIDDEN = ("cdn.tailwindcss.com", "fonts.googleapis.com", "lh3.googleusercontent.com", "material-symbols")
SHELL_PAGES = ("tenants", "queue", "quarantine", "audit", "llm")   # login.tsx is the only admin page without the shell


def src(*names):
    return "\n".join((SRC / name).read_text(encoding="utf-8") for name in names)


def admin_src(*pages):
    return src(*(f"pages/admin/{page}.tsx" for page in pages))


def test_admin_pages_offline():
    """The built SPA (which serves the admin pages) and its sources load no CDN fonts, Tailwind or icon hosts."""
    app = STATIC / "app"
    files = [app / "index.html", *app.glob("assets/*.js"), *app.glob("assets/*.css"),
             *SRC.rglob("*.ts"), *SRC.rglob("*.tsx"), *SRC.rglob("*.css")]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{path} {bad}"


def test_admin_pages_keep_local_assets_and_navigation_contracts():
    text = (STATIC / "app/index.html").read_text(encoding="utf-8")
    assets = re.findall(r'<(?:script|img|link)\b[^>]*(?:src|href)="([^\"]+)"', text, flags=re.I)
    assert any(a.startswith("/static/app/assets/") for a in assets)
    for asset in assets:
        assert asset.startswith("/static/"), asset
        assert (STATIC / asset.removeprefix("/static/").split("?", 1)[0]).is_file(), asset
    assert not re.search(r'(?:src|href)="https?://', text, flags=re.I)

    shell = src("components/app/admin-shell.tsx")
    assert "href={`/admin/${p}`}" in shell and "<LangToggle />" in shell
    for page in SHELL_PAGES:
        assert f"page: '{page}'" in shell, page                        # every page links every other one
        assert f'<AdminShell page="{page}"' in admin_src(page), page


def test_admin_pages_keep_operation_policy_visible():
    for page in SHELL_PAGES:
        assert "scene editor" not in admin_src(page).lower(), page
    assert "T('admin.policy')" in admin_src("tenants") and "T('admin.policy')" in admin_src("quarantine")
    assert "'admin.queue.retries'" in admin_src("queue")
    policy = json.loads(src("i18n/ko.json"))["admin.policy"]
    assert "재시도 상한 4회" in policy and "에셋 생성 2회" in policy
    assert "T('admin.audit.appendOnly')" in admin_src("audit")
    assert "<video" not in admin_src("quarantine").lower()


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
            assert page.locator('a[href="/"]').first.is_visible()
            assert "dev-admin" not in page.content() and "mina@keepframe.app" not in page.content()
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
    for path in [*ADMIN.glob("*.tsx"), SRC / "components/app/admin-shell.tsx"]:
        text = path.read_text(encoding="utf-8")
        assert "+ New Project" not in text, path
        assert "v 3.4.0" not in text and "V 0.4.0" not in text, path


def test_pages_call_admin_api():
    assert "/admin/api/tenants" in admin_src("tenants")
    assert "/admin/api/jobs" in admin_src("queue")
    quarantine = admin_src("quarantine")
    assert "/admin/api/quarantine" in quarantine
    assert "<video" not in quarantine.lower()
    audit = admin_src("audit")
    assert "/admin/api/audit" in audit
    assert "delete" not in audit.lower()


MAKER_PAGES = ("landing", "library", "ingest", "analyze", "review", "agent")


def test_maker_pages_link_admin():
    nav = src("components/app/maker-nav.tsx")
    assert 'href="/admin"' in nav and "T('admin.manage')" in nav
    assert "<MakerNav " in src("components/app/app-header.tsx")
    for name in MAKER_PAGES:
        assert "<AppHeader" in src(f"pages/{name}/index.tsx"), name


def test_login_page_returns_to_maker_without_seed_credential_disclosure():
    text = admin_src("login")
    assert 'href="/"' in text
    assert "admin.localSeed" not in text
    assert "mina@keepframe.app" not in text and "dev-admin" not in text


def test_llm_page_has_oauth_fields():
    text = admin_src("llm")
    assert 'id="auth"' in text
    assert 'value="oauth"' in text
    for field in ("clientId", "tokenUrl", "tenantId"):                 # rendered as id={id} from one list
        assert f"['{field}', 'admin.llm.{field}'" in text, field
    assert 'id="chatgptConnect"' in text
    assert "/admin/api/llm/oauth/start" in text
    assert 'id="chatgptOAuth" hidden={vis.hideChatgptOAuth}' in text
    assert "applyProviderDefaults" in text
    assert 'id="modelOptions"' in text
    assert "/admin/api/llm/models" in text
    assert "default_base_url" in src("lib/admin.ts")                  # behaviour: frontend/src/lib/admin.test.ts
