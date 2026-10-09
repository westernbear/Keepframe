"""Task 12: a background colour request on a picture/gradient/video background asks tint / replace / cancel; tint
moves the background to the target's lightness and hue and keeps its light/dark structure (D5); cancel applies
nothing; the original assets are never touched."""
import hashlib

import cv2
import numpy as np
import pytest
from scipy.stats import spearmanr

from keepframe.edit.agent import edit
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import Intent, Target, plan
from keepframe.ir.colour import delta_e, hex_to_rgb8, srgb_to_lab
from keepframe.ir.gradient import render_gradient
from keepframe.ir.schema import (Background, Canonical, Element, Gradient, GradientKey, GradientStop, Keyframe, Scene,
                                 Track)
from keepframe.ir.store import current_scene, init_project, load_project, scene_dir
from keepframe.render.renderer import RenderResult
from keepframe.verify.verifier import VerifyReport

NAVY = "#1a2a6c"
W, H = 160, 90


def _lab_target(hexs=NAVY):
    return srgb_to_lab(np.float32(hex_to_rgb8(hexs)))


def _picture(w=W, h=H, seed=3):
    """A 'gradient + globe' picture: a diagonal sky ramp, a bright textured disc and fine grain."""
    rng = np.random.default_rng(seed)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    t = (xs / w + ys / h) / 2
    img = np.stack([40 + 120 * t, 70 + 110 * t, 140 + 90 * t], -1)
    d = np.hypot(xs - 0.62 * w, ys - 0.5 * h)
    disc = d < 0.3 * h
    land = (np.sin(xs / 5) + np.cos(ys / 4)) > 0.3
    img[disc] = np.where(land[disc, None], [70, 150, 80], [60, 120, 210])
    img += rng.normal(0, 3, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def _white_plate(w=W, h=H):
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    v = 255 - 22 * np.hypot((xs - w / 2) / w, (ys - h / 2) / h)   # a soft vignette on white
    return np.clip(np.stack([v, v - 2, v - 4], -1), 0, 255).astype(np.uint8)


def _write(path, rgb):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))


def _read(path):
    return cv2.cvtColor(cv2.imread(str(path), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)


def _gradient():
    return Gradient(kind="linear", angle=135, stops=[GradientStop(offset=0, color="#f6d365"),
                                                     GradientStop(offset=0.55, color="#fda085"),
                                                     GradientStop(offset=1, color="#5b247a")])


def _background(kind, sd):
    if kind == "color":
        return Background(kind="color", value="#203040")
    if kind == "image":
        _write(sd / "assets" / "background.png", _picture())
        (sd / "assets" / "background_synthetic.png").write_bytes(b"mask")
        return Background(kind="image", value="assets/background.png", confidence=0.8,
                          synthetic="assets/background_synthetic.png")
    if kind == "gradient":
        _write(sd / "assets" / "background.png", render_gradient(_gradient(), W, H))
        return Background(kind="gradient", gradient=_gradient(), poster="assets/background.png")
    _write(sd / "assets" / "background.png", _picture())
    (sd / "assets" / "background.webm").write_bytes(b"webm")
    return Background(kind="video", value="assets/background.webm", poster="assets/background.png")


def _scene(bg):
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=20, height=10, color="#ffffff"), visible=(0, 4),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=40), Keyframe(t=4, v=60)]), "y": Track(keys=[Keyframe(t=0, v=40)])})
    return Scene(id="s1", size=(W, H), fps=30, frames=5, background=bg, elements=[el])


def _project(tmp_path, kind, monkeypatch=None):
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    scene = _scene(_background(kind, sd))
    init_project(root, {"file": "ref.mp4", "fps": 30, "size": [W, H]}, scene)
    if monkeypatch is not None:
        monkeypatch.setattr("keepframe.edit.agent.render", lambda html, scene, out_dir: RenderResult(
            frames_dir=out_dir / "frames", frames=list(range(scene.frames)), hashes=[],
            bboxes={"e1": [[30, 35, 50, 45]] * scene.frames}))
        monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_a, **_k: VerifyReport(
            schema_ok=True, keep_pass_rate=1, temporal=1, layer_probe_complete=True, passed=True))
    return root, scene_dir(root, "s1"), scene


def _assets(sd):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((sd / "assets").iterdir())}


def _intent():
    return Intent(targets=[Target(property="background", value=NAVY)])


@pytest.mark.parametrize("kind", ["image", "gradient", "video", "color"])
def test_background_edit_on_picture_needs_choice(tmp_path, monkeypatch, kind):
    root, sd, scene = _project(tmp_path, kind, monkeypatch)
    built = plan(scene, _intent())
    conflicts = [c for c in built.conflicts if c.id == "background_kind"]
    if kind == "color":
        assert not built.conflicts
        return
    assert len(conflicts) == 1
    c = conflicts[0]
    assert (c.element, c.choices) == ("background", ["tint", "replace", "cancel"]) and c.reason
    before = _assets(sd)
    res = edit(root, "s1", "make the background navy", confirm=True, intent=_intent())
    assert res.status == "needs_choice" and res.plan.conflicts[0].id == "background_kind"
    assert current_scene(root, "s1")[1].id == "v1" and _assets(sd) == before


def test_cancel_creates_no_version_or_assets(tmp_path, monkeypatch):
    root, sd, scene = _project(tmp_path, "image", monkeypatch)
    monkeypatch.setattr("keepframe.edit.agent.compose", lambda *_a, **_k: pytest.fail("cancel must not compose"))
    monkeypatch.setattr("keepframe.edit.agent.apply_edit", lambda *_a, **_k: pytest.fail("cancel must not apply"))
    before, files = _assets(sd), sorted(p.relative_to(root) for p in root.rglob("*"))
    for choices in ({"background_kind": "cancel"}, {"background": "cancel"}):
        res = edit(root, "s1", "make the background navy", confirm=True, intent=_intent(), choices=choices)
        assert res.status == "cancelled" and res.version is None and res.error is None
    assert [v.id for v in load_project(root).versions] == ["v1"]
    assert _assets(sd) == before and sorted(p.relative_to(root) for p in root.rglob("*")) == files
    assert current_scene(root, "s1")[0] == scene


def test_replace_sets_color(tmp_path, monkeypatch):
    root, sd, scene = _project(tmp_path, "image", monkeypatch)
    before = _assets(sd)
    res = edit(root, "s1", "make the background navy", confirm=True, intent=_intent(), choices={"background": "replace"})
    assert res.status == "done" and res.version.id == "v2", res
    edited, _ = current_scene(root, "s1")
    assert edited.background == Background(kind="color", value=NAVY, confidence=1.0)
    assert edited.elements == scene.elements and _assets(sd) == before


def _check_tint(out_rgb, original_rgb, target=NAVY):
    lab, lab0, t = srgb_to_lab(out_rgb.astype(np.float32)), srgb_to_lab(original_rgb.astype(np.float32)), _lab_target(target)
    mean = lab.reshape(-1, 3).mean(0)
    assert abs(mean[0] - t[0]) <= 3, (mean, t)
    assert float(delta_e(mean[1:], t[1:])) <= 3, (mean, t)
    rho = spearmanr(lab0[..., 0].ravel(), lab[..., 0].ravel()).statistic
    assert rho >= 0.98, rho
    return mean


def test_tint_writes_new_asset_and_keeps_original(tmp_path, monkeypatch):
    root, sd, scene = _project(tmp_path, "image", monkeypatch)
    original = (sd / "assets" / "background.png").read_bytes()
    (sd / "assets" / "background.tint1.png").write_bytes(b"an earlier asset of the same name")
    before = _assets(sd)
    res = edit(root, "s1", "make the background navy", confirm=True, intent=_intent(), choices={"background": "tint"})
    assert res.status == "done" and res.version.id == "v2", res
    edited, _ = current_scene(root, "s1")
    bg = edited.background
    assert (bg.kind, bg.value, bg.synthetic) == ("image", "assets/background.tint2.png", "assets/background_synthetic.png")
    after = _assets(sd)
    assert {k: after[k] for k in before} == before and set(after) - set(before) == {"background.tint2.png"}
    assert (sd / "assets" / "background.png").read_bytes() == original
    assert edited.elements == scene.elements
    _check_tint(_read(sd / bg.value), _picture())


def test_tint_white_plate_navy_target_is_dark(tmp_path):
    from keepframe.edit.tint import tint_background
    sd = tmp_path / "s1"
    _write(sd / "assets" / "background.png", _white_plate())
    bg = tint_background(Background(kind="image", value="assets/background.png"), sd, NAVY)
    out = _read(sd / bg.value)
    mean = _check_tint(out, _white_plate())
    assert mean[0] <= _lab_target()[0] + 3
    assert np.ptp(srgb_to_lab(out.astype(np.float32))[..., 0]) > 3     # the vignette is still there


def test_tint_gradient_maps_stops(tmp_path):
    from keepframe.edit.tint import background_stats, tint_background, tint_lab
    sd = tmp_path / "s1"
    keyed = _gradient().model_copy(update={"angle": 45})
    src = Background(kind="gradient", gradient=_gradient(), poster="assets/background.png",
                     gradient_keys=[GradientKey(t=0, gradient=_gradient()), GradientKey(t=30, gradient=keyed)])
    _write(sd / "assets" / "background.png", render_gradient(_gradient(), W, H))
    poster = (sd / "assets" / "background.png").read_bytes()
    stats = background_stats(src, sd, (W, H), target_hex=NAVY)
    bg = tint_background(src, sd, NAVY, size=(W, H))
    assert bg.kind == "gradient" and bg.poster == "assets/background.tint1.png"
    assert (sd / "assets" / "background.png").read_bytes() == poster
    pairs = [(src.gradient, bg.gradient)] + [(a.gradient, b.gradient) for a, b in zip(src.gradient_keys, bg.gradient_keys)]
    assert [k.t for k in bg.gradient_keys] == [0, 30]
    for old, new in pairs:
        assert (new.kind, new.angle, [s.offset for s in new.stops]) == (old.kind, old.angle, [s.offset for s in old.stops])
        want = tint_lab(srgb_to_lab(np.float32([hex_to_rgb8(s.color) for s in old.stops])), _lab_target(), stats)
        got = srgb_to_lab(np.float32([hex_to_rgb8(s.color) for s in new.stops]))
        assert float(delta_e(got, want).max()) <= 1.5
        old_l = [srgb_to_lab(np.float32(hex_to_rgb8(s.color)))[0] for s in old.stops]
        new_l = [srgb_to_lab(np.float32(hex_to_rgb8(s.color)))[0] for s in new.stops]
        assert np.argsort(old_l).tolist() == np.argsort(new_l).tolist()
    _check_tint(render_gradient(bg.gradient, W, H), render_gradient(src.gradient, W, H))
    _check_tint(_read(sd / bg.poster), render_gradient(_gradient(), W, H))


def test_tint_video_fails_soft_until_task_13(tmp_path, monkeypatch):
    root, sd, scene = _project(tmp_path, "video", monkeypatch)
    before = _assets(sd)
    res = edit(root, "s1", "make the background navy", confirm=True, intent=_intent(), choices={"background": "tint"})
    assert res.status == "failed" and res.error == "tint_unavailable"
    assert current_scene(root, "s1")[1].id == "v1" and _assets(sd) == before


def test_apply_cancel_and_colour_background_tint(tmp_path):
    """apply_edit alone: cancel leaves the background; tint on a flat colour is the colour itself."""
    sd = tmp_path / "s1"
    scene = _scene(_background("image", sd))
    out = apply_edit(scene, sd, [Target(property="background", value=NAVY)], {"background": "cancel"}, None)
    assert out.background == scene.background
    flat = _scene(Background(kind="color", value="#ffffff"))
    out = apply_edit(flat, sd, [Target(property="background", value=NAVY)], {"background": "tint"}, None)
    assert out.background == Background(kind="color", value=NAVY, confidence=1.0)
