import json
from pathlib import Path
from urllib.request import Request, urlopen

from keepframe.web.demo import ensure_demo_project
from keepframe.web.workspace import list_projects, create_project, load_meta, project_dir
from tests.test_web_server import get, start


def _post(srv, path, payload):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    req = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req) as response:
        return response.status, json.loads(response.read())


def test_empty_workspace(tmp_path):
    assert list_projects(tmp_path) == []

def test_create_requires_video(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        create_project(tmp_path, "Autumn", tmp_path / "nope.mp4", "range", (0, 30))

def test_get_uploaded_project_includes_video_metadata(tmp_path):
    video = tmp_path / "clip.mp4"
    from tests.test_web_ingest import _mp4
    _mp4(video)
    project = create_project(tmp_path / "ws", "Clip", video, "range", (2, 7))
    srv = start(tmp_path / "ws")
    try:
        code, _, body = get(srv, f"/api/projects/{project['id']}")
    finally:
        srv.shutdown()
        srv.server_close()

    data = json.loads(body)
    assert code == 200
    assert data["project"]["status"] == "uploaded"
    assert data["project"]["range"] == [2, 7]
    assert data["project"]["video"]["frames"] == 10

def test_explicit_metadata_wins_and_rows_are_sorted(tmp_path):
    older = tmp_path / "older"
    newer = tmp_path / "newer"
    older.mkdir()
    newer.mkdir()
    (older / "meta.json").write_text(json.dumps({"id": "kept", "title": "Old", "updated": "2024-01-01T00:00:00Z"}))
    (newer / "meta.json").write_text(json.dumps({"id": "newer", "title": "New", "updated": "2025-01-01T00:00:00Z"}))
    (older / "project.json").write_text("not a project")
    (tmp_path / "unrelated.json").write_text("not a project")

    rows = list_projects(tmp_path)

    assert [row["id"] for row in rows] == ["newer", "kept"]
    assert rows[1]["id"] == "kept" and rows[1]["title"] == "Old"


def test_manifest_only_project_supports_library_state_and_approval(tmp_path):
    row = ensure_demo_project(tmp_path)
    root = project_dir(tmp_path, row["id"])
    (root / "meta.json").unlink()
    scene_id = row["scene"]

    srv = start(tmp_path)
    try:
        code, _, body = get(srv, "/api/projects")
        assert code == 200
        projects = json.loads(body)["projects"]
        assert len(projects) == 1
        assert projects[0]["id"] == row["id"]
        assert projects[0]["title"] == "demo"
        assert projects[0]["status"] == "review"
        assert projects[0]["version"] == "v1"
        assert projects[0]["scene"] == scene_id
        assert "tracks" not in projects[0]

        state_code, _, state_body = get(srv, f"/api/state?project={row['id']}&scene={scene_id}")
        assert state_code == 200
        assert json.loads(state_body)["status"] == "review"

        approve_code, approved = _post(srv, "/api/approve", {"project": row["id"], "scene": scene_id})
        assert approve_code == 200
        assert approved["project"]["status"] == "approved"
    finally:
        srv.shutdown()

    persisted = load_meta(tmp_path, row["id"])
    assert persisted is not None
    assert persisted["status"] == "approved"
    assert persisted["version"] == "v1"
    reload_srv = start(tmp_path)
    try:
        state_code, _, state_body = get(reload_srv, f"/api/state?project={row['id']}&scene={scene_id}")
    finally:
        reload_srv.shutdown()
    assert state_code == 200
    assert json.loads(state_body)["status"] == "approved"
