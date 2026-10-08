import json
import cv2, numpy as np, pytest
from keepframe.ir.colour import delta_e, hex_to_rgb8, rgb8_to_hex, srgb_to_lab
from keepframe.ir.store import save_scene
from keepframe.ir.synth import make_reference_scene, render_reference
from keepframe.ir.tracks import element_bbox, eval_props
from keepframe.verify.verifier import VerifyReport


class FakeOcr:
    """Reads the golden text boxes, so the eval runs without a real OCR model."""
    def __init__(self, scene):
        self.scene, self.f = scene, 0

    def __call__(self, frame):
        out = []
        for e in self.scene.elements:
            if e.kind == "text" and e.visible[0] <= self.f <= e.visible[1] and eval_props(e, self.f)["opacity"] > 0.3:
                out.append((e.canonical.text, tuple(int(round(v)) for v in element_bbox(e, self.f)), 0.9))
        self.f += 1
        return out


def _stand_in_clip(tmp_path):
    """A tiny synthetic stand-in for a real clip, with a gold file in the eval/clips format."""
    truth_dir = tmp_path / "clip-truth"
    truth = make_reference_scene(truth_dir, seed=11, plate="flat", n_sprites=2, frames=40)
    save_scene(truth, truth_dir / "scene.json")
    clips, gold = tmp_path / "clips", tmp_path / "gold"
    render_reference(truth, truth_dir, clips / "standin.mp4", renderer="numpy")
    title = next(e for e in truth.elements if e.kind == "text")
    gold.mkdir()
    (gold / "standin.json").write_text(json.dumps({"titles": [
        {"text": title.canonical.text, "color": title.canonical.color, "role": "title", "frame": truth.frames - 5,
         "box": [int(v) for v in element_bbox(title, truth.frames - 5)], "points": []},
        {"text": "Nowhere", "color": rgb8_to_hex((255, 255, 255)), "role": "kinetic", "frame": 0, "box": [0, 0, 1, 1], "points": []}]}))
    return truth, clips, gold


def test_eval_edits_cli_writes_metrics_and_sheet(tmp_path, monkeypatch):
    from keepframe.cli import main
    clip_truth, clips, gold = _stand_in_clip(tmp_path)
    monkeypatch.setattr("keepframe.qa.edits.make_ocr", lambda truth: FakeOcr(truth or clip_truth))
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_a, **_k: None)       # the agent's browser verify
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_a, **_k: VerifyReport(
        schema_ok=True, keep_pass_rate=1, temporal=1, layer_probe_complete=True, passed=True))
    out = tmp_path / "out"
    assert main(["eval-edits", "--clips", str(clips), "--gold", str(gold), "--out", str(out), "--synthetic", "1",
                 "--renderer", "numpy", "--no-refine", "--max-frames", "40"]) == 0
    m = json.loads((out / "metrics.json").read_text())
    assert m["vlm_calls"] == 0 and m["renderer"] == "numpy"
    assert m["options"]["synthetic"]["refine"] is False and m["options"]["clips"]["refine"] is False
    row = m["synthetic"]["rows"][0]
    assert row["plate"] == "gradient" and {"overall", "under_title", "under_logo"} <= set(row["plate_de"])
    assert row["title"]["status"] == "done"
    assert {"outside_glyph_delta", "smear_score", "glyph_de"} <= set(row["title"])
    assert row["hide"] and {"mean_de", "p95_de", "hf_ratio", "residue_fraction", "truth_de"} <= set(row["hide"][0])
    assert row["background"][0]["mode"] == "replace" and row["background"][0]["halo_ring"] is not None
    assert {"sad", "mse", "f_de_interior", "f_de_edge"} <= set(row["alpha"]) and row["matched"] >= 3
    assert row["integrity"][0]["whole"] and row["font_top3"] is not None and row["seconds"] > 0
    gates = m["gates"]["synthetic"]
    assert {"title_outside_glyph_delta", "title_smear_score", "title_glyph_de", "hide_plate_de", "hide_hf_ratio",
            "hide_residue", "recolour_halo_ring", "alpha_sad_edge", "f_de_interior", "f_de_edge", "font_top3"} == set(gates)
    assert all({"value", "threshold", "passed"} <= set(g) for g in gates.values())
    clip = m["clips"]["rows"][0]
    assert clip["clip"] == "standin" and clip["vlm_calls"] == 0 and clip["render_l1"] < 0.05
    assert [t["whole"] for t in clip["integrity"]] == [True, False]
    c = clip["colour"]                                                          # title role only
    assert len(c) == 1 and c[0]["gold"] == clip_truth.element("title1").canonical.color and c[0]["element"]
    assert c[0]["de"] == pytest.approx(float(delta_e(srgb_to_lab(np.float32(hex_to_rgb8(c[0]["analysed"]))),
                                                     srgb_to_lab(np.float32(hex_to_rgb8(c[0]["gold"]))))), abs=1e-4)
    assert "bg_leak_fraction" in clip["leak"] and clip["title"]["status"] == "done"
    assert set(m["gates"]["clips"]) >= {"title_colour_de", "vlm_calls"}
    sheets = sorted((out / "sheets").glob("*.png"))
    assert row["sheet"] == "sheets/synthetic-seed1.png" and clip["sheet"] == "sheets/clip-standin.png"
    assert [p.stem for p in sheets] == ["clip-standin", "synthetic-seed1"]
    sheet = cv2.imread(str(sheets[1]))
    assert sheet.shape[1] >= 5 * 480 and sheet.shape[0] > 3 * 270
    summary = (out / "summary.md").read_text()
    assert "hide_plate_de" in summary and "refine" in summary


def _hand_project(tmp_path, monkeypatch, *, title_span, junk):
    """A truth scene stored as its own analysis, with the title cut short and/or a junk layer that keeps the
    old title's pixels: the two ways a title edit can 'pass' while the old title stays on screen."""
    from keepframe.ir.schema import Element
    from keepframe.ir.store import init_project
    from keepframe.ir.synth import ground_truth, render_frames, true_plate
    tdir = tmp_path / "truth"
    truth = make_reference_scene(tdir, seed=7, plate="flat", n_sprites=1, logo=False, frames=20)
    root = tmp_path / "proj"
    sd = root / "scenes" / "s1"
    analysed = truth.model_copy(deep=True, update={"id": "s1"})
    title = analysed.element("title1")
    title.visible = title_span
    if junk:
        analysed.elements.append(Element(id="junk", kind="sprite", canonical=title.canonical.model_copy(update={"text": None}),
                                         visible=(0, 19), tracks=title.tracks, z=title.z))
    import shutil
    shutil.copytree(tdir / "assets", sd / "assets")
    init_project(root, {"file": "ref.mp4", "fps": 30, "size": [640, 360], "mode": "range", "range": [0, 19]}, analysed)
    (sd / "stages").mkdir()
    np.save(sd / "stages" / "frames.npy", np.stack([f.round().astype(np.uint8) for f in render_frames(truth, tdir, range(20))]))
    monkeypatch.setattr("keepframe.edit.agent.render", lambda *_a, **_k: None)
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_a, **_k: VerifyReport(
        schema_ok=True, keep_pass_rate=1, temporal=1, layer_probe_complete=True, passed=True))
    frames = [19]
    gt = ground_truth(truth, tdir, frames)
    return root, {"scene": truth, "dir": tdir, "layers": gt, "plates": {19: true_plate(truth, tdir, 19)}, "title": "title1",
                  "pairs": {e.id: e.id for e in truth.elements}}


def test_title_check_fails_when_the_old_title_stays_on_screen(tmp_path, monkeypatch):
    from keepframe.qa.edits import edit_checks
    root, truth = _hand_project(tmp_path / "clean", monkeypatch, title_span=(0, 19), junk=False)
    ok = edit_checks(root, "s1", title="title1", hide=["title1"], frames=[19], renderer="numpy", truth=truth)
    assert ok["title"]["smear_score"] < 1 and ok["title"]["glyph_de"] < 1 and ok["hide"][0]["truth_de"] < 0.5
    root, truth = _hand_project(tmp_path / "gone", monkeypatch, title_span=(0, 2), junk=True)
    gone = edit_checks(root, "s1", title="title1", hide=["title1"], frames=[19], renderer="numpy", truth=truth)
    assert gone["title"]["frames"][0]["visible"] is False and gone["title"]["smear_score"] is None
    root, truth = _hand_project(tmp_path / "kept", monkeypatch, title_span=(0, 19), junk=True)
    kept = edit_checks(root, "s1", title="title1", hide=["title1"], frames=[19], renderer="numpy", truth=truth)
    assert kept["title"]["smear_score"] > 3                        # a second layer still draws the old glyphs
    assert kept["hide"][0]["residue_fraction"] > 0.02 and kept["hide"][0]["truth_de"] > 3
