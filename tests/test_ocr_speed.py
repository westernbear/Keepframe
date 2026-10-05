from threading import Barrier, Lock

import numpy as np
import pytest

from keepframe.analyze.text import TextBox, ocr_frames


class PixelOcr:
    def __init__(self):
        self.calls = []

    def __call__(self, frame):
        value = int(frame.sum())
        self.calls.append(value)
        return [(str(value), (1, 2, 3, 4), value / 1000),
                ("other", (4, 3, 2, 1), 0.987654321)] if value else []


def sequential(frames, step=1):
    engine = PixelOcr()
    return [[TextBox(i, t, b, c) for t, b, c in engine(frame)] if i % step == 0 else []
            for i, frame in enumerate(frames)]


def test_identical_frames_reuse_results_with_new_frame_indices_and_own_boxes():
    frames = np.ones((4, 3, 4, 3), np.uint8)
    engine = PixelOcr()
    output = ocr_frames(frames, engine)
    assert engine.calls == [36]
    assert output == sequential(frames)
    assert len({id(box) for row in output for box in row}) == 8
    output[1][0].text = "edited"
    assert output[0][0].text == output[2][0].text == "36"


def test_one_changed_pixel_is_not_reused_and_only_previous_ocr_frame_is_compared():
    frames = np.ones((4, 3, 4, 3), np.uint8)
    frames[1, -1, -1, -1] += 1
    engine = PixelOcr()
    assert ocr_frames(frames, engine) == sequential(frames)
    assert engine.calls == [36, 37, 36]


def test_empty_results_are_reused():
    engine = PixelOcr()
    assert ocr_frames(np.zeros((4, 3, 4, 3), np.uint8), engine) == [[], [], [], []]
    assert engine.calls == [0]


def test_step_compares_previous_sampled_frame_and_leaves_unsampled_frames_empty():
    frames = np.ones((7, 3, 4, 3), np.uint8)
    frames[1::2] *= 7
    frames[4:] *= 2
    engine = PixelOcr()
    assert ocr_frames(frames, engine, step=2) == sequential(frames, step=2)
    assert engine.calls == [36, 72]


def test_empty_input_does_not_call_engine():
    engine = PixelOcr()
    assert ocr_frames(np.empty((0, 3, 4, 3), np.uint8), engine) == []
    assert engine.calls == []


def test_pool_completes_out_of_order_but_returns_sequential_results_and_reuses():
    frames = np.full((5, 3, 4, 3), 1, np.uint8)
    frames[1:3] *= 2
    frames[3:] *= 3
    barrier = Barrier(3, timeout=10)
    lock = Lock()
    completed = []

    class ConcurrentOcr(PixelOcr):
        def __call__(self, frame):
            value = int(frame[0, 0, 0])
            barrier.wait()
            # Require actual overlapping calls and reverse completion order without sleeps.
            if value == 3:
                with lock:
                    completed.append(value)
            barrier.wait()
            if value == 2:
                with lock:
                    completed.append(value)
            barrier.wait()
            if value == 1:
                with lock:
                    completed.append(value)
            return super().__call__(frame)

    engine = ConcurrentOcr()
    assert ocr_frames(frames, engine, workers=3) == sequential(frames)
    assert completed == [3, 2, 1]
    assert sorted(engine.calls) == [36, 72, 108]


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_pool_preserves_step_and_progress_in_frame_order(monkeypatch, workers):
    progress = []
    monkeypatch.setattr("keepframe.progress.report_stage", lambda stage, detail: progress.append((stage, detail)))
    frames = np.arange(18, dtype=np.uint8).reshape(6, 1, 1, 3)
    assert ocr_frames(frames, PixelOcr(), step=2, workers=workers) == sequential(frames, step=2)
    assert progress == [("text", f"{i}/6") for i in range(1, 7)]


@pytest.mark.parametrize("workers", [0, -1])
def test_pool_rejects_invalid_size(workers):
    with pytest.raises(ValueError, match="workers"):
        ocr_frames(np.ones((1, 1, 1, 3), np.uint8), PixelOcr(), workers=workers)


def test_pool_propagates_engine_failure():
    def broken(frame):
        raise RuntimeError("OCR failure")

    with pytest.raises(RuntimeError, match="OCR failure"):
        ocr_frames(np.ones((3, 1, 1, 3), np.uint8), broken, workers=2)


def test_pool_uses_independent_engines_and_automatic_worker_count():
    barrier = Barrier(3, timeout=10)
    instances = []
    calls = []
    lock = Lock()

    class ForkedOcr(PixelOcr):
        workers = 3

        def __init__(self):
            super().__init__()
            instances.append(self)

        def fork(self):
            return ForkedOcr()

        def __call__(self, frame):
            barrier.wait()
            with lock:
                calls.append(self)
            return super().__call__(frame)

    frames = np.arange(9, dtype=np.uint8).reshape(3, 1, 1, 3)
    assert ocr_frames(frames, ForkedOcr()) == sequential(frames)
    assert len(instances) == len({id(engine) for engine in calls}) == 3


@pytest.mark.parametrize("cores,expected", [(1, 1), (2, 2), (4, 4), (16, 4), (None, 1)])
def test_cpu_workers_use_one_ort_thread_each_and_keep_detection_uncapped(monkeypatch, cores, expected):
    import sys
    import types
    from keepframe.analyze.text import RapidOcr

    constructed = []

    class FakeRapid:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", types.SimpleNamespace(RapidOCR=FakeRapid))
    monkeypatch.setattr("keepframe.analyze.text.ocr_cuda", lambda: False)
    monkeypatch.setattr("keepframe.analyze.text.ocr_cuda_expected", lambda: False)
    monkeypatch.setattr("os.cpu_count", lambda: cores)
    ocr = RapidOcr()
    assert ocr.workers == expected
    assert ocr.max_side is None
    assert constructed == [dict(intra_op_num_threads=1, inter_op_num_threads=1)]
    forked = ocr.fork()
    assert forked._ocr is not ocr._ocr
    assert forked.max_side is None and forked.workers == expected
    assert constructed == [dict(intra_op_num_threads=1, inter_op_num_threads=1)] * 2
