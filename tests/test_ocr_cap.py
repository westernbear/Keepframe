import sys
import types

import cv2
import numpy as np
import pytest

from keepframe.analyze.pipeline import AnalyzeOptions, _stage_text
from keepframe.analyze.text import RapidOcr


@pytest.fixture
def fake_engine(monkeypatch):
    class FakeRapid:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            self.crops = []
            self.rec_inputs = []
            self.use_cls = True
            self.empty = False
            self.box = (240, 120, 880, 240)
            self.frame_shape = (1080, 1920)

        def __call__(self, bgr, use_cls=None, use_rec=None):
            self.calls.append((bgr.copy(), use_cls, use_rec))
            if self.empty:
                return None, None
            x0, y0, x1, y1 = self.box
            h, w = self.frame_shape
            quad = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float64)
            quad *= [bgr.shape[1] / w, bgr.shape[0] / h]
            if use_cls is False and use_rec is False:
                return [quad.tolist()], [0.01]
            return [[quad.tolist(), "Original text", 0.98]], [0.01, 0.01, 0.01]

        def get_crop_img_list(self, bgr, quads):
            # Match RapidOCR's perspective crop rather than an axis-aligned thumbnail.
            crops = []
            for quad in quads:
                width = int(np.linalg.norm(quad[1] - quad[0]))
                height = int(np.linalg.norm(quad[3] - quad[0]))
                dest = np.array([[0, 0], [width, 0], [width, height], [0, height]], np.float32)
                warp = cv2.getPerspectiveTransform(quad, dest)
                crops.append(cv2.warpPerspective(bgr, warp, (width, height), flags=cv2.INTER_CUBIC,
                                                 borderMode=cv2.BORDER_REPLICATE))
            self.crops = crops
            return crops

        def text_cls(self, crops):
            return crops, [["0", 1.0] for _ in crops], 0.01

        def text_rec(self, crops):
            self.rec_inputs = crops
            return [("Original text", 0.98) for _ in crops], 0.01

        def get_final_res(self, boxes, cls, rec, det_time, cls_time, rec_time):
            return [[box.tolist(), *result] for box, result in zip(boxes, rec)], [det_time, cls_time, rec_time]

    module = types.ModuleType("rapidocr_onnxruntime")
    module.RapidOCR = FakeRapid
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", module)
    monkeypatch.setattr("keepframe.analyze.device.preload_torch_cuda", lambda: None)
    monkeypatch.setattr("keepframe.analyze.text.ocr_cuda", lambda: False)
    monkeypatch.setattr("keepframe.analyze.text.ocr_cuda_expected", lambda: False)
    return FakeRapid


@pytest.mark.parametrize("shape", [(1080, 1920), (2003, 801), (801, 2003)])
def test_cap_restores_boxes_and_recognizes_original_resolution_crops(fake_engine, shape):
    frame = np.random.default_rng(19).integers(0, 256, (*shape, 3), dtype=np.uint8)
    ocr = RapidOcr(max_side=1280)
    ocr._ocr.frame_shape = shape
    ocr._ocr.box = (240, 120, 560, 240)
    result = ocr(frame)
    assert result == [("Original text", (240, 120, 560, 240), 0.98)]
    detected, use_cls, use_rec = ocr._ocr.calls[0]
    assert max(detected.shape[:2]) == 1280
    assert use_cls is False and use_rec is False
    assert ocr._ocr.kwargs["det_limit_type"] == "max"
    crop = ocr._ocr.rec_inputs[0]
    assert crop.shape[:2] == (120, 320)
    np.testing.assert_array_equal(crop, frame[120:240, 240:560, ::-1])


def test_none_preserves_original_engine_call_and_coordinates(fake_engine):
    frame = np.full((1080, 1920, 3), (21, 42, 84), np.uint8)
    ocr = RapidOcr(max_side=None)
    assert ocr(frame) == [("Original text", (240, 120, 880, 240), 0.98)]
    bgr, use_cls, use_rec = ocr._ocr.calls[0]
    np.testing.assert_array_equal(bgr, frame[..., ::-1])
    assert use_cls is None and use_rec is None
    assert ocr._ocr.kwargs == {}


def test_default_cap_is_1280_and_empty_detection_skips_recognition(fake_engine):
    ocr = RapidOcr()
    ocr._ocr.empty = True
    assert ocr(np.zeros((1080, 1920, 3), np.uint8)) == []
    assert max(ocr._ocr.calls[0][0].shape[:2]) == 1280
    assert ocr._ocr.rec_inputs == []


def test_smaller_frames_are_passed_at_original_resolution(fake_engine):
    frame = np.zeros((480, 640, 3), np.uint8)
    ocr = RapidOcr(max_side=1280)
    ocr._ocr.frame_shape = frame.shape[:2]
    ocr(frame)
    np.testing.assert_array_equal(ocr._ocr.calls[0][0], frame[..., ::-1])


def test_pipeline_constructs_ocr_with_configured_cap(fake_engine, tmp_path):
    (tmp_path / "stages").mkdir()
    constructed = []
    module = sys.modules["rapidocr_onnxruntime"]

    def create(**kwargs):
        engine = fake_engine(**kwargs)
        engine.empty = True
        constructed.append(engine)
        return engine

    module.RapidOCR = create
    opts = AnalyzeOptions(ocr_max_side=640)
    _stage_text(np.zeros((1, 1080, 1920, 3), np.uint8), (0, 0, 0), opts, None, tmp_path)
    assert max(constructed[0].calls[0][0].shape[:2]) == 640
    assert AnalyzeOptions().ocr_max_side == 1280


@pytest.mark.parametrize("cap", [None, 768])
def test_cli_passes_default_or_requested_cap(monkeypatch, tmp_path, cap):
    from keepframe.cli import main
    captured = []

    def analyze(video, start, end, root, options, captioner):
        captured.append(options)
        return types.SimpleNamespace(scenes=[types.SimpleNamespace(id="s1")],
                                     versions=[types.SimpleNamespace(id="v1")])

    monkeypatch.setattr("keepframe.analyze.pipeline.analyze", analyze)
    args = ["analyze", "--video", str(tmp_path / "clip.mp4"), "--end", "0", "--out", str(tmp_path), "--no-captions"]
    if cap is not None:
        args += ["--ocr-max-side", str(cap)]
    assert main(args) == 0
    assert captured[0].ocr_max_side == (1280 if cap is None else cap)


@pytest.mark.ocr
def test_installed_engine_detects_reduced_frame_and_recognizes_full_size_text(monkeypatch):
    pytest.importorskip("rapidocr_onnxruntime")
    monkeypatch.setattr("keepframe.analyze.text.ocr_cuda", lambda: False)
    monkeypatch.setattr("keepframe.analyze.text.ocr_cuda_expected", lambda: False)
    frame = np.full((1080, 1920, 3), 255, np.uint8)
    cv2.putText(frame, "Keepframe 1280", (300, 500), cv2.FONT_HERSHEY_SIMPLEX, 3, (0, 0, 0), 5)
    ocr = RapidOcr(max_side=640)
    recognize = ocr._ocr.text_rec
    crop_shapes = []

    def record_recognition(crops):
        crop_shapes.extend(crop.shape[:2] for crop in crops)
        return recognize(crops)

    ocr._ocr.text_rec = record_recognition
    result = ocr(frame)
    assert any(text.replace(" ", "").lower() == "keepframe1280" for text, _, _ in result)
    assert any(height > 60 and width > 500 for height, width in crop_shapes)
