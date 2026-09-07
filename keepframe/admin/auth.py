from __future__ import annotations

import os
import secrets

COOKIE = "keepframe_admin"

_DEFAULT_USERS = {
    "mina@keepframe.app": "dev-admin",
}


def load_admin_users(env: dict[str, str] | None = None) -> dict[str, str]:
    """Users from KEEPFRAME_ADMIN_EMAIL/PASSWORD and KEEPFRAME_ADMIN_USERS.

    KEEPFRAME_ADMIN_USERS is comma-separated email:password pairs.
    If none are set, the local seed account is used.
    """
    src = env if env is not None else os.environ
    users: dict[str, str] = {}
    raw = (src.get("KEEPFRAME_ADMIN_USERS") or "").strip()
    if raw:
        for part in raw.split(","):
            email, sep, password = part.strip().partition(":")
            if sep and email and password:
                users[email.strip()] = password
    email = (src.get("KEEPFRAME_ADMIN_EMAIL") or "").strip()
    password = src.get("KEEPFRAME_ADMIN_PASSWORD") or ""
    if email and password:
        users[email] = password
    return users or dict(_DEFAULT_USERS)


class MemoryAuth:
    def __init__(self, users: dict[str, str] | None = None) -> None:
        self._users = dict(users) if users is not None else load_admin_users()
        self._sessions: dict[str, str] = {}

    def login(self, email: str, password: str) -> str | None:
        expected = self._users.get(email)
        if expected is None or not secrets.compare_digest(expected, password):
            return None
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = email
        return session_id

    def get(self, session_id: str) -> str | None:
        return self._sessions.get(session_id)

    def logout(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


def cookie_header(session_id: str) -> str:
    return f"{COOKIE}={session_id}; HttpOnly; Path=/admin; SameSite=Lax"
