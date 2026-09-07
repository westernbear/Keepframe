import json
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import init_project
from tests.test_web_server import start


def test_state_from_synthetic(tmp_path):
    root = tmp_path / "ws" / "p1"
    scene = make_synthetic_scene(root / "gold", seed=11, with_text=False)
    init_project(
        root,
        {
            "file": "ref.mp4",
            "fps": scene.fps,
            "size": list(scene.size),
            "mode": "range",
            "range": [0, scene.frames - 1],
        },
        scene,
    )
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "t", "status": "review"}))
    srv = start(tmp_path / "ws")
    import urllib.request

    url = f"http://127.0.0.1:{srv.server_address[1]}/api/state?project=p1&scene={scene.id}"
    with urllib.request.urlopen(url) as r:
        body = json.loads(r.read())
    srv.shutdown()
    assert body["scene"]["id"] == scene.id
    assert body["version"]["id"] == "v1"
