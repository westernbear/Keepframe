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

pytestmark = pytest.mark.skip(reason="Task 12R: tint is the agent's explicit mode (Target.mode), no forced choice/cancel; not re-run (user)")
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
    if kind == "color":
        assert not built.conflicts
        return
    cid, choices = ("background_video", ["replace", "cancel"]) if kind == "video" else ("background_kind", ["tint", "replace", "cancel"])
    assert len(built.conflicts) == 1
    c = built.conflicts[0]
    assert (c.id, c.element, c.choices) == (cid, "background", choices) and c.reason
    before = _assets(sd)
    res = edit(root, "s1", "make the background navy", confirm=True, intent=_intent())
    assert res.status == "needs_choice" and res.plan.conflicts[0].id == cid
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


def _ab_miss(rgb, target=NAVY):
    mean = srgb_to_lab(rgb.astype(np.float32)).reshape(-1, 3).mean(0)
    return float(delta_e(mean[1:], _lab_target(target)[1:]))


def _check_tint(out_rgb, original_rgb, target=NAVY, pure_rgb=None):
    """Mean L within ±3 of L_T and the light/dark order kept (ρ ≥ 0.98). Mean (a, b) within ΔE 3 of the target
    when nothing had to leave sRGB (the white plate); for pictures whose dark parts cannot hold the target's
    chroma (R53: report, do not gate) the miss is printed and must be no worse than pure D5's (`pure_rgb`)."""
    lab, lab0, t = srgb_to_lab(out_rgb.astype(np.float32)), srgb_to_lab(original_rgb.astype(np.float32)), _lab_target(target)
    mean = lab.reshape(-1, 3).mean(0)
    assert abs(mean[0] - t[0]) <= 3, (mean, t)
    miss = _ab_miss(out_rgb, target)
    if pure_rgb is None:
        assert miss <= 3, (mean, t)
    else:
        print(f"mean (a, b) miss {miss:.2f} ΔE (pure D5 {_ab_miss(pure_rgb, target):.2f})")
        assert miss <= _ab_miss(pure_rgb, target) + 0.25
    rho = spearmanr(lab0[..., 0].ravel(), lab[..., 0].ravel()).statistic
    assert rho >= 0.98, rho
    return mean


def _pure(rgb, target=NAVY):
    from keepframe.edit.tint import image_stats, tint_rgb
    return tint_rgb(rgb, _lab_target(target), image_stats(rgb))


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
    _check_tint(_read(sd / bg.value), _picture(), pure_rgb=_pure(_picture()))


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
    src = Background(kind="gradient", value="#c06a80", gradient=_gradient(), poster="assets/background.png",
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
    value = tint_lab(srgb_to_lab(np.float32(hex_to_rgb8(src.value))), _lab_target(), stats)   # R54: AE's flat fallback
    assert float(delta_e(srgb_to_lab(np.float32(hex_to_rgb8(bg.value))), value)) <= 1.5
    pure = src.model_copy(update={"gradient": _tint_gradient_pure(src.gradient)})
    _check_tint(render_gradient(bg.gradient, W, H), render_gradient(src.gradient, W, H), pure_rgb=render_gradient(pure.gradient, W, H))
    _check_tint(_read(sd / bg.poster), render_gradient(_gradient(), W, H), pure_rgb=_pure(render_gradient(_gradient(), W, H)))


def _tint_gradient_pure(g):
    from keepframe.edit.tint import _tint_gradient, image_stats
    return _tint_gradient(g, _lab_target(), image_stats(render_gradient(g, W, H)))


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


@pytest.mark.parametrize("payload", [b"not a picture", None])
def test_tint_of_an_unreadable_picture_fails_with_a_code(tmp_path, payload):
    """A missing or corrupt picture: TintError('tint_failed'), no asset written, no exception text."""
    from keepframe.edit.tint import TintError, tint_background
    sd = tmp_path / "s1"
    (sd / "assets").mkdir(parents=True)
    if payload is not None:
        (sd / "assets" / "background.png").write_bytes(payload)
    with pytest.raises(TintError) as err:
        tint_background(Background(kind="image", value="assets/background.png"), sd, NAVY)
    assert err.value.code == "tint_failed" and str(err.value) == "tint_failed"
    assert sorted(p.name for p in (sd / "assets").iterdir()) == (["background.png"] if payload else [])


def test_tint_refuses_a_huge_picture_before_decoding(tmp_path, monkeypatch):
    from keepframe.edit import tint
    sd = tmp_path / "s1"
    _write(sd / "assets" / "background.png", _picture())
    monkeypatch.setattr(tint, "MAX_PIXELS", W * H - 1)
    monkeypatch.setattr(tint.cv2, "imread", lambda *_a, **_k: pytest.fail("decoded a picture over the cap"))
    with pytest.raises(tint.TintError):
        tint.tint_background(Background(kind="image", value="assets/background.png"), sd, NAVY)


def _ramp(c0, c1, w=200, h=112):
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    t = ((xs / w + ys / h) / 2)[..., None]
    return np.clip(np.float32(c0) * (1 - t) + np.float32(c1) * t, 0, 255).round().astype(np.uint8)


def _max_step(lab):
    lab = np.asarray(lab, np.float64)
    return float(max(np.linalg.norm(np.diff(lab, axis=1), axis=-1).max(), np.linalg.norm(np.diff(lab, axis=0), axis=-1).max()))


SMOOTH = {"pink→magenta (ig2)": ((250, 180, 210), (214, 60, 140)), "teal→blue": ((60, 200, 190), (20, 40, 160)),
          "orange→red": ((255, 170, 40), (180, 20, 30)), "white→grey": ((255, 255, 255), (150, 150, 150))}


@pytest.mark.parametrize("target", [NAVY, "#ffe066", "#e53935"])
@pytest.mark.parametrize("ramp", list(SMOOTH))
def test_tint_keeps_smooth_backgrounds_smooth(tmp_path, ramp, target):
    """No false edges: the largest neighbour step out stays within c = 3 times the largest step in. D5 itself moves
    L by k ≤ 1 times and (a, b) one-to-one, so a step keeps its size; the gamut limit then changes by at most
    CAP_LAMBDA_L = 2.5 chroma per unit L (and a bounded share per unit hue), so a step ΔL gains at most 2.5·ΔL of
    chroma: √(1 + 2.5²) ≈ 2.7 < 3. The written 8-bit picture adds rounding — near black one sRGB code is up to
    ~0.8 ΔE76 — so it gets 1 ΔE on top."""
    from keepframe.edit.tint import background_stats, tint_background, tint_lab
    from keepframe.ir.colour import srgb8_to_lab
    rgb = _ramp(*SMOOTH[ramp])
    sd = tmp_path / "s1"
    _write(sd / "assets" / "background.png", rgb)
    src = Background(kind="image", value="assets/background.png")
    lab0 = srgb8_to_lab(rgb).astype(np.float64)
    step_in = _max_step(lab0)
    mapped = tint_lab(lab0, _lab_target(target), background_stats(src, sd, target_hex=target))
    out = _read(sd / tint_background(src, sd, target).value)
    step_out, step_png = _max_step(mapped), _max_step(srgb8_to_lab(out))
    print(f"{ramp} → {target}: step in {step_in:.2f}, out {step_out:.2f}, written {step_png:.2f}")
    assert step_out <= 3 * step_in and step_png <= 3 * step_in + 1.0


def _vivid(kind, w=240, h=135):
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    if kind == "vivid green":
        t = (xs / w)[..., None]
        return np.clip(np.float32([20, 200, 60]) * (1 - t) + np.float32([160, 240, 40]) * t, 0, 255).astype(np.uint8)
    hsv = np.dstack([xs / w * 179, np.full_like(xs, 255), 120 + 120 * ys / h]).astype(np.uint8)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


@pytest.mark.parametrize("kind,target", [("rainbow", NAVY), ("vivid green", "#ffe066")])
def test_balance_is_a_small_correction(kind, target):
    """R53: the mean-(a, b) shift is at most ΔE 10, never gives a colour more chroma than pure D5 does, and keeps
    the picture's chroma variation within ±10 % of pure D5's (the mean error of such pictures is reported)."""
    from keepframe.edit.tint import balance, image_stats, tint_lab
    from keepframe.ir.colour import srgb8_to_lab
    rgb, t = _vivid(kind), _lab_target(target)
    lab0 = srgb8_to_lab(rgb.reshape(-1, 3)).astype(np.float64)
    pure_stats = image_stats(rgb)
    stats = balance(pure_stats, lab0, t)
    assert float(np.hypot(*stats.shift)) <= 10 + 1e-9
    pure, out = tint_lab(lab0, t, pure_stats), tint_lab(lab0, t, stats)
    assert float((np.hypot(out[:, 1], out[:, 2]) - np.hypot(pure[:, 1], pure[:, 2])).max()) <= 1e-9
    spread = lambda lab: float(np.sqrt(((lab[:, 1:] - lab[:, 1:].mean(0)) ** 2).sum(1).mean()))
    assert abs(spread(out) / spread(pure) - 1) <= 0.10, (spread(out), spread(pure))
    miss = lambda lab: float(np.hypot(*(lab[:, 1:].mean(0) - t[1:])))
    print(f"{kind} → {target}: |shift| {np.hypot(*stats.shift):.2f}, mean (a, b) miss {miss(out):.2f} (pure D5 {miss(pure):.2f})")
    assert miss(out) <= miss(pure) + 1e-6


def test_video_background_offers_replace_or_cancel(tmp_path, monkeypatch):
    """R54: until Task 13, a video background is not offered a tint."""
    root, sd, scene = _project(tmp_path, "video", monkeypatch)
    c = plan(scene, _intent()).conflicts
    assert [(x.id, x.element, x.choices) for x in c] == [("background_video", "background", ["replace", "cancel"])]
    res = edit(root, "s1", "make the background navy", confirm=True, intent=_intent(), choices={"background_video": "replace"})
    assert res.status == "done" and current_scene(root, "s1")[0].background.kind == "color"


@pytest.mark.parametrize("kind,choice,want", [
    ("image", None, "배경에 #1a2a6c 적용 — 방식을 고르세요. 트랙은 유지합니다."),
    ("image", "tint", "배경에 #1a2a6c 색을 입힙니다(밝고 어두운 결 유지). 트랙은 유지합니다."),
    ("image", "replace", "배경을 #1a2a6c 단색으로 바꿉니다. 트랙은 유지합니다."),
    ("color", None, "배경색을 #1a2a6c로 바꿉니다. 트랙은 유지합니다.")])
def test_background_summary_follows_the_choice(tmp_path, monkeypatch, kind, choice, want):
    """Review fix 4: before a choice the summary asks how; afterwards it says what was done; cancel says so."""
    from keepframe.edit.intent import describe
    root, sd, scene = _project(tmp_path, kind, monkeypatch)
    assert describe(_intent().targets, scene=scene, choices={"background": choice} if choice else None) == want
    preview = edit(root, "s1", "make the background navy", intent=_intent())
    assert preview.summary == describe(_intent().targets, scene=scene)
    if kind == "image":
        done = edit(root, "s1", "x", confirm=True, intent=_intent(), choices={"background_kind": choice or "cancel"})
        assert done.summary == (want if choice else "취소했습니다. 바뀐 것은 없습니다.")
        if choice:
            assert load_project(root).versions[-1].note.startswith(want)
