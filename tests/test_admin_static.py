from pathlib import Path

STATIC = Path("refstudio/web/static")
FORBIDDEN = ("cdn.tailwindcss.com", "fonts.googleapis.com", "lh3.googleusercontent.com", "material-symbols")
PAGES = ("admin-login.html", "admin-tenants.html", "admin-queue.html", "admin-quarantine.html", "admin-audit.html")


def test_admin_pages_offline():
    for name in PAGES:
        text = (STATIC / name).read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{name} {bad}"
        assert 'href="/static/css/app.css"' in text


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
