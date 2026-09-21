import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import init_project
from keepframe.session.llm import NullClient
from tests.test_web_server import start, get


def _post(srv, path, payload):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    req = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def _project(tmp_path):
    root = tmp_path / "ws" / "p1"
    scene = make_synthetic_scene(root / "gold", seed=11, with_text=False)
    init_project(
        root,
        {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, scene.frames - 1]},
        scene,
    )
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "t", "status": "approved"}))
    return root, scene


def test_agent_page_serves(tmp_path):
    srv = start(tmp_path)
    try:
        code, ctype, body = get(srv, "/agent")
    finally:
        srv.shutdown()
    assert code == 200
    assert "agent-chat" in body.decode("utf-8")
    assert "세션 에이전트".encode("utf-8") in body


def test_agent_api_returns_reply(tmp_path, monkeypatch):
    monkeypatch.setattr("keepframe.session.agent.make_llm", lambda: NullClient())
    _project(tmp_path)
    srv = start(tmp_path / "ws")
    try:
        code, body = _post(srv, "/api/agent", {"project": "p1", "scene": "synth11", "message": "안녕"})
    finally:
        srv.shutdown()
    assert code == 200
    assert body["status"] == "done"
    assert "reply" in body
    assert body["tool_calls"] == []


def test_agent_api_requires_message(tmp_path, monkeypatch):
    monkeypatch.setattr("keepframe.session.agent.make_llm", lambda: NullClient())
    _project(tmp_path)
    srv = start(tmp_path / "ws")
    try:
        code, body = _post(srv, "/api/agent", {"project": "p1"})
    finally:
        srv.shutdown()
    assert code == 400
