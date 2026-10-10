"""Final review (R52, plan global constraint): every Stage A fail-soft path leaves a fixed code in report messages and
stage data — never the exception text, whose paths and tokens only reach the server log, scrubbed — and lowers the
affected elements' confidence once (FALLBACK_CONF), also across reruns."""
import json
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from keepframe.analyze import matting, pipeline, plate as plate_mod, text as text_module, textstyle
from keepframe.analyze.pipeline import AnalyzeOptions, _stage_solids, _stage_sprites, _texture_messages
from keepframe.analyze.text import TextBox, TextTrack
from keepframe.ir.gradient import render_gradient
from keepframe.ir.schema import Gradient, GradientStop, TextureMeta

LEAK = "/home/secret-operator/keepframe-ws/p1/stages/x.npy token=sk-live-4f9a8b7c6d5e"
FRAGMENTS = ("secret-operator", "keepframe-ws", "sk-live", "4f9a8b7c6d5e", "/home/", "RuntimeError")


def boom(*a, **k):
    raise RuntimeError(LEAK)


def _clean(*texts):
    for text in texts:
        found = [frag for frag in FRAGMENTS if frag in text]
        assert not found, (found, text[:300])


def _sd(tmp_path):
    (tmp_path / "stages").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_plate_v2_failure_is_a_code(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(pipeline, "build_plate", boom)
    sd = _sd(tmp_path)
    model = pipeline._stage_plate(np.zeros((2, 8, 8, 3), np.uint8), 30, [[], []], [[], []], [], [], [], (10, 20, 30),
                                  0.9, None, AnalyzeOptions(), sd)
    assert model.stats["message"] == "plate v2 skipped (plate_failed)"
    _clean((sd / "stages" / "plate.json").read_text())
    assert "…/x.npy" in caplog.text and "keepframe-ws" not in caplog.text    # the detail: in the log, scrubbed


def test_mover_search_and_classification_failures_are_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(plate_mod, "_movers", boom)
    monkeypatch.setattr(plate_mod, "classify", boom)
    g = Gradient(kind="linear", angle=90.0, stops=[GradientStop(offset=0, color="#202060"), GradientStop(offset=1, color="#e0a040")])
    frames = np.stack([render_gradient(g, 96, 54)] * 6)
    n = len(frames)
    model = plate_mod.build_plate(frames, [[] for _ in range(n)], [[] for _ in range(n)], [], [], bg_rgb=(0, 0, 0),
                                  bconf=0.6, bg_override=None, pass1=frames[0], obj_tracks=[])
    assert model.stats["movers_message"] == "movers skipped (movers_failed)"
    assert model.stats["message"] == "plate classification skipped (plate_classify_failed)"
    assert model.confidence <= 0.5 * 0.6
    plate_mod.save_plate(_sd(tmp_path), model)
    _clean((tmp_path / "stages" / "plate.json").read_text())


def test_solids_with_movers_failure_is_a_code(tmp_path, monkeypatch):
    calls = []

    def find(frames, fg, tracks, excluded):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError(LEAK)
        return []

    monkeypatch.setattr(pipeline, "find_solids", find)
    mover = SimpleNamespace(id=1, claimed=[4], claimed_shapes=[2])
    notes, dropped = [], []
    solids, movers = _stage_solids(np.zeros((2, 8, 8, 3), np.uint8), (0, 0, 0), [[], []], [], _sd(tmp_path),
                                   movers=[mover], notes=notes, dropped=dropped)
    assert (solids, movers, notes, dropped) == ([], [], ["movers dropped (movers_failed)"], [mover])


def _text_case():
    frames = np.zeros((1, 30, 50, 3), np.uint8)
    frames[0, 8:12, 10:24] = 255
    return frames, TextTrack(id=1, boxes={0: TextBox(0, "Text", (5, 3, 35, 23), 0.9)}, text="Text")


CASES = {   # name: (module, attribute, stored check, report message)
    "texture": (matting, "texture_v2", lambda props: props["t1"]["texture_error"] == "texture_failed",
                "e1: matted texture failed (texture_failed); kept the binary texture"),
    "textures": (matting, "_layers", lambda props: "texture_meta" not in props["t1"],
                 "textures v2 skipped (textures_failed)"),
    "style": (textstyle, "style_props", lambda props: "style" not in props["t1"], "text style skipped (style_failed)"),
    "full_opacity": (text_module, "full_opacity_frame",
                     lambda props: props["t1"]["full_opacity_error"] == "full_opacity_failed",
                     "e1: full-opacity frame not found (full_opacity_failed); kept the largest box"),
}


@pytest.mark.parametrize("case", list(CASES))
def test_sprite_phase_failures_are_codes(tmp_path, monkeypatch, case):
    module, name, stored, message = CASES[case]
    monkeypatch.setattr(module, name, boom)
    frames, track = _text_case()
    sd = _sd(tmp_path)
    props = _stage_sprites(frames, (0, 0, 0), [track], [], [], AnalyzeOptions(refine=False), sd, 1)
    assert stored(props)
    raw = (sd / "stages" / "props.pkl").read_bytes()
    _clean(raw.decode("latin-1"))
    messages = [m for m in (props.get("_textures_message"), props.get("_style_message")) if m]
    messages += _texture_messages(props, {"t1": "e1"})
    assert message in messages, messages
    _clean(*messages)
    assert "e1" in pipeline.fallback_ids(props, {"t1": "e1"})                 # less sure, whichever path


def test_mover_props_failure_is_a_code_and_flags_its_fragments(tmp_path, monkeypatch):
    """A mover dropped in the sprites stage (mover_props) or before it (solids with movers, `dropped`): the shapes
    it claimed come back as sprites, at lower confidence."""
    monkeypatch.setattr(pipeline, "mover_props", boom)
    frames, track = _text_case()
    other = TextTrack(id=2, boxes={0: TextBox(0, "Text", (36, 3, 48, 23), 0.9)}, text="Text")
    sd = _sd(tmp_path)
    props = _stage_sprites(frames, (0, 0, 0), [], [track, other], [], AnalyzeOptions(refine=False), sd, 1,
                           movers=[SimpleNamespace(id=3, claimed=[], claimed_shapes=[1])],
                           dropped=[SimpleNamespace(id=9, claimed=[], claimed_shapes=[2])])
    assert props["_movers_message"] == "mover m3 dropped (mover_dropped)"
    assert props["s1"]["mover_dropped"] == props["s2"]["mover_dropped"] == "mover_dropped"
    _clean((sd / "stages" / "props.pkl").read_bytes().decode("latin-1"))
    assert pipeline.fallback_ids(props, {"s1": "e1", "s2": "e2"}) == {"e1", "e2"}


def test_legacy_stage_text_is_never_shown():
    """Stage data from before the codes: the report message is the fixed one."""
    legacy = {"o1": {"texture_error": f"RuntimeError: {LEAK}"},
              "t2": {"fade_note": f"full-opacity frame not found (RuntimeError: {LEAK}); kept the largest box"}}
    msgs = _texture_messages(legacy, {"o1": "e1", "t2": "e2"})
    assert msgs == ["e1: matted texture failed (texture_failed); kept the binary texture",
                    "e2: full-opacity frame not found (full_opacity_failed); kept the largest box"]
    assert pipeline.fallback_ids(legacy, {"o1": "e1", "t2": "e2"}) == {"e1", "e2"}


def test_fallback_ids_cover_every_fail_soft_path_once():
    meta = TextureMeta(method="triangulation", frames=[0], confidence=0.9, padding=0).model_dump()
    props = {
        "o1": {"video_error": "encode_failed"}, "o2": {"texture_note": "no frame shows it"},
        "t3": {"style_error": "style_failed", "font_error": "match_failed", "kind": "text"},   # two reasons: once
        "t4": {"font_skipped": "work_cap"}, "t5": {"full_opacity_error": "full_opacity_failed"},
        "o6": {"mover_dropped": "mover_dropped"}, "o7": {"texture_meta": meta, "kind": "sprite"},
        "t8": {"kind": "text", "texture_meta": meta},
    }
    ids = {k: f"e{k[1:]}" for k in props}
    assert pipeline.fallback_ids(props, ids) == {"e1", "e2", "e3", "e4", "e5", "e6"}
    wholesale = {**props, "_textures_message": "textures v2 skipped (textures_failed)"}
    assert pipeline.fallback_ids(wholesale, ids) >= {"e7", "e8"}               # nothing was matted
    styled = {**props, "_style_message": "text style skipped (style_failed)"}
    assert pipeline.fallback_ids(styled, ids) == {"e1", "e2", "e3", "e4", "e5", "e6", "e8"}   # every text, no sprite
    solid = {"solid1": {"fragments": {"o10": {"mover_dropped": "mover_dropped"}, "o11": {}}}}
    assert pipeline.fallback_ids(solid, {"solid1": "e9", "o10": "e10", "o11": "e11"}) == {"e9", "e10"}


def test_low_confidence_texture_gets_a_message():
    meta = lambda c, method="triangulation": TextureMeta(method=method, frames=[0], confidence=c, padding=0).model_dump()
    props = {"o1": {"texture_meta": meta(0.04)}, "o2": {"texture_meta": meta(0.2)},
             "o3": {"texture_meta": meta(0.04, "binary")}}
    assert _texture_messages(props, {"o1": "e1", "o2": "e2", "o3": "e3"}) == [
        "e1: matted texture at low confidence (0.04); it replaced the binary texture"]


def test_confidence_lowered_once_and_not_compounded_on_rerun(tmp_path, monkeypatch):
    """End to end: a texture that fails to matte halves its element's confidence; a rerun from keyframes gives the
    same value (the factor applies to the measured confidence, never to a stored one)."""
    from keepframe.analyze.pipeline import analyze_scene_frames, rerun
    from keepframe.analyze.report import reconstruction_and_confidence
    from keepframe.ir.schema import Background, Keyframe, Track
    from keepframe.ir.store import current_scene, init_project, scene_dir
    from tests.test_matting import OPTS, _disc, _frames, _grad, _scene
    monkeypatch.setattr(matting, "texture_v2", boom)
    n = 8
    tracks = {"x": Track(keys=[Keyframe(t=0, v=80.0), Keyframe(t=n - 1, v=200.0)]), "y": Track(keys=[Keyframe(t=0, v=90.0)])}
    bg = Background(kind="gradient", gradient=_grad("#355c7d", "#c06c84", 90.0))
    truth = _scene(tmp_path / "truth", [("d", _disc(), tracks, 1)], bg, n)
    frames = _frames(truth, tmp_path / "truth", sigma=0.5)
    root = tmp_path / "proj"
    scene = analyze_scene_frames(frames, 30, root, "s1", OPTS)
    init_project(root, {"file": "ref.mp4", "fps": 30, "size": list(scene.size)}, scene)
    sd = scene_dir(root, "s1")
    (el,) = scene.elements
    measured = reconstruction_and_confidence(scene, sd, frames, 0)[1][el.id]
    assert el.confidence == pytest.approx(measured * 0.5)
    report = json.loads((sd / "report.json").read_text())
    assert report["confidence"][el.id] == pytest.approx(el.confidence)
    _clean(json.dumps(report), (sd / "stages" / "props.pkl").read_bytes().decode("latin-1"))
    rerun(root, "s1", "keyframes", note="again")
    again = current_scene(root, "s1")[0].element(el.id)
    assert again.confidence == pytest.approx(el.confidence)
