import numpy as np, pytest
from refstudio.ir.synth import make_synthetic_scene
from refstudio.ir.tracks import element_bbox, eval_props
from refstudio.analyze.composite import composite_scene
from refstudio.analyze.text import TextBox, ocr_frames, track_text, apply_copy, text_exclusion_mask, text_props

class FakeOcr:
    """Returns the golden text box (jittered) so tracking can be tested without a real OCR model."""
    def __init__(self, scene, noise=0.0):
        self.scene, self.noise, self.f = scene, noise, 0
    def __call__(self, frame):
        out = []
        for e in self.scene.elements:
            if e.kind == "text" and e.visible[0] <= self.f <= e.visible[1] and eval_props(e, self.f)["opacity"] > 0.3:
                x0, y0, x1, y1 = [int(round(v)) for v in element_bbox(e, self.f)]
                txt = e.canonical.text if (self.f % 7) else e.canonical.text[:-1] + "x"   # occasional misread
                out.append((txt, (x0, y0, x1, y1), 0.9))
        self.f += 1
        return out

def test_track_text_and_copy(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=41)
    frames = np.stack([(composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8) for f in range(scene.frames)])
    gold = [e for e in scene.elements if e.kind == "text"][0]
    boxes = ocr_frames(frames, FakeOcr(scene))
    tracks = track_text(boxes)
    assert len(tracks) == 1 and tracks[0].text == gold.canonical.text          # majority vote fixes misreads
    apply_copy(tracks, ["Totally different", gold.canonical.text])
    assert tracks[0].text == gold.canonical.text
    f = tracks[0].first
    mask = text_exclusion_mask(boxes[f], frames.shape[1:3])
    x0, y0, x1, y1 = [int(v) for v in element_bbox(gold, f)]
    assert mask[(y0 + y1) // 2, (x0 + x1) // 2]
    raw, canon, cf, font, color = text_props(tracks[0], frames, (0x10, 0x14, 0x18), scene.frames, 0)
    p = eval_props(gold, cf)
    assert abs(raw[cf, 0] - p["x"]) < 2 and abs(raw[cf, 1] - p["y"]) < 2 and canon.shape[2] == 4
    assert 25 <= font.size_px <= 60 and color.startswith("#")

@pytest.mark.ocr
def test_rapidocr_reads_synthetic_text(tmp_scene_dir):
    from refstudio.analyze.text import RapidOcr
    import difflib
    scene = make_synthetic_scene(tmp_scene_dir, seed=41)
    gold = [e for e in scene.elements if e.kind == "text"][0]
    f = gold.visible[1]
    frame = (composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8)
    res = RapidOcr()(frame)
    assert res and max(difflib.SequenceMatcher(None, t.lower(), gold.canonical.text.lower()).ratio() for t, _, _ in res) >= 0.6
