from keepframe.admin.auth import COOKIE, MemoryAuth, cookie_header, load_admin_users

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


def test_load_admin_users_from_email_and_list():
    users = load_admin_users({
        "KEEPFRAME_ADMIN_EMAIL": "ops@keepframe.app",
        "KEEPFRAME_ADMIN_PASSWORD": "s3cret",
        "KEEPFRAME_ADMIN_USERS": "a@x.com:one,b@x.com:two",
    })
    assert users["ops@keepframe.app"] == "s3cret"
    assert users["a@x.com"] == "one"
    assert users["b@x.com"] == "two"
    auth = MemoryAuth(users)
    assert auth.login("ops@keepframe.app", "s3cret")
    assert auth.login("a@x.com", "one")
    assert auth.login("mina@keepframe.app", "dev-admin") is None


def test_load_admin_users_falls_back_to_seed():
    assert load_admin_users({}) == {"mina@keepframe.app": "dev-admin"}
