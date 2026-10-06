"""Execute the production ES3 entry points against persisted fake AE projects."""
import copy
import json
import re
import struct
from pathlib import Path

import pytest

from tests.ae_fake_runner import run_jsx
from keepframe.ae.spec import comp_spec, spec_asset_paths
from keepframe.ir.schema import FontGuess, Group
from tests.test_ae_spec import EASE, element, png, scene, track


HOST = Path(__file__).resolve().parents[1] / "extension/host/keepframe.jsx"


def spec_for(root, *elements, **kwargs):
    value = scene(*elements, **kwargs)
    spec = comp_spec(value, root, project="demo", scene_id="s1", version="v1")
    assets = {name: str(path) for name, path in spec_asset_paths(value, root).items()}
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
    assert composition["renderer"] == "ADBE Advanced 3d"
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
    assert [k["value"] for k in scale["keys"]] == [[50, 100], [150, 200]]
    assert scale["keys"][0]["outEases"] == [{"speed": 200, "influence": 25}] * 2
    assert prop(image, "ADBE Anchor Point")["value"] == [5, 9]
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
                        "fillColor": [1, 136 / 255, 0], "applyFill": True, "justification": "LEFT_JUSTIFY"}
    assert prop(text, "ADBE Anchor Point")["value"] == [42, -1]
    model = actual["kf:model"]
    assert model["threeDLayer"] is True
    assert prop(model, "ADBE Anchor Point")["value"] == [5, 3, 0]
    assert prop(model, "ADBE Position_2")["value"] == 0
    model_scale = prop(model, "ADBE Scale")
    assert [k["value"] for k in model_scale["keys"]] == [[3, 6, 3], [9, 15, 9]]
    assert model_scale["keys"][0]["outEases"] == [
        {"speed": 12, "influence": 25}, {"speed": 18, "influence": 25}, {"speed": 12, "influence": 25}]
    assert [k["value"] for k in prop(model, "ADBE Rotate X")["keys"]] == [10, 90]
    assert prop(model, "ADBE Rotate Y")["value"] == -20
    assert actual["kf:null"]["nullLayer"] is True
    solid = items[actual["kf:background"]["source"] - 1]
    assert solid["mainSource"]["color"] == [170 / 255, 187 / 255, 204 / 255]
    assert (solid["width"], solid["height"]) == (320, 180)
    assert result["keys"]["kf:image"] == 10
    assert result["keys"]["kf:model"] == 4
    assert result["warnings"] == ["null has no image; not drawn in AE"]
    assert result["ae_version"] == "24.6.0x45"


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
    args = {"text": "Replacement"} if kind == "text" else {"model": "model.glb"} if kind == "3d" else {}
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
    assert response["value"] == {"ok": True, "applied": False, "hand_edited": ["kf:image"]}
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
    extra, _ = spec_for(tmp_path, element("new", kind="text", text="New"))
    extra["layers"][1]["order"] = 5
    spec["layers"].append(extra["layers"][1])
    sync(path, spec, assets)
    assert_users()
    spec["layers"][2]["source"]["text"] = "Updated"
    sync(path, spec, assets)
    assert_users()
    replacement, _ = spec_for(tmp_path, element("image", kind="text", text="Recreated"))
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
        replacement, _ = spec_for(tmp_path, element("image", kind="text", text="Recreated"))
        spec["layers"][1] = replacement["layers"][1]
    response = sync(path, spec, assets)
    if action == "update":
        assert response["value"]["updated"] == ["kf:image"]
        item = layers(read_state(path))["kf:image"]
        assert (effects(item) if extra == "effect" else prop(item, "ADBE Mask Parade")["properties"]) == [added]
        assert sync(path, spec, assets)["writes"] == 0
    else:
        assert response["value"] == {"ok": True, "applied": False, "hand_edited": ["kf:image"]}
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
    assert response["value"] == {"ok": True, "applied": False, "hand_edited": [eid]}
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
        prop(item, "ADBE Anchor Point")["value"] = [99, 99]
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
    assert response["value"] == {"ok": True, "applied": False, "hand_edited": [eid]}
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
    assert response["value"] == {"ok": True, "ae_version": "25.4", "project_name": "Demo.aep" if saved else None,
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
    user["comment"] = "User footage"
    user["mainSource"]["file"] = str(tmp_path / "user.png")
    owned["name"] = "old-image.png"
    state["project"]["items"].append(user)
    user_comp = copy.deepcopy(comp(state))
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
    assert source["mainSource"]["color"] == [17 / 255, 34 / 255, 51 / 255]
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
    spec, assets = spec_for(tmp_path, element(kind="text", text="Text"))
    path = tmp_path / "ae.json"
    sync(path, spec, assets)
    spec["comp"].update(name="Renamed", frames=90)
    response = sync(path, spec, assets)
    assert response["writes"] == 2
    assert response["value"]["unchanged"] == 2
    assert comp(read_state(path))["duration"] == 3
    assert sync(path, spec, assets)["writes"] == 0


def test_fps_change_retimes_layers_even_when_layer_spec_hash_is_unchanged(tmp_path):
    value = scene(element(kind="text", text="Text", tracks={"x": track((0, 0), (30, 100))}))
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


@pytest.mark.parametrize("field,value", [("width", 0), ("width", 1.5), ("fps", 0), ("frames", -1)])
def test_invalid_comp_settings_fail_before_writes(tmp_path, field, value):
    spec, _ = spec_for(tmp_path)
    spec["comp"][field] = value
    response = sync(tmp_path / "ae.json", spec)
    assert response["value"]["ok"] is False
    assert response["writes"] == response["undo_groups"] == 0


def test_spec_tag_hashes_the_parsed_layer_and_warnings_are_combined(tmp_path):
    spec, assets = spec_for(tmp_path, element(kind="group"))
    spec["warnings"] = ["Comp warning"]
    path = tmp_path / "ae.json"
    result = sync(path, spec, assets)["value"]
    assert result["warnings"] == ["Comp warning", "e1 has no image; not drawn in AE"]
    for layer_spec in spec["layers"]:
        # Independent FNV-1a multiplication over JavaScript UTF-16 code units.
        # JSON.stringify writes integral doubles as integers (100.0 becomes 100).
        normalized = json.loads(json.dumps(layer_spec), parse_float=lambda v: int(float(v)) if float(v).is_integer() else float(v))
        serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        units = serialized.encode("utf-16-le")
        expected = 2166136261
        for unit in struct.unpack("<" + "H" * (len(units) // 2), units):
            expected = ((expected ^ unit) * 16777619) & 0xFFFFFFFF
        assert f";spec={expected:08x};" in layers(read_state(path))[layer_spec["id"]]["comment"]
