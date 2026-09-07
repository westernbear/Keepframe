import json, io
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
    from refstudio.web.workspace import create_project, list_projects
    row = create_project(tmp_path / "ws", "Card", vid, "range", (0, 9))
    assert row["status"] == "uploaded"
    assert list_projects(tmp_path / "ws")[0]["id"] == row["id"]
