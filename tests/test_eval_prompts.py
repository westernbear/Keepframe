import errno
import json
import pickle
import sys

import numpy as np
import pytest

from scripts import eval_prompts
from keepframe.ir.schema import Background, Canonical, Element, Scene
from keepframe.ir.store import current_scene, init_project
from keepframe.session.agent import SessionAgent
from keepframe.session.llm import AssistantReply, NullClient
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
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


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
        assert all(not p.parent.exists() for p in copies)
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
    assert set(report) == {"ok", "typed_ok", "n", "rows"}
    assert report["ok"] == 5 and report["n"] == 8
    assert report["typed_ok"] == 5
    assert eval_prompts.PROMPTS == PROMPTS
    assert llm.prompts == PROMPTS and llm.replies == []
    assert made == [(saved, workspace)]
    assert [row["prompt"] for row in report["rows"]] == PROMPTS
    assert [row["ok"] for row in report["rows"]] == [True, True, True, False, True, False, False, True]
    assert [row["typed"] for row in report["rows"]] == [True, True, True, False, True, True, True, True]
    for row in report["rows"]:
        assert set(row) == {"prompt", "ok", "typed", "calls", "results", "targets", "reply"}
        assert isinstance(row["ok"], bool)
        assert isinstance(row["typed"], bool)
        assert len(row["calls"]) == len(row["results"])
        assert len(row["reply"]) <= 200
    rows = report["rows"]
    assert [c["name"] for c in rows[0]["calls"]] == ["set_keep", "edit"]
    assert rows[0]["targets"] == [{"element": "e1", "property": "text", "value": "가을 신상", "element_exists": True}]
    assert rows[1]["targets"][0]["value"] == "attachment"
    assert rows[2]["targets"] == [{"element": None, "property": "background", "value": "#001133", "element_exists": None}]
    assert rows[3]["targets"] == rows[3]["calls"] == []
    assert rows[3]["reply"] == "x" * 200
    assert rows[4]["results"][0]["needs_confirm"] is True
    assert "confirm" not in rows[4]["calls"][0]["arguments"]
    assert rows[4]["targets"][0]["delay"] == 0.3
    assert rows[4]["targets"][0]["value"] is None
    assert rows[5]["targets"][0]["element_exists"] is False
    assert rows[6]["targets"][0]["element_exists"] is True
    assert _snapshot(root) == before
    assert len(copies) == 8 and all(not p.exists() for p in copies)
    assert len({p.parent for p in copies}) == 8
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


@pytest.mark.parametrize("migrated", [True, False])
def test_main_skips_bulk_directories_only_when_migration_is_unneeded(tmp_path, monkeypatch, capsys, migrated):
    root = _project(tmp_path)
    sd = root / "scenes/s1"
    stages = sd / "stages"
    stages.mkdir()
    (stages / "ids.json").write_text("{}")
    (stages / "tracks.pkl").write_bytes(pickle.dumps([]))
    (stages / "text.pkl").write_bytes(pickle.dumps({"tracks": []}))
    np.save(stages / "frames.npy", np.zeros((10, 2, 2, 3), np.uint8))
    for parent in (root, sd):
        for name in ("stages", "analysis"):
            directory = parent / name
            directory.mkdir(exist_ok=True)
            (directory / "sentinel").write_bytes(b"bulk")
    (sd / "assets").mkdir()
    (sd / "assets/logo.png").write_bytes(b"asset")
    (sd / "report.json").write_text("{}")
    path = root / "project.json"
    project = json.loads(path.read_text())
    project["analysis_migrated"] = migrated
    path.write_text(json.dumps(project))
    before = _snapshot(root)
    llm = ScriptedLLM([_edit(i, [{"element": "e1", "property": "text", "value": "New"}]) for i in range(8)])
    monkeypatch.setattr(eval_prompts, "make_llm", lambda: llm)
    real_turn = SessionAgent.turn
    copies = []

    def turn(self, ctx, prompt, history):
        copies.append(ctx.root)
        for parent in (ctx.root, ctx.root / "scenes/s1"):
            for name in ("stages", "analysis"):
                assert (parent / name / "sentinel").exists() is (not migrated)
        assert (ctx.root / "scenes/s1/assets/logo.png").read_bytes() == b"asset"
        assert (ctx.root / "scenes/s1/report.json").is_file()
        if not migrated:
            scene, version = current_scene(ctx.root, "s1")
            assert version.analysis_file is not None
            manifest = json.loads((ctx.root / version.analysis_file).read_text())
            frozen = ctx.root / "scenes/s1" / manifest["frames_file"]
            assert np.array_equal(np.load(frozen), np.zeros((10, 2, 2, 3), np.uint8))
        return real_turn(self, ctx, prompt, history)

    monkeypatch.setattr(SessionAgent, "turn", turn)
    monkeypatch.setattr(sys, "argv", ["eval_prompts.py", "--project", str(root)])
    assert eval_prompts.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] == 8
    assert _snapshot(root) == before
    assert len(copies) == 8 and all(not p.parent.exists() for p in copies)


@pytest.mark.parametrize("error", [RuntimeError("ChatGPT 429"), RuntimeError("ChatGPT 503 " + "x" * 400), TimeoutError("timeout")])
def test_main_keeps_prior_rows_and_continues_after_provider_errors(tmp_path, monkeypatch, capsys, error):
    root = _project(tmp_path)
    llm = ScriptedLLM([
        _edit(0, [{"element": "e1", "property": "text", "value": "First"}]),
        error,
        _edit(2, [{"property": "background", "value": "#001133"}]),
        *[AssistantReply(content="done") for _ in range(5)],
    ])
    monkeypatch.setattr(eval_prompts, "make_llm", lambda: llm)
    monkeypatch.setattr(sys, "argv", ["eval_prompts.py", "--project", str(root)])
    assert eval_prompts.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["n"] == 8 and report["ok"] == 2
    assert [r["ok"] for r in report["rows"][:3]] == [True, False, True]
    row = report["rows"][1]
    assert row["prompt"] == PROMPTS[1]
    assert row["error"] == f"{type(error).__name__}: {error}"[:300]
    assert row["typed"] is False and row["targets"] == []
    assert llm.prompts == PROMPTS and llm.replies == []


@pytest.mark.parametrize("failure", ["copy", "scene"])
def test_main_recovers_from_copy_and_scene_failures(tmp_path, monkeypatch, capsys, failure):
    root = _project(tmp_path)
    before = _snapshot(root)
    llm = ScriptedLLM([_edit(0, [{"element": "e1", "property": "text", "value": "New"}]) for _ in range(7)])
    monkeypatch.setattr(eval_prompts, "make_llm", lambda: llm)
    copytree = eval_prompts.shutil.copytree
    copies = []
    error = OSError(errno.ENOSPC, "No space left on device")

    def copy(src, dst, *args, **kwargs):
        if str(src) != str(root):
            return copytree(src, dst, *args, **kwargs)
        copies.append(dst)
        if len(copies) == 2 and failure == "copy":
            dst.mkdir()
            (dst / "partial").write_bytes(b"partial")
            raise error
        result = copytree(src, dst, *args, **kwargs)
        if len(copies) == 2 and failure == "scene":
            (dst / "scenes/s1/scene.v1.json").write_text("bad scene")
        return result

    monkeypatch.setattr(eval_prompts.shutil, "copytree", copy)
    monkeypatch.setattr(sys, "argv", ["eval_prompts.py", "--project", str(root)])
    assert eval_prompts.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["n"] == 8 and report["ok"] == 7
    assert [r["ok"] for r in report["rows"][:3]] == [True, False, True]
    row = report["rows"][1]
    assert row["error"] == f"OSError: {error}" if failure == "copy" else row["error"].startswith("JSONDecodeError:")
    assert _snapshot(root) == before
    assert len(copies) == 8 and all(not p.parent.exists() for p in copies)


@pytest.mark.parametrize("targets", ["bad", {}, 0, None, ["bad"], [None], [{"element": ["e1"], "property": "text", "value": "New"}], [{"element": {"id": "e1"}, "property": "text", "value": "New"}]])
def test_main_malformed_targets_are_unsuccessful_without_crashing(tmp_path, monkeypatch, capsys, targets):
    root = _project(tmp_path)
    llm = ScriptedLLM([_edit(0, targets, element="e1"), *[AssistantReply(content="done") for _ in range(8)]])
    monkeypatch.setattr(eval_prompts, "make_llm", lambda: llm)
    monkeypatch.setattr(sys, "argv", ["eval_prompts.py", "--project", str(root)])
    assert eval_prompts.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["n"] == 8 and report["ok"] == 0
    assert all(r["ok"] is False for r in report["rows"])
    assert "error" not in report["rows"][0]
    if isinstance(targets, list) and isinstance(targets[0], dict):
        assert report["rows"][0]["targets"][0]["element_exists"] is False


def test_main_counts_typed_previews_separately_from_regex_fallback(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path)
    fallback = AssistantReply(tool_calls=[{"id": "fallback", "name": "edit", "arguments": {"prompt": PROMPTS[0], "element": "e1"}}])
    llm = ScriptedLLM([
        _edit(0, [{"element": "e1", "property": "text", "value": "New"}]),
        fallback,
        _edit(0, [], element="e1"),
        *[AssistantReply(content="done") for _ in range(5)],
    ])
    monkeypatch.setattr(eval_prompts, "make_llm", lambda: llm)
    monkeypatch.setattr(sys, "argv", ["eval_prompts.py", "--project", str(root)])
    assert eval_prompts.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] == 3 and report["typed_ok"] == 1 and report["n"] == 8
    assert [r["typed"] for r in report["rows"]] == [True, False, False, False, False, False, False, False]
    assert all(r["ok"] is True for r in report["rows"][:3])


@pytest.mark.parametrize("saved", [False, True])
def test_main_fails_fast_for_null_client(tmp_path, monkeypatch, capsys, saved):
    workspace = tmp_path / "workspace"
    if saved:
        save_llm_settings(workspace, ProviderConfig(provider="chatgpt", api_key="dummy"))
    monkeypatch.setattr(eval_prompts, "make_llm", lambda *args: NullClient())
    monkeypatch.setattr(sys, "argv", ["eval_prompts.py", "--project", str(tmp_path / "missing"), "--workspace", str(workspace)])
    assert eval_prompts.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error" in captured.err.lower() and "NullClient" in captured.err


def test_main_strips_confirm_before_edit_and_never_creates_an_edit_version(tmp_path, monkeypatch, capsys):
    from keepframe.edit import agent
    from keepframe.verify.verifier import VerifyReport

    root = _project(tmp_path)
    before = _snapshot(root)
    first = _edit(0, [{"element": "e1", "property": "text", "value": "New"}], confirm=True)
    llm = ScriptedLLM([first, *[AssistantReply(content="done") for _ in range(8)]])
    monkeypatch.setattr(eval_prompts, "make_llm", lambda: llm)
    monkeypatch.setattr(agent, "compose", lambda *args: None)
    monkeypatch.setattr(agent, "render", lambda *args: None)
    monkeypatch.setattr(agent, "verify", lambda *args, **kwargs: VerifyReport(schema_ok=True, passed=True))
    real_turn = SessionAgent.turn
    versions = []

    def turn(self, ctx, prompt, history):
        result = real_turn(self, ctx, prompt, history)
        versions.append(current_scene(ctx.root, "s1")[1].id)
        return result

    monkeypatch.setattr(SessionAgent, "turn", turn)
    monkeypatch.setattr(sys, "argv", ["eval_prompts.py", "--project", str(root)])
    assert eval_prompts.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert versions == ["v1"] * 8
    row = report["rows"][0]
    assert row["ok"] is True and row["results"][0]["needs_confirm"] is True
    assert "confirm" not in row["calls"][0]["arguments"]
    assert first.tool_calls[0]["arguments"]["confirm"] is True
    assert _snapshot(root) == before
