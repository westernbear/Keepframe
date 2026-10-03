import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from keepframe.after_effects.auth import AEProjectAuth, controller_cookie_name
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import init_project
from keepframe.session.agent import SessionTurn
from keepframe.session.llm import NullClient
from tests.test_web_server import start, get


def _post(srv, path, payload, *, cookie=None, origin=None):
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    headers = {
        "Content-Type": "application/json",
        "Origin": origin or base,
    }
    if cookie is not None:
        headers["Cookie"] = cookie
    req = Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
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


@pytest.mark.parametrize("ui_context, has_attachment", [
    ({"schema": "keepframe.ui-context/1", "summary": {"attachment": {"name": "replacement.png"}}}, True),
    ({"schema": "keepframe.ui-context/1", "summary": {"attachment": None}}, False),
    ({"schema": "keepframe.ui-context/1", "summary": {"attachment": {}}}, False),
    ({"schema": "keepframe.ui-context/1", "summary": {}}, False),
    ({"schema": "keepframe.ui-context/1", "summary": '{"attachment": true}'}, False),
    ({"schema": "keepframe.ui-context/1", "summary": [{"attachment": True}]}, False),
    ({"schema": "keepframe.ui-context/1", "state": {"attachment": True}}, False),
    (None, False),
])
def test_agent_api_attachment_flag_from_raw_summary(tmp_path, monkeypatch, ui_context, has_attachment):
    monkeypatch.setattr("keepframe.web.server.make_llm", lambda *_: NullClient())
    captured = []

    def capture_turn(_self, ctx, _message, _history, ui_context=None):
        captured.append(ctx.has_attachment)
        return SessionTurn(reply="captured")

    monkeypatch.setattr("keepframe.web.server.SessionAgent.turn", capture_turn)
    _project(tmp_path)
    srv = start(tmp_path / "ws")
    try:
        code, body = _post(srv, "/api/agent", {
            "project": "p1", "scene": "synth11", "message": "이미지 교체", "ui_context": ui_context,
        })
    finally:
        srv.shutdown()
    assert code == 200, body
    assert captured == [has_attachment]


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


def test_agent_api_requires_existing_controller_cookie(tmp_path, monkeypatch):
    monkeypatch.setattr("keepframe.session.agent.make_llm", lambda: NullClient())
    root, _ = _project(tmp_path)
    pairing = AEProjectAuth(root, "p1").create_pairing(None, {})
    srv = start(tmp_path / "ws")
    payload = {"project": "p1", "scene": "synth11", "message": "안녕"}
    try:
        denied, _ = _post(srv, "/api/agent", payload)
        allowed, _ = _post(
            srv,
            "/api/agent",
            payload,
            cookie=f"{controller_cookie_name('p1')}={pairing.controller_token}",
        )
    finally:
        srv.shutdown()
    assert denied == 401
    assert allowed == 200


def test_agent_cannot_prepare_ae_plan_before_pairing(tmp_path, monkeypatch):
    _project(tmp_path)

    def request_ae_plan(_self, ctx, _message, _history):
        assert ctx.prepare_render is not None
        ctx.prepare_render("preview", "after_effects", None)
        raise AssertionError("AE plan preparation should require pairing")

    monkeypatch.setattr(
        "keepframe.web.server.SessionAgent.turn",
        request_ae_plan,
    )
    srv = start(tmp_path / "ws")
    try:
        code, body = _post(
            srv,
            "/api/agent",
            {"project": "p1", "scene": "synth11", "message": "render"},
        )
    finally:
        srv.shutdown()
    assert code == 401
    assert body == {"error": "controller authorization failed"}


def test_agent_rejects_cross_origin_and_oversized_bodies_before_work(tmp_path):
    _project(tmp_path)
    srv = start(tmp_path / "ws")
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        cross_origin = Request(
            f"{base}/api/agent",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://attacker.example",
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as forbidden:
            urlopen(cross_origin)
        oversized = Request(
            f"{base}/api/agent",
            data=b"x" * (64 * 1024 + 1),
            headers={
                "Content-Type": "application/json",
                "Origin": base,
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as too_large:
            urlopen(oversized)
    finally:
        srv.shutdown()
    assert forbidden.value.code == 403
    assert too_large.value.code == 413


def test_agent_rejects_oversized_message_with_ui_context(tmp_path):
    _project(tmp_path)
    srv = start(tmp_path / "ws")
    try:
        code, body = _post(srv, "/api/agent", {
            "project": "p1",
            "scene": "synth11",
            "message": "x" * (64 * 1024 + 1),
            "ui_context": {"schema": "keepframe.ui-context/1", "summary": {}, "images": []},
        })
    finally:
        srv.shutdown()
    assert code == 413
    assert body["error"] == "message is too large"
