# tests/test_cli.py
import json, subprocess, sys, pytest
from refstudio.gates import m1_gate

def run(*args):
    return subprocess.run([sys.executable, "-m", "refstudio.cli", *args], capture_output=True, text=True)

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
