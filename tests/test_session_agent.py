from keepframe.analyze.constraints import extract_constraints
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import init_project
from keepframe.session.agent import SessionAgent, SessionTurn
from keepframe.session.llm import AssistantReply
from keepframe.session.tools import SessionContext


class StubLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, messages, tools):
        return self.replies.pop(0)


def _ctx(root):
    return SessionContext(root=root, scene_id="s1")


def _project(tmp_path):
    root = tmp_path / "proj"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=3, with_text=False, frames=12)
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene.model_copy(update={"id": "s1"}))
    return root


def test_turn_returns_plain_reply(tmp_path):
    agent = SessionAgent(StubLLM([AssistantReply(content="안녕하세요.")]))
    turn = agent.turn(_ctx(_project(tmp_path)), "안녕", [])
    assert turn.status == "done"
    assert turn.reply == "안녕하세요."
    assert turn.tool_calls == []


def test_turn_runs_tool_then_replies(tmp_path):
    root = _project(tmp_path)
    replies = [
        AssistantReply(tool_calls=[{"id": "c1", "name": "report", "arguments": {}}]),
        AssistantReply(content="리포트를 만들었습니다."),
    ]
    agent = SessionAgent(StubLLM(replies))
    turn = agent.turn(_ctx(root), "리포트 줘", [])
    assert turn.status == "done"
    assert turn.tool_calls[0]["name"] == "report"
    assert turn.results[0]["ok"] is True
    assert turn.reply == "리포트를 만들었습니다."


def test_turn_stops_on_pending(tmp_path):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=4, with_text=True, frames=24)
    scene = scene.model_copy(update={"id": "s1"})
    scene.constraints = [c.model_copy(update={"keep": c.pred.startswith("type(")}) for c in extract_constraints(scene)]
    text = next(e for e in scene.elements if e.kind == "text")
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 23]}, scene)

    replies = [
        AssistantReply(tool_calls=[{"id": "c1", "name": "edit", "arguments": {"prompt": "문구를 Hello로", "confirm": False, "element": text.id}}]),
    ]
    agent = SessionAgent(StubLLM(replies))
    turn = agent.turn(_ctx(root), "문구 바꿔줘", [])
    assert turn.status == "pending"
    assert turn.needs_confirm is True
