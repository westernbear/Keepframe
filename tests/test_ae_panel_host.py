"""Run the real AE panel script in a Node ExtendScript stand-in (no JSON, like AE's ES3
engine) against the real Python bridge. The other AE tests only read the panel's text."""

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from keepframe.after_effects import connector, mcp_server
from keepframe.after_effects.bridge import Bridge

HOST = Path(__file__).with_name("ae_panel_host.js")
PANEL = Path(connector.__file__).with_name("assets") / "keepframe_panel.jsx"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@pytest.fixture
def panel(tmp_path):
    proc = subprocess.Popen(
        ["node", str(HOST), str(PANEL), str(tmp_path), "30"],
        stderr=subprocess.PIPE,
        text=True,
    )
    root = tmp_path / "Keepframe" / "ae-bridge"
    deadline = time.monotonic() + 10
    while not (root / "inflight").is_dir() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    yield root, proc
    proc.kill()
    _, errors = proc.communicate()
    assert "TASK ERROR" not in errors and "ALERT" not in errors, errors


def test_panel_answers_the_connector_preflight_heartbeat(panel, monkeypatch):
    root, _proc = panel
    monkeypatch.setattr(connector, "_is_windows", lambda: True)

    def heartbeat(_root):
        result = Bridge(root).dispatch("capability_heartbeat", {}, command_id="preflight-1", nonce="n1", timeout=10)
        assert result.ok, result.error
        raw = connector.Connector._normalize_mcp_result(mcp_server._result_value(result), "preflight-1", "n1")
        raw.setdefault("timestamp", time.time())
        return raw

    checked = connector.preflight(
        "http://localhost:8767",
        deployment_token="deployment-secret",
        private_root=root,
        ffmpeg="ffmpeg.exe",
        panel_heartbeat=heartbeat,
        which=lambda name: name,
        run=lambda argv, **kwargs: object(),
        sid_provider=lambda: "S-1-5-21-1234",
    )
    assert checked.ae_version.startswith("24.1")
    assert checked.capabilities.ready
