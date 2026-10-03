import json
import pickle

import cv2
import numpy as np
import pytest

from keepframe.analyze.regions import Region
from keepframe.analyze.text import TextBox, TextTrack
from keepframe.analyze.tracking import ObjectTrack
from keepframe.ir.schema import Background, Canonical, Element, Scene
from keepframe.ir.store import init_project, load_project, new_version, scene_dir
from keepframe.review.overlay import frame_overlay, read_manifest, snapshot_from_stages
from keepframe.web.server import ReviewState
from tests.test_web_server import start, get


@pytest.fixture
def observed_project(tmp_path):
    root = tmp_path / "ws/p1"
    sd = scene_dir(root, "s1")
    stages = sd / "stages"
    stages.mkdir(parents=True)
    scene = Scene(id="s1", size=(40, 30), fps=30, frames=4, background=Background(), elements=[
        Element(id="graphic", kind="sprite", canonical=Canonical(width=999, height=999), visible=(0, 3)),
        Element(id="text", kind="text", canonical=Canonical(width=999, height=999, text="corrected"), visible=(0, 3)),
    ])
    mask = np.zeros((20, 20), np.uint8)
    mask[1:12, 1:12] = 1
    mask[4:9, 4:9] = 0  # hole
    mask[15:19, 15:19] = 1  # disconnected island
    moved = np.rot90(mask).copy()
    occluded = moved.copy()
    occluded[:, :8] = 0
    track = ObjectTrack(1, {f: Region(f, 1, (255, 0, 0), bbox, int(m.sum()), (10, 10), m)
        for f, bbox, m in [(0, (0, 0, 20, 20), mask), (1, (10, 5, 30, 25), moved), (2, (10, 5, 30, 25), occluded)]})
    box = TextBox(1, '실제 OCR <text>', (3, 24, 28, 29), .87)
    for name, obj in [("tracks", [track]), ("text", {"tracks": [TextTrack(1, {1: box}, box.text)], "boxes": [[], [box], [], []]})]:
        (stages / f"{name}.pkl").write_bytes(pickle.dumps(obj))
    (stages / "ids.json").write_text(json.dumps({"o1": "graphic", "t1": "text"}))
    np.save(stages / "frames.npy", np.full((4, 30, 40, 3), 32, np.uint8))
    ref = snapshot_from_stages(sd, scene)
    project = init_project(root, {"file": "fixture"}, scene, analysis_file=ref)
    return root, scene, project.versions[0]


def test_observations_preserve_holes_islands_motion_occlusion_and_absence(observed_project):
    root, scene, v = observed_project
    first = frame_overlay(root, scene, v, 0)
    region = first["objects"][0]["regions"][0]
    assert region["bbox"] == [1, 1, 19, 19]  # not the 999px reconstruction
    assert len(region["rings"]) == 3
    assert sum(r["parent"] == -1 for r in region["rings"]) == 2
    hole = next(r for r in region["rings"] if r["parent"] != -1)
    assert cv2.pointPolygonTest(np.array(hole["points"], np.int32), (6, 6), False) > 0
    second = frame_overlay(root, scene, v, 1)
    assert second["objects"][0]["id"] == first["objects"][0]["id"]
    assert second["objects"][0]["regions"] != first["objects"][0]["regions"]
    assert frame_overlay(root, scene, v, 2)["objects"][0]["regions"] != second["objects"][0]["regions"]
    assert second["objects"][1]["ocr"] == [{"text": '실제 OCR <text>', "confidence": .87, "bbox": [3, 24, 28, 29]}]
    assert second["objects"][1]["regions"][0]["bbox"] == [3, 24, 28, 29]
    assert frame_overlay(root, scene, v, 3)["objects"] == []
    manifest = read_manifest(root, v.analysis_file)
    assert manifest["objects"][0]["intervals"] == [[0, 2]]
    assert manifest["objects"][1]["intervals"] == [[1, 1]]


def test_snapshots_inherit_on_edit_and_freeze_frames_and_detections(observed_project):
    root, scene, v1 = observed_project
    before = frame_overlay(root, scene, v1, 0)
    orig = ReviewState(root, "s1").orig_png(0, "v1")
    scene.elements[0].visible = (2, 2)  # composition editing does not rewrite observations
    v2 = new_version(root, "s1", scene, "edit")
    assert v2.analysis_file == v1.analysis_file
    stages = scene_dir(root, "s1") / "stages"
    tracks = pickle.loads((stages / "tracks.pkl").read_bytes())
    tracks[0].regions[0].mask[:] = 1
    (stages / "tracks.pkl").write_bytes(pickle.dumps(tracks))
    np.save(stages / "frames.npy", np.zeros((4, 30, 40, 3), np.uint8))
    v3 = new_version(root, "s1", scene, "reanalyze", analysis_file=snapshot_from_stages(stages.parent, scene))
    assert v3.analysis_file != v1.analysis_file
    assert frame_overlay(root, scene, v1, 0) == before
    assert ReviewState(root, "s1").orig_png(0, "v1") == orig
    assert ReviewState(root, "s1").orig_png(0, "v3") != orig
    assert frame_overlay(root, scene, v3, 0)["objects"] != before["objects"]


def test_legacy_migration_only_binds_latest_and_never_guesses_history(observed_project):
    root, scene, _ = observed_project
    new_version(root, "s1", scene, "legacy edit")
    path = root / "project.json"
    project = json.loads(path.read_text())
    project.pop("analysis_migrated")
    for v in project["versions"]:
        v.pop("analysis_file")
    path.write_text(json.dumps(project))
    migrated = load_project(root)
    assert migrated.versions[0].analysis_file is None
    assert migrated.versions[1].analysis_file
    assert not frame_overlay(root, scene, migrated.versions[0], 1)["available"]
    assert load_project(root) == migrated
    branch = new_version(root, "s1", scene, "edit historical", parent_version="v1")
    assert branch.parent == "v1" and branch.analysis_file is None
    assert new_version(root, "s1", scene, "edit", parent_version="v2").analysis_file == migrated.versions[1].analysis_file


def test_overlay_endpoint_is_versioned_and_validates_frame(observed_project):
    root, scene, _ = observed_project
    srv = start(root.parent)
    try:
        code, _, body = get(srv, "/api/analysis-overlay?project=p1&scene=s1&frame=1&v=v1")
        assert code == 200
        data = json.loads(body)
        assert data["version"] == "v1" and data["frame"] == 1 and data["size"] == [40, 30]
        assert data["available"] and data["snapshot"]
        assert len(data["objects"]) == 2
        for frame in [-1, 4]:
            assert get(srv, f"/api/analysis-overlay?project=p1&scene=s1&frame={frame}")[0] == 404
        assert get(srv, "/api/analysis-overlay?project=p1&frame=oops")[0] == 400
        assert get(srv, "/api/analysis-overlay?project=p1&v=unknown")[0] == 404
        assert get(srv, "/api/analysis-overlay?scene=s1")[0] == 400
        state = json.loads(get(srv, "/api/state?project=p1&scene=s1")[2])
        assert all("confidence" not in el for el in state["scene"]["elements"])
    finally:
        srv.shutdown()
        srv.server_close()


def test_manual_text_edit_keeps_observations_and_text_box_correction_forks(observed_project, monkeypatch):
    from keepframe.review import corrections
    root, scene, v1 = observed_project
    before = frame_overlay(root, scene, v1, 1)
    text_edit = corrections.edit_text(root, "s1", "text", text="보정된 문구")
    assert text_edit.analysis_file == v1.analysis_file
    # Isolate the observation update from sprite fitting, which has separate pipeline tests.
    def rerun(root, sid, stage, note):
        assert stage == "regions"
        return new_version(root, sid, scene, note, analysis_file=snapshot_from_stages(scene_dir(root, sid), scene))
    monkeypatch.setattr(corrections, "rerun", rerun)
    corrected = corrections.add_bbox_prompt(root, "s1", 1, (4, 23, 29, 30), "text")
    assert corrected.analysis_file != text_edit.analysis_file
    now = frame_overlay(root, scene, corrected, 1)["objects"][1]
    assert now["regions"][0]["bbox"] == [4, 23, 29, 30]
    assert now["regions"][0]["source"] == "manual_box"
    assert now["ocr"][0]["confidence"] == .87
    assert frame_overlay(root, scene, v1, 1) == before
