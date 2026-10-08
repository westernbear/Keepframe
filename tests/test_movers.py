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
from keepframe.ir.synth import make_spinning_sphere_video, make_texture
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
    assert (p["kind"], p["z"], p["mover"], p["stable"]) == ("sprite", -1, True, False)
    ids = json.loads((_sd(root) / "stages" / "ids.json").read_text())
    el = scene.element(ids["m1"])
    assert el.kind == "sprite" and el.z.keys[0].v == -1 and el.visible == (0, len(frames) - 1)
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
        assert [e.id for e in again.elements] == [eid] and again.element(eid).z.keys[0].v == -1
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
    assert p["stable"] and p["z"] == -1
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


# --- review round 1 -------------------------------------------------------------------------------------


class _Ocr:
    """Fake OCR: the same box of copy in every frame."""
    def __init__(self, box, text="Launch"):
        self.box, self.text = box, text

    def __call__(self, frame):
        return [(self.text, self.box, 0.99)]


OCR_OPTS = AnalyzeOptions(ocr=True, refine=False, use_ecc=False, generate_3d=False)
DISC = (lambda yy, xx: (xx - CX) ** 2 + (yy - CY) ** 2 <= (R - 1) ** 2)(*np.mgrid[:H, :W])


def _lift(frames, box, fs, lift=14):
    """A faint layer: the plate, lighter by `lift` (ΔE76 ≈ 6.8), at box in frames fs."""
    x0, y0, x1, y1 = box
    for f in fs:
        frames[f, y0:y1, x0:x1] = np.clip(_plate()[y0:y1, x0:x1].astype(int) + lift, 0, 255).astype(np.uint8)
    return frames


def test_faint_far_element_is_not_claimed(tmp_path):
    frames = _globe_clip(n=24)
    far = (10, 10, 60, 40)   # ΔE76 ≈ 6.8 above the plate, ~100 px from the globe
    faint = _track(7, range(24), far)
    _lift(frames, far, range(24))
    sample = sample_frames(len(frames))
    est = established_mask((H, W), [], [faint], len(frames), sample)
    (m,) = find_movers(frames, sample, instability(frames, sample, est), [faint], [], plate=_plate(), established=est)
    assert m.claimed == []
    frames = _lift(_globe_clip(n=32), (10, 120, 70, 170), range(12))   # through the pipeline: it stays a layer
    analyze_scene_frames(frames, 30, tmp_path, "s1", OPTS)
    tracks = pickle.loads((_sd(tmp_path) / "stages" / "tracks.pkl").read_bytes())
    mine = [t.id for t in tracks if all(r.bbox[2] <= 100 for r in t.regions.values())]
    assert mine and all(f"o{i}" in _props(tmp_path) for i in mine) and not set(mine) & set(_movers(tmp_path)[0].claimed)


@pytest.mark.parametrize("sigma", [1.5, 3.0])
def test_noisy_globe_mover_bbox_and_visibility(tmp_path, sigma):
    frames = _globe_clip(n=32)
    frames[:12] = _plate()   # the globe appears at frame 12
    noise = np.random.default_rng(0).normal(0, sigma, frames.shape)
    frames = np.clip(frames + noise, 0, 255).round().astype(np.uint8)
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", OPTS)
    (m,) = _movers(tmp_path)
    assert min(m.frames) == 12 and max(m.frames) == 31
    assert np.abs(np.subtract(_union_box(m), GLOBE_BOX)).max() <= 4
    ids = json.loads((_sd(tmp_path) / "stages" / "ids.json").read_text())
    assert scene.element(ids["m1"]).visible == (12, 31)


def test_mover_texture_covers_the_globe(globe):
    root, scene, frames = globe
    p = _props(root)["m1"]
    (m,) = _movers(root)
    x0, y0, x1, y1 = m.frames[p["cf"]][0]
    a = np.zeros((H, W))
    a[y0:y1, x0:x1] = p["canon"][..., 3] / 255.0
    assert a[DISC].mean() >= 0.95   # the dark body is the globe, not plate
    plate = load_plate(_sd(root)).image.astype(np.float64)
    comp = plate.copy()
    comp[y0:y1, x0:x1] = plate[y0:y1, x0:x1] * (1 - a[y0:y1, x0:x1, None]) + p["canon"][..., :3] * a[y0:y1, x0:x1, None]
    de = delta_e(srgb_to_lab(comp), srgb_to_lab(frames[p["cf"]]))
    assert np.percentile(de[DISC], 95) < 1.0


def test_mover_draws_below_text(tmp_path):
    from keepframe.analyze.composite import composite_scene
    from keepframe.ir.tracks import eval_z
    frames = _globe_clip(n=24)
    box = (CX - R - 40, CY - 10, CX - R + 24, CY + 10)   # left of the globe, over its edge, from frame 0
    frames[:, box[1]:box[3], box[0]:box[2]] = (245, 245, 245)
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", OCR_OPTS, ocr=_Ocr(box))
    ids = json.loads((_sd(tmp_path) / "stages" / "ids.json").read_text())
    mover, text = scene.element(ids["m1"]), next(e for e in scene.elements if e.kind == "text")
    assert text.visible[0] == 0 and scene.elements.index(text) < scene.elements.index(mover)   # a tie would draw it first
    assert eval_z(mover, 0) < min(eval_z(e, 0) for e in scene.elements if e is not mover)
    got = composite_scene(scene, _sd(tmp_path), 5) * 255
    over = np.zeros((H, W), bool)
    over[box[1] + 4:box[3] - 4, CX - R + 4:box[2] - 4] = True   # the title where it covers the globe
    assert np.percentile(delta_e(srgb_to_lab(got[over]), srgb_to_lab(frames[5][over])), 95) < 3


@pytest.mark.browser
def test_negative_z_layer_renders_above_the_stage_and_below_others(tmp_path):
    from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
    from keepframe.ir.synth import render_frames
    make_texture(tmp_path / "assets" / "under.png", "rect", 40, 40, (200, 40, 40))
    make_texture(tmp_path / "assets" / "over.png", "rect", 40, 40, (40, 40, 200))

    def el(eid, x, z):
        return Element(id=eid, kind="sprite", visible=(0, 0), canonical=Canonical(width=40, height=40, texture=f"assets/{eid}.png"),
                       tracks={"x": Track(keys=[Keyframe(t=0, v=x)]), "y": Track(keys=[Keyframe(t=0, v=40)])},
                       z=Track(keys=[Keyframe(t=0, v=z)]))

    scene = Scene(id="z", size=(120, 80), fps=30, frames=1, background=Background(value="#101418"),
                  elements=[el("over", 70, 0), el("under", 50, -1)])
    (img,) = render_frames(scene, tmp_path, [0], renderer="browser")
    assert np.abs(img[40, 35] - (200, 40, 40)).max() < 12   # the z −1 layer shows over the stage background
    assert np.abs(img[40, 62] - (40, 40, 200)).max() < 12   # and under the z 0 layer


def _titled_globe(n=24):
    """The globe with a text-like row of bars over its middle: the gaps between glyphs show the globe."""
    clean = _globe_clip(n=n)
    frames = clean.copy()
    box = (CX - 24, CY - 9, CX + 24, CY + 9)
    for x in range(box[0] + 2, box[2] - 2, 8):
        frames[:, box[1] + 2:box[3] - 2, x:x + 4] = (250, 250, 250)
    return clean, frames, box


def test_text_over_mover_is_filled_in(tmp_path):
    clean, frames, box = _titled_globe()
    scene = analyze_scene_frames(frames, 30, tmp_path, "s1", OCR_OPTS, ocr=_Ocr(box))
    ids = json.loads((_sd(tmp_path) / "stages" / "ids.json").read_text())
    p = _props(tmp_path)["m1"]
    (m,) = _movers(tmp_path)
    assert p["synthetic"] > 0 and m.cut
    msgs = _messages(tmp_path)
    assert f"{ids['m1']}: {p['synthetic']} px of its texture under text are filled in (synthetic)" in msgs
    alone = scene.model_copy(update={"elements": [scene.element(ids["m1"])]})   # plate + the mover sprite
    from keepframe.analyze.composite import composite_scene
    cf = p["cf"]
    got = composite_scene(alone, _sd(tmp_path), cf) * 255
    x0, y0, x1, y1 = box
    gaps = np.zeros((H, W), bool)
    gaps[y0 + 3:y1 - 3, x0 + 3:x1 - 3] = True
    gaps &= (frames[cf] < 240).any(-1)   # between the bars
    bx0, by0, bx1, by1 = m.frames[cf][0] if m.cut.get(cf) is None else (
        min(m.frames[cf][0][0], m.cut[cf][0][0]), min(m.frames[cf][0][1], m.cut[cf][0][1]),
        max(m.frames[cf][0][2], m.cut[cf][0][2]), max(m.frames[cf][0][3], m.cut[cf][0][3]))
    alpha = np.zeros((H, W))
    alpha[by0:by1, bx0:bx1] = p["canon"][..., 3]
    assert (alpha[gaps] == 255).all()   # opaque: the plate does not show between the glyphs
    ring = np.zeros((H, W), bool)
    ring[y0 - 8:y1 + 8, x0 - 8:x1 + 8] = True
    ring[y0:y1, x0:x1] = False
    globe = srgb_to_lab(np.median(clean[cf][ring], 0))
    de = lambda px: float(delta_e(srgb_to_lab(np.median(px, 0).astype(np.float64)), globe))
    assert de(got[gaps]) < 6 and de(got[gaps]) < de(load_plate(_sd(tmp_path)).image[gaps])   # globe-coloured


def test_mover_props_failure_drops_the_mover_and_keeps_its_fragments(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("props exploded")

    monkeypatch.setattr("keepframe.analyze.pipeline.mover_props", boom)
    scene = analyze_scene_frames(_globe_clip(), 30, tmp_path, "s1", OPTS)
    props = _props(tmp_path)
    assert not any(k.startswith("m") for k in props) and any(k.startswith("o") for k in props) and scene.elements
    assert "mover m1 dropped: RuntimeError: props exploded" in _messages(tmp_path)


def test_mover_cover_failure_in_solids_drops_the_movers(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cover exploded")

    monkeypatch.setattr("keepframe.analyze.pipeline.mover_cover", boom)
    analyze_scene_frames(_globe_clip(), 30, tmp_path, "s1", OPTS)
    assert len(_movers(tmp_path)) == 1   # found by the plate stage, then dropped
    assert not any(k.startswith("m") for k in _props(tmp_path))
    assert "movers dropped: RuntimeError: cover exploded" in _messages(tmp_path)


def test_faint_layer_beside_mover_without_overlap_is_not_claimed():
    import keepframe.analyze.movers as mv
    frames = _globe_clip(n=24)
    box = (CX + R + 18, 80, CX + R + 32, 112)   # ΔE76 ≈ 6.8, within the halo reach, outside the dilated component
    faint = _track(7, range(24), box)
    _lift(frames, box, range(24))
    sample = sample_frames(len(frames))
    est = established_mask((H, W), [], [faint], len(frames), sample)
    u = instability(frames, sample, est)
    (core,) = mv._components(frames, sample, u, est, 0.04, 0.40, 0.60)
    region = cv2.dilate(core, mv._kernel(mv.REFILL_PX)) > 0
    assert not region[box[1]:box[3], box[0]:box[2]].any()
    (m,) = find_movers(frames, sample, u, [faint], [], plate=_plate(), established=est)
    assert m.claimed == []
