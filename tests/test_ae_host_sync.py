"""Execute the production ES3 entry points against persisted fake AE projects."""
import copy
import math
import json
import re
import struct
from pathlib import Path

import pytest

from tests.ae_fake_runner import run_jsx
from keepframe.ae.spec import comp_spec, ease_to_ae, spec_asset_paths
from keepframe.ir.schema import FontGuess, Group
from tests.test_ae_spec import EASE, element, png, scene, track


HOST = Path(__file__).resolve().parents[1] / "extension/host/keepframe.jsx"


def spec_for(root, *elements, **kwargs):
    value = scene(*elements, **kwargs)
    spec = comp_spec(value, root, project="demo", scene_id="s1", version="v1")
    assets = {name: str(path) for name, path in spec_asset_paths(value, root).items()}
    return spec, assets


def solid_spec_with_x_keys(root, keys):
    spec, assets = spec_for(root)
    spec["layers"][0]["props"]["position_x"] = keys
    spec["comp"]["frames"] = keys[-1][0] + 1
    spec["layers"][0]["out"] = keys[-1][0]
    return spec, assets


def sync(state_path, spec, assets=None, force=False):
    result = run_jsx(state_path, HOST, "kfSync", json.dumps(spec, ensure_ascii=False),
                     json.dumps(assets or {}), str(force).lower())
    assert "value" in result, result
    return result


def read_state(path):
    return json.loads(path.read_text())


def save_state(path, state):
    path.write_text(json.dumps(state))


def test_render_writes_requested_frames(tmp_path, full_spec):
    spec, assets = full_spec
    state_path = tmp_path / "ae.json"
    sync(state_path, spec, assets)
    before = read_state(state_path)
    job = "j_" + "0" * 16
    folder = tmp_path / "temp" / ("keepframe-" + job)
    folder.mkdir(parents=True)
    (folder / "stale.png").write_bytes(b"stale")
    out = run_jsx(state_path, HOST, "kfRender", json.dumps({
        "job": job, "tag": spec["comp"]["tag"], "frames": [0, 5, 29]}), documents=tmp_path)
    assert out["value"]["ok"], out
    assert [f["frame"] for f in out["value"]["frames"]] == [0, 5, 29]
    assert (out["value"]["width"], out["value"]["height"]) == (spec["comp"]["width"], spec["comp"]["height"])
    for frame in out["value"]["frames"]:
        file = Path(frame["path"])
        assert file.is_absolute() and file.parent == folder
        assert file.name == f"frame_{frame['frame']:04d}.png"
        from PIL import Image
        with Image.open(file) as image:
            image.load()
            assert image.size == (spec["comp"]["width"], spec["comp"]["height"])
    assert not (folder / "stale.png").exists()
    assert out["writes"] == out["undo_groups"] == 0
    assert out["calls"]["saveFrameToPng"] == 3
    assert read_state(state_path) == before


def test_render_returns_paths_without_waiting_for_writes(tmp_path, full_spec):
    spec, assets = full_spec
    state_path = tmp_path / "ae.json"
    sync(state_path, spec, assets)
    state = read_state(state_path)
    state["testHooks"] = {"frameWriteFails": True}
    save_state(state_path, state)
    script = tmp_path / "no-wait.jsx"
    # Replace the global rather than the fake's read-only sleep member.
    script.write_text(HOST.read_text() + '\n$ = {sleep: function () { throw new Error("host must not wait"); }};\n')
    out = run_jsx(state_path, script, "kfRender", json.dumps({
        "job": "j_" + "0" * 16, "tag": spec["comp"]["tag"], "frames": [0, 5, 29]}), documents=tmp_path)
    assert out["value"]["ok"], out
    assert [f["frame"] for f in out["value"]["frames"]] == [0, 5, 29]
    assert all(not Path(f["path"]).exists() for f in out["value"]["frames"])
    assert out["calls"]["saveFrameToPng"] == 3
    assert out["writes"] == out["undo_groups"] == 0


@pytest.mark.parametrize("exporter", ["undefined", "null", "42"])
def test_render_requires_a_callable_frame_exporter(tmp_path, exporter):
    script = tmp_path / "unsupported-export.jsx"
    script.write_text(HOST.read_text() + """
CompItem = function () {};
var oldComp = new CompItem();
oldComp.comment = "keepframe:demo/s1";
oldComp.duration = 1; oldComp.frameRate = 30;
oldComp.saveFrameToPng = """ + exporter + ";\n" + """
app = {project: {items: {length: 1, 1: oldComp}}};
""")
    out = run_jsx(tmp_path / "ae.json", script, "kfRender", json.dumps({
        "job": "j_" + "0" * 16, "tag": "keepframe:demo/s1", "frames": [0]}), documents=tmp_path)
    assert out["value"]["ok"] is False, out
    assert out["value"]["error"] == "this After Effects cannot export frames; update to After Effects 24.1 or newer"
    assert out["calls"].get("saveFrameToPng", 0) == 0


def test_render_without_the_comp_says_send_first(tmp_path):
    out = run_jsx(tmp_path / "ae.json", HOST, "kfRender", json.dumps({
        "job": "j_" + "0" * 16, "tag": "keepframe:demo/s1", "frames": [0]}), documents=tmp_path)
    assert out["value"]["ok"] is False
    assert out["value"]["error"] == "the Keepframe comp is missing; send the scene to AE first"


@pytest.mark.parametrize("patch", [
    {"job": "../escape"}, {"tag": None}, {"frames": []}, {"frames": [0, 0]},
    {"frames": [-1]}, {"frames": [1.5]}, {"frames": [True]}, {"frames": ["0"]},
    {"frames": [1000000]}, {"frames": list(range(17))},
])
def test_render_rejects_invalid_requests_before_writing(tmp_path, patch):
    spec, assets = spec_for(tmp_path)
    state_path = tmp_path / "ae.json"
    sync(state_path, spec, assets)
    request = {"job": "j_" + "0" * 16, "tag": spec["comp"]["tag"], "frames": [0]}
    request.update(patch)
    out = run_jsx(state_path, HOST, "kfRender", json.dumps(request), documents=tmp_path)
    assert out["value"]["ok"] is False, out
    assert out["calls"].get("saveFrameToPng", 0) == 0
    assert not (tmp_path / "temp").exists()


def comp(state):
    return next(item for item in state["project"]["items"] if item["type"] == "CompItem")


def layers(state):
    return {item["comment"].split(";")[0][len("keepframe:"):]: item
            for item in comp(state)["layers"] if item["comment"].startswith("keepframe:")}


def prop(item, match):
    for child in item["properties"]:
        if child["matchName"] == match:
            return child
        if "properties" in child:
            found = prop(child, match)
            if found is not None:
                return found
    return None


def effects(item):
    return prop(item, "ADBE Effect Parade")["properties"]


@pytest.fixture
def full_spec(tmp_path):
    png(tmp_path / "image.png")
    model_json = b'{"asset":{"version":"2.0"}}'
    model_json += b" " * (-len(model_json) % 4)
    (tmp_path / "model.glb").write_bytes(
        struct.pack("<III", 0x46546C67, 2, 20 + len(model_json))
        + struct.pack("<II", len(model_json), 0x4E4F534A) + model_json)
    return spec_for(tmp_path,
        element("image", texture="image.png", anchor=(0.25, 0.75), tracks={
            "x": track((3, 10, EASE), (33, 110)),
            "sx": track((0, 1, EASE), (30, 3)), "sy": track((0, 2), (30, 4)),
            "opacity": track((0, 0.2, EASE), (30, 0.8)),
            "skx": track((0, 10), (30, 20)), "reveal": track((0, 0, EASE), (30, 1)),
        }),
        element("text", kind="text", text="Hello 한", anchor=(0.5, 0.75), color="#f80",
                font=FontGuess(family_guess="Example", size_px=20)),
        element("null", kind="group"),
        element("model", kind="3d", model="model.glb", tracks={
            "sx": track((0, 1, EASE), (30, 3)), "sy": track((0, 2), (30, 5)),
            "rx": track((0, 10), (30, 90)), "ry": track((0, -20)),
        }), groups=[Group(id="g", members=["image", "text"])])


def test_create_from_empty(tmp_path, full_spec):
    spec, assets = full_spec
    state_path = tmp_path / "ae.json"
    response = sync(state_path, spec, assets)
    result = response["value"]
    assert result["ok"] and result["applied"]
    assert result["created"] == [item["id"] for item in spec["layers"]]
    assert result["updated"] == result["deleted"] == []
    assert result["unchanged"] == 0
    assert response["undo_groups"] == 1
    state = read_state(state_path)
    items = state["project"]["items"]
    folders = [(i + 1, item) for i, item in enumerate(items) if item["type"] == "FolderItem"]
    assert [(item["name"], item["parentFolder"]) for _, item in folders] == [
        ("Keepframe", None), ("demo", folders[0][0])]
    project_folder = folders[1][0]
    composition = comp(state)
    assert composition["parentFolder"] == project_folder
    assert composition["comment"] == "keepframe:demo/s1"
    assert (composition["width"], composition["height"], composition["pixelAspect"],
            composition["frameRate"], composition["duration"]) == (320, 180, 1, 30, 2)
    assert composition["renderer"] == "ADBE Calder"
    for asset in spec["assets"]:
        footage = next(item for item in items if item["comment"] == "keepframe-asset:" + asset["sha256"])
        assert footage["name"] == asset["name"]
        assert footage["parentFolder"] == project_folder
        assert footage["mainSource"]["file"] == assets[asset["name"]]
    actual = layers(state)
    assert list(actual) == [item["id"] for item in reversed(spec["layers"])]
    for layer_spec in spec["layers"]:
        item = actual[layer_spec["id"]]
        assert re.fullmatch(r"keepframe:" + re.escape(layer_spec["id"]) + r";spec=[0-9a-f]{8};fp=[0-9a-f]{8}", item["comment"])
        assert item["name"] == layer_spec["name"]
        assert item["label"] == (layer_spec["label"] or 0)
        assert item["inPoint"] == layer_spec["in"] / 30
        assert item["outPoint"] == (layer_spec["out"] + 1) / 30
        assert prop(item, "ADBE Position")["dimensionsSeparated"] is True
    image = actual["kf:image"]
    x = prop(image, "ADBE Position_0")
    assert [(k["time"], k["value"], k["inInterpolation"], k["outInterpolation"]) for k in x["keys"]] == [
        (0.1, 10, "LINEAR", "BEZIER"), (1.1, 110, "BEZIER", "LINEAR")]
    assert x["keys"][0]["outEases"] == [{"speed": 200, "influence": 25}]
    assert x["keys"][1]["inEases"] == [{"speed": 200, "influence": 25}]
    scale = prop(image, "ADBE Scale")
    assert [k["value"] for k in scale["keys"]] == [[50, 100, 100], [150, 200, 100]]
    assert scale["keys"][0]["outEases"] == [{"speed": 200, "influence": 25}] * 3
    assert prop(image, "ADBE Anchor Point")["value"] == [5, 9, 0]
    assert prop(image, "ADBE Opacity")["keys"][0]["value"] == 20
    assert [effect["name"] for effect in effects(image)] == ["Keepframe Reveal", "Keepframe Skew"]
    assert prop(image, "ADBE Linear Wipe-0002")["value"] == 270
    assert prop(image, "ADBE Linear Wipe-0003")["value"] == 0
    assert [k["value"] for k in prop(image, "ADBE Linear Wipe-0001")["keys"]] == [100, 0]
    assert prop(image, "ADBE Geometry2-0001")["value"] == [5, 9]
    assert prop(image, "ADBE Geometry2-0002")["value"] == [5, 9]
    assert prop(image, "ADBE Geometry2-0006")["value"] == 0
    assert len(prop(image, "ADBE Geometry2-0005")["keys"]) == 2
    text = actual["kf:text"]
    document = prop(text, "ADBE Text Document")["value"]
    assert document == {"text": "Hello 한", "font": "Example", "fontSize": 20,
                        "fillColor": [1, struct.unpack("f", struct.pack("f", 136 / 255))[0], 0],
                        "applyFill": True, "justification": "LEFT_JUSTIFY"}
    assert prop(text, "ADBE Anchor Point")["value"] == [5, -4.5, 0]
    model = actual["kf:model"]
    assert model["threeDLayer"] is True
    assert prop(model, "ADBE Anchor Point")["value"] == [100, -100, 0]
    assert prop(model, "ADBE Position_2")["value"] == 0
    model_scale = prop(model, "ADBE Scale")
    assert [k["value"] for k in model_scale["keys"]] == [[2.7, 5.4, 2.7], [8.1, 13.5, 8.1]]
    assert model_scale["keys"][0]["outEases"] == [
        {"speed": 10.8, "influence": 25}, {"speed": 16.2, "influence": 25}, {"speed": 10.8, "influence": 25}]
    assert [k["value"] for k in prop(model, "ADBE Rotate X")["keys"]] == [10, 90]
    assert prop(model, "ADBE Rotate Y")["value"] == -20
    assert actual["kf:null"]["nullLayer"] is True
    solid = items[actual["kf:background"]["source"] - 1]
    assert solid["mainSource"]["color"] == [struct.unpack("f", struct.pack("f", v / 255))[0] for v in (170, 187, 204)]
    assert (solid["width"], solid["height"]) == (320, 180)
    assert result["keys"]["kf:image"] == 10
    assert result["keys"]["kf:model"] == 4
    assert result["warnings"] == ["null has no image; not drawn in AE"]
    assert result["ae_version"] == "24.6.0x45"


@pytest.mark.parametrize("fit_box,scale", [([10, 6], 2.7), ([4, 10], 1.8)])
def test_model_centres_off_origin_bounds_and_fits_ninety_percent(tmp_path, full_spec, fit_box, scale):
    spec, assets = full_spec
    spec["layers"] = [s for s in spec["layers"] if s["kind"] == "model"]
    spec["layers"][0]["source"]["fit_box"] = fit_box
    spec["layers"][0]["props"]["scale"] = [[0, [100, 100], None, None]]
    path = tmp_path / "ae.json"
    response = sync(path, spec, assets)
    assert response["value"]["ok"] and response["value"]["applied"], response
    before = read_state(path)
    model = layers(before)["kf:model"]
    assert prop(model, "ADBE Anchor Point")["value"] == [100, -100, 0]
    assert prop(model, "ADBE Scale")["value"] == pytest.approx([scale, scale, scale])
    response = sync(path, spec, assets)
    assert response["value"]["unchanged"] == 1 and response["writes"] == 0
    assert read_state(path) == before


def test_existing_model_gets_corrected_once_without_a_spec_change(tmp_path, full_spec):
    spec, assets = full_spec
    legacy = HOST.read_text().replace(
        '        if (s.kind === "model") { content.model_fit = "bounds-center-0.9"; }\n', "")
    legacy = legacy.replace(
        "            anchor = [rect.left + rect.width / 2, rect.top + rect.height / 2, 0];\n", "")
    legacy = legacy.replace("factor = 0.9 * Math.min(", "factor = Math.min(")
    legacy_host = tmp_path / "legacy.jsx"
    legacy_host.write_text(legacy)
    path = tmp_path / "ae.json"
    response = run_jsx(path, legacy_host, "kfSync", json.dumps(spec), json.dumps(assets), "false")
    assert response["value"]["ok"] and response["value"]["applied"], response
    before = layers(read_state(path))
    assert prop(before["kf:model"], "ADBE Anchor Point")["value"] == [5, 3, 0]
    response = sync(path, spec, assets)
    assert response["value"]["updated"] == ["kf:model"]
    after = layers(read_state(path))
    assert prop(after["kf:model"], "ADBE Anchor Point")["value"] == [100, -100, 0]
    assert prop(after["kf:model"], "ADBE Scale")["keys"][0]["value"] == [2.7, 5.4, 2.7]
    for eid in before:
        if eid != "kf:model":
            assert after[eid] == before[eid]
    assert sync(path, spec, assets)["writes"] == 0


def test_layers_synced_with_name_and_label_fingerprints_upgrade_without_a_hand_edit(tmp_path, full_spec):
    spec, assets = full_spec
    # Builds up to 1.0.238 fingerprinted name and label.
    legacy = HOST.read_text().replace(
        "return fingerprint(layer, fps, legacy, timings, styled === true);", "return fingerprint(layer, fps, true, timings, styled === true);")
    assert legacy != HOST.read_text()
    legacy_host = tmp_path / "legacy.jsx"
    legacy_host.write_text(legacy)
    path = tmp_path / "ae.json"
    response = run_jsx(path, legacy_host, "kfSync", json.dumps(spec), json.dumps(assets), "false")
    assert response["value"]["applied"], response
    before = layers(read_state(path))
    response = sync(path, spec, assets)
    assert response["value"]["applied"] and "hand_edited" not in response["value"], response
    assert response["value"]["unchanged"] == 5
    assert response["value"]["updated"] == response["value"]["created"] == response["value"]["deleted"] == []
    assert response["writes"] == 5  # Only migrate the comments, preserving all AE state.
    after = layers(read_state(path))
    for eid in before:
        assert after[eid]["comment"] != before[eid]["comment"]
        after[eid]["comment"] = before[eid]["comment"]
    assert after == before
    assert sync(path, spec, assets)["writes"] == 0


def test_layers_synced_with_v1_ease_fingerprints_upgrade_without_a_hand_edit(tmp_path):
    spec, assets = solid_spec_with_x_keys(tmp_path, [[f, 320.0 + f, None, None] for f in range(600)])
    # Run the producer with the original all-key-ease form (without name/label).
    legacy = HOST.read_text().replace(
        "return fingerprint(layer, fps, legacy, timings, styled === true);", 'return fingerprint(layer, fps, "ease", timings, styled === true);')
    legacy_host = tmp_path / "v1-ease.jsx"
    legacy_host.write_text(legacy)
    path = tmp_path / "ae.json"
    response = run_jsx(path, legacy_host, "kfSync", json.dumps(spec), json.dumps(assets), "false")
    assert response["value"]["applied"]
    assert response["calls"]["keyInTemporalEase"] == response["calls"]["keyOutTemporalEase"] == 600
    before = read_state(path)
    response = sync(path, spec, assets)
    assert response["value"]["applied"] and "hand_edited" not in response["value"], response
    assert response["value"]["unchanged"] == 1 and response["value"]["updated"] == []
    assert response["writes"] == 1
    after = read_state(path)
    assert layers(after)["kf:background"]["comment"] != layers(before)["kf:background"]["comment"]
    layers(after)["kf:background"]["comment"] = layers(before)["kf:background"]["comment"]
    assert after == before
    response = sync(path, spec, assets)
    assert response["writes"] == 0 and response["calls"]["keyValue"] == 600
    assert response["calls"].get("keyInTemporalEase", 0) == response["calls"].get("keyOutTemporalEase", 0) == 0


def test_sync_twice_is_a_no_op(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    before = read_state(path)
    response = sync(path, spec, assets)
    assert response["value"]["ok"] and response["value"]["applied"]
    assert response["writes"] == 0
    assert response["undo_groups"] in (0, 1)
    assert response["value"]["unchanged"] == 5
    assert response["value"]["created"] == response["value"]["updated"] == response["value"]["deleted"] == []
    assert read_state(path) == before


def test_linear_keys_are_written_in_one_call_per_property(tmp_path):
    keys = [[f, 320.0 + f, None, None] for f in range(600)]
    spec, assets = solid_spec_with_x_keys(tmp_path, keys)
    path = tmp_path / "ae.json"
    result = sync(path, spec, assets)
    assert result["value"]["applied"]
    calls = result["calls"]
    assert calls.get("setValuesAtTimes", 0) == 1
    assert calls.get("setValueAtTime", 0) == 0
    assert calls.get("setTemporalEaseAtKey", 0) == 0
    assert calls.get("setInterpolationTypeAtKey", 0) == 0
    written = prop(layers(read_state(path))["kf:background"], "ADBE Position_0")["keys"]
    assert [(k["time"], k["value"]) for k in written] == [(k[0] / 30, k[1]) for k in keys]
    assert all(k["inInterpolation"] == k["outInterpolation"] == "LINEAR" for k in written)
    assert all(k["inEases"] == k["outEases"] == [{"speed": 0, "influence": 16.666667}]
               for k in written)
    before = read_state(path)
    assert sync(path, spec, assets)["writes"] == 0
    assert read_state(path) == before


def test_linear_fingerprint_skips_ease_reads(tmp_path):
    spec, assets = solid_spec_with_x_keys(tmp_path, [[f, 320.0 + f, None, None] for f in range(600)])
    response = sync(tmp_path / "ae.json", spec, assets)
    assert response["value"]["applied"]
    assert response["calls"]["keyValue"] == 600
    assert response["calls"].get("keyInTemporalEase", 0) == 0
    assert response["calls"].get("keyOutTemporalEase", 0) == 0


def test_unchanged_resend_reads_each_key_once(tmp_path):
    spec, assets = solid_spec_with_x_keys(tmp_path, [[f, 320.0 + f, None, None] for f in range(600)])
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    response = sync(path, spec, assets)
    assert response["value"]["unchanged"] == 1 and response["writes"] == 0
    for method in ("keyTime", "keyValue", "keyInInterpolationType", "keyOutInterpolationType"):
        assert response["calls"][method] == 600
    assert response["calls"].get("keyInTemporalEase", 0) == 0
    assert response["calls"].get("keyOutTemporalEase", 0) == 0


def test_eased_keys_still_get_ease_and_bezier(tmp_path):
    ease = [0.25, 0.1, 0.25, 1.0]
    converted = ease_to_ae(ease, 0.0, 100.0, 12 / 30)
    keys = [[0, 0.0, [converted["out"]], None], [12, 100.0, None, [converted["in"]]],
            [24, 50.0, None, None]]
    spec, assets = solid_spec_with_x_keys(tmp_path, keys)
    path = tmp_path / "ae.json"
    result = sync(path, spec, assets)
    assert result["value"]["applied"]
    written = prop(layers(read_state(path))["kf:background"], "ADBE Position_0")["keys"]
    assert [(k["inInterpolation"], k["outInterpolation"]) for k in written] == [
        ("LINEAR", "BEZIER"), ("BEZIER", "LINEAR"), ("LINEAR", "LINEAR")]
    assert written[0]["outEases"] == [{"speed": 100, "influence": 25}]
    assert written[1]["inEases"] == [{"speed": 0, "influence": 75}]
    assert written[2]["inEases"] == written[2]["outEases"] == [{"speed": 0, "influence": 16.666667}]
    assert result["calls"].get("setValueAtTime", 0) == 0
    assert result["calls"]["setTemporalEaseAtKey"] == 2
    assert result["calls"]["setInterpolationTypeAtKey"] == 2
    assert result["calls"]["keyInTemporalEase"] == result["calls"]["keyOutTemporalEase"] == 2
    response = sync(path, spec, assets)
    assert response["writes"] == 0 and response["calls"]["keyValue"] == 3
    assert response["calls"]["keyInTemporalEase"] == response["calls"]["keyOutTemporalEase"] == 2


@pytest.mark.parametrize("change", ["eased_key_ease", "linear_key_to_bezier"])
def test_v2_fingerprint_detects_visible_key_hand_edits(tmp_path, change):
    keys = [[f, float(f), None, None] for f in range(600)]
    if change == "eased_key_ease":
        keys[0][2] = [ease_to_ae(EASE, 0, 1, 1 / 30)["out"]]
    spec, assets = solid_spec_with_x_keys(tmp_path, keys)
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    state = read_state(path)
    key = prop(layers(state)["kf:background"], "ADBE Position_0")["keys"][0]
    if change == "eased_key_ease":
        key["outEases"][0]["speed"] += 1
    else:
        key["outInterpolation"] = "BEZIER"
    save_state(path, state)
    response = sync(path, spec, assets)
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": ["kf:background"]}
    assert response["calls"]["keyInTemporalEase"] > 0 and response["calls"]["keyOutTemporalEase"] > 0
    assert response["writes"] == response["undo_groups"] == 0
    assert read_state(path) == state


def test_2d_sync_preserves_hidden_3d_properties_and_ignores_them_in_fingerprints(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    save_state(path, {"app": {"version": "26.5"}})
    response = sync(path, spec, assets)
    assert response["value"]["ok"] and response["value"]["applied"], response
    state = read_state(path)
    actual = layers(state)
    hidden = ("ADBE Position_2", "ADBE Rotate X", "ADBE Rotate Y", "ADBE Orientation")
    before = {}
    for eid in ("kf:background", "kf:image", "kf:text", "kf:null"):
        item = actual[eid]
        assert item["threeDLayer"] is False
        for match in hidden:
            p = prop(item, match)
            assert p["value"] == ([0, 0, 0] if match == "ADBE Orientation" else 0)
            assert p["keys"] == []
            value = [7, 8, 9] if match == "ADBE Orientation" else 7
            keys = copy.deepcopy(prop(actual["kf:model"], "ADBE Rotate X")["keys"])
            for key in keys:
                key["value"] = value
                for side in ("inEases", "outEases"):
                    key[side] *= 3 if match == "ADBE Orientation" else 1
            p.update(value=value, keys=keys, expression="value", expressionEnabled=True)
        before[eid] = [copy.deepcopy(prop(item, match)) for match in hidden]
    save_state(path, state)
    response = sync(path, spec, assets)
    assert response["value"]["ok"] and response["value"]["applied"], response
    assert response["value"]["unchanged"] == len(spec["layers"])
    assert response["writes"] == 0
    assert read_state(path) == state
    for layer_spec in spec["layers"]:
        if layer_spec["kind"] != "model":
            layer_spec["props"]["rotation"][0][1] += 15
    response = sync(path, spec, assets, force=True)
    assert response["value"]["ok"] and response["value"]["applied"], response
    assert set(response["value"]["updated"]) == set(before)
    actual = layers(read_state(path))
    for eid, properties in before.items():
        assert [prop(actual[eid], match) for match in hidden] == properties
    assert sync(path, spec, assets)["writes"] == 0


@pytest.mark.parametrize("match", ["ADBE Position_2", "ADBE Rotate X", "ADBE Rotate Y", "ADBE Orientation"])
def test_model_sync_manages_3d_properties_and_force_clears_them(tmp_path, full_spec, match):
    spec, assets = full_spec
    spec["layers"] = [s for s in spec["layers"] if s["kind"] == "model"]
    path = tmp_path / "ae.json"
    response = sync(path, spec, assets)
    assert response["value"]["ok"] and response["value"]["applied"], response
    state = read_state(path)
    model = layers(state)["kf:model"]
    keys = copy.deepcopy(prop(model, "ADBE Rotate X")["keys"])
    for key in keys:
        key["value"] = [7, 8, 9] if match == "ADBE Orientation" else 7
        for side in ("inEases", "outEases"):
            key[side] *= 3 if match == "ADBE Orientation" else 1
    prop(model, match).update(keys=keys, expression="value", expressionEnabled=True)
    save_state(path, state)
    response = sync(path, spec, assets)
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": ["kf:model"]}
    assert response["writes"] == 0
    response = sync(path, spec, assets, force=True)
    assert response["value"]["ok"] and response["value"]["updated"] == ["kf:model"], response
    model = layers(read_state(path))["kf:model"]
    assert model["threeDLayer"] is True
    for name, value in (("ADBE Position_2", 0), ("ADBE Rotate Y", -20), ("ADBE Orientation", [0, 0, 0])):
        assert prop(model, name)["keys"] == []
        assert prop(model, name)["value"] == value
    assert [k["value"] for k in prop(model, "ADBE Rotate X")["keys"]] == [10, 90]
    assert prop(model, match)["expressionEnabled"] is False
    assert sync(path, spec, assets)["writes"] == 0


@pytest.mark.parametrize("eid,expected,factor", [
    ("kf:image", [[50, 100, 100], [150, 200, 100]], 1),
    ("kf:model", [[2.7, 5.4, 2.7], [8.1, 13.5, 8.1]], 0.027),
])
@pytest.mark.parametrize("animated", [True, False])
def test_av_scale_pads_values_and_eases_and_resyncs_without_writes(
        tmp_path, full_spec, eid, expected, factor, animated):
    spec, assets = full_spec
    spec["layers"] = [s for s in spec["layers"] if s["id"] == eid]
    keys = spec["layers"][0]["props"]["scale"]
    keys[0][2] = [[25, 200], [45, 300]]
    keys[1][3] = [[35, 400], [55, 500]]
    if not animated:
        spec["layers"][0]["props"]["scale"] = [keys[0]]
    path = tmp_path / "ae.json"
    response = sync(path, spec, assets)
    assert response["value"]["ok"] and response["value"]["applied"], response
    before = read_state(path)
    item = layers(before)[eid]
    assert item["threeDLayer"] is (eid == "kf:model")
    scale = prop(item, "ADBE Scale")
    assert scale["propertyValueType"] == "ThreeD"
    if animated:
        assert [k["value"] for k in scale["keys"]] == expected
        assert scale["keys"][0]["outEases"] == [
            {"speed": 200 * factor, "influence": 25},
            {"speed": 300 * factor, "influence": 45},
            {"speed": 200 * factor, "influence": 25}]
        assert scale["keys"][1]["inEases"] == [
            {"speed": 400 * factor, "influence": 35},
            {"speed": 500 * factor, "influence": 55},
            {"speed": 400 * factor, "influence": 35}]
    else:
        assert scale["keys"] == []
        assert scale["value"] == expected[0]
    anchor = prop(item, "ADBE Anchor Point")
    assert anchor["propertyValueType"] == "ThreeD_SPATIAL"
    assert anchor["value"] == ([100, -100, 0] if eid == "kf:model" else [*spec["layers"][0]["anchor"], 0])
    assert prop(item, "ADBE Position")["propertyValueType"] == "ThreeD_SPATIAL"
    assert prop(item, "ADBE Position_2")["value"] == 0
    response = sync(path, spec, assets)
    assert response["value"]["ok"] and response["value"]["applied"]
    assert "hand_edited" not in response["value"]
    assert response["value"]["unchanged"] == 1
    assert response["writes"] == 0
    assert read_state(path) == before


def test_changed_element_updates_only_that_layer(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    before = layers(read_state(path))
    changed = copy.deepcopy(spec)
    changed["layers"][2]["source"]["text"] = "Changed"
    changed["layers"][2]["source"]["font"]["postscript"] = "Example-Bold"
    changed["layers"][2]["props"]["position_y"] = [[0, 99, None, None]]
    result = sync(path, changed, assets)["value"]
    assert result["updated"] == ["kf:text"]
    assert result["unchanged"] == 4
    after = layers(read_state(path))
    for eid in before:
        if eid != "kf:text":
            assert after[eid] == before[eid]
    assert prop(after["kf:text"], "ADBE Text Document")["value"]["font"] == "Example-Bold"
    assert prop(after["kf:text"], "ADBE Position_1")["value"] == 99
    assert sync(path, changed, assets)["writes"] == 0


@pytest.mark.parametrize("kind", ["text", "group", "3d"])
def test_kind_change_recreates(tmp_path, full_spec, kind):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    args = {"text": "Replacement", "font": FontGuess()} if kind == "text" else {"model": "model.glb"} if kind == "3d" else {}
    replacement, replacement_assets = spec_for(tmp_path, element("image", kind=kind, **args))
    changed = copy.deepcopy(spec)
    changed["layers"][1] = replacement["layers"][1]
    changed["assets"] += [a for a in replacement["assets"] if a not in changed["assets"]]
    response = sync(path, changed, {**assets, **replacement_assets})
    assert response["value"]["ok"]
    assert response["value"]["created"] == ["kf:image"]
    assert response["value"]["deleted"] == ["kf:image"]
    actual = layers(read_state(path))
    assert len(actual) == 5
    assert list(actual) == [s["id"] for s in reversed(changed["layers"])]
    item = actual["kf:image"]
    assert item["type"] == ("TextLayer" if kind == "text" else "AVLayer")
    assert item["nullLayer"] is (kind == "group")
    assert item["threeDLayer"] is (kind == "3d")


def test_removed_element_deletes_its_layer(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    before = layers(read_state(path))
    spec["layers"] = [s for s in spec["layers"] if s["id"] != "kf:image"]
    response = sync(path, spec, assets)
    assert response["value"]["deleted"] == ["kf:image"]
    assert response["value"]["unchanged"] == 4
    before.pop("kf:image")
    assert layers(read_state(path)) == before


def test_removal_renumbers_order_without_rewriting_survivors(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    before = layers(read_state(path))
    spec["layers"].pop(1)
    for order, item in enumerate(spec["layers"]):
        item["order"] = order
    response = sync(path, spec, assets)
    assert response["value"]["created"] == response["value"]["updated"] == []
    assert response["value"]["deleted"] == ["kf:image"]
    assert response["value"]["unchanged"] == 4
    assert response["writes"] == 1
    before.pop("kf:image")
    assert layers(read_state(path)) == before


def test_hand_edit_refuses_before_any_write_and_force_applies(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    state = read_state(path)
    prop(layers(state)["kf:image"], "ADBE Position_0")["keys"][0]["value"] = 999
    save_state(path, state)
    before = read_state(path)
    changed = copy.deepcopy(spec)
    changed["comp"]["name"] = "New comp name"
    changed["assets"][0]["sha256"] = "a" * 64
    response = sync(path, changed, assets)
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": ["kf:image"]}
    assert response["writes"] == response["undo_groups"] == 0
    assert read_state(path) == before
    response = sync(path, changed, assets, force=True)
    assert response["value"]["updated"] == ["kf:image"]
    assert prop(layers(read_state(path))["kf:image"], "ADBE Position_0")["keys"][0]["value"] == 10
    assert comp(read_state(path))["name"] == "New comp name"
    assert sync(path, changed, assets)["writes"] == 0


def test_untagged_layers_are_never_touched(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    state = read_state(path)
    user = copy.deepcopy(layers(state)["kf:text"])
    user.update(comment="My layer", name="User", label=8)
    prop(user, "ADBE Position_0")["value"] = 456
    comp(state)["layers"].insert(3, user)  # Between text and image.
    second = copy.deepcopy(user)
    second["name"] = "Second user"
    comp(state)["layers"].insert(4, second)
    save_state(path, state)

    def assert_users():
        stack = comp(read_state(path))["layers"]
        index = next(i for i, item in enumerate(stack) if item["name"] == "User")
        assert stack[index:index + 2] == [user, second]
        assert stack[index - 1]["comment"].startswith("keepframe:kf:text;")
        assert stack[index + 2]["comment"].startswith("keepframe:kf:image;")

    assert sync(path, spec, assets)["writes"] == 0
    extra, _ = spec_for(tmp_path, element("new", kind="text", text="New", font=FontGuess()))
    extra["layers"][1]["order"] = 5
    spec["layers"].append(extra["layers"][1])
    sync(path, spec, assets)
    assert_users()
    spec["layers"][2]["source"]["text"] = "Updated"
    sync(path, spec, assets)
    assert_users()
    replacement, _ = spec_for(tmp_path, element("image", kind="text", text="Recreated", font=FontGuess()))
    spec["layers"][1] = replacement["layers"][1]
    sync(path, spec, assets)
    assert_users()
    spec["layers"] = [s for s in spec["layers"] if s["id"] not in ("kf:model", "kf:new")]
    sync(path, spec, assets)
    assert_users()


@pytest.mark.parametrize("extra", ["effect", "mask"])
@pytest.mark.parametrize("action", ["update", "delete", "recreate"])
def test_user_extras_survive_updates_and_protect_destructive_changes(tmp_path, full_spec, extra, action):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    state = read_state(path)
    item = layers(state)["kf:image"]
    if extra == "effect":
        added = copy.deepcopy(effects(item)[0])
        added["name"] = "User reveal"
        effects(item).append(added)
    else:
        added = {"matchName": "ADBE Mask Atom", "name": "User mask", "properties": []}
        prop(item, "ADBE Mask Parade")["properties"].append(added)
    save_state(path, state)
    before = read_state(path)
    if action == "update":
        spec["layers"][1]["props"]["rotation"] = [[0, 30, None, None]]
        spec["layers"][1]["effects"] = {"reveal": None, "skew": None}
    elif action == "delete":
        spec["layers"].pop(1)
    else:
        replacement, _ = spec_for(tmp_path, element("image", kind="text", text="Recreated", font=FontGuess()))
        spec["layers"][1] = replacement["layers"][1]
    response = sync(path, spec, assets)
    if action == "update":
        assert response["value"]["updated"] == ["kf:image"]
        item = layers(read_state(path))["kf:image"]
        assert (effects(item) if extra == "effect" else prop(item, "ADBE Mask Parade")["properties"]) == [added]
        assert sync(path, spec, assets)["writes"] == 0
    else:
        assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": ["kf:image"]}
        assert response["writes"] == response["undo_groups"] == 0
        assert read_state(path) == before
        assert sync(path, spec, assets, force=True)["value"]["applied"]
        actual = layers(read_state(path))
        if action == "delete":
            assert "kf:image" not in actual
        else:
            assert effects(actual["kf:image"]) == []
            assert prop(actual["kf:image"], "ADBE Mask Parade")["properties"] == []


@pytest.mark.parametrize("match", ["ADBE Opacity", "ADBE Position", "ADBE Linear Wipe-0001", "ADBE Geometry2-0003", "ADBE Text Document"])
def test_enabled_expression_is_a_hand_edit_and_force_disables_it(tmp_path, full_spec, match):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    state = read_state(path)
    eid = "kf:text" if match == "ADBE Text Document" else "kf:image"
    p = prop(layers(state)[eid], match)
    p.update(expression="value", expressionEnabled=True)
    save_state(path, state)
    response = sync(path, spec, assets)
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": [eid]}
    assert response["writes"] == response["undo_groups"] == 0
    assert sync(path, spec, assets, force=True)["value"]["updated"] == [eid]
    assert prop(layers(read_state(path))[eid], match)["expressionEnabled"] is False
    assert sync(path, spec, assets)["writes"] == 0


@pytest.mark.parametrize("change", ["time", "in_ease", "out_interpolation", "static", "inPoint", "outPoint", "text", "owned_effect", "name", "label"])
def test_fingerprint_covers_managed_state(tmp_path, full_spec, change):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    state = read_state(path)
    eid = "kf:text" if change == "text" else "kf:image"
    item = layers(state)[eid]
    x = prop(item, "ADBE Position_0")
    if change == "time":
        x["keys"][0]["time"] = 0.2
    elif change == "in_ease":
        x["keys"][1]["inEases"][0]["speed"] = 123
    elif change == "out_interpolation":
        x["keys"][0]["outInterpolation"] = "HOLD"
    elif change == "static":
        prop(item, "ADBE Anchor Point")["value"] = [99, 99, 0]
    elif change in ("inPoint", "outPoint"):
        item[change] += 0.1
    elif change == "label":
        item[change] += 1
    elif change == "name":
        item["name"] = "Hand renamed"
    elif change == "text":
        prop(item, "ADBE Text Document")["value"]["fillColor"] = [1, 0, 0]
    else:
        prop(item, "ADBE Linear Wipe-0002")["value"] = 90
    save_state(path, state)
    response = sync(path, spec, assets)
    if change in ("name", "label"):
        assert response["value"]["applied"]
        assert read_state(path) == state
    else:
        assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": [eid]}
    assert response["writes"] == 0


def test_missing_asset_is_reported_before_changes(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    response = sync(path, spec)
    assert response["value"] == {"ok": False, "error": "asset image.png was not downloaded", "line": 0}
    assert response["writes"] == response["undo_groups"] == 0
    assert read_state(path)["project"]["items"] == []
    sync(path, spec, assets)
    before = read_state(path)
    spec["comp"]["name"] = "Changed"
    assert sync(path, spec)["writes"] == 0
    assert read_state(path) == before


@pytest.mark.parametrize("saved", [False, True])
def test_info_maps_fonts_and_project_with_no_builtin_json(tmp_path, saved):
    path = tmp_path / "ae.json"
    save_state(path, {"app": {"version": "25.4", "fonts": [
        [{"familyName": "Example", "styleName": "Regular", "postScriptName": "Example-Regular"},
         {"familyName": "Example", "styleName": "Bold", "postScriptName": "Example-Bold"}],
        [{"familyName": "한글", "styleName": "Regular", "postScriptName": "Hangul"}]]},
        "project": {"file": str(tmp_path / "Demo.aep") if saved else None}})
    response = run_jsx(path, HOST, "kfInfo")
    assert response["value"] == {"ok": True, "host_build": "dev", "ae_version": "25.4", "project_name": "Demo.aep" if saved else None,
        "project_saved": saved, "fonts": [
            {"family": "Example", "style": "Regular", "postscript": "Example-Regular"},
            {"family": "Example", "style": "Bold", "postscript": "Example-Bold"},
            {"family": "한글", "style": "Regular", "postscript": "Hangul"}]}
    assert response["writes"] == response["undo_groups"] == 0
    assert not re.search(r"\b(?:const|let)\b|=>|`", HOST.read_text())


def test_errors_are_json_with_extendscript_line(tmp_path):
    script = tmp_path / "throw.jsx"
    script.write_text(HOST.read_text() + '\nvar originalApp = app; app = {version: originalApp.version, '
        'project: originalApp.project, beginUndoGroup: function () { var e = new Error("injected failure"); '
        'e.line = 321; throw e; }};\n')
    spec, _ = spec_for(tmp_path)
    response = run_jsx(tmp_path / "ae.json", script, "kfSync", json.dumps(spec), "{}", "false")
    assert response["value"] == {"ok": False, "error": "injected failure", "line": 321}
    assert response["writes"] == 0


def test_error_during_apply_closes_the_undo_group(tmp_path, full_spec):
    script = tmp_path / "throw-import.jsx"
    script.write_text(HOST.read_text() + '\nvar originalApp = app, ended = 0; app = {version: originalApp.version, '
        'project: {items: originalApp.project.items, file: null, importFile: function (options) { '
        'var e = new Error("import failed"); e.line = 77; throw e; }}, '
        'beginUndoGroup: function (name) { originalApp.beginUndoGroup(name); }, '
        'endUndoGroup: function () { ended += 1; originalApp.endUndoGroup(); }}; '
        'var originalSync = kfSync; kfSync = function (s, a, f) { var result = JSON.parse(originalSync(s, a, f)); '
        'result.ended = ended; return JSON.stringify(result); };\n')
    spec, assets = full_spec
    response = run_jsx(tmp_path / "ae.json", script, "kfSync", json.dumps(spec), json.dumps(assets), "false")
    assert response["value"] == {"ok": False, "error": "import failed", "line": 77, "ended": 1}
    assert response["undo_groups"] == 1


@pytest.mark.parametrize("payload", ['{"schema": app.project.items.addFolder("BAD")}', '{"x": (function(){return 1;}())}', 'null'])
def test_json_input_never_executes_code(tmp_path, payload):
    response = run_jsx(tmp_path / "ae.json", HOST, "kfSync", payload, "{}", "false")
    assert response["value"]["ok"] is False
    assert "error" in response["value"] and "line" in response["value"]
    assert response["writes"] == response["undo_groups"] == 0


def test_asset_hash_reuse_name_replacement_and_changed_layer_source(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    initial = read_state(path)
    footage_before = [i for i in initial["project"]["items"] if i["comment"].startswith("keepframe-asset:")]
    assert len(footage_before) == 2
    alias = {**spec["assets"][0], "name": "alias.png"}
    spec["assets"].append(alias)
    assets["alias.png"] = assets["image.png"]
    assert sync(path, spec, assets)["writes"] == 0
    # New bytes with the same exported name replace the existing owned footage.
    png(tmp_path / "changed.png", 40, 24)
    assets["image.png"] = str(tmp_path / "changed.png")
    spec["assets"][0]["sha256"] = "a" * 64
    spec["assets"].pop()  # Drop the alias to the previous hash.
    response = sync(path, spec, assets)
    assert response["value"]["unchanged"] == 5
    after = read_state(path)
    footage = [i for i in after["project"]["items"] if i["comment"].startswith("keepframe-asset:")]
    assert len(footage) == 2
    assert footage[0]["comment"] == "keepframe-asset:" + "a" * 64
    assert footage[0]["mainSource"]["file"] == assets["image.png"]
    assert sync(path, spec, assets)["writes"] == 0
    # A different exported source name/hash must replace the layer source, too.
    new_asset = {**spec["assets"][0], "name": "other.png", "sha256": "b" * 64}
    spec["assets"].append(new_asset)
    assets["other.png"] = assets["image.png"]
    spec["layers"][1]["source"]["asset"] = "other.png"
    response = sync(path, spec, assets)
    assert response["value"]["updated"] == ["kf:image"]
    state = read_state(path)
    item = state["project"]["items"][layers(state)["kf:image"]["source"] - 1]
    assert item["name"] == "other.png"
    assert sync(path, spec, assets)["writes"] == 0


def test_model_asset_change_reapplies_measured_fit_without_layer_spec_change(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    before = layers(read_state(path))
    next(a for a in spec["assets"] if a["name"] == "model.glb")["sha256"] = "d" * 64
    response = sync(path, spec, assets)
    assert response["value"]["updated"] == ["kf:model"]
    assert response["value"]["unchanged"] == 4
    after = layers(read_state(path))
    assert before["kf:model"]["comment"].split(";spec=")[1].split(";")[0] == after["kf:model"]["comment"].split(";spec=")[1].split(";")[0]
    for eid in before:
        if eid != "kf:model":
            assert after[eid] == before[eid]
    assert sync(path, spec, assets)["writes"] == 0


def test_untagged_project_items_are_not_claimed_by_name(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    state = read_state(path)
    # Put an unowned collision in the correct project folder and a comp with the same name at root.
    owned = next(i for i in state["project"]["items"] if i["name"] == "image.png")
    user = copy.deepcopy(owned)
    user["id"] = max(i["id"] for i in state["project"]["items"]) + 1
    user["comment"] = "User footage"
    user["mainSource"]["file"] = str(tmp_path / "user.png")
    owned["name"] = "old-image.png"
    state["project"]["items"].append(user)
    user_comp = copy.deepcopy(comp(state))
    user_comp["id"] = user["id"] + 1
    user_comp.update(comment="User comp", parentFolder=None, layers=[])
    state["project"]["items"].append(user_comp)
    save_state(path, state)
    spec["assets"][0]["sha256"] = "c" * 64
    sync(path, spec, assets)
    items = read_state(path)["project"]["items"]
    assert next(i for i in items if i["comment"] == "User footage") == user
    assert next(i for i in items if i["comment"] == "User comp") == user_comp
    assert any(i["comment"] == "keepframe-asset:" + "c" * 64 for i in items)


def test_solid_source_color_updates_without_destroying_layer_extras(tmp_path):
    path = tmp_path / "ae.json"
    spec, assets = spec_for(tmp_path)
    sync(path, spec, assets)
    state = read_state(path)
    item = layers(state)["kf:background"]
    mask = {"matchName": "ADBE Mask Atom", "name": "User mask", "properties": []}
    prop(item, "ADBE Mask Parade")["properties"].append(mask)
    save_state(path, state)
    old_source = item["source"]
    old_footage = copy.deepcopy(state["project"]["items"][old_source - 1])
    spec["layers"][0]["source"]["color"] = "#112233"
    response = sync(path, spec, assets)
    assert response["value"]["updated"] == ["kf:background"]
    state = read_state(path)
    item = layers(state)["kf:background"]
    assert prop(item, "ADBE Mask Parade")["properties"] == [mask]
    assert state["project"]["items"][old_source - 1] == old_footage
    source = state["project"]["items"][item["source"] - 1]
    assert source["mainSource"]["color"] == [struct.unpack("f", struct.pack("f", v / 255))[0] for v in (17, 34, 51)]
    assert source["parentFolder"] == comp(state)["parentFolder"]
    assert sync(path, spec, assets)["writes"] == 0


def test_order_is_applied_from_order_field_and_is_stable(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    spec["layers"].reverse()
    sync(path, spec, assets)
    assert list(layers(read_state(path))) == ["kf:model", "kf:null", "kf:text", "kf:image", "kf:background"]
    assert sync(path, spec, assets)["writes"] == 0
    next(s for s in spec["layers"] if s["id"] == "kf:image")["order"] = 99
    sync(path, spec, assets)
    assert list(layers(read_state(path))) == ["kf:image", "kf:model", "kf:null", "kf:text", "kf:background"]
    assert sync(path, spec, assets)["writes"] == 0


def test_comp_updates_only_different_settings(tmp_path):
    spec, assets = spec_for(tmp_path, element(kind="text", text="Text", font=FontGuess()))
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    spec["comp"].update(name="Renamed", frames=90)
    response = sync(path, spec, assets)
    assert response["writes"] == 2
    assert response["value"]["unchanged"] == 2
    assert comp(read_state(path))["duration"] == 3
    assert sync(path, spec, assets)["writes"] == 0


def test_fps_change_retimes_layers_even_when_layer_spec_hash_is_unchanged(tmp_path):
    value = scene(element(kind="text", text="Text", tracks={"x": track((0, 0), (30, 100))}, font=FontGuess()))
    spec = comp_spec(value, tmp_path, project="demo", scene_id="s1", version="v1")
    path = tmp_path / "ae.json"
    sync(path, spec)
    before = layers(read_state(path))
    value.fps = 15
    changed = comp_spec(value, tmp_path, project="demo", scene_id="s1", version="v2")
    assert changed["layers"] == spec["layers"]
    response = sync(path, changed)
    assert response["value"]["updated"] == ["kf:background", "kf:e1"]
    actual = layers(read_state(path))
    assert actual["kf:e1"]["inPoint"] == 2 / 15
    assert actual["kf:e1"]["outPoint"] == 31 / 15
    assert [k["time"] for k in prop(actual["kf:e1"], "ADBE Position_0")["keys"]] == [0, 2]
    assert before["kf:e1"]["comment"].split(";spec=")[1].split(";")[0] == actual["kf:e1"]["comment"].split(";spec=")[1].split(";")[0]
    assert sync(path, changed)["writes"] == 0


def test_final_identical_layers_retime_from_30_to_25_fps(tmp_path):
    spec, assets = solid_spec_with_x_keys(tmp_path, [[0, 0, None, None], [30, 100, None, None]])
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    original = copy.deepcopy(spec["layers"])
    spec["comp"]["fps"] = 25
    result = sync(path, spec, assets)
    assert spec["layers"] == original
    assert result["value"]["updated"] == ["kf:background"]
    assert result["value"]["unchanged"] == 0
    assert [key["time"] for key in prop(layers(read_state(path))["kf:background"], "ADBE Position_0")["keys"]] == [0, 1.2]
    assert sync(path, spec, assets)["writes"] == 0


def test_final_model_same_name_new_bytes_is_updated(tmp_path, full_spec):
    import hashlib

    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    original = copy.deepcopy(spec["layers"])
    model = Path(assets["model.glb"])
    model.write_bytes(model.read_bytes().replace(b'"2.0"', b'"2.1"'))
    next(asset for asset in spec["assets"] if asset["name"] == "model.glb")["sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
    result = sync(path, spec, assets)
    assert spec["layers"] == original
    assert result["value"]["updated"] == ["kf:model"]
    assert result["value"]["unchanged"] == 4
    assert sync(path, spec, assets)["writes"] == 0


def test_final_sync_timings_are_nonnegative_integers_and_noop_has_no_writes(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    for resend in (False, True):
        response = sync(path, spec, assets)
        timings = response["value"]["timings"]
        assert set(timings) == {"validate", "read", "hash", "assets", "write", "order", "total"}
        assert all(type(ms) is int and ms >= 0 for ms in timings.values())
        assert response["value"]["applied"] is True
        if resend:
            assert response["writes"] == 0
            assert response["value"]["unchanged"] == len(spec["layers"])


def test_final_sync_timings_do_not_charge_json_hashing_to_ae_reads(tmp_path):
    spec, assets = solid_spec_with_x_keys(tmp_path, [[0, 0, None, None], [30, 100, None, None]])
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    script = tmp_path / "clocked-host.jsx"
    script.write_text(HOST.read_text() + """
var clock = 0;
Date = function () { this.getTime = function () { return clock; }; };
var stringify = JSON.stringify;
JSON.stringify = function (value) { clock += 7; return stringify(value); };
""")
    response = run_jsx(path, script, "kfSync", json.dumps(spec), json.dumps(assets), "false")
    assert response["value"]["timings"] == {"validate": 0, "read": 0, "hash": 14,
                                            "assets": 0, "write": 0, "order": 0, "total": 14}
    assert response["calls"]["keyTime"] > 0
    assert response["writes"] == 0


@pytest.mark.parametrize("field,value", [("width", 0), ("width", 1.5), ("fps", 0), ("frames", -1)])
def test_invalid_comp_settings_fail_before_writes(tmp_path, field, value):
    spec, _ = spec_for(tmp_path)
    spec["comp"][field] = value
    response = sync(tmp_path / "ae.json", spec)
    assert response["value"]["ok"] is False
    assert response["writes"] == response["undo_groups"] == 0


def test_spec_tag_hashes_layer_content_and_warnings_are_combined(tmp_path):
    spec, assets = spec_for(tmp_path, element(kind="group"))
    spec["warnings"] = ["Comp warning"]
    path = tmp_path / "ae.json"
    result = sync(path, spec, assets)["value"]
    assert result["warnings"] == ["Comp warning", "e1 has no image; not drawn in AE"]
    for layer_spec in spec["layers"]:
        # Independent FNV-1a multiplication over JavaScript UTF-16 code units.
        # JSON.stringify writes integral doubles as integers (100.0 becomes 100).
        normalized = json.loads(json.dumps(layer_spec), parse_float=lambda v: int(float(v)) if float(v).is_integer() else float(v))
        for field in ("order", "name", "label"):
            normalized.pop(field)  # Ordering is independent; names/labels belong to the user.
        serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        units = serialized.encode("utf-16-le")
        expected = 2166136261
        for unit in struct.unpack("<" + "H" * (len(units) // 2), units):
            expected = ((expected ^ unit) * 16777619) & 0xFFFFFFFF
        assert f";spec={expected:08x};" in layers(read_state(path))[layer_spec["id"]]["comment"]


def stack_ids(state):
    return [item["comment"].split(";")[0].removeprefix("keepframe:")
            if item["comment"].startswith("keepframe:") else item["name"]
            for item in comp(state)["layers"]]


def user_copy(item, name="USER-TOP"):
    item = copy.deepcopy(item)
    item.update(comment="User layer", name=name)
    return item


@pytest.mark.parametrize("change", ["insert", "decrease", "swap", "ordered"])
def test_review_order_preserves_users_and_moves_only_outside_lis(tmp_path, change):
    spec, _ = spec_for(tmp_path, element("a", kind="text", text="A", font=FontGuess()),
                       element("b", kind="text", text="B", font=FontGuess()))
    path = tmp_path / "ae.json"
    assert sync(path, spec)["value"]["ok"]
    state = read_state(path)
    comp(state)["layers"].insert(0, user_copy(layers(state)["kf:a"]))
    comp(state)["layers"].insert(2, user_copy(layers(state)["kf:a"], "USER-MIDDLE"))
    save_state(path, state)
    if change == "insert":
        extra, _ = spec_for(tmp_path, element("c", kind="text", text="C", font=FontGuess()))
        extra["layers"][1]["order"] = 1.5
        spec["layers"].append(extra["layers"][1])
        expected = ["USER-TOP", "kf:b", "kf:c", "USER-MIDDLE", "kf:a", "kf:background"]
    elif change == "decrease":
        spec["layers"][2]["order"] = -1
        expected = ["USER-TOP", "USER-MIDDLE", "kf:a", "kf:background", "kf:b"]
    elif change == "swap":
        spec["layers"][1]["order"], spec["layers"][2]["order"] = 2, 1
        # Either length-2 LIS is legal; this implementation retains b/background.
        expected = ["USER-TOP", "kf:a", "kf:b", "USER-MIDDLE", "kf:background"]
    else:
        expected = stack_ids(state)
    response = sync(path, spec)
    assert response["value"]["ok"], response
    assert stack_ids(read_state(path)) == expected
    if change == "ordered":
        assert response["writes"] == 0
    # Repeating the new order must never move again.
    assert sync(path, spec)["writes"] == 0


@pytest.mark.parametrize("fps", [29.97, 23.976])
def test_review_fractional_fps_and_float32_colors_are_clean(tmp_path, full_spec, fps):
    spec, assets = full_spec
    spec["comp"]["fps"] = fps
    path = tmp_path / "ae.json"
    assert sync(path, spec, assets)["value"]["ok"]
    initial = read_state(path)
    assert comp(initial)["frameRate"] == struct.unpack("f", struct.pack("f", fps))[0]
    for force in (False, True, False):
        response = sync(path, spec, assets, force=force)
        assert response["value"]["applied"], response
        assert response["writes"] == 0
    # Force a rewrite, then read back using the same spec FPS.
    state = read_state(path)
    prop(layers(state)["kf:image"], "ADBE Anchor Point")["value"] = [99, 99, 0]
    save_state(path, state)
    assert sync(path, spec, assets, force=True)["value"]["updated"] == ["kf:image"]
    assert sync(path, spec, assets)["writes"] == 0


@pytest.mark.parametrize("edit", ["footage", "solid_color", "solid_size"])
def test_review_changed_source_is_a_hand_edit(tmp_path, full_spec, edit):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    state = read_state(path)
    eid = "kf:image" if edit == "footage" else "kf:background"
    item = layers(state)[eid]
    if edit == "footage":
        other = copy.deepcopy(state["project"]["items"][item["source"] - 1])
        other.pop("id", None)
        other.update(name="USER FOOTAGE", comment="User footage")
        state["project"]["items"].append(other)
        item["source"] = len(state["project"]["items"])
    elif edit == "solid_color":
        state["project"]["items"][item["source"] - 1]["mainSource"]["color"] = [0.1, 0.1, 0.1]
    else:
        state["project"]["items"][item["source"] - 1]["width"] = 100
    save_state(path, state)
    response = sync(path, spec, assets)
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": [eid]}
    assert response["writes"] == response["undo_groups"] == 0
    assert sync(path, spec, assets, force=True)["value"]["applied"]
    assert sync(path, spec, assets)["writes"] == 0


def test_review_sub_tolerance_ae_reads_do_not_cause_edits_or_new_solids(tmp_path):
    spec, _ = spec_for(tmp_path, element(kind="text", text="T", color="#112233", font=FontGuess()))
    path = tmp_path / "ae.json"
    sync(path, spec)
    state = read_state(path)
    prop(layers(state)["kf:e1"], "ADBE Opacity")["value"] += 0.00001
    solid = state["project"]["items"][layers(state)["kf:background"]["source"] - 1]
    solid["mainSource"]["color"][0] += 0.1 / 255
    comp(state)["duration"] += 0.01 / spec["comp"]["fps"]
    save_state(path, state)
    response = sync(path, spec)
    assert response["value"]["applied"], response
    assert response["writes"] == 0
    assert len(read_state(path)["project"]["items"]) == len(state["project"]["items"])


@pytest.mark.parametrize("existing", [False, True])
def test_review_model_renderer_unavailable_refuses_before_changes(tmp_path, full_spec, existing):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    if existing:
        assert sync(path, spec, assets)["value"]["ok"]
        state = read_state(path)
        comp(state)["renderers"] = ["ADBE Advanced 3d", "ADBE Ernst"]
        comp(state)["renderer"] = "ADBE Advanced 3d"
    else:
        state = {"app": {"version": "23.6"}}
    save_state(path, state)
    response = sync(path, spec, assets, force=True)
    assert response["value"]["ok"] is False
    assert response["value"]["error"] == "this After Effects has no Advanced 3D renderer (needed for 3D models)"
    assert response["writes"] == response["undo_groups"] == 0


def test_review_effect_compositing_groups_are_not_managed_properties(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    response = sync(path, spec, assets)
    assert response["value"]["ok"], response
    item = layers(read_state(path))["kf:image"]
    assert prop(item, "ADBE Effect Built In Params") is not None
    assert sync(path, spec, assets)["writes"] == 0


@pytest.mark.parametrize("relation", ["parent", "trackMatteLayer"])
@pytest.mark.parametrize("action", ["delete", "recreate"])
@pytest.mark.parametrize("tagged_dependent", [False, True])
def test_review_untagged_dependents_protect_destructive_changes(tmp_path, relation, action, tagged_dependent):
    spec, _ = spec_for(tmp_path, element("a", kind="text", text="A", font=FontGuess()),
                       *([element("b", kind="group")] if tagged_dependent else []))
    path = tmp_path / "ae.json"
    sync(path, spec)
    state = read_state(path)
    user = layers(state)["kf:b"] if tagged_dependent else user_copy(layers(state)["kf:a"])
    if not tagged_dependent:
        comp(state)["layers"].append(user)
    user[relation] = comp(state)["layers"].index(layers(state)["kf:a"]) + 1
    save_state(path, state)
    if action == "delete":
        spec["layers"].pop(1)
    else:
        replacement, _ = spec_for(tmp_path, element("a", kind="group"))
        spec["layers"][1] = replacement["layers"][1]
    response = sync(path, spec)
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": ["kf:a"]}
    assert response["writes"] == response["undo_groups"] == 0
    assert sync(path, spec, force=True)["value"]["applied"]


def test_review_partial_failure_tags_new_layer_and_retry_has_no_duplicate(tmp_path):
    spec, _ = spec_for(tmp_path, element("a", kind="text", text="A", font=FontGuess()))
    path = tmp_path / "ae.json"
    # Fail after layer creation, before final spec/fingerprint stamping.
    save_state(path, {"testHooks": {"writeProperty": "ADBE Anchor Point"}})
    response = sync(path, spec)
    assert response["value"]["ok"] is False
    state = read_state(path)
    assert comp(state)["layers"]
    assert all(item["comment"].startswith("keepframe:") for item in comp(state)["layers"])
    state.pop("testHooks")
    save_state(path, state)
    interrupted = stack_ids(state)
    response = sync(path, spec)
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": interrupted,
                                 "interrupted": interrupted}
    assert response["writes"] == response["undo_groups"] == 0
    response = sync(path, spec, force=True)
    assert response["value"]["ok"], response
    assert len(comp(read_state(path))["layers"]) == len(spec["layers"])
    assert sync(path, spec)["writes"] == 0


def test_review_missing_asset_file_refuses_before_writes(tmp_path, full_spec):
    spec, assets = full_spec
    Path(assets["model.glb"]).unlink()
    path = tmp_path / "ae.json"
    response = sync(path, spec, assets)
    assert response["value"]["ok"] is False
    assert "model.glb" in response["value"]["error"]
    assert response["writes"] == response["undo_groups"] == 0
    assert read_state(path)["project"]["items"] == []


def test_review_owned_effect_updates_preserve_user_order_and_enabled_state(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    assert sync(path, spec, assets)["value"]["ok"]
    state = read_state(path)
    owned = effects(layers(state)["kf:image"])
    user = copy.deepcopy(owned[0]); user["name"] = "User effect"
    owned.insert(1, user)
    save_state(path, state)
    spec["layers"][1]["effects"]["skew"]["axis"] = 90
    response = sync(path, spec, assets)
    assert response["value"]["updated"] == ["kf:image"]
    assert [e["name"] for e in effects(layers(read_state(path))["kf:image"])] == [
        "Keepframe Reveal", "User effect", "Keepframe Skew"]
    state = read_state(path)
    effects(layers(state)["kf:image"])[0]["enabled"] = False
    save_state(path, state)
    response = sync(path, spec, assets)
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": ["kf:image"]}
    assert response["writes"] == 0
    spec["layers"][1]["props"]["rotation"] = [[0, 15, None, None]]
    assert sync(path, spec, assets, force=True)["value"]["updated"] == ["kf:image"]
    assert effects(layers(read_state(path))["kf:image"])[0]["enabled"] is False
    assert sync(path, spec, assets)["writes"] == 0


def test_review_text_update_preserves_unmanaged_styling(tmp_path):
    spec, _ = spec_for(tmp_path, element(kind="text", text="T", font=FontGuess()))
    path = tmp_path / "ae.json"
    sync(path, spec)
    state = read_state(path)
    prop(layers(state)["kf:e1"], "ADBE Text Document")["value"].update(
        tracking=37, applyStroke=True, strokeColor=[1, 0, 0])
    save_state(path, state)
    spec["layers"][1]["source"]["text"] = "Updated"
    assert sync(path, spec)["value"]["updated"] == ["kf:e1"]
    doc = prop(layers(read_state(path))["kf:e1"], "ADBE Text Document")["value"]
    assert (doc["tracking"], doc["applyStroke"], doc["strokeColor"]) == (37, True, [1, 0, 0])


@pytest.mark.parametrize("force", [False, True])
def test_review_duplicate_tag_refuses_even_with_force(tmp_path, force):
    spec, _ = spec_for(tmp_path, element("a", kind="text", text="A", font=FontGuess()))
    path = tmp_path / "ae.json"
    sync(path, spec)
    state = read_state(path)
    comp(state)["layers"].insert(0, copy.deepcopy(layers(state)["kf:a"]))
    save_state(path, state)
    response = sync(path, spec, force=force)
    assert response["value"]["ok"] is False
    assert response["value"]["error"] == "two layers are tagged kf:a (a duplicated Keepframe layer). Delete the copy, or keep it by clearing its layer comment, then send again"
    assert response["writes"] == response["undo_groups"] == 0


def test_review_fingerprint_read_failure_is_hand_edit_and_force_does_not_throw(tmp_path):
    spec, _ = spec_for(tmp_path, element("a", kind="text", text="A", font=FontGuess()))
    path = tmp_path / "ae.json"
    sync(path, spec)
    state = read_state(path)
    state["testHooks"] = {"readProperty": "ADBE Text Document"}
    save_state(path, state)
    response = sync(path, spec)
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": ["kf:a"]}
    assert response["writes"] == response["undo_groups"] == 0
    # Transform reads can fail persistently without preventing a forced write.
    state["testHooks"] = {"readProperty": "ADBE Scale"}
    save_state(path, state)
    response = sync(path, spec, force=True)
    assert response["value"]["ok"] and response["value"]["applied"], response


def test_review_info_decodes_project_display_name(tmp_path):
    path = tmp_path / "ae.json"
    save_state(path, {"project": {"file": str(tmp_path / "한글 project.aep")}})
    assert run_jsx(path, HOST, "kfInfo")["value"]["project_name"] == "한글 project.aep"


def test_review_fresh_wrappers_do_not_create_folders_replace_sources_or_self_move(tmp_path):
    png(tmp_path / "image.png")
    spec, assets = spec_for(tmp_path, element("image", texture="image.png"))
    path = tmp_path / "ae.json"
    response = sync(path, spec, assets)
    assert response["value"]["ok"], response
    before = read_state(path)
    response = sync(path, spec, assets)
    assert response["value"]["ok"], response
    assert response["writes"] == 0
    assert len([i for i in read_state(path)["project"]["items"] if i["name"] == "Keepframe"]) == 1
    assert read_state(path) == before


def test_review_comp_resize_updates_the_solid_source_once(tmp_path):
    spec, _ = spec_for(tmp_path)
    path = tmp_path / "ae.json"
    sync(path, spec)
    spec["comp"].update(width=640, height=360)
    response = sync(path, spec)
    assert response["value"]["updated"] == ["kf:background"]
    state = read_state(path)
    source = state["project"]["items"][layers(state)["kf:background"]["source"] - 1]
    assert (source["width"], source["height"]) == (640, 360)
    assert sync(path, spec)["writes"] == 0


def test_fix2_ae_24_0_empty_project_supports_models(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    save_state(path, {"app": {"version": "24.0"}})
    response = sync(path, spec, assets)
    assert response["value"]["ok"] and response["value"]["applied"], response
    assert comp(read_state(path))["renderer"] == "ADBE Calder"
    assert sync(path, spec, assets)["writes"] == 0


@pytest.mark.parametrize("anchor_fraction", [(0, 0), (0.25, 0.75), (0.5, 0.5), (1, 1)])
def test_fix8_narrow_fallback_keeps_composer_box_left_and_vertical_centre(tmp_path, anchor_fraction):
    spec, _ = spec_for(tmp_path, element(kind="text", text="Title", anchor=anchor_fraction,
                                       font=FontGuess(family_guess="Example", size_px=20),
                                       tracks={"x": track((0, 200)), "y": track((0, 100))}))
    source = spec["layers"][1]["source"]
    source["box"] = [160, 80]
    path = tmp_path / "ae.json"
    assert sync(path, spec)["value"]["ok"]
    first = prop(layers(read_state(path))["kf:e1"], "ADBE Anchor Point")["value"]
    source["font"] = {"postscript": "ArialMT", "family": "Arial", "style": "Regular", "substituted": True}
    response = sync(path, spec)
    assert response["value"]["updated"] == ["kf:e1"]
    actual = layers(read_state(path))["kf:e1"]
    anchor = prop(actual, "ADBE Anchor Point")["value"]
    assert anchor == first == [anchor_fraction[0] * 160, -6 + (anchor_fraction[1] - 0.5) * 80, 0]
    assert 200 - anchor[0] == 200 - anchor_fraction[0] * 160
    assert 100 - anchor[1] - 16 + 10 == 100 + (0.5 - anchor_fraction[1]) * 80
    assert prop(actual, "ADBE Text Document")["value"]["justification"] == "LEFT_JUSTIFY"
    assert sync(path, spec)["writes"] == 0


def test_fix8_fontless_companions_visibility_updates_and_deletion(tmp_path):
    png(tmp_path / "glyphs.png")
    el = element(kind="text", text="Original", texture="glyphs.png", anchor=(0.25, 0.75),
                 tracks={"x": track((0, 10), (30, 100)), "reveal": track((0, 0), (30, 1)), "skx": track((0, 10))})
    spec, assets = spec_for(tmp_path, el, element("above", kind="group"))
    path = tmp_path / "ae.json"
    response = sync(path, spec, assets)
    assert response["value"]["ok"], response
    state = read_state(path)
    actual = layers(state)
    assert "kf:e1~text" in actual
    image, text = actual["kf:e1"], actual["kf:e1~text"]
    assert text["type"] == "TextLayer" and text["enabled"] is False and image["enabled"] is True
    assert stack_ids(state) == ["kf:above", "kf:e1~text", "kf:e1", "kf:background"]
    assert prop(text, "ADBE Anchor Point")["value"] == pytest.approx([2.5, 0.06, 0])
    assert prop(text, "ADBE Text Document")["value"]["text"] == "Original"
    assert [effect["name"] for effect in effects(text)] == [effect["name"] for effect in effects(image)]
    assert prop(text, "ADBE Position_0")["keys"] == prop(image, "ADBE Position_0")["keys"]
    text["enabled"], image["enabled"] = True, False
    save_state(path, state)
    for force in (False, True):
        response = sync(path, spec, assets, force=force)
        assert response["value"]["applied"] and "hand_edited" not in response["value"]
        assert response["writes"] == 0 and read_state(path) == state
    el.canonical.text = "Changed"
    changed, assets = spec_for(tmp_path, el, element("above", kind="group"))
    response = sync(path, changed, assets)
    assert response["value"]["updated"] == ["kf:e1", "kf:e1~text"]
    actual = layers(read_state(path))
    assert actual["kf:e1~text"]["enabled"] is True and actual["kf:e1"]["enabled"] is False
    assert prop(actual["kf:e1~text"], "ADBE Text Document")["value"]["text"] == "Changed"
    assert sync(path, changed, assets)["writes"] == 0
    removed, _ = spec_for(tmp_path, element("above", kind="group"))
    response = sync(path, removed)
    assert set(response["value"]["deleted"]) == {"kf:e1", "kf:e1~text"}
    assert set(layers(read_state(path))) == {"kf:background", "kf:above"}


def test_fix8_companion_added_to_existing_image_is_directly_above_it(tmp_path):
    png(tmp_path / "glyphs.png")
    spec, assets = spec_for(tmp_path, element(kind="text", text="Title", texture="glyphs.png"),
                            element("above", kind="group"))
    image_only = copy.deepcopy(spec)
    image_only["layers"] = [item for item in image_only["layers"] if item["id"] != "kf:e1~text"]
    path = tmp_path / "ae.json"
    assert sync(path, image_only, assets)["value"]["ok"]
    state = read_state(path)
    comp(state)["layers"].insert(1, user_copy(layers(state)["kf:e1"], "USER"))
    save_state(path, state)
    response = sync(path, spec, assets)
    assert response["value"]["created"] == ["kf:e1~text"]
    assert stack_ids(read_state(path)) == ["kf:above", "USER", "kf:e1~text", "kf:e1", "kf:background"]
    assert sync(path, spec, assets)["writes"] == 0


@pytest.mark.parametrize("box", [None, [], [10], [0, 6], [10, -1], ["10", 6]])
def test_fix8_invalid_text_box_refuses_before_writes(tmp_path, box):
    spec, _ = spec_for(tmp_path, element(kind="text", text="Title", font=FontGuess()))
    spec["layers"][1]["source"]["box"] = box
    response = sync(tmp_path / "ae.json", spec)
    assert response["value"]["ok"] is False
    assert response["writes"] == response["undo_groups"] == 0


def test_fix2_force_resets_owned_effect_unwritten_parameters(tmp_path, full_spec):
    spec, assets = full_spec
    path = tmp_path / "ae.json"
    assert sync(path, spec, assets)["value"]["ok"]
    state = read_state(path)
    skew = effects(layers(state)["kf:image"])[1]
    defaults = {p["matchName"]: copy.deepcopy(p["value"]) for p in skew["properties"]
                if p["matchName"] in ("ADBE Geometry2-0003", "ADBE Geometry2-0004",
                                      "ADBE Geometry2-0007", "ADBE Geometry2-0008", "ADBE Geometry2-0011")}
    for match in defaults:
        prop(skew, match)["value"] = 50
    save_state(path, state)
    response = sync(path, spec, assets)
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": ["kf:image"]}
    assert response["writes"] == response["undo_groups"] == 0
    response = sync(path, spec, assets, force=True)
    assert response["value"]["updated"] == ["kf:image"], response
    skew = effects(layers(read_state(path))["kf:image"])[1]
    assert {match: prop(skew, match)["value"] for match in defaults} == defaults
    assert sync(path, spec, assets)["writes"] == 0


def test_fix2_force_enables_user_disabled_text_fill(tmp_path):
    spec, _ = spec_for(tmp_path, element("a", kind="text", text="A", color="#112233", font=FontGuess()))
    path = tmp_path / "ae.json"
    assert sync(path, spec)["value"]["ok"]
    state = read_state(path)
    doc = prop(layers(state)["kf:a"], "ADBE Text Document")["value"]
    doc.update(applyFill=False, tracking=37, applyStroke=True, strokeColor=[1, 0, 0])
    save_state(path, state)
    response = sync(path, spec)
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": ["kf:a"]}
    assert response["writes"] == response["undo_groups"] == 0
    response = sync(path, spec, force=True)
    assert response["value"]["ok"] and response["value"]["updated"] == ["kf:a"], response
    doc = prop(layers(read_state(path))["kf:a"], "ADBE Text Document")["value"]
    assert doc["applyFill"] is True
    assert doc["fillColor"] == [struct.unpack("f", struct.pack("f", v / 255))[0] for v in (17, 34, 51)]
    assert (doc["tracking"], doc["applyStroke"], doc["strokeColor"]) == (37, True, [1, 0, 0])
    assert sync(path, spec)["writes"] == 0


@pytest.mark.parametrize("partial", ["id", "spec"])
def test_fix2_partial_tags_report_interrupted_ids(tmp_path, partial):
    spec, _ = spec_for(tmp_path, element("a", kind="group"))
    path = tmp_path / "ae.json"
    assert sync(path, spec)["value"]["ok"]
    state = read_state(path)
    for item in comp(state)["layers"]:
        item["comment"] = item["comment"].split(";fp=")[0] if partial == "spec" else item["comment"].split(";")[0]
    save_state(path, state)
    response = sync(path, spec)
    ids = ["kf:a", "kf:background"]
    assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": ids, "interrupted": ids}
    assert response["writes"] == response["undo_groups"] == 0
    assert sync(path, spec, force=True)["value"]["applied"]
    assert len(comp(read_state(path))["layers"]) == len(spec["layers"])
    assert sync(path, spec)["writes"] == 0


@pytest.mark.parametrize("relation", ["parent", "trackMatteLayer"])
@pytest.mark.parametrize("action", ["update", "delete", "recreate"])
def test_fix2_tagged_layer_user_relationships_are_extras(tmp_path, relation, action):
    spec, _ = spec_for(tmp_path, element("a", kind="group"))
    path = tmp_path / "ae.json"
    assert sync(path, spec)["value"]["ok"]
    state = read_state(path)
    user = user_copy(layers(state)["kf:a"], "USER")
    comp(state)["layers"].append(user)
    layers(state)["kf:a"][relation] = len(comp(state)["layers"])
    save_state(path, state)
    if action == "update":
        spec["layers"][1]["props"]["rotation"] = [[0, 30, None, None]]
    elif action == "delete":
        spec["layers"].pop(1)
    else:
        replacement, _ = spec_for(tmp_path, element("a", kind="text", text="A", font=FontGuess()))
        spec["layers"][1] = replacement["layers"][1]
    response = sync(path, spec)
    if action == "update":
        assert response["value"]["updated"] == ["kf:a"], response
        assert layers(read_state(path))["kf:a"][relation] == len(comp(read_state(path))["layers"])
    else:
        assert response["value"] == {"timings": response["value"]["timings"], "ok": True, "applied": False, "hand_edited": ["kf:a"]}
        assert response["writes"] == response["undo_groups"] == 0
        assert read_state(path) == state
        assert sync(path, spec, force=True)["value"]["applied"]
        if action == "recreate":
            assert relation not in layers(read_state(path))["kf:a"]
    assert comp(read_state(path))["layers"][-1] == user
    assert sync(path, spec)["writes"] == 0


@pytest.mark.parametrize("start", [10, 15, 30])
def test_final_timing_moves_in_past_old_out(tmp_path, start):
    path = tmp_path / "ae.json"
    spec, _ = spec_for(tmp_path, element("a", kind="group", visible=(0, 9)))
    assert sync(path, spec)["value"]["ok"]
    updated, _ = spec_for(tmp_path, element("a", kind="group", visible=(start, 45)))
    result = sync(path, updated)
    assert result["value"]["ok"], result
    item = layers(read_state(path))["kf:a"]
    assert item["inPoint"] == start / 30
    assert item["outPoint"] == 46 / 30
    assert sync(path, updated)["writes"] == 0


def test_final_names_labels_and_comp_folder_belong_to_user(tmp_path):
    path = tmp_path / "ae.json"
    spec, _ = spec_for(tmp_path, element("a", kind="group"))
    assert sync(path, spec)["value"]["ok"]
    state = read_state(path)
    item = layers(state)["kf:a"]
    assert item["name"] == spec["layers"][1]["name"]
    assert item["label"] == 0
    item.update(name="User's name", label=12)
    comp(state)["parentFolder"] = None  # Move the existing comp to the project root.
    save_state(path, state)
    result = sync(path, spec)
    assert result["value"]["applied"], result
    assert result["writes"] == 0
    updated, _ = spec_for(tmp_path, element("a", kind="group", tracks={"rot": track((0, 30))}))
    assert sync(path, updated)["value"]["updated"] == ["kf:a"]
    state = read_state(path)
    assert (layers(state)["kf:a"]["name"], layers(state)["kf:a"]["label"]) == ("User's name", 12)
    assert comp(state)["parentFolder"] is None


def test_final_existing_comp_stays_in_user_folder(tmp_path):
    path = tmp_path / "ae.json"
    spec, _ = spec_for(tmp_path, element("a", kind="group"))
    assert sync(path, spec)["value"]["ok"]
    state = read_state(path)
    comp(state)["parentFolder"] = None
    save_state(path, state)
    assert sync(path, spec)["writes"] == 0
    assert comp(read_state(path))["parentFolder"] is None


def test_final_info_omits_fonts_when_not_requested(tmp_path):
    path = tmp_path / "ae.json"
    save_state(path, {"app": {"fonts": [[{"familyName": "Example", "styleName": "Regular",
                                         "postScriptName": "Example-Regular"}]]}})
    with_fonts = run_jsx(path, HOST, "kfInfo", "true")["value"]
    assert with_fonts["fonts"]
    without_fonts = run_jsx(path, HOST, "kfInfo", "false")["value"]
    assert "fonts" not in without_fonts
    assert without_fonts == {key: value for key, value in with_fonts.items() if key != "fonts"}


# --- Task 14: gradients, footage and styled text --------------------------------------------------------------------

def effect_named(item, name):
    return next(effect for effect in effects(item) if effect["name"] == name)


def params(effect):
    return {child["matchName"].rsplit("-", 1)[-1]: child for child in effect["properties"] if "matchName" in child
            and "-" in child["matchName"]}


def f32(value):
    return struct.unpack("f", struct.pack("f", value))[0]


def styled_spec(root, end="#0000ff"):
    from keepframe.ir.schema import AlphaStop, Background, Fade, Gradient, GradientKey, GradientStop, TextEffect, TextStyle
    stops = lambda a, b: [GradientStop(offset=0, color=a), GradientStop(offset=1, color=b)]
    background = Background(kind="gradient", value="#808080", gradient_keys=[
        GradientKey(t=0, gradient=Gradient(kind="linear", angle=90, stops=stops("#ff0000", "#00ff00"))),
        GradientKey(t=30, gradient=Gradient(kind="linear", angle=180, stops=stops("#ff0000", end)))])
    style = TextStyle(tracking_em=0.1, shear_deg=6, fade=Fade(stops=[AlphaStop(offset=0, alpha=1), AlphaStop(offset=1, alpha=0.5)]),
                      fill=Gradient(kind="linear", angle=180, stops=stops("#ff3d00", "#ffd600")),
                      effects=[TextEffect(kind="stroke", color="#112233", width=2),
                               TextEffect(kind="shadow", color="#1a0a3a", opacity=0.7, dx=4, dy=5, blur=4),
                               TextEffect(kind="glow", color="#ffd27a", opacity=0.9, blur=3)])
    text = element("t", kind="text", text="Sale", color="#ffffff", style=style,
                   font=FontGuess(family_guess="Inter", weight=700, size_px=40))
    return spec_for(root, text, background=background)


def test_gradient_background_and_styled_text_create_then_resync_is_a_no_op(tmp_path):
    spec, assets = styled_spec(tmp_path)
    path = tmp_path / "ae.json"
    response = sync(path, spec, assets)
    result = response["value"]
    assert result["ok"] and result["created"] == ["kf:background", "kf:t"], result
    assert result["warnings"] == ["text t stroke takes the gradient fill in AE", "text fade not exported"]
    state = read_state(path)
    bg, text = layers(state)["kf:background"], layers(state)["kf:t"]
    assert [e["name"] for e in effects(bg)] == ["Keepframe Gradient"]
    ramp = params(effect_named(bg, "Keepframe Gradient"))
    assert [(k["time"], k["value"]) for k in ramp["0001"]["keys"]] == [(0, [0, 90]), (1, [160, 0])]
    assert [(k["time"], k["value"]) for k in ramp["0003"]["keys"]] == [(0, [320, 90]), (1, [160, 180])]
    assert [k["value"] for k in ramp["0002"]["keys"]] == [[1, 0, 0, 1], [1, 0, 0, 1]]
    assert [k["value"] for k in ramp["0004"]["keys"]] == [[0, 1, 0, 1], [0, 0, 1, 1]]
    assert ramp["0005"]["value"] == 1 and ramp["0005"]["keys"] == []

    assert [e["name"] for e in effects(text)] == [
        "Keepframe Fill", "Keepframe Fill Matte", "Keepframe Skew", "Keepframe Shadow 1", "Keepframe Shadow 2"]
    doc = prop(text, "ADBE Text Document")["value"]
    assert (doc["font"], doc["tracking"], doc["applyStroke"], doc["strokeWidth"], doc["strokeOverFill"]) == (
        "Inter", 100, True, 4, False)
    assert doc["strokeColor"] == [f32(v / 255) for v in (0x11, 0x22, 0x33)]
    anchor = prop(text, "ADBE Anchor Point")["value"][:2]
    assert anchor == [5, -12]   # fake "Sale" at 40 px: rect top -32, height 40; box 10x6 centred
    fill = params(effect_named(text, "Keepframe Fill"))
    assert (fill["0001"]["value"], fill["0003"]["value"], fill["0005"]["value"]) == ([5, -15], [5, -9], 1)
    assert fill["0002"]["value"] == pytest.approx([1, 61 / 255, 0, 1], abs=1e-4)
    matte = params(effect_named(text, "Keepframe Fill Matte"))
    stack = comp(state)["layers"]
    assert matte["0001"]["value"] == stack.index(text) + 1 and matte["0002"]["value"] == 4
    skew = params(effect_named(text, "Keepframe Skew"))
    assert skew["0005"]["value"] == pytest.approx(-6, abs=1e-3)
    assert skew["0001"]["value"] == [5, -12]
    assert skew["0002"]["value"] == pytest.approx([5 + 12 * math.tan(math.radians(6)), -12], abs=1e-3)
    shadow, glow = (params(effect_named(text, f"Keepframe Shadow {i}")) for i in (1, 2))
    assert shadow["0001"]["value"] == pytest.approx([26 / 255, 10 / 255, 58 / 255, 1], abs=1e-4)
    assert (shadow["0002"]["value"], shadow["0003"]["value"], shadow["0004"]["value"], shadow["0005"]["value"]) == \
        pytest.approx((0.7 * 255, math.degrees(math.atan2(4, -5)), math.hypot(4, 5), 4), abs=1e-3)
    assert (glow["0002"]["value"], glow["0004"]["value"], glow["0005"]["value"]) == pytest.approx((0.9 * 255, 0, 3))

    before = read_state(path)
    response = sync(path, spec, assets)
    assert response["writes"] == 0 and response["value"]["unchanged"] == 2, response
    assert read_state(path) == before


def test_stop_colour_change_updates_only_that_effect(tmp_path):
    spec, assets = styled_spec(tmp_path)
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    before = read_state(path)
    changed, _ = styled_spec(tmp_path, end="#ffff00")
    response = sync(path, changed, assets)
    assert response["value"]["updated"] == ["kf:background"] and response["value"]["unchanged"] == 1, response
    after = read_state(path)
    assert layers(after)["kf:t"] == layers(before)["kf:t"]
    def visible(effect):   # a keyed property's static value is not shown (AE keeps the last removed key's there)
        return {m: {k: v for k, v in p.items() if not (k == "value" and p["keys"])} for m, p in params(effect).items()}

    old, new = (visible(effect_named(layers(s)["kf:background"], "Keepframe Gradient")) for s in (before, after))
    assert [k["value"] for k in new["0004"]["keys"]] == [[0, 1, 0, 1], [1, 1, 0, 1]]
    assert {m: p for m, p in new.items() if m != "0004"} == {m: p for m, p in old.items() if m != "0004"}
    old_bg, new_bg = layers(before)["kf:background"], layers(after)["kf:background"]
    assert [p for p in new_bg["properties"] if p["matchName"] != "ADBE Effect Parade"] == \
        [p for p in old_bg["properties"] if p["matchName"] != "ADBE Effect Parade"]
    assert sync(path, changed, assets)["writes"] == 0


def test_styled_text_hand_edits_are_detected_and_unstyled_text_upgrades(tmp_path):
    spec, assets = styled_spec(tmp_path)
    plain = copy.deepcopy(spec)
    source = plain["layers"][1]["source"]
    del source["tracking"], source["stroke"]
    plain["layers"][1]["effects"] = {"skew": None, "reveal": None}
    path = tmp_path / "ae.json"
    sync(path, plain, assets)
    # A layer synced before the text was styled (or by an older host) takes the styled spec without a hand edit.
    response = sync(path, spec, assets)
    assert response["value"]["updated"] == ["kf:t"] and "hand_edited" not in response["value"], response
    assert sync(path, spec, assets)["writes"] == 0
    for edit in ("tracking", "strokeWidth", "shadow", "matte"):
        state = read_state(path)
        text = layers(state)["kf:t"]
        if edit == "shadow":
            params(effect_named(text, "Keepframe Shadow 1"))["0004"]["value"] = 50
        elif edit == "matte":
            params(effect_named(text, "Keepframe Fill Matte"))["0002"]["value"] = 1
        else:
            prop(text, "ADBE Text Document")["value"][edit] = 9
        save_state(path, state)
        response = sync(path, spec, assets)
        assert response["value"].get("hand_edited") == ["kf:t"], (edit, response)
        assert sync(path, spec, assets, force=True)["value"]["updated"] == ["kf:t"]
        assert sync(path, spec, assets)["writes"] == 0
    # Moving the layer keeps the self matte (AE references the layer, not its index).
    state = read_state(path)
    comp(state)["layers"].insert(0, user_copy(layers(state)["kf:background"], "USER"))
    save_state(path, state)
    assert sync(path, spec, assets)["writes"] == 0


def test_footage_layers_start_at_their_first_visible_frame(tmp_path):
    from tests.test_ae_spec import _video_scene
    value = _video_scene(tmp_path)
    spec = comp_spec(value, tmp_path, project="demo", scene_id="s1", version="v1")
    assets = {name: str(p) for name, p in spec_asset_paths(value, tmp_path).items()}
    path = tmp_path / "ae.json"
    result = sync(path, spec, assets)["value"]
    assert result["ok"] and result["created"] == ["kf:background", "kf:e1"], result
    state = read_state(path)
    items = state["project"]["items"]
    bg, sprite = layers(state)["kf:background"], layers(state)["kf:e1"]
    assert items[bg["source"] - 1]["mainSource"]["file"] == assets["background.mp4"]
    assert items[sprite["source"] - 1]["mainSource"]["file"] == assets["e1.mov"]
    assert items[sprite["source"] - 1]["duration"] == pytest.approx(4 / 30, abs=1e-3)
    assert items[sprite["source"] - 1]["mainSource"]["alphaMode"] == "STRAIGHT"
    assert "alphaMode" not in items[bg["source"] - 1]["mainSource"]
    assert (bg["startTime"], bg["inPoint"], bg["outPoint"]) == pytest.approx((0, 0, 6 / 30))
    assert (sprite["startTime"], sprite["inPoint"], sprite["outPoint"]) == pytest.approx((2 / 30, 2 / 30, 6 / 30))
    assert sync(path, spec, assets)["writes"] == 0
    # A retimed sprite slides its clip; a hand-moved clip is a hand edit.
    value.elements[0].visible = (3, 5)
    moved = comp_spec(value, tmp_path, project="demo", scene_id="s1", version="v1")
    assert sync(path, moved, assets)["value"]["updated"] == ["kf:e1"]
    sprite = layers(read_state(path))["kf:e1"]
    assert (sprite["startTime"], sprite["inPoint"], sprite["outPoint"]) == pytest.approx((0.1, 0.1, 0.2))
    state = read_state(path)
    layers(state)["kf:e1"]["startTime"] = 0.2
    save_state(path, state)
    assert sync(path, moved, assets)["value"]["hand_edited"] == ["kf:e1"]


def test_retimed_footage_plays_at_its_rate(tmp_path):
    """Final review: a speed edit's footage gets AE's time stretch (100 / rate); its span ends with the stretched
    clip; a stretch changed by hand is a hand edit."""
    from tests.test_ae_spec import _video_scene
    from keepframe.edit.retime import retime_scene
    value = _video_scene(tmp_path)
    plain = comp_spec(value, tmp_path, project="demo", scene_id="s1", version="v1")
    retime_scene(value, 2.0)
    spec = comp_spec(value, tmp_path, project="demo", scene_id="s1", version="v1")
    assets = {name: str(p) for name, p in spec_asset_paths(value, tmp_path).items()}
    path = tmp_path / "ae.json"
    assert sync(path, plain, assets)["value"]["ok"]
    assert "stretch" not in layers(read_state(path))["kf:e1"]                # unretimed footage: as before
    result = sync(path, spec, assets)["value"]
    assert result["ok"] and result["updated"] == ["kf:background", "kf:e1"], result
    bg, sprite = layers(read_state(path))["kf:background"], layers(read_state(path))["kf:e1"]
    assert bg["stretch"] == sprite["stretch"] == 50
    # 6 plate frames at twice the speed span 3 comp frames (the 4th, past the clip, holds no clip in AE)
    assert (bg["startTime"], bg["inPoint"], bg["outPoint"]) == pytest.approx((0, 0, 3 / 30))
    assert (sprite["startTime"], sprite["inPoint"], sprite["outPoint"]) == pytest.approx((1 / 30, 1 / 30, 3 / 30))
    assert sync(path, spec, assets)["writes"] == 0
    state = read_state(path)
    layers(state)["kf:e1"]["stretch"] = 80
    save_state(path, state)
    assert sync(path, spec, assets)["value"]["hand_edited"] == ["kf:e1"]


def test_postscript_preferred(tmp_path):
    fonts = [{"family": "Brand Wide", "style": "Bold", "postscript": "BrandWide-Bold"},
             {"family": "BrandWide AE", "style": "Book", "postscript": "BrandWide-Regular"}]
    el = element("t", kind="text", text="Sale", font=FontGuess(
        family_guess="Brand Wide", weight=700, source="uploaded", postscript="BrandWide-Regular"))
    spec = comp_spec(scene(el), tmp_path, project="demo", scene_id="s1", version="v1", fonts=fonts)
    assert layers_spec_font(spec) == {"postscript": "BrandWide-Regular", "family": "BrandWide AE", "style": "Book",
                                      "substituted": False}
    path = tmp_path / "ae.json"
    assert sync(path, spec)["value"]["ok"]
    assert prop(layers(read_state(path))["kf:t"], "ADBE Text Document")["value"]["font"] == "BrandWide-Regular"


def layers_spec_font(spec):
    return next(item for item in spec["layers"] if item["id"] == "kf:t")["source"]["font"]


def test_removing_a_text_style_clears_the_stroke_and_tracking_keepframe_set(tmp_path):
    spec, assets = styled_spec(tmp_path)
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    plain = copy.deepcopy(spec)
    source = plain["layers"][1]["source"]
    del source["tracking"], source["stroke"]
    plain["layers"][1]["effects"] = {"skew": None, "reveal": None}
    response = sync(path, plain, assets)
    assert response["value"]["updated"] == ["kf:t"] and "hand_edited" not in response["value"], response
    text = layers(read_state(path))["kf:t"]
    doc = prop(text, "ADBE Text Document")["value"]
    assert (doc["tracking"], doc["applyStroke"]) == (0, False)
    assert effects(text) == []
    assert sync(path, plain, assets)["writes"] == 0
