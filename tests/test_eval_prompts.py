import json
import sys

import pytest

from scripts import eval_prompts
from keepframe.ir.schema import Background, Canonical, Element, Scene
from keepframe.ir.store import current_scene, init_project
from keepframe.session.agent import SessionAgent
from keepframe.session.llm import AssistantReply
from keepframe.session.provider import ProviderConfig, save_llm_settings


PROMPTS = [
    "제목 문구를 '가을 신상'으로 바꿔줘",
    "로고를 첨부한 이미지로 바꿔줘",
    "배경을 짙은 남색으로 바꿔줘",
    "전체를 1.5배 빠르게 해줘",
    "두 번째로 등장하는 텍스트를 0.3초 늦게 나오게 해줘",
    "제목 폰트를 더 굵게 해줘",
    "카드 색을 브랜드 컬러 #ff5a1f로 바꿔줘",
    "Change the headline to 'Fall Drop'",
]


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, messages, tools):
        assert tools
        if messages[-1]["role"] == "user":
            self.prompts.append(messages[-1]["content"])
        return self.replies.pop(0)


def _project(tmp_path, scene_id="s1"):
    root = tmp_path / "gate-output"
    scene = Scene(
        id=scene_id, size=(640, 360), fps=30, frames=10, background=Background(),
        elements=[
            Element(id="e1", kind="text", label="title", visible=(0, 9),
                    canonical=Canonical(width=500, height=40, text="Sale")),
            Element(id="e2", kind="sprite", label="logo", visible=(0, 9),
                    canonical=Canonical(width=40, height=40)),
            Element(id="e3", kind="text", visible=(3, 9),
                    canonical=Canonical(width=500, height=40, text="Details")),
        ],
    )
    init_project(root, {"file": "ref.mp4", "fps": 30, "size": [640, 360]}, scene)
    return root


def _edit(index, targets, **args):
    return AssistantReply(tool_calls=[{
        "id": f"c{index}", "name": "edit",
        "arguments": {"prompt": PROMPTS[index], "targets": targets, **args},
    }])


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("scene_id", ["s1", "s2"])
def test_main_evaluates_typed_edits_on_independent_copies(tmp_path, monkeypatch, capsys, scene_id):
    root = _project(tmp_path, scene_id)
    before = _snapshot(root)
    workspace = tmp_path / "workspace"
    saved = ProviderConfig(provider="chatgpt", auth="oauth", model="gpt-6.1-sol",
                           api_key="test-access-token", refresh_token="test-refresh-token")
    save_llm_settings(workspace, saved)
    first = _edit(0, [{"element": "e1", "property": "text", "value": "가을 신상"}])
    first.tool_calls.insert(0, {"id": "keep", "name": "set_keep", "arguments": {"preset": "none"}})
    llm = ScriptedLLM([
        first,
        _edit(1, [{"element": "e2", "property": "texture", "value": "attachment"}]),
        _edit(2, [{"property": "background", "value": "#001133"}]),
        AssistantReply(content="x" * 250),
        _edit(4, [{"element": "e3", "property": "timing", "delay": 0.3}], confirm=True),
        _edit(5, [{"element": "missing", "property": "font", "weight": 700}]),
        AssistantReply(content="대상을 찾지 못했습니다."),
        _edit(6, [{"element": "e2", "property": "color", "value": "invalid"}]),
        AssistantReply(content="색을 읽지 못했습니다."),
        _edit(7, [{"element": "e1", "property": "text", "value": "Fall Drop"}]),
    ])
    made = []

    def make_llm(config=None, workspace=None):
        made.append((config, workspace))
        return llm

    monkeypatch.setattr(eval_prompts, "make_llm", make_llm)
    copies = []
    real_turn = SessionAgent.turn

    def turn(self, ctx, prompt, history):
        assert history == []
        assert ctx.scene_id == scene_id
        assert ctx.has_attachment is (prompt == PROMPTS[1])
        assert ctx.root != root and ctx.root not in copies
        assert current_scene(ctx.root, scene_id)[1].id == "v1"
        copies.append(ctx.root)
        result = real_turn(self, ctx, prompt, history)
        if prompt == PROMPTS[0]:
            assert current_scene(ctx.root, scene_id)[1].id == "v2"
        return result

    monkeypatch.setattr(SessionAgent, "turn", turn)
    argv = ["eval_prompts.py", "--project", str(root), "--workspace", str(workspace)]
    if scene_id != "s1":
        argv.extend(["--scene", scene_id])
    monkeypatch.setattr(sys, "argv", argv)
    assert eval_prompts.main() == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert set(report) == {"ok", "n", "rows"}
    assert report["ok"] == 5 and report["n"] == 8
    assert eval_prompts.PROMPTS == PROMPTS
    assert llm.prompts == PROMPTS and llm.replies == []
    assert made == [(saved, workspace)]
    assert [row["prompt"] for row in report["rows"]] == PROMPTS
    assert [row["ok"] for row in report["rows"]] == [True, True, True, False, True, False, False, True]
    for row in report["rows"]:
        assert set(row) == {"prompt", "ok", "calls", "results", "targets", "reply"}
        assert isinstance(row["ok"], bool)
        assert len(row["calls"]) == len(row["results"])
        assert len(row["reply"]) <= 200
    rows = report["rows"]
    assert [c["name"] for c in rows[0]["calls"]] == ["set_keep", "edit"]
    assert rows[0]["targets"] == [{"element": "e1", "property": "text", "value": "가을 신상", "element_exists": True}]
    assert rows[1]["targets"][0]["value"] == "attachment"
    assert rows[2]["targets"] == [{"element": None, "property": "background", "value": "#001133", "element_exists": None}]
    assert rows[3]["targets"] == rows[3]["calls"] == []
    assert rows[3]["reply"] == "x" * 200
    assert rows[4]["results"][0]["needs_choice"] is True
    assert rows[4]["targets"][0]["delay"] == 0.3
    assert rows[4]["targets"][0]["value"] is None
    assert rows[5]["targets"][0]["element_exists"] is False
    assert rows[6]["targets"][0]["element_exists"] is True
    assert _snapshot(root) == before
    assert len(copies) == 8 and all(not p.exists() for p in copies)
    assert "test-access-token" not in captured.out + captured.err
    assert "test-refresh-token" not in captured.out + captured.err


@pytest.mark.parametrize("name, result", [
    ("edit", {"ok": True, "needs_confirm": True, "payload": {"intent": {"targets": []}}}),
    ("edit", {"ok": False, "needs_confirm": True, "payload": {"intent": {"targets": [{"property": "background"}]}}}),
    ("edit", {"ok": True, "payload": {"intent": {"targets": [{"property": "background"}]}}}),
    ("report", {"ok": True, "needs_confirm": True, "payload": {"intent": {"targets": [{"property": "background"}]}}}),
    ("edit", {"ok": True, "needs_confirm": True}),
])
def test_main_does_not_count_nonqualifying_results(tmp_path, monkeypatch, capsys, name, result):
    root = _project(tmp_path)
    replies = [reply for _ in PROMPTS for reply in (
        AssistantReply(tool_calls=[{"id": "c", "name": name, "arguments": {}}]),
        AssistantReply(content="done"),
    )] if not result.get("needs_confirm") else [
        AssistantReply(tool_calls=[{"id": "c", "name": name, "arguments": {}}]) for _ in PROMPTS
    ]
    llm = ScriptedLLM(replies)
    monkeypatch.setattr(eval_prompts, "make_llm", lambda: llm)
    monkeypatch.setattr("keepframe.session.agent.run_tool", lambda *args: result)
    monkeypatch.setattr(sys, "argv", ["eval_prompts.py", "--project", str(root)])
    assert eval_prompts.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] == 0 and report["n"] == 8
    assert all(row["ok"] is False for row in report["rows"])
    assert llm.prompts == PROMPTS and llm.replies == []
