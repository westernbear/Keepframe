import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from keepframe.analyze.constraints import extract_constraints
from keepframe.ir.store import init_project
from keepframe.ir.synth import make_synthetic_scene
from tests.test_web_server import start


def _post(srv, path, payload):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    req = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def test_edit_api_confirm_creates_version(tmp_path):
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
    srv = start(ws)
    try:
        preview = _post(srv, "/api/edit", {"project": "p1", "scene": "s1", "prompt": "문구를 Hello로", "element": text.id})
        missing = _post(srv, "/api/edit", {"scene": "s1", "prompt": "문구를 Hello로"})
        done = _post(srv, "/api/edit", {
            "project": "p1",
            "scene": "s1",
            "prompt": "문구를 Hello로",
            "element": text.id,
            "confirm": True,
            "intent": preview[1]["intent"],
        })
    finally:
        srv.shutdown()
    assert missing[0] == 400
    assert preview[0] == 200 and preview[1]["status"] == "needs_confirm"
    assert done[0] == 200
    assert done[1]["status"] == "done"
    assert done[1]["version"]["id"] == "v2"
    assert done[1]["verify"]["keep_pass_rate"] >= 0.95
