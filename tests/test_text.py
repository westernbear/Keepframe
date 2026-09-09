import numpy as np, pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.tracks import element_bbox, eval_props
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.text import TextBox, ocr_frames, track_text, apply_copy, text_exclusion_mask, text_props

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
    # Hershey bbox + scale/rotation AABB; 40px canonical can read back above 60.
    assert 25 <= font.size_px <= 80 and color.startswith("#")

def test_rapidocr_passes_cuda_flags_when_ep_available(monkeypatch):
    import sys, types
    from keepframe.analyze.text import RapidOcr

    seen = {}

    class FakeRapid:
        def __init__(self, **kw):
            seen.update(kw)

    fake = types.ModuleType("rapidocr_onnxruntime")
    fake.RapidOCR = FakeRapid
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake)
    monkeypatch.setattr("keepframe.analyze.text.ocr_cuda", lambda: True)
    RapidOcr()
    assert seen == dict(det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)


def test_ocr_gpu_mem_limit_stays_small():
    from keepframe.analyze.text import _ocr_gpu_mem_limit, _cap_ort_cuda_arena
    assert 1024 ** 3 <= _ocr_gpu_mem_limit() <= 4 * 1024 ** 3
    _cap_ort_cuda_arena(2 * 1024 ** 3)


def test_ocr_cuda_opts_keep_exhaustive_conv_search():
    from keepframe.analyze.text import _cuda_provider_opts
    opts = _cuda_provider_opts({"cudnn_conv_algo_search": "EXHAUSTIVE"}, 2 * 1024 ** 3)
    assert opts["cudnn_conv_algo_search"] == "EXHAUSTIVE"
    assert opts["gpu_mem_limit"] == 2 * 1024 ** 3
    assert opts["arena_extend_strategy"] == "kSameAsRequested"


def test_rapidocr_stays_cpu_without_cuda_ep(monkeypatch):
    import sys, types
    from keepframe.analyze.text import RapidOcr

    seen = {}

    class FakeRapid:
        def __init__(self, **kw):
            seen.update(kw)

    fake = types.ModuleType("rapidocr_onnxruntime")
    fake.RapidOCR = FakeRapid
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake)
    monkeypatch.setattr("keepframe.analyze.text.ocr_cuda", lambda: False)
    monkeypatch.setattr("keepframe.analyze.text.ocr_cuda_expected", lambda: False)
    RapidOcr()
    assert seen == {}


@pytest.mark.ocr
def test_rapidocr_reads_synthetic_text(tmp_scene_dir):
    from keepframe.analyze.text import RapidOcr
    import difflib
    scene = make_synthetic_scene(tmp_scene_dir, seed=41)
    gold = [e for e in scene.elements if e.kind == "text"][0]
    f = gold.visible[1]
    frame = (composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8)
    res = RapidOcr()(frame)
    assert res and max(difflib.SequenceMatcher(None, t.lower(), gold.canonical.text.lower()).ratio() for t, _, _ in res) >= 0.6
