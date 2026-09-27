import json
import subprocess
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from keepframe.cli import main
from keepframe.gates import m1_gate

def run(*args):
    return subprocess.run([sys.executable, "-m", "keepframe.cli", *args], capture_output=True, text=True)

def test_serve_help_exposes_host():
    r = run("serve", "--help")
    assert r.returncode == 0 and "--host" in r.stdout
    assert "--admin" in r.stdout and "--no-admin" in r.stdout


def test_serve_defaults_admin_on(tmp_path):
    srv = MagicMock()
    with patch("keepframe.web.server.make_server", return_value=srv) as ms:
        assert main(["serve", "--workspace", str(tmp_path)]) == 0
    kwargs = ms.call_args.kwargs
    assert kwargs["admin"] is True
    assert kwargs["admin_svc"] is not None
    assert kwargs["admin_svc"]._workspace == tmp_path
    assert kwargs["admin_auth"] is not None
    srv.serve_forever.assert_called_once()


def test_serve_no_admin_skips_svc(tmp_path):
    srv = MagicMock()
    with patch("keepframe.web.server.make_server", return_value=srv) as ms:
        assert main(["serve", "--workspace", str(tmp_path), "--no-admin"]) == 0
    kwargs = ms.call_args.kwargs
    assert kwargs["admin"] is False
    assert "admin_svc" not in kwargs
    assert "admin_auth" not in kwargs


def test_serve_relay_configuration_is_all_or_nothing(tmp_path, monkeypatch):
    names = (
        "KEEPFRAME_AE_RELAY_URL",
        "KEEPFRAME_AE_RELAY_HOST",
        "KEEPFRAME_AE_RELAY_PORT",
        "KEEPFRAME_AE_RELAY_TOKEN",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_TOKEN", "secret")
    with pytest.raises(ValueError, match="all KEEPFRAME_AE_RELAY"):
        main(["serve", "--workspace", str(tmp_path), "--no-admin"])

    monkeypatch.setenv("KEEPFRAME_AE_RELAY_URL", "http://relay.example")
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_HOST", "127.0.0.1")
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_PORT", "8766")
    with pytest.raises(ValueError, match="HTTPS"):
        main(["serve", "--workspace", str(tmp_path), "--no-admin"])

    for invalid_url in (
        "https://relay.example:abc",
        "https://relay.example:99999",
        "https://relay.example:0",
    ):
        monkeypatch.setenv("KEEPFRAME_AE_RELAY_URL", invalid_url)
        with pytest.raises(ValueError, match="URL"):
            main(["serve", "--workspace", str(tmp_path), "--no-admin"])


def test_serve_starts_and_closes_authenticated_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_URL", "https://relay.example")
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_HOST", "127.0.0.1")
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_PORT", "8766")
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_TOKEN", "deployment-secret")
    private = MagicMock()
    relay = MagicMock()
    relay_stopped = threading.Event()
    relay.serve_forever.side_effect = lambda: relay_stopped.wait(1)
    relay.shutdown.side_effect = relay_stopped.set
    with (
        patch("keepframe.web.server.make_server", return_value=private) as make_private,
        patch("keepframe.after_effects.relay.make_relay_server", return_value=relay) as make_relay,
    ):
        assert main(["serve", "--workspace", str(tmp_path), "--no-admin"]) == 0

    assert make_private.call_args.kwargs["ae_relay_url"] == "https://relay.example"
    make_relay.assert_called_once_with(
        tmp_path,
        host="127.0.0.1",
        port=8766,
        deployment_token="deployment-secret",
    )
    relay.serve_forever.assert_called_once()
    relay.shutdown.assert_called_once()
    relay.server_close.assert_called_once()
    private.server_close.assert_called_once()


def test_serve_closes_both_listeners_when_startup_check_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_URL", "https://relay.example")
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_HOST", "127.0.0.1")
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_PORT", "8766")
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_TOKEN", "deployment-secret")
    private = MagicMock()
    relay = MagicMock()
    relay.serve_forever.side_effect = RuntimeError(
        "relay failed before private start"
    )
    with (
        patch("keepframe.web.server.make_server", return_value=private),
        patch(
            "keepframe.after_effects.relay.make_relay_server",
            return_value=relay,
        ),
        patch(
            "keepframe.analyze.device.gpu_status",
            side_effect=RuntimeError("GPU probe failed"),
        ),
        pytest.raises(RuntimeError, match="GPU probe failed"),
    ):
        main(["serve", "--workspace", str(tmp_path), "--no-admin"])

    private.server_close.assert_called_once()
    private.shutdown.assert_not_called()
    relay.shutdown.assert_called_once()
    relay.server_close.assert_called_once()
    assert not any(
        thread.name == "keepframe-ae-relay" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_serve_propagates_relay_thread_failure_and_stops_private_listener(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_URL", "https://relay.example")
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_HOST", "127.0.0.1")
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_PORT", "8766")
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_TOKEN", "deployment-secret")
    private = MagicMock()
    relay = MagicMock()
    private_stopped = threading.Event()
    private.serve_forever.side_effect = lambda: private_stopped.wait(1)
    private.shutdown.side_effect = private_stopped.set
    relay.serve_forever.side_effect = RuntimeError("relay failed")

    with (
        patch("keepframe.web.server.make_server", return_value=private),
        patch(
            "keepframe.after_effects.relay.make_relay_server",
            return_value=relay,
        ),
        pytest.raises(RuntimeError, match="relay failed"),
    ):
        main(["serve", "--workspace", str(tmp_path), "--no-admin"])

    private.shutdown.assert_called_once()
    private.server_close.assert_called_once()
    relay.shutdown.assert_called_once()
    relay.server_close.assert_called_once()

def test_synth_compose_verify_cli(tmp_scene_dir):
    d = tmp_scene_dir / "s"
    assert run("synth", "--out", str(d), "--seed", "11").returncode == 0
    assert (d / "scene.json").exists()
    assert run("compose", "--scene", str(d / "scene.json"), "--out", str(d / "c.html")).returncode == 0
    assert run("render", "--scene", str(d / "scene.json"), "--html", str(d / "c.html"), "--out", str(d / "render")).returncode == 0
    r = run("verify", "--scene", str(d / "scene.json"), "--render-json", str(d / "render" / "render.json"))
    assert r.returncode == 0 and json.loads(r.stdout)["passed"] is True

@pytest.mark.browser
def test_m1_gate_small(tmp_scene_dir):
    res = m1_gate(tmp_scene_dir, n=3)
    assert res["passed"], res
