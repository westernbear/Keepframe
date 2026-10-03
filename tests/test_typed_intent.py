import copy

import pytest
from pydantic import ValidationError

from keepframe.analyze.constraints import apply_keep_preset, extract_constraints
from keepframe.edit.agent import edit
from keepframe.edit.intent import Target
from keepframe.ir.store import current_scene, init_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.session.agent import SYSTEM, SessionAgent
from keepframe.session.llm import AssistantReply
from keepframe.session.tools import TOOL_SCHEMAS, SessionContext, run_tool


def _project(tmp_path):
    root = tmp_path / "proj"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=4, with_text=True, frames=24).model_copy(update={"id": "s1"})
    scene.constraints = apply_keep_preset(extract_constraints(scene), "content_only")
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 23]}, scene)
    return root, next(e for e in scene.elements if e.kind == "text")


def test_edit_tool_accepts_typed_targets(tmp_path):
    root, text = _project(tmp_path)
    targets = [{"element": text.id, "property": "text", "value": "Hi"}]
    first = run_tool("edit", SessionContext(root=root, scene_id="s1"), {"prompt": "제목을 Hi로", "targets": targets})
    assert first["needs_confirm"] and first["payload"]["intent"]["targets"][0]["value"] == "Hi"
    done = run_tool("edit", SessionContext(root=root, scene_id="s1"), {"prompt": "제목을 Hi로", "targets": targets, "confirm": True})
    assert done["ok"] and current_scene(root, "s1")[0].element(text.id).canonical.text == "Hi"


def test_edit_tool_rejects_unknown_element(tmp_path):
    root, text = _project(tmp_path)
    res = run_tool("edit", SessionContext(root=root, scene_id="s1"), {"prompt": "x", "targets": [{"element": "e99", "property": "text", "value": "a"}]})
    assert res["ok"] is False and text.id in res["message"]


def test_edit_tool_rejects_non_hex_color(tmp_path):
    root, text = _project(tmp_path)
    res = run_tool("edit", SessionContext(root=root, scene_id="s1"), {"prompt": "x", "targets": [{"element": text.id, "property": "color", "value": "blue"}]})
    assert res["ok"] is False and "#" in res["message"]
    with pytest.raises(ValidationError):
        Target(element="e1", property="color", value="rgb(0,0,255)")


def _assert_refs_resolve(schema):
    def visit(node):
        if isinstance(node, dict):
            if "$ref" in node:
                assert node["$ref"].startswith("#/")
                resolved = schema
                for part in node["$ref"][2:].split("/"):
                    resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
                assert isinstance(resolved, dict)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)


def test_edit_schema_is_typed():
    params = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "edit")["function"]["parameters"]
    assert "text" in params["properties"]["targets"]["items"]["properties"]["property"]["enum"]
    _assert_refs_resolve(params)


def test_edit_schema_resolves_pydantic_defs(monkeypatch):
    from keepframe.session.tools import _edit_params, _fn

    item = copy.deepcopy(Target.model_json_schema())
    item["$defs"] = {"Prop": item["properties"]["property"]}
    item["properties"]["property"] = {"$ref": "#/$defs/Prop"}
    monkeypatch.setattr(Target, "model_json_schema", classmethod(lambda cls: item))
    params = _fn("edit", "", _edit_params(), ["prompt"])["function"]["parameters"]
    _assert_refs_resolve(params)
    assert "text" in params["properties"]["targets"]["items"]["properties"]["property"]["enum"]


def test_session_agent_forwards_llm_targets(tmp_path):
    root, text = _project(tmp_path)

    class Scripted:
        supports_vision = False

        def complete(self, messages, tools):
            return AssistantReply(tool_calls=[{"id": "c1", "name": "edit", "arguments": {
                "prompt": "제목 바꿔", "targets": [{"element": text.id, "property": "text", "value": "Hi"}]}}])

    turn = SessionAgent(Scripted()).turn(SessionContext(root=root, scene_id="s1"), "제목 바꿔", [])
    assert turn.needs_confirm is True
    assert "- edit는 targets(요소 id, property, value)를 채워 호출한다. 색은 #rrggbb, 첨부 이미지(UI 요약의 attachment)를 쓰면 texture value를 'attachment'로 둔다. 도구가 형식 오류를 돌려주면 고쳐 다시 호출한다.\n" in SYSTEM


@pytest.mark.parametrize("value, expected", [("#AbC", "#aabbcc"), ("#A0b1C2", "#a0b1c2")])
def test_target_normalizes_hex(value, expected):
    assert Target(element="e1", property="color", value=value).value == expected


@pytest.mark.parametrize("value", [None, "#12345", "#1234567", "#abc\n"])
def test_target_requires_full_hex(value):
    with pytest.raises(ValidationError, match="color value must be #rrggbb"):
        Target(element="e1", property="color", value=value)


@pytest.mark.parametrize("fields", [
    {"value": "a" * 501}, {"weight": 99}, {"weight": 901}, {"speed": 0.1},
    {"speed": 10.1}, {"delay": -30.1}, {"delay": 30.1},
])
def test_target_rejects_out_of_bounds_fields(fields):
    with pytest.raises(ValidationError):
        Target(element="e1", property="text", **fields)


@pytest.mark.parametrize("fields", [
    {"value": "a" * 500, "weight": 100, "speed": 0.1001, "delay": -30},
    {"weight": 900, "speed": 10, "delay": 30},
])
def test_target_accepts_bounds(fields):
    target = Target(element="e1", property="text", **fields)
    assert all(getattr(target, name) == value for name, value in fields.items())


def test_typed_intent_without_element_is_ambiguous(tmp_path):
    root, _ = _project(tmp_path)
    result = edit(root, "s1", "x", confirm=True, intent={"targets": [{"property": "text", "value": "Hi"}]})
    assert result.status == "failed" and result.intent.ambiguous
    assert result.intent.candidates == [e.id for e in current_scene(root, "s1")[0].elements]
    assert result.attempts == 0


def test_typed_intent_gets_summary(tmp_path):
    root, text = _project(tmp_path)
    result = edit(root, "s1", "x", intent={"targets": [{"element": text.id, "property": "text", "value": "Hi"}]})
    assert result.status == "needs_confirm"
    assert result.summary == f"{text.id} 문구를 Hi(으)로 바꿉니다. 트랙은 유지합니다."


def test_edit_tool_preserves_legacy_intent(tmp_path):
    root, text = _project(tmp_path)
    res = run_tool("edit", SessionContext(root, "s1"), {"prompt": "x", "intent": {
        "targets": [{"element": text.id, "property": "text", "value": "Hi"}], "summary": "legacy summary"}})
    assert res["needs_confirm"] and res["message"] == "legacy summary"


def test_edit_tool_targets_override_legacy_intent(tmp_path):
    root, text = _project(tmp_path)
    res = run_tool("edit", SessionContext(root, "s1"), {"prompt": "x",
        "targets": [{"element": text.id, "property": "text", "value": "Hi"}],
        "intent": {"targets": [{"element": "e99", "property": "text", "value": "old"}]}})
    assert res["needs_confirm"] and res["payload"]["intent"]["targets"][0]["value"] == "Hi"


@pytest.mark.parametrize("has_attachment, value", [(True, "new image"), (False, "attachment")])
def test_typed_attachment_summary(tmp_path, has_attachment, value):
    root, _ = _project(tmp_path)
    sprite = next(e for e in current_scene(root, "s1")[0].elements if e.kind == "sprite")
    res = run_tool("edit", SessionContext(root, "s1", has_attachment=has_attachment), {
        "prompt": "x", "targets": [{"element": sprite.id, "property": "texture", "value": value}]})
    assert res["needs_confirm"]
    assert res["message"] == f"{sprite.id} 이미지를 첨부로 바꿉니다. 트랙은 유지합니다."


def test_typed_attachment_required_before_conflict_choice(tmp_path, monkeypatch):
    root, text = _project(tmp_path)
    sprite = next(e for e in current_scene(root, "s1")[0].elements if e.kind == "sprite")
    monkeypatch.setattr("keepframe.edit.agent.AssetClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("generated")))
    targets = [{"element": sprite.id, "property": "texture", "value": "attachment"},
               {"element": text.id, "property": "text", "value": "long text " * 40}]
    preview = run_tool("edit", SessionContext(root, "s1", has_attachment=True), {"prompt": "x", "targets": targets})
    assert preview["needs_confirm"] and preview["payload"]["plan"]["conflicts"]
    done = run_tool("edit", SessionContext(root, "s1", has_attachment=True), {"prompt": "x", "targets": targets, "confirm": True})
    assert done["ok"] is False and done["message"] == "attachment_required"
    assert done["needs_choice"] is False
