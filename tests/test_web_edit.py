import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from keepframe.analyze.constraints import extract_constraints
from keepframe.ir.store import current_scene, init_project, load_project
from keepframe.ir.synth import make_synthetic_scene
from keepframe.session.llm import AssistantReply
from tests.test_session_agent import StubLLM
from tests.test_web_server import start


def _post(srv, path, payload):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    req = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", f"http://127.0.0.1:{srv.server_address[1]}")
    try:
        with urlopen(req) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def test_edit_api_confirm_creates_version(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    root = ws / "p1"
    sd = root / "scenes" / "s1"
    scene = make_synthetic_scene(sd, seed=6, with_text=True, frames=20)
    scene = scene.model_copy(update={"id": "s1"})
    scene.constraints = [
        c.model_copy(update={"keep": c.pred.startswith("type(")})
        for c in extract_constraints(scene)
    ]
    text = next(e for e in scene.elements if e.kind == "text")
    init_project(
        root,
        {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, scene.frames - 1]},
        scene,
    )
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "t", "status": "review"}))
    llm = StubLLM([
        AssistantReply(tool_calls=[{"id": "c1", "name": "edit", "arguments": {
            "prompt": "문구를 Hello로", "confirm": True,
            "targets": [{"element": text.id, "property": "text", "value": "Hello"}],
        }}]),
        AssistantReply(content="편집을 마쳤습니다."),
    ])
    monkeypatch.setattr("keepframe.web.server.make_llm", lambda *_: llm)
    srv = start(ws)
    try:
        preview = _post(srv, "/api/edit", {"project": "p1", "scene": "s1", "prompt": "문구를 Hello로", "element": text.id})
        missing = _post(srv, "/api/edit", {"scene": "s1", "prompt": "문구를 Hello로"})
        pending = _post(srv, "/api/agent", {"project": "p1", "scene": "s1", "message": "문구를 Hello로"})
        assert pending[0] == 200 and pending[1]["status"] == "pending", pending
        assert pending[1]["needs_confirm"] and not pending[1]["needs_choice"]
        assert [v.id for v in load_project(root).versions] == ["v1"]
        assert current_scene(root, "s1")[0] == scene
        done = _post(srv, "/api/edit", {
            "project": "p1",
            "scene": "s1",
            "v": "v1",
            "prompt": "문구를 Hello로",
            "element": text.id,
            "confirm": True,
            "intent": pending[1]["results"][0]["payload"]["intent"],
            "choices": {},
        })
    finally:
        srv.shutdown()
    assert missing[0] == 400
    assert preview[0] == 200 and preview[1]["status"] == "needs_confirm"
    assert done[0] == 200
    assert done[1]["status"] == "done"
    assert done[1]["version"]["id"] == "v2"
    assert done[1]["verify"]["keep_pass_rate"] >= 0.95
    assert [v.id for v in load_project(root).versions] == ["v1", "v2"]
    assert current_scene(root, "s1")[0].element(text.id).canonical.text == "Hello"
