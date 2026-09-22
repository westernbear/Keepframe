import json, io
import http.client
from pathlib import Path
import cv2, numpy as np
from tests.test_web_server import start, get

def _mp4(path: Path, solid=True):
    w, h, n = 64, 36, 10
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    wr = cv2.VideoWriter(str(path), fourcc, 10, (w, h))
    for i in range(n):
        if solid:
            fr = np.full((h, w, 3), (20, 40, 80), np.uint8)
        else:
            fr = np.random.randint(0, 255, (h, w, 3), np.uint8)
        wr.write(fr)
    wr.release()

def test_upload_range_project(tmp_path):
    vid = tmp_path / "a.mp4"
    _mp4(vid)
    from keepframe.web.workspace import create_project, list_projects
    row = create_project(tmp_path / "ws", "Card", vid, "range", (0, 9))
    assert row["status"] == "uploaded"
    assert list_projects(tmp_path / "ws")[0]["id"] == row["id"]


def test_live_action_upload_is_rejected_without_retaining_media(tmp_path):
    video = tmp_path / "clip.mp4"
    ycrcb = np.full((36, 64, 3), (80, 150, 100), np.uint8)
    ycrcb[::2, ::2, 0] = 200
    frame = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 36))
    for _ in range(3):
        writer.write(frame)
    writer.release()

    boundary = "keepframe-upload-test"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"clip.mp4\"\r\n"
        "Content-Type: video/mp4\r\n\r\n"
    ).encode() + video.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    workspace = tmp_path / "ws"
    srv = start(workspace)
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
    try:
        conn.request("POST", "/api/projects", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        response = conn.getresponse()
        payload = json.loads(response.read())
        assert response.status == 422
        assert payload == {"error": "실사 푸티지. 평면 2D MG·UI 녹화만 받음.", "code": "live_action"}
        assert list(workspace.iterdir()) == []
        assert json.loads(get(srv, "/api/projects")[2]) == {"projects": []}
        assert get(srv, "/api/projects/clip/filmstrip")[0] == 404
        assert get(srv, "/api/projects/clip/frame/0")[0] == 404
    finally:
        conn.close()
        srv.shutdown()
        srv.server_close()
