from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from refstudio.web.workspace import list_projects

STATIC = Path(__file__).parent / "static"
PAGES = {
    "/": "library.html",
    "/new": "ingest.html",
    "/analyze": "analyze.html",
    "/review": "review.html",
}
ADMIN_OFF = {"error": "로컬판에는 이 화면이 없습니다."}


def make_server(workspace: Path, port: int = 8765, host: str = "127.0.0.1", admin: bool = False) -> ThreadingHTTPServer:
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/admin", "/admin/") and not admin:
                return self._json(404, ADMIN_OFF)
            if u.path in PAGES:
                p = STATIC / PAGES[u.path]
                if not p.exists():
                    return self._json(404, {"error": f"{PAGES[u.path]} missing"})
                return self._send(200, p.read_bytes(), "text/html; charset=utf-8")
            if u.path.startswith("/static/"):
                rel = u.path[len("/static/"):]
                if ".." in rel:
                    return self._json(400, {"error": "bad path"})
                p = STATIC / rel
                if not p.is_file():
                    return self._json(404, {"error": "not found"})
                ctype = "text/css" if p.suffix == ".css" else "application/javascript" if p.suffix == ".js" else "application/octet-stream"
                return self._send(200, p.read_bytes(), ctype)
            if u.path == "/api/projects":
                return self._json(200, {"projects": list_projects(workspace)})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/projects":
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length) if length else b""
                try:
                    json.loads(body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "bad json"})
                return self._json(400, {"error": "video required"})
            return self._json(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), H)
