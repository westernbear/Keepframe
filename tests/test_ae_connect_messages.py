import asyncio
import sys

import pytest

from pathlib import Path

from keepframe.after_effects import connector


def test_run_connector_reports_missing_token(monkeypatch, capsys):
    monkeypatch.delenv("KEEPFRAME_AE_RELAY_TOKEN", raising=False)
    assert connector.run_connector("https://relay.example", code=None, project="demo") == 1
    err = capsys.readouterr().err
    assert "ae-connect failed: deployment credential is unavailable" in err
    assert "KEEPFRAME_AE_RELAY_TOKEN" in err


def test_failure_text_never_echoes_the_token(monkeypatch):
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_TOKEN", "s3cret-token-value")
    text = connector._connector_failure_text(connector.ConnectorError("bad s3cret-token-value"))
    assert "s3cret-token-value" not in text and "[redacted]" in text


def test_panel_failure_has_a_hint():
    text = connector._connector_failure_text(connector.ConnectorError("After Effects panel is not ready"))
    assert "Keepframe Panel" in text


def test_agent_pairing_values_have_copy_buttons():
    html = Path("keepframe/web/static/agent.html").read_text(encoding="utf-8")
    js = Path("keepframe/web/static/js/agent.js").read_text(encoding="utf-8")
    assert 'data-copy-target="render-pairing-code"' in html
    assert 'data-copy-target="render-pairing-command"' in html
    assert "setTextIfChanged(renderPairingCodeEl" in js
    assert 'document.execCommand("copy")' in js


def test_private_root_grants_the_sid_with_star_prefix(tmp_path):
    calls = []
    root = connector.ensure_private_root(
        tmp_path / "bridge",
        sid_provider=lambda: "S-1-5-21-1-2-3-1001",
        acl_runner=lambda argv, **kw: calls.append(argv),
        windows=lambda: True,
    )
    assert root == tmp_path / "bridge"
    grant = calls[1]
    assert grant[0].lower().endswith("icacls.exe")
    assert "*S-1-5-21-1-2-3-1001:(OI)(CI)F" in grant


def test_private_root_failure_includes_icacls_detail(tmp_path):
    import subprocess
    import pytest

    def fail(argv, **kw):
        raise subprocess.CalledProcessError(1, argv, stderr="No mapping between account names and security IDs was done.")

    with pytest.raises(connector.PreflightError) as err:
        connector.ensure_private_root(tmp_path / "b", sid_provider=lambda: "S-1-5-21-1", acl_runner=fail, windows=lambda: True)
    assert "No mapping between account names" in str(err.value)


class _FakeMCP:
    class StdioServerParameters:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class ClientSession:
        def __init__(self, *_):
            pass


def _failing_stdio(raise_exc, child_says=""):
    class FailingStdio:
        def __call__(self, parameters, errlog=None):
            self.errlog = errlog
            return self

        async def __aenter__(self):
            self.errlog.write(child_says)
            self.errlog.flush()
            raise raise_exc

        async def __aexit__(self, *_):
            return False

    return FailingStdio()


def _client(stdio):
    return connector.MCPStdioClient(
        python=sys.executable, environment={}, import_module=lambda _name: _FakeMCP, stdio_client=stdio
    )


def test_mcp_failure_names_the_root_cause_and_the_servers_last_line():
    group = ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("Connection closed")])
    stdio = _failing_stdio(group, "Traceback ...\nModuleNotFoundError: No module named 'keepframe'\n")
    with pytest.raises(connector.MCPError) as caught:
        asyncio.run(_client(stdio)._call_async("capability_heartbeat", {}))
    text = str(caught.value)
    assert "RuntimeError: Connection closed" in text
    assert "No module named 'keepframe'" in text
    assert "ExceptionGroup" not in text


def test_connector_error_inside_a_task_group_is_reraised_as_is():
    inner = connector.PreflightError("panel heartbeat is invalid")
    stdio = _failing_stdio(ExceptionGroup("g", [ExceptionGroup("h", [inner])]))
    with pytest.raises(connector.PreflightError) as caught:
        asyncio.run(_client(stdio)._call_async("capability_heartbeat", {}))
    assert caught.value is inner


def test_mcp_tool_error_carries_the_tools_message():
    with pytest.raises(connector.MCPError, match="MCP tool failed: panel did not answer within 5 s"):
        connector.MCPStdioClient._result({"isError": True, "content": [{"type": "text", "text": "panel did not answer\n within 5 s"}]})


def test_mcp_tool_failure_reports_the_chained_root_cause_from_server_stderr():
    import tempfile

    log = tempfile.TemporaryFile("w+")
    log.write(
        "Traceback (most recent call last):\n  File \"bridge.py\", line 639\n"
        "keepframe.after_effects.bridge.BridgeBusy: one bridge command is already in flight\n\n"
        "The above exception was the direct cause of the following exception:\n\n"
        "Traceback (most recent call last):\n  File \"base.py\"\n"
        "mcp.server.mcpserver.exceptions.UnexpectedToolError: Error executing tool capability_heartbeat\n"
    )
    assert connector._stderr_tail(log) == "keepframe.after_effects.bridge.BridgeBusy: one bridge command is already in flight"
    log.close()
