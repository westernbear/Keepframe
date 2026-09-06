from __future__ import annotations

import json
import re
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from refstudio.admin.auth import COOKIE, cookie_header
from refstudio.admin.policy import ASSET_GEN_CAP, RETRY_CAP, policy_note

if TYPE_CHECKING:
    from refstudio.admin.auth import MemoryAuth
    from refstudio.admin.memory import MemoryAdmin

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"

ADMIN_PAGES = {
    "/admin": "admin-login.html",
    "/admin/": "admin-login.html",
    "/admin/login": "admin-login.html",
    "/admin/tenants": "admin-tenants.html",
    "/admin/queue": "admin-queue.html",
    "/admin/quarantine": "admin-quarantine.html",
    "/admin/audit": "admin-audit.html",
}

_LOGIN_PATHS = {"/admin/api/login"}
_PUBLIC_HTML = {"/admin", "/admin/", "/admin/login"}


def _session_id(handler: BaseHTTPRequestHandler) -> str | None:
    raw = handler.headers.get("Cookie", "")
    prefix = f"{COOKIE}="
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix) :]
    return None


def _quarantine_item(item) -> dict:
    return {
        "id": item.id,
        "filename": item.filename,
        "tenant_id": item.tenant_id,
        "rejected_at": item.rejected_at,
        "reason": item.reason,
    }


class AdminRoutes:
    def __init__(self, admin_svc: MemoryAdmin, admin_auth: MemoryAuth) -> None:
        self.admin_svc = admin_svc
        self.admin_auth = admin_auth

    def _require(self, handler: BaseHTTPRequestHandler) -> str | None:
        sid = _session_id(handler)
        if not sid:
            handler._json(401, {"error": "unauthorized"})
            return None
        email = self.admin_auth.get(sid)
        if not email:
            handler._json(401, {"error": "unauthorized"})
            return None
        return email

    def handle_get(self, handler: BaseHTTPRequestHandler, u) -> bool:
        path = u.path

        if path in ADMIN_PAGES:
            p = STATIC / ADMIN_PAGES[path]
            if not p.is_file():
                handler._json(404, {"error": f"{ADMIN_PAGES[path]} missing"})
                return True
            if path not in _PUBLIC_HTML and self._require(handler) is None:
                return True
            handler._send(200, p.read_bytes(), "text/html; charset=utf-8")
            return True

        if path == "/admin/api/policy":
            if self._require(handler) is None:
                return True
            handler._json(
                200,
                {"retry_cap": RETRY_CAP, "asset_gen_cap": ASSET_GEN_CAP, "note": policy_note()},
            )
            return True

        if path == "/admin/api/tenants":
            if self._require(handler) is None:
                return True
            tenants = [asdict(t) for t in self.admin_svc.list_tenants()]
            handler._json(200, {"tenants": tenants})
            return True

        m = re.fullmatch(r"/admin/api/tenants/([^/]+)", path)
        if m:
            if self._require(handler) is None:
                return True
            tid = m.group(1)
            try:
                tenant = self.admin_svc.get_tenant(tid)
            except KeyError:
                handler._json(404, {"error": "not found"})
                return True
            members = [asdict(m) for m in self.admin_svc.list_members(tid)]
            handler._json(200, {"tenant": asdict(tenant), "members": members})
            return True

        if path == "/admin/api/jobs":
            if self._require(handler) is None:
                return True
            handler._json(200, {"jobs": self.admin_svc.list_jobs()})
            return True

        if path == "/admin/api/quarantine":
            if self._require(handler) is None:
                return True
            items = [_quarantine_item(i) for i in self.admin_svc.list_quarantine()]
            handler._json(200, {"items": items})
            return True

        if path == "/admin/api/audit":
            if self._require(handler) is None:
                return True
            events = [asdict(e) for e in self.admin_svc.list_audit()]
            handler._json(200, {"events": events})
            return True

        if path == "/admin/api/audit.csv":
            if self._require(handler) is None:
                return True
            body = self.admin_svc.audit_csv().encode("utf-8")
            handler._send(200, body, "text/csv; charset=utf-8")
            return True

        if re.fullmatch(r"/admin/api/quarantine/[^/]+/video", path):
            if self._require(handler) is None:
                return True
            handler._json(404, {"error": "not found"})
            return True

        if path.startswith("/admin"):
            handler._json(404, {"error": "not found"})
            return True

        return False

    def handle_post(self, handler: BaseHTTPRequestHandler, u) -> bool:
        path = u.path

        if path == "/admin/api/login":
            try:
                data = json.loads(handler._read_body().decode("utf-8") or "{}")
            except json.JSONDecodeError:
                handler._json(400, {"error": "bad json"})
                return True
            sid = self.admin_auth.login(data.get("email", ""), data.get("password", ""))
            if not sid:
                handler._json(401, {"error": "invalid credentials"})
                return True
            handler.send_response(200)
            handler.send_header("content-type", "application/json; charset=utf-8")
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            handler.send_header("content-length", str(len(body)))
            handler.send_header("Set-Cookie", cookie_header(sid))
            handler.end_headers()
            handler.wfile.write(body)
            return True

        if path == "/admin/api/logout":
            sid = _session_id(handler)
            if sid:
                self.admin_auth.logout(sid)
            handler._json(200, {"ok": True})
            return True

        if path.startswith("/admin"):
            handler._json(404, {"error": "not found"})
            return True

        return False

    def handle_delete(self, handler: BaseHTTPRequestHandler, u) -> bool:
        if u.path == "/admin/api/audit":
            if self._require(handler) is None:
                return True
            handler._json(404, {"error": "not found"})
            return True
        if u.path.startswith("/admin"):
            handler._json(404, {"error": "not found"})
            return True
        return False
