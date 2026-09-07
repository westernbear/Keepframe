import secrets

COOKIE = "keepframe_admin"

_SEED_USERS = {
    "mina@keepframe.app": "dev-admin",
}


class MemoryAuth:
    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}

    def login(self, email: str, password: str) -> str | None:
        if _SEED_USERS.get(email) != password:
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
