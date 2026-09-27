from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from .auth import AEAuthError, AEDeviceIdentity, AEProjectAuth
from .bridge import MAX_BRIDGE_JSON_BYTES
from .coordinator import AECoordinator, CoordinatorConflict
from .models import AECapabilities, canonical_json

_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PLAN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,255}$")
_MAX_PAIR_BODY = 64 * 1024
# /results carries one bridge-sized object plus a fixed sequence/result wrapper.
_MAX_RESULT_ENVELOPE_BYTES = len(b'{"sequence":1000000000,"result":}')
_MAX_RESULT_BODY = MAX_BRIDGE_JSON_BYTES + _MAX_RESULT_ENVELOPE_BYTES
_MAX_CAPABILITY_BODY = MAX_BRIDGE_JSON_BYTES
_UPLOAD_READ_TIMEOUT = 60.0
_REQUEST_TIMEOUT = 15.0
_MAX_HANDLERS = 64


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


def _project_root(workspace: Path, project: str) -> Path:
    if not _PROJECT_RE.fullmatch(project):
        raise CoordinatorConflict("relay authorization failed")
    root = Path(workspace) / project
    try:
        workspace_real = Path(workspace).resolve(strict=True)
        root_real = root.resolve(strict=True)
    except OSError as exc:
        raise CoordinatorConflict("relay authorization failed") from exc
    if _is_reparse(root) or not root_real.is_dir() or root_real.parent != workspace_real:
        raise CoordinatorConflict("relay authorization failed")
    return root_real


def _context(
    workspace: Path,
    query: dict[str, list[str]],
    identity: AEDeviceIdentity,
    *,
    bind: bool,
    allow_unbound: bool = False,
) -> AECoordinator:
    requested_project = query.get("project", [identity.project_id])[0]
    plan = query.get("plan", [None])[0]
    if requested_project != identity.project_id:
        raise CoordinatorConflict("relay authorization failed")
    if not isinstance(plan, str) or not _PLAN_RE.fullmatch(plan):
        raise CoordinatorConflict("relay request is invalid")
    coordinator = AECoordinator.cached(_project_root(workspace, identity.project_id), plan)
    session = coordinator.state()
    if (
        bind
        and identity.status == "active"
        and session.status == "waiting_for_connector"
        and session.device_id in {None, identity.device_id}
    ):
        session = coordinator.transition(
            "device_ready",
            revision=session.revision,
            device_id=identity.device_id,
        )
    if session.device_id != identity.device_id and not (
        allow_unbound and session.device_id is None
    ):
        raise CoordinatorConflict("relay authorization failed")
    return coordinator



def _capabilities_match(
    coordinator: AECoordinator,
    snapshot: AECapabilities | None,
) -> bool:
    plan = coordinator.plan
    if snapshot is None or plan.capability_hash is None or plan.capability_manifest is None:
        return False
    if snapshot.capability_hash != plan.capability_hash:
        return False
    try:
        manifest = snapshot.model_dump(
            mode="json",
            exclude={"capability_hash", "project_open", "timestamp"},
        )
        return canonical_json(manifest) == canonical_json(plan.capability_manifest)
    except (TypeError, ValueError, OverflowError):
        return False


def _pause_for_capabilities(coordinator: AECoordinator, session: Any) -> None:
    if session.status in {"paused:capabilities_changed", "done", "failed"}:
        return
    try:
        coordinator.transition(
            "capabilities_changed",
            revision=session.revision,
        )
    except CoordinatorConflict:
        # A concurrent state transition is not a relay protocol error.  The
        # capability check still prevents this request from binding or leasing.
        return


def _project_plan_ids(workspace: Path, identity: AEDeviceIdentity) -> tuple[str, ...]:
    project_root = _project_root(workspace, identity.project_id)
    renders = project_root / "renders"
    if not renders.exists():
        return ()
    try:
        renders_real = renders.resolve(strict=True)
    except OSError as exc:
        raise CoordinatorConflict("relay authorization failed") from exc
    if (
        _is_reparse(renders)
        or not renders_real.is_dir()
        or renders_real.parent != project_root
    ):
        raise CoordinatorConflict("relay authorization failed")
    plans = []
    for candidate in renders_real.iterdir():
        if (
            not _PLAN_RE.fullmatch(candidate.name)
            or _is_reparse(candidate)
            or not candidate.is_dir()
        ):
            continue
        state_path = candidate / "ae" / "session.json"
        if state_path.is_file() and not _is_reparse(state_path):
            plans.append(candidate.name)
    return tuple(sorted(plans))


def _next_command(
    workspace: Path,
    query: dict[str, list[str]],
    identity: AEDeviceIdentity,
    snapshot: AECapabilities | None,
):
    requested_plan = "plan" in query
    plan_ids = (
        (query["plan"][0],)
        if requested_plan
        else _project_plan_ids(workspace, identity)
    )
    for plan_id in plan_ids:
        target_query = {**query, "plan": [plan_id]}
        try:
            coordinator = _context(
                workspace,
                target_query,
                identity,
                bind=False,
                allow_unbound=True,
            )
            session = coordinator.state()
        except CoordinatorConflict:
            if requested_plan:
                raise
            continue
        if session.status in {"paused:capabilities_changed", "done", "failed"}:
            continue
        if not _capabilities_match(coordinator, snapshot):
            _pause_for_capabilities(coordinator, session)
            continue
        try:
            coordinator = _context(
                workspace,
                target_query,
                identity,
                bind=True,
            )
        except CoordinatorConflict:
            # State races are intentionally invisible to the connector.  A
            # later poll rechecks capabilities and the current session state.
            continue
        command = coordinator.next_command(identity.device_id)
        if command is not None:
            return command
    return None




def make_relay_server(
    workspace: Path,
    *,
    deployment_token: str,
    port: int = 0,
    host: str = "127.0.0.1",
) -> ThreadingHTTPServer:
    """Build the authenticated connector-only listener."""

    if not isinstance(deployment_token, str) or not deployment_token:
        raise ValueError("AE relay deployment token is required")
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    class RelayServer(ThreadingHTTPServer):
        request_queue_size = _MAX_HANDLERS

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._handler_slots = threading.BoundedSemaphore(_MAX_HANDLERS)
            super().__init__(*args, **kwargs)

        def process_request(self, request: Any, client_address: Any) -> None:
            if not self._handler_slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                self._handler_slots.release()
                raise

        def process_request_thread(
            self,
            request: Any,
            client_address: Any,
        ) -> None:
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._handler_slots.release()

    class RelayHandler(BaseHTTPRequestHandler):
        server_version = "Keepframe-AE-Relay/1"
        sys_version = ""
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(_REQUEST_TIMEOUT)


        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-content-type-options", "nosniff")
            if code == 401:
                self.send_header("www-authenticate", "Bearer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: Any) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        def _unauthorized(self) -> None:
            self._json(401, {"error": "relay authorization failed"})

        def _body(self, limit: int = _MAX_PAIR_BODY) -> bytes | None:
            raw_length = self.headers.get("content-length")
            try:
                length = int(raw_length or "0")
            except ValueError:
                self._json(400, {"error": "request is invalid"})
                return None
            if length < 0 or length > limit:
                self._json(413, {"error": "request body is too large"})
                return None
            body = self.rfile.read(length)
            if len(body) != length:
                self._json(400, {"error": "request body is incomplete"})
                return None
            return body

        def _bearer(self) -> str | None:
            values = self.headers.get_all("Authorization") or []
            if len(values) != 1:
                return None
            scheme, separator, token = values[0].partition(" ")
            if scheme != "Bearer" or separator != " " or not token or len(token) > 512:
                return None
            return token

        def _deployment_authorized(self) -> bool:
            return hmac.compare_digest(self._bearer() or "", deployment_token)

        def _query(self, parsed=None) -> dict[str, list[str]]:
            parsed = parsed or urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            for name, header in (
                ("project", "X-Keepframe-Project"),
                ("plan", "X-Keepframe-Plan"),
            ):
                if not query.get(name) and self.headers.get(header):
                    query[name] = [self.headers[header]]
            return query

        def _auth_target(
            self,
            query: dict[str, list[str]],
        ) -> tuple[AEProjectAuth, str | None] | None:
            token = self._bearer()
            project = query.get("project", [None])[0]
            if isinstance(project, str) and _PROJECT_RE.fullmatch(project):
                try:
                    return (
                        AEProjectAuth(_project_root(workspace, project), project),
                        token,
                    )
                except (AEAuthError, CoordinatorConflict):
                    pass
            candidate = token if isinstance(token, str) else ""
            _ = hmac.compare_digest(
                hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
                "0" * 64,
            )
            return None

        def _error(self, exc: Exception) -> None:
            if isinstance(exc, (AEAuthError, CoordinatorConflict)):
                self._json(409, {"error": "relay request conflicted"})
            else:
                self._json(400, {"error": "request is invalid"})

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in {"/health", "/healthz", "/api/health"}:
                return self._json(200, {"ok": True, "relay": "after_effects"})
            if parsed.path != "/next" and not (
                parsed.path.startswith("/assets/") and parsed.path.count("/") == 2
            ):
                return self._json(404, {"error": "not found"})
            query = self._query(parsed)
            target = self._auth_target(query)
            if target is None:
                return self._unauthorized()
            auth, token = target
            if parsed.path == "/next":
                try:
                    wait_raw = query.get("wait", ["0"])[0]
                    wait = float(wait_raw)
                    if not math.isfinite(wait) or wait < 0 or wait > 25:
                        raise CoordinatorConflict("relay request is invalid")
                    deadline = time.monotonic() + wait
                    command = None
                    while True:
                        with auth.authorize_device_with_capabilities(
                            token,
                            allow_draining=False,
                        ) as authorized:
                            identity, snapshot = authorized
                            if identity is None:
                                return self._unauthorized()
                            command = _next_command(
                                workspace,
                                query,
                                identity,
                                snapshot,
                            )
                        if command is not None or time.monotonic() >= deadline:
                            break
                        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                    if command is None:
                        return self._send(204, b"")
                    return self._json(200, {"command": command.model_dump(mode="json")})
                except Exception as exc:  # noqa: BLE001 - public protocol errors are generic
                    return self._error(exc)
            asset_id = parsed.path.rsplit("/", 1)[1]
            if not _COMMAND_RE.fullmatch(asset_id):
                return self._json(400, {"error": "request is invalid"})
            try:
                identity = auth.authenticate_device(
                    token,
                    allow_draining=True,
                )
                if identity is None:
                    return self._unauthorized()
                coordinator = _context(
                    workspace,
                    query,
                    identity,
                    bind=False,
                )
                asset, stream = coordinator.open_asset(identity.device_id, asset_id)
                try:
                    self.send_response(200)
                    self.send_header("content-type", asset.media_kind)
                    self.send_header("content-length", str(asset.length))
                    self.send_header("cache-control", "no-store")
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

        def do_POST(self):
            parsed = urlparse(self.path)
            if (
                parsed.path != "/pair"
                and parsed.path != "/capabilities"
                and not (
                    parsed.path.startswith("/results/") and parsed.path.count("/") == 2
                )
            ):
                return self._json(404, {"error": "not found"})
            if parsed.path == "/pair":
                if not self._deployment_authorized():
                    return self._unauthorized()
                body = self._body()
                if body is None:
                    return
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                    if not isinstance(payload, dict) or set(payload) != {"project", "code"}:
                        raise AEAuthError("pairing request is invalid")
                    project = payload.get("project")
                    code = payload.get("code")
                    if (
                        not isinstance(project, str)
                        or not _PROJECT_RE.fullmatch(project)
                        or not isinstance(code, str)
                    ):
                        raise AEAuthError("pairing request is invalid")
                    grant = AEProjectAuth(
                        _project_root(workspace, project),
                        project,
                    ).redeem_pairing(code)
                    return self._json(
                        200,
                        {
                            "project": grant.identity.project_id,
                            "device": grant.identity.device_id,
                            "token": grant.token,
                            "capability_request": grant.capability_request,
                        },
                    )
                except Exception:
                    return self._unauthorized()
            if parsed.path == "/capabilities":
                query = self._query(parsed)
                target = self._auth_target(query)
                if target is None:
                    return self._unauthorized()
                auth, token = target
                identity = auth.authenticate_device(token, allow_draining=False)
                if identity is None:
                    return self._unauthorized()
                body = self._body(_MAX_CAPABILITY_BODY)
                if body is None:
                    return
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                    snapshot = AECapabilities.model_validate(payload)
                    encoded = canonical_json(snapshot.model_dump(mode="json"))
                    if len(encoded) > _MAX_CAPABILITY_BODY:
                        raise ValueError("capability snapshot is too large")
                    stored = auth.publish_capabilities(identity.device_id, snapshot)
                    return self._json(
                        200,
                        {"capability_hash": stored.capability_hash},
                    )
                except Exception as exc:  # noqa: BLE001
                    return self._error(exc)
            query = self._query(parsed)
            target = self._auth_target(query)
            if target is None:
                return self._unauthorized()
            auth, token = target
            command_id = parsed.path.rsplit("/", 1)[1]
            if not _COMMAND_RE.fullmatch(command_id):
                return self._json(400, {"error": "request is invalid"})
            try:
                identity = auth.authenticate_device(
                    token,
                    allow_draining=True,
                )
                if identity is None:
                    return self._unauthorized()
                body = self._body(_MAX_RESULT_BODY)
                if body is None:
                    return
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise CoordinatorConflict("relay request is invalid")
                sequence = payload.pop("sequence", None)
                if not isinstance(sequence, int) or isinstance(sequence, bool):
                    raise CoordinatorConflict("relay request is invalid")
                result = payload.pop("result", payload)
                if not isinstance(result, dict):
                    raise CoordinatorConflict("relay request is invalid")
                with auth.authorize_device(
                    token,
                    allow_draining=True,
                ) as authorized:
                    if (
                        authorized is None
                        or authorized.device_id != identity.device_id
                    ):
                        return self._unauthorized()
                    coordinator = _context(
                        workspace,
                        query,
                        authorized,
                        bind=False,
                    )
                    accepted = coordinator.accept_result(
                        authorized.device_id,
                        command_id,
                        sequence=sequence,
                        result=result,
                    )
                return self._json(200, {"result": accepted.model_dump(mode="json")})
            except Exception as exc:  # noqa: BLE001
                return self._error(exc)

        def do_PUT(self):
            parsed = urlparse(self.path)
            if not (
                parsed.path.startswith("/artifacts/") and parsed.path.count("/") == 2
            ):
                return self._json(404, {"error": "not found"})
            query = self._query(parsed)
            target = self._auth_target(query)
            if target is None:
                return self._unauthorized()
            auth, token = target
            reservation_id = parsed.path.rsplit("/", 1)[1]
            if not _COMMAND_RE.fullmatch(reservation_id):
                return self._json(400, {"error": "request is invalid"})
            raw_length = self.headers.get("content-length")
            try:
                content_length = int(raw_length or "-1")
            except ValueError:
                content_length = -1
            if content_length < 0:
                return self._json(411, {"error": "content length is required"})
            try:
                self.connection.settimeout(_UPLOAD_READ_TIMEOUT)
                identity = auth.authenticate_device(
                    token,
                    allow_draining=True,
                )
                if identity is None:
                    return self._unauthorized()
                coordinator = _context(
                    workspace,
                    query,
                    identity,
                    bind=False,
                )
                artifact = coordinator.publish_artifact(
                    identity.device_id,
                    reservation_id,
                    _LimitedReader(self.rfile, content_length),
                    content_length=content_length,
                    require_live_command=True,
                )
                return self._json(201, {"artifact": artifact.model_dump(mode="json")})
            except Exception as exc:  # noqa: BLE001
                return self._error(exc)

        def do_DELETE(self):
            return self._json(404, {"error": "not found"})

        def do_PATCH(self):
            return self._json(404, {"error": "not found"})

    server = RelayServer((host, port), RelayHandler)
    server.daemon_threads = True
    return server


__all__ = ["make_relay_server"]
