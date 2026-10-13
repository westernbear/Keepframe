import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from keepframe.cli import main
from keepframe.ir.schema import (Background, Canonical, Constraint, Element, FitError, FontGuess, Gradient, GradientKey,
                                 GradientStop, GlyphReveal, Group, Keyframe, Scene, TextEffect, TextStyle, TextureMeta,
                                 Track, UIComponent, UIModel, UIStateRange)
from keepframe.ir.score import dumps, from_score, loads, to_score
from keepframe.ir.store import save_scene
from keepframe.ir.synth import make_reference_scene, make_synthetic_scene
from keepframe.ir.tracks import PRESET_EASES

GOLDEN = Path(__file__).parent / "golden" / "score_example.json"


def _rare_scene() -> Scene:
    """Every scene/1 field the synthetic generators leave at its default."""
    grad = Gradient(stops=[GradientStop(offset=0.0, color="#ffffff"), GradientStop(offset=1.0, color="#ffe3ea")], angle=90.0)
    title = Element(
        id="title", kind="text", role="primary", visible=(18, 149), confidence=0.91, label="headline", caption="big title",
        canonical=Canonical(width=420.0, height=96.0, texture="assets/title.png", text="A Weekend Away", color="#a8001c",
                            font=FontGuess(family_guess="Urbanist", weight=800, size_px=96, candidates=["Urbanist", "Outfit"],
                                           scores=[0.9, 0.7], source="bundled"),
                            style=TextStyle(fill=grad, effects=[TextEffect(kind="shadow", color="#000000", opacity=0.3, dy=6, blur=18)],
                                            tracking_em=-0.02),
                            glyphs=GlyphReveal(x=[0.1, 0.5, 0.9], delays=[0.0, 2.0, 4.0], ramp=5.0), texture_pad=12.0,
                            texture_meta=TextureMeta(method="two_colour", frames=[30, 40], confidence=0.8)),
        tracks={"y": Track(keys=[Keyframe(t=18, v=900.0, ease=PRESET_EASES["out_cubic"]), Keyframe(t=30, v=820.0)]),
                "x": Track(keys=[Keyframe(t=18, v=540.0, ease=(0.2, 0.0, 0.1, 1.0)), Keyframe(t=30, v=541.5)]),
                "ry": Track(keys=[Keyframe(t=18, v=-30.0), Keyframe(t=60, v=0.0)])},
        z=Track(keys=[Keyframe(t=0, v=3.0), Keyframe(t=90, v=7.0)]), raw="assets/title_raw.npz",
        fit_error=FitError(max_px=1.4, max_frames=1))
    globe = Element(id="globe", kind="sprite", visible=(0, 149), provenance="manual",
                    canonical=Canonical(width=300.0, height=300.0, texture="assets/globe.png", video="assets/globe.webm",
                                        video_rate=0.5, anchor=(0.5, 0.6)),
                    tracks={"x": Track(keys=[Keyframe(t=0, v=200.0)])}, z=Track(keys=[Keyframe(t=0, v=1.0)]))
    model = Element(id="cube", kind="3d", visible=(10, 100), pending_asset="3d",
                    canonical=Canonical(width=120.0, height=120.0, model="assets/cube.glb"),
                    tracks={"ry": Track(keys=[Keyframe(t=10, v=0.0), Keyframe(t=100, v=360.0)])}, z=Track(keys=[Keyframe(t=5, v=2.0)]))
    ui = UIModel(components=[UIComponent(id="card", kind="card", bbox=(10, 10, 200, 120), text="Pro",
                                         states=[UIStateRange(frames=(0, 20), state={"hover": True})])])
    return Scene(id="s1", size=(1080, 1920), fps=30.0, frames=150,
                 background=Background(kind="gradient", gradient=grad, gradient_keys=[GradientKey(t=0, gradient=grad)],
                                       poster="assets/plate.png", confidence=0.9),
                 elements=[title, globe, model], groups=[Group(id="g1", members=["title", "globe"], reason="text_fragments")],
                 constraints=[Constraint(pred="type(m_title,'translation')", keep=True), Constraint(pred="before(m_title,m_cube)")],
                 ui=ui)


def _round_trips(scene: Scene) -> None:
    score = to_score(scene)
    assert from_score(score) == scene
    assert loads(dumps(score)) == score


def test_rare_fields_round_trip():
    _round_trips(_rare_scene())


@pytest.mark.parametrize("seed", range(1, 21))
def test_synthetic_scenes_round_trip(tmp_path, seed):
    _round_trips(make_synthetic_scene(tmp_path, seed=seed))


@pytest.mark.parametrize("kw", [dict(plate="flat"), dict(plate="gradient", title="stagger"), dict(plate="animated", mover=True),
                                dict(plate="image", title="plane", n_titles=2)])
def test_reference_scenes_round_trip(tmp_path, kw):
    _round_trips(make_reference_scene(tmp_path, seed=4, **kw))


def test_golden_score_is_stable():
    assert dumps(to_score(_rare_scene())) == GOLDEN.read_text(encoding="utf-8")


def test_score_reads_as_a_document():
    text = dumps(to_score(_rare_scene()))
    doc = json.loads(text)
    assert doc["format"] == "keepframe.score/2" and doc["canvas"] == {"w": 1080, "h": 1920, "fps": 30.0, "frames": 150}
    title = doc["layers"][0]
    assert title["motion"][0]["keys"]["y"] == [[18, 900.0, "out_cubic"], [30, 820.0]]   # preset eases by name
    assert title["motion"][0]["keys"]["x"][0] == [18, 540.0, [0.2, 0.0, 0.1, 1.0]]
    assert doc["layers"][1]["type"] == "video" and doc["layers"][2]["type"] == "model3d"
    assert "role" not in doc["layers"][1] and "conf" not in doc["layers"][1]   # defaults stay out of the text
    assert '"y": [[18, 900.0, "out_cubic"], [30, 820.0]]' in text   # key rows stay on one line


def _doc() -> dict:
    return json.loads(dumps(to_score(_rare_scene())))


@pytest.mark.parametrize("break_it", [
    lambda d: d.update(format="keepframe.scene/1"),
    lambda d: d.update(extra=1),
    lambda d: d["layers"][0].update(colour="#ffffff"),
    lambda d: d["layers"][0]["motion"][0]["keys"]["y"][0].__setitem__(2, "bouncy"),
    lambda d: d["layers"][0]["motion"][0]["keys"].update(wobble=[[0, 1.0]]),
    lambda d: d["layers"][0]["motion"][0]["keys"]["y"].reverse(),
    lambda d: d["layers"][0]["motion"].append({"keys": {"y": [[0, 1.0]]}}),
    lambda d: d["layers"][1].update(id="title"),
    lambda d: d["layers"][1].update(type="sticker"),
])
def test_invalid_scores_are_refused(break_it):
    doc = _doc()
    break_it(doc)
    with pytest.raises(ValidationError):
        loads(json.dumps(doc))


def test_cli_prints_the_score(tmp_path, capsys):
    save_scene(_rare_scene(), tmp_path / "scene.json")
    assert main(["score", "--scene", str(tmp_path / "scene.json")]) == 0
    assert loads(capsys.readouterr().out) == to_score(_rare_scene())


def test_cli_sheet_shows_the_frames(tmp_path):
    import cv2
    scene = make_synthetic_scene(tmp_path, seed=3)
    save_scene(scene, tmp_path / "scene.json")
    assert main(["score", "--scene", str(tmp_path / "scene.json"), "--out", str(tmp_path / "score.json"),
                 "--sheet", str(tmp_path / "sheet.png")]) == 0
    sheet = cv2.imread(str(tmp_path / "sheet.png"))
    assert sheet.shape == (3 * 360, 2 * 640, 3)
    assert loads((tmp_path / "score.json").read_text(encoding="utf-8")) == to_score(scene)
