import json, subprocess, sys, pytest
from unittest.mock import MagicMock, patch
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


def test_synth_compose_verify_cli(tmp_scene_dir):
    d = tmp_scene_dir / "s"
    assert run("synth", "--out", str(d), "--seed", "11").returncode == 0
    assert (d / "scene.json").exists()
    assert run("compose", "--scene", str(d / "scene.json"), "--out", str(d / "c.html")).returncode == 0
    r = run("verify", "--scene", str(d / "scene.json"))
    assert r.returncode == 0 and json.loads(r.stdout)["passed"] is True

@pytest.mark.browser
def test_m1_gate_small(tmp_scene_dir):
    res = m1_gate(tmp_scene_dir, n=3)
    assert res["passed"], res
