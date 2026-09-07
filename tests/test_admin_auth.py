# tests/test_admin_auth.py
from refstudio.admin.auth import COOKIE, MemoryAuth, cookie_header

def test_login_and_cookie_name():
    auth = MemoryAuth()
    assert auth.login("mina@keepframe.app", "wrong") is None
    sid = auth.login("mina@keepframe.app", "dev-admin")
    assert sid and auth.get(sid) == "mina@keepframe.app"
    h = cookie_header(sid)
    assert h.startswith(f"{COOKIE}=")
    assert "Path=/admin" in h
    assert COOKIE == "keepframe_admin"
    auth.logout(sid)
    assert auth.get(sid) is None
