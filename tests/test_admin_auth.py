# tests/test_admin_auth.py
from refstudio.admin.auth import COOKIE, MemoryAuth, cookie_header

def test_login_and_cookie_name():
    auth = MemoryAuth()
    assert auth.login("mina@ref.studio", "wrong") is None
    sid = auth.login("mina@ref.studio", "dev-admin")
    assert sid and auth.get(sid) == "mina@ref.studio"
    h = cookie_header(sid)
    assert h.startswith(f"{COOKIE}=")
    assert "Path=/admin" in h
    assert COOKIE == "refstudio_admin"
    auth.logout(sid)
    assert auth.get(sid) is None
