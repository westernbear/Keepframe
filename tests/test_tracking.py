import numpy as np
from keepframe.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from keepframe.ir.synth import make_texture
from keepframe.ir.tracks import eval_props, element_bbox
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.background import foreground_mask
from keepframe.analyze.regions import build_palette, extract_regions
from keepframe.analyze.tracking import track_regions


def three_object_scene(d):
    make_texture(d / "assets/a.png", "rect", 40, 30, (239, 71, 111))
    make_texture(d / "assets/b.png", "ellipse", 36, 36, (6, 214, 160))
    make_texture(d / "assets/c.png", "rect", 24, 24, (255, 209, 102))
    els = [
        Element(id="a", kind="sprite", canonical=Canonical(width=40, height=30, texture="assets/a.png"), visible=(0, 29),
                tracks={"x": Track(keys=[Keyframe(t=0, v=60), Keyframe(t=29, v=260)]), "y": Track(keys=[Keyframe(t=0, v=60)])}),
        Element(id="b", kind="sprite", canonical=Canonical(width=36, height=36, texture="assets/b.png"), visible=(0, 29),
                tracks={"x": Track(keys=[Keyframe(t=0, v=260), Keyframe(t=29, v=60)]), "y": Track(keys=[Keyframe(t=0, v=120)])}),
        Element(id="c", kind="sprite", canonical=Canonical(width=24, height=24, texture="assets/c.png"), visible=(10, 29),
                tracks={"x": Track(keys=[Keyframe(t=10, v=160)]), "y": Track(keys=[Keyframe(t=10, v=30), Keyframe(t=29, v=150)])}),
    ]
    return Scene(id="t3", size=(320, 180), fps=30, frames=30, background=Background(value="#101418"), elements=els)


def test_tracks_are_consistent_and_follow_ground_truth(tmp_scene_dir):
    scene = three_object_scene(tmp_scene_dir)
    frames = np.stack([(composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8) for f in range(30)])
    bg = (0x10, 0x14, 0x18)
    fg = np.stack([foreground_mask(f, bg) for f in frames])
    pal = build_palette(frames, fg)
    rbf = [extract_regions(i, frames[i], fg[i], pal) for i in range(30)]
    tracks = track_regions(rbf)
    assert len(tracks) == 3
    by_first = sorted(tracks, key=lambda t: (t.first, t.regions[t.first].centroid[0]))
    assert [t.first for t in by_first] == [0, 0, 10] and all(t.last == 29 for t in tracks)
    gt = {"a": scene.elements[0], "b": scene.elements[1], "c": scene.elements[2]}

    def _occluded(el, f):
        eb = element_bbox(el, f)
        for other in scene.elements:
            if other.id == el.id:
                continue
            ob = element_bbox(other, f)
            if not (eb[2] <= ob[0] or ob[2] <= eb[0] or eb[3] <= ob[1] or ob[3] <= eb[1]):
                return True
        return False

    for t, eid in zip(by_first, ["a", "b", "c"]):
        for f, r in t.regions.items():
            if _occluded(gt[eid], f):
                continue
            p = eval_props(gt[eid], f)
            assert abs(r.centroid[0] - p["x"]) < 1.5 and abs(r.centroid[1] - p["y"]) < 1.5, (eid, f)


def test_occlusion_rejoins_track_despite_interleaved_birth(tmp_path):
    from keepframe.analyze.pipeline import _stage_tracking
    from keepframe.analyze.regions import Region

    def region(frame, x):
        return Region(
            frame, 1, (239.0, 71.0, 111.0), (x, 20, x + 20, 40),
            400, (x + 9.5, 29.5), np.ones((20, 20), bool),
        )

    observations = []
    for frame in range(13):
        regions = []
        if frame <= 3 or frame >= 8:
            regions.append(region(frame, 10 + frame))
        if frame >= 2:
            regions.append(region(frame, 300))
        observations.append(regions)

    tracks = _stage_tracking(observations, tmp_path)
    assert len(tracks) == 2
    resumed = next(track for track in tracks if track.first == 0)
    assert set(resumed.regions) == set(range(4)) | set(range(8, 13))
    other = next(track for track in tracks if track is not resumed)
    assert set(other.regions) == set(range(2, 13))
    assert other.regions[8].centroid == (309.5, 29.5)


def test_occlusion_recovery_does_not_join_distant_same_color_objects(tmp_path):
    from keepframe.analyze.pipeline import _stage_tracking
    from keepframe.analyze.regions import Region

    observations = []
    for frame in range(13):
        if 4 <= frame < 8:
            observations.append([])
            continue
        x = 10 if frame < 4 else 300
        observations.append([Region(
            frame, 1, (239.0, 71.0, 111.0), (x, 20, x + 20, 40),
            400, (x + 9.5, 29.5), np.ones((20, 20), bool),
        )])

    tracks = _stage_tracking(observations, tmp_path)
    assert [set(track.regions) for track in tracks] == [set(range(4)), set(range(8, 13))]
