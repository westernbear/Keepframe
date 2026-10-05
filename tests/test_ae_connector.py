from __future__ import annotations

import asyncio
import io
import time
import errno
import struct
import subprocess
import zlib
import hashlib
import http.client
import json
import socket
import ssl
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

import keepframe.after_effects.connector as connector


def _png(width: int = 1280, height: int = 720) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I4s", len(ihdr), b"IHDR")
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
        + struct.pack(">I4s", 0, b"IEND")
        + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    )


def _scoped_renders(root: Path) -> Path:
    return root / "projects" / "p1" / "plans" / "plan-1" / "sessions" / "session-1" / "renders"


class _Response:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None):
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            body, self._body = self._body, b""
            return body
        body, self._body = self._body[:size], self._body[size:]
        return body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

def _panel_heartbeat(*, timestamp: float = 100.0) -> dict[str, object]:
    return {
        "version": "24.1.0",
        "major": 24,
        "host": "after-effects",
        "ready": True,
        "project_open": True,
        "timestamp": timestamp,
        "capabilities": {
            "font_names": ["Arial"],
            "fonts": [
                {
                    "match_name": "Arial",
                    "family": "Arial",
                    "style": "Regular",
                    "version": "1",
                    "version_or_hash": "1",
                }
            ],
            "effect_names": ["ADBE Fill"],
            "effects": [
                {
                    "match_name": "ADBE Fill",
                    "display_name": "Fill",
                    "version": "1",
                    "version_or_hash": "1",
                    "properties": {"ADBE Fill-0002": "color"},
                }
            ],
            "property_schemas": {"ADBE Opacity": "number"},
            "properties": {"ADBE Opacity": "number"},
            "plugin_versions": {"ADBE Fill": "1"},
        },
    }


def _preview_fixture(
    *,
    checkpoint: int = 2,
    frame_count: int = 4,
    fps: float = 24.0,
    representatives: list[int] | None = None,
) -> tuple[dict[str, object], dict[str, object], list[str]]:
    selected = representatives or [0, 2, 3]
    names = [f"checkpoint-{checkpoint}-{frame:06d}.png" for frame in range(frame_count)]
    artifacts: list[dict[str, object]] = [
        {
            "reservation_id": f"mp4-{checkpoint}",
            "kind": "mp4",
            "filename": f"checkpoint-{checkpoint}.mp4",
            "directory": "renders",
        }
    ]
    artifacts.extend(
        {
            "reservation_id": f"png-{checkpoint}-{frame}",
            "kind": "png",
            "filename": names[frame],
            "directory": "renders",
        }
        for frame in selected
    )
    payload: dict[str, object] = {
        "checkpoint_index": checkpoint,
        "frame_count": frame_count,
        "fps": fps,
        "representative_frames": selected,
        "artifacts": artifacts,
    }
    result: dict[str, object] = {
        "rendered": True,
        "kind": "preview",
        "checkpoint_index": checkpoint,
        "frame_count": frame_count,
        "fps": fps,
        "width": 1280,
        "height": 720,
        "representative_frames": selected,
        "sequence": {
            "directory": "renders",
            "pattern": "checkpoint-N-%06d.png",
            "frame_count": frame_count,
            "first_frame": names[0],
            "last_frame": names[-1],
        },
        "representative_files": [names[frame] for frame in selected],
    }
    return payload, result, names


def _command(
    kind: str,
    payload: dict[str, object],
    *,
    command_id: str,
    sequence: int = 1,
    expected_checkpoint: int | None = 0,
) -> dict[str, object]:
    return {
        "id": command_id,
        "nonce": f"nonce-{command_id}",
        "project_id": "p1",
        "device_id": "d1",
        "plan_id": "plan-1",
        "plan_digest": "a" * 64,
        "session_id": "session-1",
        "expected_state": "iterating",
        "expected_checkpoint": expected_checkpoint,
        "sequence": sequence,
        "kind": kind,
        "payload": payload,
        "payload_digest": connector._canonical_digest(payload),
    }


def test_capability_hash_ignores_transient_project_and_heartbeat_state():
    first = connector.AECapabilities.from_heartbeat(_panel_heartbeat(timestamp=100.0))
    heartbeat = _panel_heartbeat(timestamp=200.0)
    heartbeat["project_open"] = False
    second = connector.AECapabilities.from_heartbeat(heartbeat)

    assert first.capability_hash == second.capability_hash


def test_mcp_v2_error_is_not_treated_as_text_success():
    class FailedResult:
        is_error = True
        structured_content = None
        content = [{"text": "tool exception: failed"}]

    with pytest.raises(connector.MCPError):
        connector.MCPStdioClient._result(FailedResult())


def test_mcp_v2_structured_content_is_preferred_to_text():
    class Result:
        is_error = False
        structured_content = {"version": "24.1.0"}
        content = [{"text": "not the structured result"}]

    assert connector.MCPStdioClient._result(Result()) == {"version": "24.1.0"}


def test_pairing_code_and_pair_use_deployment_bearer_without_query_token(monkeypatch):
    captured: dict[str, object] = {}

    def opener(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data
        return _Response(
            json.dumps({"project": "p1", "device": "d1", "token": "device-secret"}).encode()
        )

    relay = connector.RelayClient(
        "https://relay.example/base",
        deployment_token="deployment-secret",
        opener=opener,
    )
    code = "p1.opaque-code-123456"
    project, suffix = connector.parse_pairing_code(code)
    assert (project, suffix) == ("p1", "opaque-code-123456")
    paired = relay.pair(code)
    assert paired.project == "p1" and paired.device_id == "d1"
    assert "deployment-secret" not in str(captured["url"])
    assert captured["headers"]["Authorization"] == "Bearer deployment-secret"
    assert json.loads(captured["body"]) == {"project": "p1", "code": code}

def test_pairing_code_rejects_unbound_or_malformed_project():
    for value in ("opaque", ".opaque", "p1.", "../opaque", "p1/opaque", "p1.other.more"):
        with pytest.raises(connector.ConnectorError):
            connector.parse_pairing_code(value)


def test_preflight_is_windows_only_and_validates_panel_ffmpeg_and_url(monkeypatch, tmp_path):
    monkeypatch.setattr(connector, "_is_windows", lambda: False)
    with pytest.raises(connector.PreflightError):
        connector.preflight(
            "https://relay.example",
            deployment_token="deployment-secret",
            private_root=tmp_path,
        )

    monkeypatch.setattr(connector, "_is_windows", lambda: True)
    calls: list[tuple[list[str], bool]] = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["shell"]))
        return object()

    result = connector.preflight(
        "http://localhost:8767",
        deployment_token="deployment-secret",
        private_root=tmp_path,
        ffmpeg="ffmpeg.exe",
        panel_heartbeat=_panel_heartbeat(),
        now=100.5,
        which=lambda name: name,
        run=run,
        sid_provider=lambda: "S-1-5-21-1234",
    )
    assert result.ae_version == "24.1.0"
    assert calls and calls[0][0][0].lower().endswith("icacls.exe") and calls[0][1] is False

    with pytest.raises(connector.PreflightError):
        connector.preflight(
            "http://public.example",
            deployment_token="deployment-secret",
            private_root=tmp_path / "other",
            ffmpeg="ffmpeg.exe",
            panel_heartbeat=_panel_heartbeat(timestamp=1.0),
            which=lambda name: name,
            run=run,
            sid_provider=lambda: "S-1-5-21-1234",
            now=2.0,
        )


def test_run_connector_redeems_explicit_code_over_existing_record(monkeypatch, tmp_path):
    snapshot = connector.AECapabilities.from_heartbeat(_panel_heartbeat())

    class Checked:
        relay_url = "https://relay.example"
        private_root = tmp_path
        capabilities = snapshot

    class Store:
        saved = []

        def __init__(self, root, **kwargs):
            self.path = Path(root) / kwargs["filename"]

        def save_record(self, record):
            self.saved.append(record)

        def load_record(self):
            raise AssertionError("explicit pairing must not load an old record")

    class Relay:
        instances = []

        def __init__(self, url, deployment_token):
            self.pairs = []
            self.published = []
            self.project_id = None
            self.device_id = None
            self.instances.append(self)

        def pair(self, code):
            self.pairs.append(code)
            return connector.Pairing("p1", "device-" + "d" * 16, "new-device-token", {})

        def publish_capabilities(self, value, *, project):
            self.published.append((value, project))
        def set_device_token(self, token):
            self.device_token = token

    class MCP:
        def __init__(self, **kwargs):
            pass

    class Runner:
        def __init__(self, *args, **kwargs):
            self.args = args

        def run_forever(self):
            return None

    monkeypatch.setenv("KEEPFRAME_AE_RELAY_TOKEN", "deployment-secret")
    monkeypatch.setattr(connector, "preflight", lambda *args, **kwargs: Checked())
    monkeypatch.setattr(connector, "DPAPITokenStore", Store)
    monkeypatch.setattr(connector, "RelayClient", Relay)
    monkeypatch.setattr(connector, "MCPStdioClient", MCP)
    monkeypatch.setattr(connector, "Connector", Runner)

    code = "p1." + "c" * 16
    assert connector.run_connector("https://relay.example", code=code) == 0
    relay = Relay.instances[0]
    assert relay.pairs == [code]
    assert relay.published == [(snapshot, "p1")]
    assert Store.saved == [
        {
            "relay_url": "https://relay.example",
            "project": "p1",
            "device_id": "device-" + "d" * 16,
            "token": "new-device-token",
        }
    ]


def test_token_store_is_ciphertext_only_and_dpapi_flags_forbid_ui(monkeypatch, tmp_path):
    seen: list[int] = []

    def protect(raw: bytes, flags: int) -> bytes:
        seen.append(flags)
        return bytes(byte ^ 0xA5 for byte in raw)

    def unprotect(raw: bytes, flags: int) -> bytes:
        seen.append(flags)
        return bytes(byte ^ 0xA5 for byte in raw)

    store = connector.DPAPITokenStore(
        tmp_path,
        protect=protect,
        unprotect=unprotect,
        windows=lambda: True,
        sid_provider=lambda: "S-1-5-21-1234",
        acl_runner=lambda *args, **kwargs: None,
    )
    store.save("device-secret")
    raw = (tmp_path / connector.TOKEN_FILENAME).read_bytes()
    assert b"device-secret" not in raw
    assert store.load() == "device-secret"
    assert seen and all(flags == connector.CRYPTPROTECT_UI_FORBIDDEN for flags in seen)


def test_asset_download_verifies_server_hash_and_length_before_replace(tmp_path):
    body = b"asset-bytes"
    digest = hashlib.sha256(body).hexdigest()

    def opener(request, timeout=None):
        assert request.get_header("Authorization") == "Bearer device-secret"
        return _Response(body, headers={"Content-Length": str(len(body))})

    relay = connector.RelayClient(
        "https://relay.example",
        device_token="device-secret",
        opener=opener,
    )
    destination = tmp_path / "asset.bin"
    relay.download_asset(
        "asset-1",
        destination,
        expected_sha256=digest,
        expected_length=len(body),
        project="p1",
    )
    assert destination.read_bytes() == body

    with pytest.raises(connector.RelayError):
        relay.download_asset(
            "asset-1",
            tmp_path / "bad.bin",
            expected_sha256="0" * 64,
            expected_length=len(body),
            project="p1",
        )
    assert not (tmp_path / "bad.bin").exists()


def test_serial_connector_maps_fixed_tools_and_replays_completed_results(tmp_path):
    class Relay:
        project_id = "p1"
        device_id = "d1"

        def __init__(self):
            self.commands = [
                {
                    "id": "command-1",
                    "nonce": "nonce-1",
                    "project_id": "p1",
                    "device_id": "d1",
                    "plan_id": "plan-1",
                    "plan_digest": "a" * 64,
                    "session_id": "session-1",
                    "expected_state": "iterating",
                    "expected_checkpoint": 0,
                    "sequence": 1,
                    "kind": "heartbeat",
                    "payload": {"probe": True},
                    "payload_digest": hashlib.sha256(b'{"probe":true}').hexdigest(),
                },
                {
                    "id": "command-1",
                    "nonce": "nonce-1",
                    "project_id": "p1",
                    "device_id": "d1",
                    "plan_id": "plan-1",
                    "plan_digest": "a" * 64,
                    "session_id": "session-1",
                    "expected_state": "iterating",
                    "expected_checkpoint": 0,
                    "sequence": 1,
                    "kind": "heartbeat",
                    "payload": {"probe": True},
                    "payload_digest": hashlib.sha256(b'{"probe":true}').hexdigest(),
                },
            ]
            self.commands[0].update(
                lease_seconds=30.0,
                lease_expires_at=100.0,
                status="leased",
                created_at=1.0,
                delivered_at=2.0,
            )
            self.commands[1].update(
                lease_seconds=30.0,
                lease_expires_at=200.0,
                status="queued",
                result={"spoofed": True},
                result_digest="f" * 64,
                created_at=3.0,
                delivered_at=4.0,
            )
            self.replay_command = dict(self.commands[1])
            self.results = []

        def next(self, **kwargs):
            return self.commands.pop(0) if self.commands else None

        def post_result(self, command_id, sequence, result, **kwargs):
            self.results.append((command_id, sequence, result, kwargs))
            return result

    class MCP:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool, envelope):
            self.calls.append((tool, envelope))
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": {"ok": True},
            }

    relay = Relay()
    mcp = MCP()
    runner = connector.Connector(
        relay,
        mcp,
        project_id="p1",
        device_id="d1",
        plan_digest="a" * 64,
        private_root=tmp_path,
    )
    assert runner.run_once() is True
    assert runner.run_once() is True
    assert mcp.calls == [
        (
            "capability_heartbeat",
            {
                "command_id": "command-1",
                "nonce": "nonce-1",
                "payload": {
                    "probe": True,
                    "project_id": "p1",
                    "plan_id": "plan-1",
                    "session_id": "session-1",
                },
            },
        )
    ]
    assert relay.results == [
        ("command-1", 1, {"ok": True}, {"project": "p1", "plan": "plan-1"}),
        ("command-1", 1, {"ok": True}, {"project": "p1", "plan": "plan-1"}),
    ]
    conflicting = dict(relay.replay_command)
    conflicting["nonce"] = "nonce-2"
    with pytest.raises(connector.ConnectorError, match="conflicting completed command"):
        runner.execute_command(conflicting)
    assert len(mcp.calls) == 1


def test_project_tool_payload_uses_validated_command_scope(tmp_path):
    payload = {"width": 1920, "session_id": "forged", "plan_id": "forged"}
    command = {
        "id": "command-project-1",
        "nonce": "nonce-project-1",
        "project_id": "p1",
        "device_id": "d1",
        "plan_id": "plan-1",
        "plan_digest": "a" * 64,
        "session_id": "session-1",
        "expected_state": "baseline",
        "expected_checkpoint": None,
        "sequence": 1,
        "kind": "create_project",
        "payload": payload,
        "payload_digest": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }

    class Relay:
        def post_result(self, *args, **kwargs):
            return {"ok": True}

    class MCP:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool, envelope):
            self.calls.append((tool, envelope))
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": {"ok": True},
            }

    mcp = MCP()
    connector.Connector(
        Relay(),
        mcp,
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
    ).execute_command(command)

    assert mcp.calls == [
        (
            "create_or_open_project",
            {
                "command_id": "command-project-1",
                "nonce": "nonce-project-1",
                "payload": {
                    "width": 1920,
                    "session_id": "session-1",
                    "plan_id": "plan-1",
                    "project_id": "p1",
                },
            },
        )
    ]


def test_connector_rejects_cross_project_device_digest_and_sequence(tmp_path):
    class Relay:
        def next(self, **kwargs):
            return {
                "id": "command-1",
                "nonce": "nonce-1",
                "project_id": "wrong",
                "device_id": "d1",
                "plan_id": "plan-1",
                "plan_digest": "a" * 64,
                "session_id": "session-1",
                "expected_state": "iterating",
                "expected_checkpoint": 0,
                "sequence": 1,
                "kind": "heartbeat",
                "payload": {},
                "payload_digest": hashlib.sha256(b"{}").hexdigest(),
            }

    with pytest.raises(connector.ConnectorError):
        connector.Connector(
            Relay(),
            object(),
            project_id="p1",
            device_id="d1",
            plan_digest="a" * 64,
            private_root=tmp_path,
        ).run_once()


def test_device_record_is_protected_and_validated(tmp_path):
    def protect(raw: bytes, flags: int) -> bytes:
        return b"cipher:" + bytes(byte ^ 0xA5 for byte in raw)

    def unprotect(raw: bytes, flags: int) -> bytes:
        assert raw.startswith(b"cipher:")
        return bytes(byte ^ 0xA5 for byte in raw[len(b"cipher:") :])

    store = connector.DPAPITokenStore(
        tmp_path,
        filename=connector.DEVICE_RECORD_FILENAME,
        protect=protect,
        unprotect=unprotect,
        windows=lambda: True,
        sid_provider=lambda: "S-1-5-21-1234",
        acl_runner=lambda *args, **kwargs: None,
    )
    store.save_record(
        {
            "relay_url": "https://relay.example",
            "project": "p1",
            "device_id": "d1",
            "token": "device-secret",
        }
    )
    assert store.load_record() == {
        "relay_url": "https://relay.example",
        "project": "p1",
        "device_id": "d1",
        "token": "device-secret",
    }
    assert b"device-secret" not in store.path.read_bytes()


def test_connector_sequences_are_scoped_to_plan_and_session(tmp_path):
    payload_digest = hashlib.sha256(b"{}").hexdigest()
    commands = [
        {
            "id": "command-plan-1",
            "nonce": "nonce-plan-1",
            "project_id": "p1",
            "device_id": "d1",
            "plan_id": "plan-1",
            "plan_digest": "a" * 64,
            "session_id": "session-1",
            "expected_state": "iterating",
            "expected_checkpoint": 0,
            "sequence": 1,
            "kind": "heartbeat",
            "payload": {},
            "payload_digest": payload_digest,
        },
        {
            "id": "command-plan-2",
            "nonce": "nonce-plan-2",
            "project_id": "p1",
            "device_id": "d1",
            "plan_id": "plan-2",
            "plan_digest": "b" * 64,
            "session_id": "session-2",
            "expected_state": "iterating",
            "expected_checkpoint": 0,
            "sequence": 1,
            "kind": "heartbeat",
            "payload": {},
            "payload_digest": payload_digest,
        },
    ]

    class Relay:
        def __init__(self):
            self.results = []

        def post_result(self, command_id, sequence, result, **kwargs):
            self.results.append((command_id, sequence, kwargs["plan"]))

    class MCP:
        def call_tool(self, tool, envelope):
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": {"ok": True},
            }

    relay = Relay()
    runner = connector.Connector(
        relay,
        MCP(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
    )
    runner.execute_command(commands[0])
    runner.execute_command(commands[1])
    assert relay.results == [
        ("command-plan-1", 1, "plan-1"),
        ("command-plan-2", 1, "plan-2"),
    ]


def test_command_journal_persists_scope_high_water_and_prunes_only_acknowledged(tmp_path, monkeypatch):
    monkeypatch.setattr(connector, "_MAX_JOURNAL_ENTRIES", 2)
    journal = connector.CommandJournal(tmp_path)
    scope = ("p1", "plan-1", "session-1")
    journal.record("command-1", 1, scope, "a" * 64, "1" * 64, {"ok": True})
    journal.record("command-2", 2, scope, "a" * 64, "2" * 64, {"ok": True})
    with pytest.raises(connector.ConnectorError):
        journal.record("command-3", 3, scope, "a" * 64, "3" * 64, {"ok": True})
    journal.acknowledge("command-1")
    journal.record("command-3", 3, scope, "a" * 64, "3" * 64, {"ok": True})
    assert journal.get("command-1") is None
    assert journal.get("command-2") is not None
    assert journal.get("command-3") is not None
    persisted = json.loads(journal.path.read_text())
    assert "high_water" not in persisted
    reopened = connector.CommandJournal(tmp_path)
    with pytest.raises(connector.ConnectorError):
        reopened.check_scope(scope, "b" * 64, 4)


def test_command_journal_repairs_scope_tombstone_after_entry_commit(tmp_path):
    journal = connector.CommandJournal(tmp_path)
    scope = ("p1", "plan-1", "session-1")
    plan_digest = "a" * 64
    journal.record("command-1", 1, scope, plan_digest, "1" * 64, {"ok": True})
    next(iter(journal.scope_dir.glob("*.json"))).unlink()

    reopened = connector.CommandJournal(tmp_path)
    reopened.check_scope(scope, plan_digest, 2)
    with pytest.raises(connector.ConnectorError):
        reopened.check_scope(scope, plan_digest, 1)


def test_command_journal_reopens_after_more_than_max_scopes_without_replay_downgrade(tmp_path):
    journal = connector.CommandJournal(tmp_path)
    earliest = ("p1", "plan-0", "session-0")
    plan_digest = "a" * 64
    journal.record("command-0", 3, earliest, plan_digest, "0" * 64, {"ok": True})
    journal.acknowledge("command-0")
    for index in range(1, 1025):
        scope = ("p1", f"plan-{index}", f"session-{index}")
        command_id = f"command-{index}"
        journal.record(command_id, 1, scope, plan_digest, f"{index:064x}", {"ok": True})
        journal.acknowledge(command_id)

    reopened = connector.CommandJournal(tmp_path)
    reopened.check_scope(earliest, plan_digest, 4)
    with pytest.raises(connector.ConnectorError):
        reopened.check_scope(earliest, "b" * 64, 4)
    with pytest.raises(connector.ConnectorError):
        reopened.check_scope(earliest, plan_digest, 2)


def test_mcp_import_is_lazy_and_child_environment_is_allowlisted(monkeypatch):
    assert "mcp" not in __import__("sys").modules
    client = connector.MCPStdioClient()
    assert "mcp" not in __import__("sys").modules
    with pytest.raises(connector.MCPError, match="absolute"):
        connector.MCPStdioClient(python="python")

    source = {
        "PATH": "x",
        "SystemRoot": "C:\\Windows",
        "KEEPFRAME_AE_RELAY_TOKEN": "relay-secret",
        "KEEPFRAME_DEVICE_TOKEN": "device-secret",
        "OPENAI_API_KEY": "provider-secret",
        "HTTP_PROXY": "http://proxy-secret",
    }
    env = connector.build_child_env(source)
    assert env == {"SystemRoot": "C:\\Windows"}
    assert client.child_argv() == [
        str(Path(sys.executable).resolve()),
        "-E",
        "-P",
        "-m",
        "keepframe.after_effects.mcp_server",
    ]


def test_mcp_child_uses_trusted_cwd_and_same_isolated_argv(monkeypatch, tmp_path):
    shadow = tmp_path / "keepframe"
    (shadow / "after_effects").mkdir(parents=True)
    (shadow / "__init__.py").write_text("raise RuntimeError('shadow import')\n")
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    class FakeParameters:
        def __init__(self, **values):
            captured.update(values)

    class FakeSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, tool, arguments):
            return {"structured_content": {"tool": tool, "arguments": arguments}}

    class FakeMCP:
        StdioServerParameters = FakeParameters
        ClientSession = FakeSession

    class FakeStdio:
        def __call__(self, parameters):
            self.parameters = parameters
            return self

        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *_):
            return False

    stdio = FakeStdio()
    client = connector.MCPStdioClient(
        python=sys.executable,
        environment={"PATH": "caller-controlled", "SystemRoot": "C:\\Windows"},
        import_module=lambda _name: FakeMCP,
        stdio_client=stdio,
    )
    result = asyncio.run(client._call_async("capability_heartbeat", {"ok": True}))

    argv = client.child_argv()
    assert result["tool"] == "capability_heartbeat"
    assert captured["command"] == argv[0]
    assert captured["args"] == argv[1:]
    assert captured["env"] == {"SystemRoot": "C:\\Windows"}
    trusted_cwd = Path(connector.__file__).resolve().parents[2]
    assert captured["cwd"] == str(trusted_cwd)
    assert trusted_cwd.is_absolute()
    assert str(tmp_path) not in " ".join(argv)



def test_render_preview_runs_fixed_ffmpeg_uploads_selected_frames_and_retains_sequence(
    tmp_path,
):
    payload, panel_result, names = _preview_fixture()
    renders = _scoped_renders(tmp_path)
    renders.mkdir(parents=True)
    for name in names:
        (renders / name).write_bytes(_png())
    process_calls: list[tuple[list[str], dict[str, object]]] = []

    def process(argv, **kwargs):
        process_calls.append((argv, kwargs))
        Path(argv[-1]).write_bytes(b"mp4")
        return type("Completed", (), {"stdout": b"", "stderr": b""})()

    class Relay:
        def __init__(self):
            self.uploads = []
            self.results = []

        def upload_artifact(self, reservation, source, **kwargs):
            self.uploads.append((reservation, Path(source), kwargs))
            return {"artifact": {"id": reservation}}

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    class MCP:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool, envelope):
            self.calls.append((tool, envelope))
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": panel_result,
            }

    relay = Relay()
    runner = connector.Connector(
        relay,
        MCP(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
        ffmpeg="preflighted-ffmpeg",
        process_runner=process,
    )
    result = runner.execute_command(
        _command(
            "render_preview",
            payload,
            command_id="command-preview-1",
            expected_checkpoint=2,
        )
    )
    assert [item[0] for item in relay.uploads] == [
        "mp4-2",
        "png-2-0",
        "png-2-2",
        "png-2-3",
    ]
    assert [item[1].name for item in relay.uploads] == [
        "checkpoint-2.mp4",
        "checkpoint-2-000000.png",
        "checkpoint-2-000002.png",
        "checkpoint-2-000003.png",
    ]
    assert (renders / "checkpoint-2.mp4").exists()
    assert all((renders / name).exists() for name in names)
    argv, kwargs = process_calls[0]
    assert argv == [
        "preflighted-ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-framerate",
        "24.0",
        "-i",
        str(renders / "checkpoint-2-%06d.png"),
        "-vf",
        "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
        "-frames:v",
        "4",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "18",
        str(renders / "checkpoint-2.mp4"),
    ]
    assert kwargs["stdout"] is not subprocess.PIPE
    assert kwargs["stderr"] is not subprocess.PIPE
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == connector._FFMPEG_TIMEOUT
    assert result["artifacts"] == [
        {"id": "mp4-2"},
        {"id": "png-2-0"},
        {"id": "png-2-2"},
        {"id": "png-2-3"},
    ]


def test_render_preview_rejects_panel_metadata_and_unsafe_sequence(tmp_path):
    payload, panel_result, names = _preview_fixture()
    renders = _scoped_renders(tmp_path)
    renders.mkdir(parents=True)
    for name in names:
        (renders / name).write_bytes(_png())
    panel_result["sequence"]["directory"] = "../outside"

    class Relay:
        uploads = []
        results = []

        def upload_artifact(self, *args, **kwargs):
            self.uploads.append((args, kwargs))
            return {"artifact": {"id": "unexpected"}}

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    class MCP:
        def call_tool(self, tool, envelope):
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": panel_result,
            }

    relay = Relay()
    runner = connector.Connector(
        relay,
        MCP(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
        ffmpeg="preflighted-ffmpeg",
        process_runner=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid metadata must fail before ffmpeg")
        ),
    )
    result = runner.execute_command(
        _command(
            "render_preview",
            payload,
            command_id="command-preview-invalid",
            expected_checkpoint=2,
        )
    )
    assert result == {"ok": False, "error": "connector preview processing failed"}
    assert relay.results[-1][0][2] == result
    assert relay.uploads == []
    assert all((renders / name).exists() for name in names)


def test_render_preview_rejects_symlink_and_non_png_sequence_files(tmp_path):
    payload, panel_result, names = _preview_fixture(frame_count=2, representatives=[0])
    renders = _scoped_renders(tmp_path)
    renders.mkdir(parents=True)
    (renders / names[0]).write_bytes(_png())
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png())
    (renders / names[1]).symlink_to(outside)

    class MCP:
        def call_tool(self, tool, envelope):
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": panel_result,
            }

    class Relay:
        def __init__(self):
            self.results = []

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    relay = Relay()
    runner = connector.Connector(
        relay,
        MCP(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
        ffmpeg="preflighted-ffmpeg",
        process_runner=lambda *args, **kwargs: pytest.fail("ffmpeg must not run"),
    )
    first = runner.execute_command(
        _command(
            "render_preview",
            payload,
            command_id="command-preview-symlink",
            expected_checkpoint=2,
        )
    )
    assert first == {"ok": False, "error": "connector preview processing failed"}
    (renders / names[1]).unlink()
    (renders / names[1]).write_bytes(b"not-png")
    second = runner.execute_command(
        _command(
            "render_preview",
            payload,
            command_id="command-preview-non-png",
            sequence=2,
            expected_checkpoint=2,
        )
    )
    assert second == {"ok": False, "error": "connector preview processing failed"}
    assert len(relay.results) == 2

def test_render_preview_keeps_sequence_when_upload_fails(tmp_path):
    payload, panel_result, names = _preview_fixture()
    renders = _scoped_renders(tmp_path)
    renders.mkdir(parents=True)
    for name in names:
        (renders / name).write_bytes(_png())

    def process(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"mp4")
        return type("Completed", (), {"stdout": b"", "stderr": b""})()

    class Relay:
        def __init__(self):
            self.uploads = []
            self.results = []

        def upload_artifact(self, reservation, source, **kwargs):
            self.uploads.append(reservation)
            raise connector.RelayError("upload failed")

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    class MCP:
        def call_tool(self, tool, envelope):
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": panel_result,
            }

    relay = Relay()
    runner = connector.Connector(
        relay,
        MCP(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
        ffmpeg="preflighted-ffmpeg",
        process_runner=process,
    )
    result = runner.execute_command(
        _command(
            "render_preview",
            payload,
            command_id="command-preview-upload-failure",
            expected_checkpoint=2,
        )
    )
    assert result == {
        "ok": False,
        "error": "connector artifact upload failed",
        "reason": "upload_failed",
    }
    assert relay.results[-1][0][2] == result
    assert (renders / "checkpoint-2.mp4").exists()
    assert all((renders / name).exists() for name in names)
    replay = runner.execute_command(
        _command(
            "render_preview",
            payload,
            command_id="command-preview-upload-failure",
            expected_checkpoint=2,
        )
    )
    assert replay == result
    assert len(relay.results) == 2

@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["ffmpeg"], 1),
        RuntimeError("ffmpeg process failed"),
    ],
)
def test_render_preview_bounds_ffmpeg_failures(tmp_path, failure):
    payload, panel_result, names = _preview_fixture(frame_count=1, representatives=[0])
    renders = _scoped_renders(tmp_path)
    renders.mkdir(parents=True)
    (renders / names[0]).write_bytes(_png())

    class MCP:
        def call_tool(self, tool, envelope):
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": panel_result,
            }

    def process(*args, **kwargs):
        raise failure

    class Relay:
        def __init__(self):
            self.results = []

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    relay = Relay()
    runner = connector.Connector(
        relay,
        MCP(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
        ffmpeg="preflighted-ffmpeg",
        process_runner=process,
    )
    result = runner.execute_command(
        _command(
            "render_preview",
            payload,
            command_id="command-preview-process-failure",
            expected_checkpoint=2,
        )
    )
    assert result == {"ok": False, "error": "connector preview processing failed"}
    assert relay.results[-1][0][2] == result


def test_render_preview_rejects_excessive_ffmpeg_output(tmp_path):
    payload, panel_result, names = _preview_fixture(frame_count=1, representatives=[0])
    renders = _scoped_renders(tmp_path)
    renders.mkdir(parents=True)
    (renders / names[0]).write_bytes(_png())

    class MCP:
        def call_tool(self, tool, envelope):
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": panel_result,
            }

    def process(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"mp4")
        return type(
            "Completed",
            (),
            {"stdout": b"x" * (connector._MAX_FFMPEG_OUTPUT_BYTES + 1), "stderr": b""},
        )()

    class Relay:
        def __init__(self):
            self.results = []

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    relay = Relay()
    runner = connector.Connector(
        relay,
        MCP(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
        ffmpeg="preflighted-ffmpeg",
        process_runner=process,
    )
    result = runner.execute_command(
        _command(
            "render_preview",
            payload,
            command_id="command-preview-output-limit",
            expected_checkpoint=2,
        )
    )
    assert result == {"ok": False, "error": "connector preview processing failed"}
    assert relay.results[-1][0][2] == result


def test_preview_png_validator_rejects_malformed_ihdr(tmp_path):
    path = tmp_path / "bad.png"
    valid = _png()
    path.write_bytes(valid[:8] + struct.pack(">I4s", 12, b"IHDR") + valid[16:])
    with pytest.raises(connector.ConnectorError, match="IHDR"):
        connector.Connector._validate_png_sequence_file(path)


def test_preview_dimension_failure_posts_generic_durable_result(tmp_path):
    payload, panel_result, names = _preview_fixture(frame_count=1, representatives=[0])
    renders = _scoped_renders(tmp_path)
    renders.mkdir(parents=True)
    (renders / names[0]).write_bytes(_png(1281, 720))

    class MCP:
        def call_tool(self, tool, envelope):
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": panel_result,
            }

    class Relay:
        def __init__(self):
            self.results = []

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    relay = Relay()
    runner = connector.Connector(
        relay,
        MCP(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
        ffmpeg="preflighted-ffmpeg",
        process_runner=lambda *args, **kwargs: pytest.fail("invalid PNG must stop before ffmpeg"),
    )
    result = runner.execute_command(
        _command(
            "render_preview",
            payload,
            command_id="command-preview-dimension-failure",
            expected_checkpoint=2,
        )
    )
    assert result == {"ok": False, "error": "connector preview processing failed"}
    assert len(result["error"]) <= connector._MAX_ERROR_BYTES
    assert str(tmp_path) not in result["error"]
    assert relay.results[-1][0][2] == result
    assert (renders / names[0]).exists()


def test_save_checkpoint_sends_only_compact_instructions_and_replays(tmp_path):
    payload = {
        "index": 4,
        "checkpoint_context_digest": "a" * 64,
        "workflow": {"state": "server-only"},
    }

    class Relay:
        def __init__(self):
            self.results = []

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    class MCP:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool, envelope):
            self.calls.append((tool, envelope))
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": {
                    "saved": True,
                    "checkpoint": {"forged": True},
                    "checkpoint_context": {"forged": True},
                    "workflow": {"forged": True},
                },
            }

    relay = Relay()
    mcp = MCP()
    runner = connector.Connector(
        relay,
        mcp,
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
    )
    command = _command(
        "save_checkpoint",
        payload,
        command_id="command-save-context",
        expected_checkpoint=4,
    )
    result = runner.execute_command(command)
    assert mcp.calls[0][1]["payload"] == {
        "index": 4,
        "project_id": "p1",
        "plan_id": "plan-1",
        "session_id": "session-1",
    }
    assert result == {"saved": True}
    replay = runner.execute_command(command)
    assert replay == result
    assert len(mcp.calls) == 1
    assert relay.results[-1][0][2] == result


def test_connector_recomputes_live_capabilities_before_mutation(tmp_path):
    class Relay:
        def post_result(self, *args, **kwargs):
            return None

    class MCP:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool, envelope):
            self.calls.append((tool, envelope))
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": _panel_heartbeat(),
            }

    runner = connector.Connector(
        Relay(),
        MCP(),
        project_id="p1",
        device_id="d1",
        capability_hash="a" * 64,
        private_root=tmp_path,
    )
    with pytest.raises(connector.ConnectorError, match="capabilities changed"):
        runner._refresh_live_capabilities(
            "command-capability",
            "nonce-capability",
            "plan-1",
            "session-1",
        )


def test_live_capability_probe_uses_startup_font_enrichment(monkeypatch, tmp_path):
    enriched = []

    def enrich(heartbeat):
        enriched.append(heartbeat)
        return dict(heartbeat)

    monkeypatch.setattr(connector, "enrich_heartbeat_fonts", enrich)
    heartbeat = _panel_heartbeat()
    approved_hash = connector.AECapabilities.from_heartbeat(heartbeat).capability_hash

    class Relay:
        def post_result(self, *args, **kwargs):
            return None

    class MCP:
        def call_tool(self, tool, envelope):
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": heartbeat,
            }

    runner = connector.Connector(
        Relay(),
        MCP(),
        project_id="p1",
        device_id="d1",
        capability_hash=approved_hash,
        private_root=tmp_path,
    )
    assert runner._refresh_live_capabilities(
        "command-capability",
        "nonce-capability",
        "plan-1",
        "session-1",
    ).capability_hash == approved_hash
    assert enriched



def test_compact_inspection_reuses_startup_font_enrichment(monkeypatch, tmp_path):
    raw = _panel_heartbeat()
    capabilities = dict(raw["capabilities"])
    font = dict(capabilities["fonts"][0])
    font["version"] = None
    font["version_or_hash"] = None
    capabilities["fonts"] = [font]
    raw["capabilities"] = capabilities
    enriched = []

    def enrich(heartbeat):
        enriched.append(heartbeat)
        value = json.loads(json.dumps(heartbeat))
        value["capabilities"]["fonts"][0]["version_or_hash"] = "font-file-sha"
        return value

    monkeypatch.setattr(connector, "enrich_heartbeat_fonts", enrich)
    approved_hash = connector.AECapabilities.from_heartbeat(enrich(raw)).capability_hash
    runner = connector.Connector(
        object(),
        object(),
        project_id="p1",
        device_id="d1",
        capability_hash=approved_hash,
        private_root=tmp_path,
    )
    compact = runner._compact_inspection_result(
        {
            "schema_version": "keepframe.ae-inspection/1",
            "heartbeat": raw,
            "layers": [],
            "samples": [],
        }
    )
    assert compact["heartbeat"]["capability_hash"] == approved_hash
    assert len(enriched) == 2


def test_connector_posts_capability_change_without_mutating_ae(tmp_path):
    payload = {
        "batch": {
            "capability_digest": "a" * 64,
            "operations": [],
        },
        "approved_capabilities": {
            "digest": "a" * 64,
            "fonts": [],
            "effects": [],
            "properties": {},
        },
        "scene_frame_count": 10,
        "duration": 1.0,
        "layer_count": 0,
        "baseline": False,
        "locked_source_ids": [],
        "layer_sources": {},
        "layer_native_ids": {},
    }

    class Relay:
        def __init__(self):
            self.results = []

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    class MCP:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool, envelope):
            self.calls.append((tool, envelope))
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": _panel_heartbeat(),
            }

    relay = Relay()
    mcp = MCP()
    runner = connector.Connector(
        relay,
        mcp,
        project_id="p1",
        device_id="d1",
        capability_hash="a" * 64,
        private_root=tmp_path,
    )
    result = runner.execute_command(
        _command("apply_batch", payload, command_id="command-capability-change")
    )
    assert result == {
        "ok": False,
        "error": "capabilities_changed",
        "reason": "capabilities_changed",
    }
    assert [tool for tool, _envelope in mcp.calls] == ["capability_heartbeat"]
    assert relay.results[-1][0][2] == result


def test_connector_materializes_selected_checkpoint_before_open(tmp_path):
    checkpoint = b"%PDF-1.4 checkpoint"
    checkpoint_digest = hashlib.sha256(checkpoint).hexdigest()

    class Relay:
        def __init__(self):
            self.downloads = []
            self.results = []

        def download_artifact(
            self,
            artifact_id,
            destination,
            *,
            expected_sha256,
            expected_length,
            project,
            plan,
        ):
            self.downloads.append(
                (
                    artifact_id,
                    destination,
                    expected_sha256,
                    expected_length,
                    project,
                    plan,
                )
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(checkpoint)
            return destination

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    class MCP:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool, envelope):
            self.calls.append((tool, envelope))
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": {"opened": True},
            }

    relay = Relay()
    mcp = MCP()
    runner = connector.Connector(
        relay,
        mcp,
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
    )
    payload = {
        "checkpoint_index": 7,
        "checkpoint_artifact": {
            "id": "art-checkpoint",
            "sha256": checkpoint_digest,
            "length": len(checkpoint),
        },
    }
    result = runner.execute_command(
        _command("open_project", payload, command_id="command-open-selected")
    )
    assert result == {"opened": True}
    assert relay.downloads[0][0] == "art-checkpoint"
    assert relay.downloads[0][2:] == (
        checkpoint_digest,
        len(checkpoint),
        "p1",
        "plan-1",
    )
    assert relay.downloads[0][1].name == "checkpoint-7.aep"
    assert mcp.calls[0][1]["payload"] == {
        "checkpoint_index": 7,
        "project_id": "p1",
        "plan_id": "plan-1",
        "session_id": "session-1",
    }

def test_connector_scopes_every_coordinator_mcp_payload(tmp_path):
    kinds = [
        "heartbeat",
        "create_project",
        "import_asset",
        "apply_batch",
        "inspect_layers",
        "save_checkpoint",
        "render_final",
        "package_project",
    ]

    class Relay:
        def post_result(self, *args, **kwargs):
            return {"ok": True}

    class MCP:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool, envelope):
            self.calls.append((tool, envelope))
            result = {"ok": True}
            if tool == "inspect_mapped_layers":
                result.update(
                    {
                        "schema_version": "keepframe.ae-inspection/1",
                        "heartbeat": {
                            "capability_hash": "a" * 64,
                            "version": "24.1.0",
                            "major": 24,
                            "host": "after-effects",
                        },
                    }
                )
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": result,
            }

    mcp = MCP()
    runner = connector.Connector(
        Relay(),
        mcp,
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
    )
    for sequence, kind in enumerate(kinds, start=1):
        payload = {
            "project_id": "spoofed-project",
            "plan_id": "spoofed-plan",
            "session_id": "spoofed-session",
            "marker": kind,
        }
        command = {
            "id": f"command-scope-{sequence}",
            "nonce": f"nonce-scope-{sequence}",
            "project_id": "p1",
            "device_id": "d1",
            "plan_id": "plan-1",
            "plan_digest": "a" * 64,
            "session_id": "session-1",
            "expected_state": "iterating",
            "expected_checkpoint": 0,
            "sequence": sequence,
            "kind": kind,
            "payload": payload,
            "payload_digest": connector._canonical_digest(payload),
        }
        runner.execute_command(command)

    assert [tool for tool, _ in mcp.calls] == [
        connector.COMMAND_TO_TOOL[kind] for kind in kinds
    ]
    for _, envelope in mcp.calls:
        assert envelope["payload"]["project_id"] == "p1"
        assert envelope["payload"]["plan_id"] == "plan-1"
        assert envelope["payload"]["session_id"] == "session-1"


def test_preflight_capability_heartbeat_is_unscoped(monkeypatch, tmp_path):
    captured = {}

    class MCP:
        def __init__(self, **kwargs):
            pass

        def call_tool(self, tool, envelope):
            captured["tool"] = tool
            captured["envelope"] = envelope
            return {"ok": True}

    monkeypatch.setattr(connector, "MCPStdioClient", MCP)
    connector._probe_panel_with_mcp(tmp_path)
    envelope = captured["envelope"]
    assert captured["tool"] == "capability_heartbeat"
    assert set(envelope) == {"command_id", "nonce", "payload"}
    assert envelope["payload"] == {}

 


def test_relay_default_timeout_has_long_poll_headroom():
    assert connector.RelayClient("https://relay.example").timeout >= 35


@pytest.mark.parametrize("status", [500, 503, 599])
def test_relay_5xx_statuses_are_retryable(status):
    def opener(request, timeout=None):
        return _Response(b"not-json", status=status)

    relay = connector.RelayClient(
        "https://relay.example",
        device_token="device-secret",
        opener=opener,
    )
    with pytest.raises(connector.TransientRelayError):
        relay.next(project="p1")



def test_relay_http_error_preserves_bounded_draining_detail():
    def opener(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            409,
            "draining",
            {},
            io.BytesIO(b'{"error":"command lease is draining"}'),
        )

    relay = connector.RelayClient(
        "https://relay.example",
        device_token="device-secret",
        opener=opener,
    )
    with pytest.raises(connector.RelayError, match="command lease is draining"):
        relay.renew_command("command-1", 1, "nonce-1")


@pytest.mark.parametrize("status", [400, 401, 408, 429, 499, 600])
def test_relay_non_5xx_statuses_are_terminal(status):
    def opener(request, timeout=None):
        return _Response(b"not-json", status=status)

    relay = connector.RelayClient(
        "https://relay.example",
        device_token="device-secret",
        opener=opener,
    )
    with pytest.raises(connector.RelayError) as raised:
        relay.next(project="p1")
    assert not isinstance(raised.value, connector.TransientRelayError)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timed out"),
        ConnectionError("refused"),
        OSError(errno.ECONNREFUSED, "connection refused"),
        socket.gaierror(getattr(socket, "EAI_AGAIN", -3), "temporary DNS failure"),
    ],
)
def test_relay_temporary_network_failures_are_retryable(failure):
    def opener(request, timeout=None):
        raise failure

    relay = connector.RelayClient(
        "https://relay.example",
        device_token="device-secret",
        opener=opener,
    )
    with pytest.raises(connector.TransientRelayError):
        relay.next(project="p1")


@pytest.mark.parametrize(
    "failure",
    [
        ssl.SSLCertVerificationError("certificate verify failed"),
        ssl.CertificateError("hostname mismatch"),
        socket.gaierror(getattr(socket, "EAI_NONAME", -2), "name not found"),
        OSError(errno.EHOSTUNREACH, "host unreachable"),
        FileNotFoundError("local resource missing"),
        urllib.error.URLError("protocol failure"),
        http.client.BadStatusLine("invalid response"),
    ],
)
def test_relay_permanent_failures_are_terminal(failure):
    def opener(request, timeout=None):
        raise failure

    relay = connector.RelayClient(
        "https://relay.example",
        device_token="device-secret",
        opener=opener,
    )
    with pytest.raises(connector.RelayError) as raised:
        relay.next(project="p1")
    assert not isinstance(raised.value, connector.TransientRelayError)


def test_relay_cyclic_url_error_reason_is_terminal():
    failure = urllib.error.URLError("cyclic reason")
    failure.reason = failure

    def opener(request, timeout=None):
        raise failure

    relay = connector.RelayClient(
        "https://relay.example",
        device_token="device-secret",
        opener=opener,
    )
    with pytest.raises(connector.RelayError) as raised:
        relay.next(project="p1")
    assert not isinstance(raised.value, connector.TransientRelayError)


def test_run_forever_retries_transient_poll_and_honors_stop_event(tmp_path):
    class Stop:
        def __init__(self):
            self.stopped = False
            self.waits = []

        def is_set(self):
            return self.stopped

        def wait(self, delay):
            self.waits.append(delay)
            self.stopped = True

    class Relay:
        def __init__(self):
            self.calls = 0

        def next(self, **kwargs):
            self.calls += 1
            raise connector.TransientRelayError("temporarily unavailable")

    stop = Stop()
    relay = Relay()
    connector.Connector(
        relay,
        object(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
    ).run_forever(stop_event=stop)
    assert relay.calls == 1
    assert stop.waits and 0 < stop.waits[0] <= 5


def test_run_forever_does_not_retry_auth_failure(tmp_path):
    class Stop:
        def __init__(self):
            self.waits = []

        def is_set(self):
            return False

        def wait(self, delay):
            self.waits.append(delay)

    class Relay:
        def next(self, **kwargs):
            raise connector.RelayError("unauthorized")

    stop = Stop()
    with pytest.raises(connector.RelayError, match="unauthorized"):
        connector.Connector(
            Relay(),
            object(),
            project_id="p1",
            device_id="d1",
            private_root=tmp_path,
        ).run_forever(stop_event=stop)
    assert stop.waits == []


def test_run_forever_continues_after_idle_poll(tmp_path):
    class Stop:
        def __init__(self):
            self.polls = 0

        def is_set(self):
            return self.polls >= 2

    class Relay:
        def __init__(self, stop):
            self.stop = stop

        def next(self, **kwargs):
            self.stop.polls += 1
            return None

    stop = Stop()
    relay = Relay(stop)
    connector.Connector(
        relay,
        object(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
    ).run_forever(stop_event=stop)
    assert stop.polls == 2

def test_command_lease_renewal_retries_transient_failures_while_live():
    class Relay:
        def __init__(self):
            self.calls = 0

        def renew_command(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise connector.TransientRelayError("temporary")
            return {"lease_expires_at": time.time() + 100.0}

    relay = Relay()
    lease = connector._CommandLeaseRenewal(
        relay,
        "command-renewal",
        1,
        "nonce-renewal",
        30.0,
        None,
        project="p1",
        plan="plan-1",
    )
    lease._renew_once()
    lease.check()
    lease._renew_once()
    lease.check()
    assert relay.calls == 2


def test_command_lease_renewal_uses_monotonic_local_deadline(monkeypatch):
    wall_clock = {"value": 1_000.0}
    monotonic_clock = {"value": 50.0}

    class Relay:
        def renew_command(self, *args, **kwargs):
            return {"lease_expires_at": 1.0}

    monkeypatch.setattr(connector.time, "time", lambda: wall_clock["value"])
    monkeypatch.setattr(connector.time, "monotonic", lambda: monotonic_clock["value"])
    lease = connector._CommandLeaseRenewal(
        Relay(),
        "command-monotonic",
        1,
        "nonce-monotonic",
        30.0,
        900.0,
        project="p1",
        plan="plan-1",
    )
    lease._renew_once()
    lease.check()
    monotonic_clock["value"] = 79.9
    lease.check()
    monotonic_clock["value"] = 80.0
    with pytest.raises(connector.ConnectorError, match="renewal failed"):
        lease.check()


def test_command_lease_renewal_treats_draining_as_graceful():
    class Relay:
        def renew_command(self, *args, **kwargs):
            raise connector.RelayError(
                "relay request failed (409): command lease is draining"
            )

    lease = connector._CommandLeaseRenewal(
        Relay(),
        "command-draining",
        1,
        "nonce-draining",
        30.0,
        None,
        project="p1",
        plan="plan-1",
    )
    lease._renew_once()
    lease.check()
    assert lease._stop.is_set()


def test_command_lease_renewal_rejects_definitive_expiry():
    class Relay:
        def renew_command(self, *args, **kwargs):
            raise connector.RelayError(
                "relay request failed (409): command lease has expired"
            )

    lease = connector._CommandLeaseRenewal(
        Relay(),
        "command-expired",
        1,
        "nonce-expired",
        30.0,
        None,
        project="p1",
        plan="plan-1",
    )
    lease._renew_once()
    with pytest.raises(connector.ConnectorError, match="renewal failed"):
        lease.check()

def _final_fixture(
    *,
    frame_count: int = 3,
    fps: float = 30.0,
    width: int = 1920,
    height: int = 1080,
) -> tuple[dict[str, object], dict[str, object], list[str]]:
    names = [f"final-{frame:06d}.png" for frame in range(frame_count)]
    payload: dict[str, object] = {
        "checkpoint_index": 4,
        "frame_count": frame_count,
        "fps": fps,
        "width": width,
        "height": height,
        "final_plan_digest": "b" * 64,
        "finalization_key": "final-key",
        "artifacts": [
            {
                "reservation_id": "mp4-final",
                "kind": "mp4",
                "filename": "final.mp4",
                "directory": "renders",
            }
        ],
    }
    result: dict[str, object] = {
        "rendered": True,
        "kind": "final",
        "checkpoint_index": 4,
        "frame_count": frame_count,
        "fps": fps,
        "width": width,
        "height": height,
        "sequence": {
            "directory": "renders",
            "pattern": "final-%06d.png",
            "frame_count": frame_count,
            "first_frame": names[0],
            "last_frame": names[-1],
        },
    }
    return payload, result, names


def test_render_final_runs_unscaled_ffmpeg_and_uploads_reserved_mp4(tmp_path):
    payload, panel_result, names = _final_fixture()
    renders = _scoped_renders(tmp_path)
    renders.mkdir(parents=True)
    for name in names:
        (renders / name).write_bytes(_png(1920, 1080))
    process_calls: list[tuple[list[str], dict[str, object]]] = []

    def process(argv, **kwargs):
        process_calls.append((argv, kwargs))
        Path(argv[-1]).write_bytes(b"final-mp4")
        return type("Completed", (), {"stdout": b"", "stderr": b""})()

    class Relay:
        def __init__(self):
            self.uploads = []
            self.results = []

        def upload_artifact(self, reservation, source, **kwargs):
            self.uploads.append((reservation, Path(source), kwargs))
            return {"artifact": {"id": reservation}}

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    class MCP:
        def call_tool(self, tool, envelope):
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": panel_result,
            }

    relay = Relay()
    result = connector.Connector(
        relay,
        MCP(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
        ffmpeg="preflighted-ffmpeg",
        process_runner=process,
    ).execute_command(
        _command(
            "render_final",
            payload,
            command_id="command-final-1",
            expected_checkpoint=4,
        )
    )

    assert [item[0] for item in relay.uploads] == ["mp4-final"]
    assert relay.uploads[0][1].name == "final.mp4"
    assert process_calls[0][0] == [
        "preflighted-ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-framerate",
        "30.0",
        "-i",
        str(renders / "final-%06d.png"),
        "-frames:v",
        "3",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "18",
        str(renders / "final.mp4"),
    ]
    assert "-vf" not in process_calls[0][0]
    assert result["artifacts"] == [{"id": "mp4-final"}]


def test_package_project_writes_allowlisted_zip_and_dependency_manifest(tmp_path):
    media = b"immutable media"
    media_digest = hashlib.sha256(media).hexdigest()
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "asset-1").write_bytes(media)
    package_dir = _scoped_renders(tmp_path).parent / "package"
    package_dir.mkdir(parents=True)
    (package_dir / "project.aep").write_bytes(b"aep")
    (_scoped_renders(tmp_path).parent / "checkpoints").mkdir(exist_ok=True)
    payload: dict[str, object] = {
        "selected_checkpoint": 4,
        "checkpoint_index": 4,
        "final_plan_digest": "b" * 64,
        "finalization_key": "final-key",
        "assets": [
            {
                "id": "asset-1",
                "sha256": media_digest,
                "length": len(media),
                "media_kind": "image/png",
                "role": "sprite",
            }
        ],
        "package_media": [
            {
                "asset_id": "asset-1",
                "filename": "asset-1",
                "sha256": media_digest,
                "length": len(media),
                "media_kind": "image/png",
            }
        ],
        "dependencies": {
            "capability_manifest": {
                "fonts": [{"match_name": "Inter", "version_or_hash": "1"}],
                "effects": [{"match_name": "ADBE Fill", "version_or_hash": "1"}],
                "plugins": [{"match_name": "ADBE Fill", "version_or_hash": "1"}],
            },
            "substitutions": [],
            "missing_nonportable_dependencies": [],
        },
        "artifacts": [
            {
                "reservation_id": "aep-final",
                "kind": "aep",
                "filename": "project.aep",
                "directory": "package",
            },
            {
                "reservation_id": "zip-final",
                "kind": "zip",
                "filename": "project.zip",
                "directory": "checkpoints",
            },
        ],
    }

    class Relay:
        def __init__(self):
            self.uploads = []
            self.results = []

        def upload_artifact(self, reservation, source, **kwargs):
            self.uploads.append((reservation, Path(source), kwargs))
            return {"artifact": {"id": reservation}}

        def post_result(self, *args, **kwargs):
            self.results.append((args, kwargs))

    class MCP:
        def call_tool(self, tool, envelope):
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": {"saved": True, "kind": "package", "filename": "project.aep"},
            }

    relay = Relay()
    runner = connector.Connector(
        relay,
        MCP(),
        project_id="p1",
        device_id="d1",
        private_root=tmp_path,
    )
    result = runner.execute_command(
        _command(
            "package_project",
            payload,
            command_id="command-package-1",
            expected_checkpoint=4,
        )
    )

    assert [item[0] for item in relay.uploads] == ["aep-final", "zip-final"]
    zip_path = next(path for reservation, path, _ in relay.uploads if reservation == "zip-final")
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == [
            "project.aep",
            "dependencies.json",
            "collected_media/asset-1",
        ]
        manifest = json.loads(archive.read("dependencies.json"))
    assert manifest["plan_digest"] == "b" * 64
    assert manifest["final_plan_digest"] == "b" * 64
    assert manifest["finalization_key"] == "final-key"
    assert manifest["selected_checkpoint"] == 4
    assert manifest["media"] == [
        {
            "asset_id": "asset-1",
            "filename": "asset-1",
            "length": len(media),
            "media_kind": "image/png",
            "role": "sprite",
            "sha256": media_digest,
        }
    ]
    assert all(
        not name.lower().endswith((".ttf", ".otf", ".plugin", ".aex"))
        for name in zipfile.ZipFile(zip_path).namelist()
        if name != "project.aep"
    )
    assert result["artifacts"] == [{"id": "aep-final"}, {"id": "zip-final"}]
    retried = runner.execute_command(
        _command(
            "package_project",
            payload,
            command_id="command-package-2",
            sequence=2,
            expected_checkpoint=4,
        )
    )
    assert retried["artifacts"] == [
        {"id": "aep-final"},
        {"id": "zip-final"},
    ]
    assert [item[0] for item in relay.uploads] == [
        "aep-final",
        "zip-final",
        "aep-final",
        "zip-final",
    ]


def test_package_media_rejects_unlisted_or_tampered_records():
    digest = "a" * 64
    assets = [{"id": "asset-1", "sha256": digest, "length": 1}]
    with pytest.raises(connector.ConnectorError, match="unlisted"):
        connector.Connector._package_media(
            {
                "assets": assets,
                "package_media": [
                    {
                        "asset_id": "asset-2",
                        "filename": "media.png",
                        "sha256": digest,
                        "length": 1,
                    }
                ],
            }
        )
    with pytest.raises(connector.ConnectorError, match="metadata"):
        connector.Connector._package_media(
            {
                "assets": assets,
                "package_media": [
                    {
                        "asset_id": "asset-1",
                        "filename": "media.png",
                        "sha256": "b" * 64,
                        "length": 1,
                    }
                ],
            }
        )
