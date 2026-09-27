from __future__ import annotations

import json
import math
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from .coordinator import AECoordinator, CoordinatorConflict


_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PLAN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,255}$")
_MAX_PAIR_BODY = 64 * 1024
_UPLOAD_READ_TIMEOUT = 60.0
def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return bool(getattr(stat, "st_file_attributes", 0) & 0x0400)

class _ReadableStream(Protocol):
    def read(self, size: int | None = -1, /) -> bytes: ...



class _LimitedReader:
    """Expose only one HTTP request body to the coordinator's streaming writer."""

    def __init__(self, stream: _ReadableStream, length: int) -> None:
        self.stream = stream
        self.remaining = length

    def read(self, size: int | None = -1, /) -> bytes:
        if self.remaining <= 0:
            return b""
        if size is None or size < 0 or size > self.remaining:
            size = self.remaining
        data = self.stream.read(size)
        if not data:
            return b""
        self.remaining -= len(data)
        return data


def _context(workspace: Path, query: dict[str, list[str]]) -> tuple[AECoordinator, str]:
    project = query.get("project", [None])[0]
    plan = query.get("plan", [None])[0]
    device = query.get("device", [None])[0]
    if not isinstance(project, str) or not _PROJECT_RE.fullmatch(project):
        raise CoordinatorConflict("project is required")
    if not isinstance(plan, str) or not _PLAN_RE.fullmatch(plan):
        raise CoordinatorConflict("plan is required")
    if not isinstance(device, str) or not _COMMAND_RE.fullmatch(device):
        raise CoordinatorConflict("device is required")
    root = Path(workspace) / project
    try:
        workspace_real = Path(workspace).resolve(strict=True)
        root_real = root.resolve(strict=True)
    except OSError as exc:
        raise CoordinatorConflict("project was not found") from exc
    if _is_reparse(root) or not root_real.is_dir() or root_real.parent != workspace_real:
        raise CoordinatorConflict("project was not found")
    return AECoordinator.cached(root_real, plan), device


def make_relay_server(
    workspace: Path,
    port: int = 0,
    host: str = "127.0.0.1",
) -> ThreadingHTTPServer:
    """Build the connector-only listener.

    This handler intentionally has no project listing, UI, static, edit, approval,
    or admin routes. Deployment/device authentication is layered on by Task 4.
    """

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    class RelayHandler(BaseHTTPRequestHandler):
        server_version = "Keepframe-AE-Relay/1"

        def log_message(self, format: str, *args: Any) -> None:
            # Keep credentials and request bodies out of relay logs.
            return

        def _send(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.send_header("x-content-type-options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: Any) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        def _body(self, limit: int = _MAX_PAIR_BODY) -> bytes | None:
            raw_length = self.headers.get("content-length")
            try:
                length = int(raw_length or "0")
            except ValueError:
                self._json(400, {"error": "content length is invalid"})
                return None
            if length < 0 or length > limit:
                self._json(413, {"error": "request body is too large"})
                return None
            return self.rfile.read(length)

        def _query(self):
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            for name, header in (
                ("project", "X-Keepframe-Project"),
                ("plan", "X-Keepframe-Plan"),
                ("device", "X-Keepframe-Device"),
            ):
                if not query.get(name) and self.headers.get(header):
                    query[name] = [self.headers[header]]
            return query

        def _error(self, exc: Exception) -> None:
            if isinstance(exc, CoordinatorConflict):
                self._json(409, {"error": str(exc)})
            else:
                self._json(400, {"error": str(exc)})

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in {"/health", "/healthz", "/api/health"}:
                return self._json(200, {"ok": True, "relay": "after_effects"})
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == "/next":
                try:
                    wait_raw = query.get("wait", ["0"])[0]
                    wait = float(wait_raw)
                    if not math.isfinite(wait) or wait < 0 or wait > 25:
                        raise CoordinatorConflict("wait must be between 0 and 25 seconds")
                    coordinator, device = _context(workspace, query)
                    deadline = time.monotonic() + wait
                    command = None
                    while True:
                        command = coordinator.next_command(device)
                        if command is not None or time.monotonic() >= deadline:
                            break
                        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                    if command is None:
                        return self._send(204, b"")
                    return self._json(200, {"command": command.model_dump(mode="json")})
                except Exception as exc:  # noqa: BLE001 - relay returns a safe protocol error
                    return self._error(exc)
            if parsed.path.startswith("/assets/") and parsed.path.count("/") == 2:
                asset_id = parsed.path.rsplit("/", 1)[1]
                if not _COMMAND_RE.fullmatch(asset_id):
                    return self._json(400, {"error": "asset id is invalid"})
                try:
                    coordinator, _device = _context(workspace, query)
                    asset, stream = coordinator.open_asset(asset_id)
                    try:
                        self.send_response(200)
                        self.send_header("content-type", asset.media_kind)
                        self.send_header("content-length", str(asset.length))
                        self.send_header("x-content-type-options", "nosniff")
                        self.end_headers()
                        while chunk := stream.read(1024 * 1024):
                            self.wfile.write(chunk)
                    finally:
                        stream.close()
                    return
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as exc:  # noqa: BLE001
                    return self._error(exc)
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/results/") and parsed.path.count("/") == 2:
                command_id = parsed.path.rsplit("/", 1)[1]
                if not _COMMAND_RE.fullmatch(command_id):
                    return self._json(400, {"error": "command id is invalid"})
                body = self._body()
                if body is None:
                    return
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise CoordinatorConflict("result must be an object")
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    coordinator, device = _context(workspace, query)
                    sequence = payload.pop("sequence", None)
                    if not isinstance(sequence, int) or isinstance(sequence, bool):
                        raise CoordinatorConflict("sequence is required")
                    result = payload.pop("result", payload)
                    if not isinstance(result, dict):
                        raise CoordinatorConflict("result must be an object")
                    accepted = coordinator.accept_result(device, command_id, sequence=sequence, result=result)
                    return self._json(200, {"result": accepted.model_dump(mode="json")})
                except Exception as exc:  # noqa: BLE001
                    return self._error(exc)
            return self._json(404, {"error": "not found"})

        def do_PUT(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/artifacts/") and parsed.path.count("/") == 2:
                reservation_id = parsed.path.rsplit("/", 1)[1]
                if not _COMMAND_RE.fullmatch(reservation_id):
                    return self._json(400, {"error": "artifact id is invalid"})
                raw_length = self.headers.get("content-length")
                try:
                    content_length = int(raw_length or "-1")
                except ValueError:
                    content_length = -1
                if content_length < 0:
                    return self._json(411, {"error": "content length is required"})
                query = parse_qs(parsed.query, keep_blank_values=True)
                try:
                    self.connection.settimeout(_UPLOAD_READ_TIMEOUT)
                    coordinator, _device = _context(workspace, query)
                    artifact = coordinator.publish_artifact(
                        reservation_id,
                        _LimitedReader(self.rfile, content_length),
                        content_length=content_length,
                    )
                    return self._json(201, {"artifact": artifact.model_dump(mode="json")})
                except Exception as exc:  # noqa: BLE001
                    return self._error(exc)
            return self._json(404, {"error": "not found"})

        def do_DELETE(self):
            return self._json(404, {"error": "not found"})

        def do_PATCH(self):
            return self._json(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), RelayHandler)


__all__ = ["make_relay_server"]
