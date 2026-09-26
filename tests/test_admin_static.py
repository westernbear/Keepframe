from pathlib import Path

STATIC = Path("keepframe/web/static")
FORBIDDEN = ("cdn.tailwindcss.com", "fonts.googleapis.com", "lh3.googleusercontent.com", "material-symbols")
PAGES = ("admin-login.html", "admin-tenants.html", "admin-queue.html", "admin-quarantine.html", "admin-audit.html")


def static_src(*names):
    return "\n".join((STATIC / name).read_text(encoding="utf-8") for name in names)


def test_admin_pages_offline():
    for name in PAGES:
        text = (STATIC / name).read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{name} {bad}"
        assert "/static/css/app.css" in text


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
