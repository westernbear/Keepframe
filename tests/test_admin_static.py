from pathlib import Path

STATIC = Path("keepframe/web/static")
FORBIDDEN = ("cdn.tailwindcss.com", "fonts.googleapis.com", "lh3.googleusercontent.com", "material-symbols")
PAGES = ("admin-login.html", "admin-tenants.html", "admin-queue.html", "admin-quarantine.html", "admin-audit.html")


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
    assert "/admin/api/tenants" in (STATIC / "admin-tenants.html").read_text(encoding="utf-8")
    assert "/admin/api/jobs" in (STATIC / "admin-queue.html").read_text(encoding="utf-8")
    assert "/admin/api/quarantine" in (STATIC / "admin-quarantine.html").read_text(encoding="utf-8")
    q = (STATIC / "admin-quarantine.html").read_text(encoding="utf-8")
    assert "<video" not in q.lower()
    assert "/admin/api/audit" in (STATIC / "admin-audit.html").read_text(encoding="utf-8")
    assert "delete" not in (STATIC / "admin-audit.html").read_text(encoding="utf-8").lower()


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


def test_login_footer_is_seed_not_local_off():
    text = (STATIC / "admin-login.html").read_text(encoding="utf-8")
    assert "admin.localSeed" in text
    assert "로컬판에는 이 화면이 없습니다." not in text
    assert "admin.localOff" not in text


def test_llm_page_has_oauth_fields():
    text = (STATIC / "admin-llm.html").read_text(encoding="utf-8")
    assert 'id="auth"' in text
    assert 'value="oauth"' in text
    assert 'id="clientId"' in text
    assert 'id="tokenUrl"' in text
    assert 'id="tenantId"' in text
    assert 'id="chatgptConnect"' in text
    assert "/admin/api/llm/oauth/start" in text
    assert '["chatgpt", "ChatGPT"]' in text
