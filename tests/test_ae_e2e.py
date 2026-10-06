"""Real server, panel core and JSX, with only the After Effects host faked."""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from keepframe.ir.schema import Background, FontGuess, Scene
from keepframe.ir.store import init_project, load_scene, new_version, scene_dir
from tests.ae_fake_runner import run_jsx
from tests.test_ae_api import json_request
from tests.test_ae_host_sync import comp, effects, layers, prop, read_state
from tests.test_ae_spec import element, png, track
from tests.test_web_server import start


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def seed(workspace):
    root = workspace / "p1"
    directory = scene_dir(root, "s1")
    png(directory / "assets" / "sprite.png", 20, 12)
    png(directory / "assets" / "background.png", 320, 180)
    model = b'{"asset":{"version":"2.0"}}'
    model += b" " * (-len(model) % 4)
    (directory / "assets" / "model.glb").write_bytes(
        struct.pack("<III", 0x46546C67, 2, 20 + len(model))
        + struct.pack("<II", len(model), 0x4E4F534A) + model)
    value = Scene(id="s1", size=(320, 180), fps=30, frames=60,
        background=Background(kind="image", value="assets/background.png"), elements=[
            element("title", kind="text", text="Reveal 한", visible=(0, 59),
                    font=FontGuess(family_guess="Arial", size_px=20), tracks={
                        "x": track((0, 25), (30, 125)), "reveal": track((0, 0), (30, 1))}),
            element("sprite", texture="assets/sprite.png", visible=(0, 59),
                    tracks={"x": track((0, 10), (30, 110))}),
            element("model", kind="3d", model="assets/model.glb", visible=(0, 59),
                    tracks={"ry": track((0, 0), (30, 90))}),
        ])
    init_project(root, {"file": "synthetic.mp4"}, value)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("ae-e2e-server")
    seed(workspace)
    srv = start(workspace)
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def isolated_server(tmp_path):
    workspace = tmp_path / "server"
    seed(workspace)
    srv = start(workspace)
    # Drive the actual API sweep directly, with a controlled test clock.
    srv.ae_routes.close()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def run_ext(*args, env=None, exit_code=0):
    completed = subprocess.run([NODE, str(ROOT / "tests/ae_fake/extension_runner.js"), *map(str, args)],
        cwd=ROOT, capture_output=True, text=True, timeout=40, env=os.environ | (env or {}))
    assert completed.returncode == exit_code, (completed.stdout, completed.stderr)
    assert len(completed.stdout.splitlines()) == 1, completed.stdout
    payload = json.loads(completed.stdout)
    assert set(payload) >= {"results", "statuses", "writes", "undo_groups"}
    return payload


@contextmanager
def paired(server, directory):
    directory.mkdir(parents=True, exist_ok=True)
    ext = SimpleNamespace(server=server, state=directory / "ae.json",
                          documents=directory / "Documents", credentials=directory / "credentials.json")
    ext.run = partial(run_ext, "--server", f"http://127.0.0.1:{server.server_address[1]}",
                      "--state", ext.state, "--documents", ext.documents, "--credentials", ext.credentials)
    status, code = json_request(server, "POST", "/api/ae/codes", browser=True)
    assert status == 200
    ext.pairing = ext.run("--pair", code["code"], "--jobs", 0)
    ext.device = json.loads(ext.credentials.read_text())["deviceId"]
    try:
        yield ext
    finally:
        assert json_request(server, "DELETE", f"/api/ae/devices/{ext.device}", browser=True)[0] == 204


@pytest.fixture
def extension(server, tmp_path):
    with paired(server, tmp_path) as ext:
        yield ext


def send(ext, *, version="v1", force=False):
    status, sent = json_request(ext.server, "POST", "/api/ae/send", {
        "project": "p1", "scene": "s1", "device": ext.device, "version": version, "force": force,
    }, browser=True)
    assert status == 202
    assert sent["job"]["state"] == "queued"
    return sent["job"]


def browser_state(ext):
    status, value = json_request(ext.server, "GET", "/api/ae/state?project=p1&scene=s1", browser=True)
    assert status == 200
    return value


def sync(ext, **options):
    sent = send(ext, **options)
    output = ext.run("--jobs", 1)
    assert len(output["results"]) == 1
    posted = output["results"][0]
    assert posted["ok"] is True
    job = next(job for job in browser_state(ext)["jobs"] if job["id"] == sent["id"])
    assert job["state"] == "done" and job["error"] is None
    assert job["result"] == posted["result"]
    return output, job


def edit(ext, script):
    filename = ext.state.parent / "edit.jsx"
    filename.write_text("""
function edit() {
    var i, comp;
    for (i = 1; i <= app.project.items.length; i += 1) {
        if (app.project.items[i].comment === "keepframe:p1/s1") { comp = app.project.items[i]; break; }
    }
""" + script + '\n    return "edited";\n}\n')
    output = run_jsx(ext.state, filename, "edit", documents=ext.documents)
    assert output.get("result") == "edited", output
    assert output["writes"] > 0


def sweep(server, monkeypatch, *, threshold):
    routes = server.ae_routes
    original = routes.jobs.sweep
    waits = iter((False, True))
    with monkeypatch.context() as patch:
        patch.setattr(routes._stop, "wait", lambda seconds: next(waits))
        # Keep Jobs.sweep's real 60-second check; advance its clock to shorten the test window.
        patch.setattr(routes.jobs, "sweep", lambda seen: original(seen, now=time.time() + 60 - threshold))
        routes._sweep()


def test_pair_announces_real_fake_ae_info(extension):
    ext = extension
    status, value = json_request(ext.server, "GET", "/api/ae/devices", browser=True)
    assert status == 200
    assert len(value["devices"]) == 1
    device = value["devices"][0]
    assert device["id"] == ext.device and device["connected"] is True
    assert device["ae_version"] == read_state(ext.state)["app"]["version"] == "24.6.0x45"
    assert device["extension_version"] == "1.0.0"
    assert device["project_saved"] is False
    assert set(json.loads(ext.credentials.read_text())) == {"serverUrl", "deviceId", "token"}
    assert ext.credentials.stat().st_mode & 0o777 == 0o600
    assert ext.pairing["results"] == [] and ext.pairing["writes"] == 0


def test_sync_builds_comp_effects_model_and_downloaded_assets(extension):
    ext = extension
    output, job = sync(ext)
    expected = {"kf:background", "kf:title", "kf:sprite", "kf:model"}
    assert job["result"]["applied"] is True
    assert set(job["result"]["created"]) == expected
    assert job["result"]["updated"] == job["result"]["deleted"] == []
    assert output["writes"] > 0 and output["undo_groups"] == 1
    assert any(status["message"].startswith("Synced v1:") for status in output["statuses"])
    state = read_state(ext.state)
    composition = comp(state)
    assert composition["comment"] == "keepframe:p1/s1"
    actual = layers(state)
    assert len(composition["layers"]) == len(actual) == 4 and set(actual) == expected
    assert [effect["matchName"] for effect in effects(actual["kf:title"])] == ["ADBE Linear Wipe"]
    assert [key["value"] for key in prop(actual["kf:title"], "ADBE Linear Wipe-0001")["keys"]] == [100, 0]
    assert actual["kf:model"]["threeDLayer"] is True and composition["renderer"] == "ADBE Calder"
    items = state["project"]["items"]
    assert items[actual["kf:model"]["source"] - 1]["isModel"] is True
    assert items[actual["kf:background"]["source"] - 1]["name"] == "background.png"
    footage = [item for item in items if item["type"] == "FootageItem"]
    assert {item["name"] for item in footage} == {"background.png", "sprite.png", "model.glb"}
    assets = ext.documents / "Keepframe" / "p1" / "assets"
    assert len(list(assets.iterdir())) == 3  # No .part downloads left behind.
    source = scene_dir(ext.server.ae_routes.workspace / "p1", "s1") / "assets"
    for item in footage:
        digest = hashlib.sha256((source / item["name"]).read_bytes()).hexdigest()
        assert item["comment"] == "keepframe-asset:" + digest
        downloaded = assets / (digest + Path(item["name"]).suffix)
        assert downloaded.read_bytes() == (source / item["name"]).read_bytes()
        assert item["mainSource"]["file"] == str(downloaded)


def test_reveal_text_without_font_downloads_image_and_editable_companion(isolated_server, tmp_path):
    root = isolated_server.ae_routes.workspace / "p1"
    directory = scene_dir(root, "s1")
    glyphs = png(directory / "assets" / "title.png", 40, 24)
    value = load_scene(directory / "scene.v1.json")
    title = value.elements[0]
    title.canonical.font = None
    title.canonical.texture = "assets/title.png"
    title.tracks["skx"] = track((0, 10))
    version = new_version(root, "s1", value, "keep undetected font glyphs")
    with paired(isolated_server, tmp_path / "panel") as ext:
        _, job = sync(ext, version=version.id)
        assert job["result"]["applied"] is True
        assert "text title kept as an image (no font detected)" in job["result"]["warnings"]
        state = read_state(ext.state)
        actual = layers(state)["kf:title"]
        assert actual["type"] == "AVLayer" and actual["nullLayer"] is False
        assert [effect["name"] for effect in effects(actual)] == ["Keepframe Reveal", "Keepframe Skew"]
        assert [key["value"] for key in prop(actual, "ADBE Linear Wipe-0001")["keys"]] == [100, 0]
        assert prop(actual, "ADBE Linear Wipe-0002")["value"] == 270
        assert prop(actual, "ADBE Anchor Point")["value"] == [20, 12, 0]
        assert prop(actual, "ADBE Scale")["value"] == [25, 25, 100]
        footage = state["project"]["items"][actual["source"] - 1]
        assert footage["name"] == "title.png"
        assert Path(footage["mainSource"]["file"]).read_bytes() == glyphs
        editable = layers(state)["kf:title~text"]
        assert editable["type"] == "TextLayer" and editable["enabled"] is False
        assert actual["enabled"] is True
        stack = comp(state)["layers"]
        assert stack.index(editable) + 1 == stack.index(actual)
        doc = prop(editable, "ADBE Text Document")["value"]
        assert (doc["text"], doc["fontSize"], doc["fillColor"]) == ("Reveal 한", 4.8, [1, 0, 0])
        assert prop(editable, "ADBE Position_0")["keys"] == prop(actual, "ADBE Position_0")["keys"]
        edit(ext, """
    for (i = 1; i <= comp.layers.length; i += 1) {
        if (comp.layers[i].comment.indexOf("keepframe:kf:title~text;") === 0) { comp.layers[i].enabled = true; }
        if (comp.layers[i].comment.indexOf("keepframe:kf:title;") === 0) { comp.layers[i].enabled = false; }
    }
""")
        before = ext.state.read_bytes()
        output, job = sync(ext, version=version.id)
        assert job["result"]["unchanged"] == 5 and output["writes"] == 0
        assert "hand_edited" not in job["result"]
        assert ext.state.read_bytes() == before
        title.canonical.text = "Changed 한"
        changed = new_version(root, "s1", value, "update fontless text")
        _, job = sync(ext, version=changed.id)
        assert job["result"]["updated"] == ["kf:title", "kf:title~text"]
        actual = layers(read_state(ext.state))
        assert actual["kf:title"]["enabled"] is False and actual["kf:title~text"]["enabled"] is True
        assert prop(actual["kf:title~text"], "ADBE Text Document")["value"]["text"] == "Changed 한"


def test_resend_same_version_is_zero_write_noop(extension):
    sync(extension)
    before = extension.state.read_bytes()
    output, job = sync(extension)
    result = job["result"]
    assert result["applied"] is True and result["unchanged"] == 4
    assert result["created"] == result["updated"] == result["deleted"] == []
    assert output["writes"] == 0
    assert extension.state.read_bytes() == before


def test_hand_edit_warns_without_writes_and_force_restores(extension):
    ext = extension
    sync(ext)
    original = read_state(ext.state)
    edit(ext, """
    for (i = 1; i <= comp.layers.length; i += 1) {
        if (comp.layers[i].comment.indexOf("keepframe:kf:title;") === 0) {
            comp.layers[i].property("ADBE Transform Group").property("ADBE Position_0").setValueAtTime(0, 999);
        }
    }
""")
    edited = ext.state.read_bytes()
    assert prop(layers(read_state(ext.state))["kf:title"], "ADBE Position_0")["keys"][0]["value"] == 999
    output, job = sync(ext)
    assert job["result"] == {"ok": True, "applied": False, "hand_edited": ["kf:title"]}
    assert output["writes"] == output["undo_groups"] == 0
    assert ext.state.read_bytes() == edited
    assert browser_state(ext)["jobs"][0]["result"]["hand_edited"] == ["kf:title"]
    assert any("edited by hand" in status["message"] for status in output["statuses"])
    output, job = sync(ext, force=True)
    assert job["result"]["applied"] is True and job["result"]["updated"] == ["kf:title"]
    restored = layers(read_state(ext.state))
    assert prop(restored["kf:title"], "ADBE Position_0")["keys"] == prop(
        layers(original)["kf:title"], "ADBE Position_0")["keys"]
    assert restored["kf:title"]["comment"] == layers(original)["kf:title"]["comment"]
    for eid in ("kf:background", "kf:sprite", "kf:model"):
        assert restored[eid] == layers(original)[eid]


def test_new_version_updates_and_deletes_only_its_layers(extension):
    ext = extension
    sync(ext)
    edit(ext, """
    var user = comp.layers.addText("User annotation");
    user.name = "My layer";
    user.comment = "user-owned";
    user.property("ADBE Transform Group").property("ADBE Position").setValue([77, 88]);
    user.moveToEnd();
""")
    before = read_state(ext.state)
    root = ext.server.ae_routes.workspace / "p1"
    value = load_scene(scene_dir(root, "s1") / "scene.v1.json")
    value.elements[0].tracks["x"] = track((0, 75), (30, 175))
    value.elements = [element for element in value.elements if element.id != "sprite"]
    version = new_version(root, "s1", value, "move title and remove sprite")
    output, job = sync(ext, version=version.id)
    result = job["result"]
    assert result["applied"] is True
    assert result["created"] == [] and result["updated"] == ["kf:title"] and result["deleted"] == ["kf:sprite"]
    assert result["unchanged"] == 2
    after = read_state(ext.state)
    assert set(layers(after)) == {"kf:background", "kf:title", "kf:model"}
    for eid in ("kf:background", "kf:model"):
        assert layers(after)[eid] == layers(before)[eid]
    assert prop(layers(after)["kf:title"], "ADBE Position_0")["keys"][0]["value"] == 75
    assert comp(after)["layers"][-1] == comp(before)["layers"][-1]
    assert comp(after)["layers"][-1]["comment"] == "user-owned"
    assert browser_state(ext)["last_synced"][ext.device] == version.id


def test_disconnect_after_claim_fails_via_api_sweep(isolated_server, tmp_path, monkeypatch):
    with paired(isolated_server, tmp_path / "panel") as ext:
        sent = send(ext)
        before = ext.state.read_bytes()
        output = ext.run("--jobs", 1, "--die-after-claim", exit_code=3)
        assert output["results"] == [] and output["writes"] == 0
        job = browser_state(ext)["jobs"][0]
        assert job["id"] == sent["id"] and job["state"] == "running"
        assert ext.state.read_bytes() == before
        sweep(isolated_server, monkeypatch, threshold=0)
        job = browser_state(ext)["jobs"][0]
        assert job["state"] == "failed" and job["error"] == "AE disconnected"
        assert job["result"] is None


def test_progress_keeps_slow_eval_alive_past_sweep_threshold(isolated_server, tmp_path, monkeypatch):
    routes = isolated_server.ae_routes
    beats = []
    original = routes._post

    def observe_progress(handler, url, device):
        original(handler, url, device)
        if url.path.endswith("/progress"):
            beats.append(time.monotonic())

    monkeypatch.setattr(routes, "_post", observe_progress)
    with paired(isolated_server, tmp_path / "panel") as ext:
        sent = send(ext)
        checked_past_threshold = False
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(ext.run, "--jobs", 1, "--timeout", 30,
                env={"KEEPFRAME_TEST_EVAL_DELAY_MS": "22000"})
            while not future.done():
                job = routes.jobs.get(sent["id"])
                if job.state == "running" and time.time() - job.started >= 18:
                    checked_past_threshold = True
                sweep(isolated_server, monkeypatch, threshold=18)
                assert routes.jobs.get(sent["id"]).state != "failed"
                time.sleep(0.1)
            output = future.result()
        assert checked_past_threshold
        assert len(beats) >= 2 and beats[1] - beats[0] >= 14
        assert output["results"][0]["result"]["applied"] is True and output["writes"] > 0
        job = next(job for job in browser_state(ext)["jobs"] if job["id"] == sent["id"])
        assert job["state"] == "done" and job["error"] is None
        assert browser_state(ext)["progress"] == {}
