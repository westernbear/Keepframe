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
        js_code, _, js_body = get(srv, "/static/js/agent.js")
    finally:
        srv.shutdown()
    assert code == 200
    html = body.decode("utf-8")
    js = js_body.decode("utf-8")
    src = html + "\n" + js
    assert "agent-chat" in html
    assert "세션 에이전트" not in html
    assert "에이전트" in html
    assert "createPreviewCache" in src
    assert "previews.wait" in src
    assert js_code == 200


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


def test_agent_uses_workspace_llm_settings(tmp_path, monkeypatch):
    from keepframe.session.llm import AssistantReply
    from keepframe.session.provider import ProviderConfig, save_llm_settings

    captured = {}

    class Fake:
        def complete(self, messages, tools):
            return AssistantReply(content="saved-settings")

    def fake_make(config=None):
        captured["config"] = config
        return Fake()

    monkeypatch.setattr("keepframe.web.server.make_llm", fake_make)
    _project(tmp_path)
    ws = tmp_path / "ws"
    save_llm_settings(ws, ProviderConfig(provider="openai", model="gpt-4o-mini", api_key="sk-from-admin"))
    srv = start(ws)
    try:
        code, body = _post(srv, "/api/agent", {"project": "p1", "scene": "synth11", "message": "안녕"})
    finally:
        srv.shutdown()
    assert code == 200
    assert body["reply"] == "saved-settings"
    assert captured["config"].api_key == "sk-from-admin"
