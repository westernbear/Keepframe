import json, pickle, re
import cv2, numpy as np, pytest
from keepframe.analyze.movers import established_mask, find_movers, instability
from keepframe.analyze.pipeline import AnalyzeOptions, analyze_scene_frames, rerun
from keepframe.analyze.plate import load_plate, sample_frames
from keepframe.analyze.text import TextBox, TextTrack
from keepframe.ir.colour import delta_e, srgb_to_lab
from keepframe.ir.gradient import render_gradient
from keepframe.ir.schema import Gradient, GradientStop
from keepframe.ir.store import current_scene, init_project, scene_dir
from keepframe.ir.synth import make_spinning_sphere_video
from keepframe.review.overlay import snapshot_from_stages

OPTS = AnalyzeOptions(ocr=False, refine=False, use_ecc=False, generate_3d=False)
MESSAGE = re.compile(r"^(e\d+) animates in place; kept as a still sprite$")
W, H = 320, 180
CX, CY, R = 200, 96, 44
GLOBE_BOX = (CX - R, CY - R, CX + R, CY + R)


def _plate():
    g = Gradient(kind="linear", angle=135.0, stops=[GradientStop(offset=0, color="#0b1430"), GradientStop(offset=1, color="#2a4a7a")])
    return render_gradient(g, W, H)


def _globe_clip(n=32, deg=5.0, r=R, cx=CX, cy=CY, spin=lambda f, n: f):
    """A dotted globe turning in place on a gradient (3× supersampled): dark body, light dots on the land."""
    plate, ss = _plate(), 3
    yy, xx = np.mgrid[:H * ss, :W * ss].astype(np.float64) / ss + 0.5 / ss
    x, y = xx - cx, yy - cy
    inside = x * x + y * y <= r * r
    z = np.sqrt(np.maximum(r * r - x * x - y * y, 0))
    lat = np.arcsin(np.clip(y / r, -1, 1))
    shade = (0.55 + 0.45 * z / r)[..., None]
    big = cv2.resize(plate, (W * ss, H * ss), interpolation=cv2.INTER_NEAREST).astype(np.float64)
    out = np.empty((n, H, W, 3), np.uint8)
    for f in range(n):
        lon = np.arctan2(x, z) + np.radians(deg * spin(f, n))
        land = np.sin(3 * lon) * np.cos(2 * lat) + 0.5 * np.sin(5 * lon + 2 * lat) > -0.4
        dot = land & (((lon * 12 / np.pi) % 1 - 0.5) ** 2 + ((lat * 12 / np.pi) % 1 - 0.5) ** 2 < 0.13)
        col = np.where(dot[..., None], np.float64((150, 200, 255)), np.float64((40, 70, 120))) * shade
        out[f] = cv2.resize(np.where(inside[..., None], col, big), (W, H), interpolation=cv2.INTER_AREA).round().clip(0, 255)
    return out


def _sd(root):
    return scene_dir(root, "s1")


def _movers(root):
    return pickle.loads((_sd(root) / "stages" / "movers.pkl").read_bytes())


def _props(root):
    return pickle.loads((_sd(root) / "stages" / "props.pkl").read_bytes())


def _messages(root):
    return json.loads((_sd(root) / "report.json").read_text())["messages"]


def _union_box(m):
    boxes = np.array([b for b, _ in m.frames.values()])
    return boxes[:, 0].min(), boxes[:, 1].min(), boxes[:, 2].max(), boxes[:, 3].max()


@pytest.fixture(scope="module")
def globe(tmp_path_factory):
    root = tmp_path_factory.mktemp("globe")
    frames = _globe_clip()
    scene = analyze_scene_frames(frames, 30, root, "s1", OPTS)
    return root, scene, frames


def test_rotating_globe_on_gradient_becomes_mover_not_plate(globe):
    root, scene, frames = globe
    movers = _movers(root)
    assert len(movers) == 1
    m = movers[0]
    assert np.abs(np.subtract(_union_box(m), GLOBE_BOX)).max() <= 4
    assert len(m.frames) == len(frames) and not m.stable and m.residual >= 3
    plate = load_plate(_sd(root))
    yy, xx = np.mgrid[:H, :W]
    under = (xx - CX) ** 2 + (yy - CY) ** 2 <= R * R
    de = delta_e(srgb_to_lab(plate.image), srgb_to_lab(_plate()))
    assert np.percentile(de[under], 95) < 3 and np.percentile(de, 99) < 3
    assert not plate.stats.get("video_candidate")   # the globe no longer animates the plate
    p = _props(root)["m1"]
    assert (p["kind"], p["z"], p["mover"], p["stable"]) == ("sprite", 0, True, False)
    ids = json.loads((_sd(root) / "stages" / "ids.json").read_text())
    el = scene.element(ids["m1"])
    assert el.kind == "sprite" and el.z.keys[0].v == 0 and el.visible == (0, len(frames) - 1)
    assert abs(el.canonical.width - 2 * R) <= 6 and abs(el.canonical.height - 2 * R) <= 6
    hits = [MESSAGE.match(s) for s in _messages(root)]
    assert [h.group(1) for h in hits if h] == [ids["m1"]]


def test_globe_fragments_are_claimed(globe):
    root, scene, _ = globe
    m = _movers(root)[0]
    tracks = pickle.loads((_sd(root) / "stages" / "tracks.pkl").read_bytes())
    x0, y0, x1, y1 = GLOBE_BOX
    inside = {t.id for t in tracks if all(x0 - 8 <= r.centroid[0] <= x1 + 8 and y0 - 8 <= r.centroid[1] <= y1 + 8
                                          for r in t.regions.values())}
    assert inside and set(m.claimed) == inside   # pass 1 shredded the globe; the mover takes every fragment
    assert [e.id for e in scene.elements] == [json.loads((_sd(root) / "stages" / "ids.json").read_text())["m1"]]
    assert not any(k.startswith(("o", "solid")) for k in _props(root))


def test_mover_claims_shape_tracks_never_text():
    frames = _globe_clip(n=24)
    title = (CX - 30, CY - 8, CX + 30, CY + 8)
    frames[:, title[1]:title[3], title[0]:title[2]] = (250, 250, 250)
    text = TextTrack(1, {f: TextBox(f, "Hi", title, 0.99) for f in range(len(frames))}, "Hi")
    junk_box = (CX - 20, CY + 18, CX + 4, CY + 30)
    junk = TextTrack(2, {f: TextBox(f, "x", junk_box, 0.4) for f in range(0, len(frames), 2)}, "x")
    sample = sample_frames(len(frames))
    est = established_mask((H, W), [text], [], len(frames), sample)
    u = instability(frames, sample, est)
    plate = np.median(frames, 0).astype(np.uint8)
    movers = find_movers(frames, sample, u, [], [junk], plate=plate, text_tracks=[text], established=est)
    assert len(movers) == 1 and movers[0].claimed_shapes == [2] and movers[0].claimed == []
    for f, ((bx0, by0, bx1, by1), mk) in movers[0].frames.items():   # the title stays its own layer
        full = np.zeros((H, W), bool)
        full[by0:by1, bx0:bx1] = mk
        assert not full[title[1]:title[3], title[0]:title[2]].any()


def _striped_card_clip(n=32, w=100, h=84, travel=220):
    """A large striped card sliding across the gradient, over each pixel of its middle path in ~45 % of the
    frames: busy enough that those pixels look unstable, but tracked as a card."""
    plate = _plate()
    out = np.repeat(plate[None], n, 0)
    tex = np.zeros((h, w, 3), np.uint8)
    tex[:] = (230, 120, 60)
    for x in range(0, w, 12):
        tex[:, x:x + 6] = (250, 230, 120)
    for f in range(n):
        x0 = round(travel * f / (n - 1))
        out[f, 48:48 + h, x0:x0 + w] = tex
    return out


def test_sliding_card_is_not_a_mover(tmp_path):
    frames = _striped_card_clip()
    sample = sample_frames(len(frames))
    raw = instability(frames, sample, None)
    assert np.nanmean(raw >= 0.4) * W * H > 0.04 * W * H   # without the card's track it would look unstable
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", OPTS)
    assert _movers(tmp_path) == []
    assert not any(k.startswith("m") for k in _props(tmp_path))
    assert scene.elements and all(e.kind == "sprite" for e in scene.elements)


@pytest.mark.parametrize("case", ["small", "brief"])
def test_small_or_brief_instability_is_not_mover(tmp_path, case):
    if case == "small":
        frames = _globe_clip(r=13)   # ~0.9 % of the frame
    else:
        frames = _globe_clip(spin=lambda f, n: min(f, n // 5))   # turns for the first fifth, then holds still
    analyze_scene_frames(frames, 30, tmp_path, "s1", OPTS)
    assert _movers(tmp_path) == []
    assert not any(k.startswith("m") for k in _props(tmp_path))
    assert not any(MESSAGE.match(s) for s in _messages(tmp_path))


def _drifting_waves(n=16, W=160, H=96):
    """Soft colour waves drifting over the whole frame: every pixel changes (too faint for pass 1 to call most of
    it foreground), nothing is still around it."""
    yy, xx = np.mgrid[:H, :W].astype(np.float64)
    out = []
    for f in range(n):
        a = np.sin(2 * np.pi * (xx + 0.5 * yy + 4 * f) / 40)
        b = np.sin(2 * np.pi * (yy - 0.3 * xx - 3 * f) / 56)
        out.append(np.stack([120 + 15 * a, 110 + 10 * b, 150 + 12 * a * b], -1))
    return np.array(out).round().clip(0, 255).astype(np.uint8)


def test_full_frame_animation_is_plate_not_mover(tmp_path):
    frames = _drifting_waves()
    sample = sample_frames(len(frames))
    u = instability(frames, sample, None)
    assert np.nanmean(u >= 0.4) > 0.6
    assert find_movers(frames, sample, u, [], [], plate=frames[0]) == []
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", OPTS)
    assert _movers(tmp_path) == [] and scene.background.kind == "image"
    assert load_plate(_sd(tmp_path)).stats["video_candidate"] is True


def test_spinning_sphere_on_flat_bg_remains_solid(tmp_path):
    analyze_scene_frames(make_spinning_sphere_video(frames=16), 30, tmp_path, "s1", OPTS)   # no bg override
    props = _props(tmp_path)
    assert _movers(tmp_path) == []
    assert any("fragments" in p for p in props.values()) and not any(k.startswith("m") for k in props)


def test_overlay_shows_mover_masks(globe):
    root, scene, frames = globe
    ref = snapshot_from_stages(_sd(root), scene)
    eid = json.loads((_sd(root) / "stages" / "ids.json").read_text())["m1"]
    manifest = json.loads((root / ref).read_text())
    assert {"id": eid, "kind": "sprite", "intervals": [[0, len(frames) - 1]]} in manifest["objects"]
    for f in (0, 17):
        obs = {o["id"]: o for o in json.loads((root / ref).with_name(f"{f}.json").read_text())}
        (region,) = obs[eid]["regions"]
        assert region["source"] == "mask" and np.abs(np.subtract(region["bbox"], GLOBE_BOX)).max() <= 4


def test_mover_ids_stable_across_rerun(tmp_path):
    scene = analyze_scene_frames(_globe_clip(n=24), 30, tmp_path, "s1", OPTS)
    init_project(tmp_path, {"file": "ref.mp4", "fps": 30, "size": [W, H]}, scene)
    eid = json.loads((_sd(tmp_path) / "stages" / "ids.json").read_text())["m1"]
    for stage in ("regions", "sprites", "plate"):
        rerun(tmp_path, "s1", stage, f"rerun from {stage}")
        again, _ = current_scene(tmp_path, "s1")
        assert json.loads((_sd(tmp_path) / "stages" / "ids.json").read_text())["m1"] == eid
        assert [e.id for e in again.elements] == [eid] and again.element(eid).z.keys[0].v == 0
    assert [m.id for m in _movers(tmp_path)] == [1]
    (_sd(tmp_path) / "stages" / "movers.pkl").unlink()   # a project analysed before movers: the plate is rebuilt
    rerun(tmp_path, "s1", "keyframes", "rerun without a movers cache")
    assert [m.id for m in _movers(tmp_path)] == [1] and [e.id for e in current_scene(tmp_path, "s1")[0].elements] == [eid]


def _track(tid, frames, box):
    from keepframe.analyze.regions import Region
    from keepframe.analyze.tracking import ObjectTrack
    x0, y0, x1, y1 = box
    mask = np.ones((y1 - y0, x1 - x0), bool)
    return ObjectTrack(tid, {f: Region(f, 1, (0.0, 0.0, 0.0), box, int(mask.sum()), ((x0 + x1) / 2, (y0 + y1) / 2), mask)
                             for f in frames})


def test_halo_fragment_is_claimed_but_a_layer_beside_the_mover_is_not():
    frames = _globe_clip(n=24)
    logo = (CX + R + 4, CY - 8, CX + R + 36, CY + 8)   # about half inside the mover's dilated component
    frames[:, logo[1]:logo[3], logo[0]:logo[2]] = (250, 60, 60)
    halo = _track(1, range(24), (CX - 20, CY - R - 30, CX + 20, CY - R + 20))   # globe top + plate above it
    beside = _track(2, range(24), logo)
    sample = sample_frames(len(frames))
    est = established_mask((H, W), [], [beside], len(frames), sample)
    u = instability(frames, sample, est)
    (m,) = find_movers(frames, sample, u, [halo, beside], [], plate=_plate(), established=est)
    assert m.claimed == [1]   # its plate-coloured part does not count against it
    for (bx0, by0, bx1, by1), mk in m.frames.values():
        full = np.zeros((H, W), bool)
        full[by0:by1, bx0:bx1] = mk
        assert not full[logo[1]:logo[3], logo[0]:logo[2]].any()   # the layer beside it stays out of its mask
        assert not full[:CY - R - 4].any()                        # and so does the halo's plate part


def _slow_card_clip(n=32, w=120, h=84, travel=48):
    """A striped card drifting so slowly that pass 1 bakes it into its plate (only a halo becomes a region)."""
    out = np.repeat(_plate()[None], n, 0)
    tex = np.zeros((h, w, 3), np.uint8)
    tex[:] = (230, 120, 60)
    for x in range(0, w, 12):
        tex[:, x:x + 6] = (250, 230, 120)
    for f in range(n):
        x0 = 60 + round(travel * f / (n - 1))
        out[f, 48:48 + h, x0:x0 + w] = tex
    return out


def test_stable_mover_gets_median_texture_and_translation(tmp_path):
    scene = analyze_scene_frames(_slow_card_clip(), 30, tmp_path, "s1", OPTS)
    (m,) = _movers(tmp_path)
    assert m.stable and m.residual < 3
    p = _props(tmp_path)["m1"]
    assert p["stable"] and p["z"] == 0
    alpha = p["canon"][..., 3] > 127
    assert abs(int(alpha.sum()) - 120 * 84) <= 120   # the card, without the mask's 2 px dilation
    assert np.abs(p["raw"][[0, 31], 0] - [120, 168]).max() <= 1 and np.abs(p["raw"][:, 1] - 90).max() <= 1
    assert load_plate(_sd(tmp_path)).kind == "gradient"
    assert not any(MESSAGE.match(s) for s in _messages(tmp_path))
    ids = json.loads((_sd(tmp_path) / "stages" / "ids.json").read_text())
    assert [e.id for e in scene.elements] == [ids["m1"]]


def test_mover_search_failure_keeps_plate_with_message(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("keepframe.analyze.movers.find_movers", boom)
    analyze_scene_frames(_globe_clip(n=16), 30, tmp_path, "s1", OPTS)
    assert _movers(tmp_path) == []
    pj = json.loads((_sd(tmp_path) / "stages" / "plate.json").read_text())
    bconf = json.loads((_sd(tmp_path) / "stages" / "background.json").read_text())["confidence"]
    assert pj["stats"]["movers_message"] == "movers skipped: RuntimeError: boom" and "movers" not in pj["stats"]
    assert pj["confidence"] <= 0.5 * bconf
    assert "movers skipped: RuntimeError: boom" in _messages(tmp_path)
