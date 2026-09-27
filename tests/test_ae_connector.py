from __future__ import annotations

import asyncio
import errno
import hashlib
import http.client
import json
import socket
import ssl
import sys
import urllib.error
from pathlib import Path

import pytest

import keepframe.after_effects.connector as connector


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
    assert calls and calls[0][0][0] == "icacls" and calls[0][1] is False

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
        "-I",
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


def test_connector_scopes_every_coordinator_mcp_payload(tmp_path):
    kinds = [
        "heartbeat",
        "create_project",
        "import_asset",
        "apply_batch",
        "inspect_layers",
        "save_checkpoint",
        "render_preview",
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
            return {
                "schema_version": 1,
                "command_id": envelope["command_id"],
                "nonce": envelope["nonce"],
                "ok": True,
                "result": {"ok": True},
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
