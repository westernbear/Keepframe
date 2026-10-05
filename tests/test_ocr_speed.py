from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

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


@pytest.mark.parametrize("failure", [RuntimeError("OCR frame 3 failed"),
                                     KeyboardInterrupt("OCR frame 3 interrupted")])
def test_pool_cancels_queued_frames_and_preserves_original_error(monkeypatch, failure):
    released = Event()
    calls = []
    shutdowns = []

    class ReleasingPool(ThreadPoolExecutor):
        def shutdown(self, wait=True, *, cancel_futures=False):
            shutdowns.append((wait, cancel_futures))
            # Cancel before releasing running calls, so queued work cannot race cleanup.
            super().shutdown(wait=False, cancel_futures=cancel_futures)
            released.set()
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    def broken(frame):
        index = int(frame[0, 0, 0])
        calls.append(index)
        if index == 3:
            raise failure
        if index > 3:
            assert released.wait(timeout=10)
        return []

    monkeypatch.setattr("keepframe.analyze.text.ThreadPoolExecutor", ReleasingPool)
    frames = np.arange(40, dtype=np.uint8).reshape(40, 1, 1, 1)
    with pytest.raises(type(failure), match=str(failure)) as raised:
        ocr_frames(frames, broken, workers=2)
    assert raised.value is failure
    assert shutdowns == [(True, True)]
    assert 3 in calls and max(calls) < 3 + 2 * 2


def test_pool_cancels_pending_frames_on_caller_keyboard_interrupt(monkeypatch):
    released = Event()
    started = Event()
    calls = []
    shutdowns = []
    failure = KeyboardInterrupt("OCR cancelled by caller")

    class ReleasingPool(ThreadPoolExecutor):
        def shutdown(self, wait=True, *, cancel_futures=False):
            shutdowns.append((wait, cancel_futures))
            super().shutdown(wait=False, cancel_futures=cancel_futures)
            released.set()
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    def blocked(frame):
        calls.append(int(frame[0, 0, 0]))
        started.set()
        assert released.wait(timeout=10)
        return []

    def interrupted(stage, detail):
        assert started.wait(timeout=10)
        raise failure

    monkeypatch.setattr("keepframe.analyze.text.ThreadPoolExecutor", ReleasingPool)
    monkeypatch.setattr("keepframe.progress.report_stage", interrupted)
    frames = np.arange(40, dtype=np.uint8).reshape(40, 1, 1, 1)
    with pytest.raises(KeyboardInterrupt, match=str(failure)) as raised:
        ocr_frames(frames, blocked, workers=2)
    assert raised.value is failure
    assert shutdowns == [(True, True)]
    assert calls and max(calls) < 2 * 2


@pytest.mark.parametrize("workers", [2, 3, 4])
@pytest.mark.parametrize("step", [1, 2])
def test_pool_bounds_unconsumed_futures_and_preserves_long_clip_output(monkeypatch, workers, step):
    pending = set()
    peaks = []

    class RecordingPool(ThreadPoolExecutor):
        def submit(self, fn, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            pending.add(future)
            peaks.append(len(pending))
            result = future.result

            def consumed(timeout=None):
                value = result(timeout=timeout)
                pending.discard(future)
                return value

            future.result = consumed
            return future

    monkeypatch.setattr("keepframe.analyze.text.ThreadPoolExecutor", RecordingPool)
    frames = np.repeat(np.arange(1, 41, dtype=np.uint8), 3).reshape(120, 1, 1, 1)
    engine = PixelOcr()
    assert ocr_frames(frames, engine, step=step, workers=workers) == sequential(frames, step=step)
    assert sorted(engine.calls) == list(range(1, 41))
    assert max(peaks) == 2 * workers
    assert not pending


def test_pool_propagates_original_rapidocr_fork_failure(monkeypatch):
    import sys
    import types
    from keepframe.analyze.text import RapidOcr

    fork_started = Event()
    constructed = []
    failure = OSError("OCR worker model could not load")

    class FakeRapid:
        def __init__(self, **kwargs):
            if constructed:
                fork_started.set()
                raise failure
            constructed.append(self)

        def __call__(self, frame):
            assert fork_started.wait(timeout=10)
            assert int(frame[0, 0, -1]) == 0
            return None, None

    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", types.SimpleNamespace(RapidOCR=FakeRapid))
    monkeypatch.setattr("keepframe.analyze.device.preload_torch_cuda", lambda: None)
    monkeypatch.setattr("keepframe.analyze.text.ocr_cuda", lambda: False)
    monkeypatch.setattr("keepframe.analyze.text.ocr_cuda_expected", lambda: False)
    frames = np.arange(12, dtype=np.uint8).reshape(4, 1, 1, 3)
    with pytest.raises(OSError, match=str(failure)) as raised:
        ocr_frames(frames, RapidOcr(), workers=2)
    assert raised.value is failure


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


@pytest.mark.parametrize("affinity,cores,expected", [
    ({0}, 16, 1), ({2, 5}, 16, 2), ({0, 1, 2, 3}, 16, 4), (set(range(16)), 16, 4),
    (None, 1, 1), (None, 2, 2), (None, 4, 4), (None, 16, 4), (None, None, 1),
    (OSError("affinity unavailable"), 2, 2),
])
def test_cpu_workers_use_one_ort_thread_each_and_keep_detection_uncapped(monkeypatch, affinity, cores, expected):
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
    if affinity is None:
        monkeypatch.delattr("os.sched_getaffinity", raising=False)
    else:
        def get_affinity(pid):
            assert pid == 0
            if isinstance(affinity, OSError):
                raise affinity
            return affinity

        monkeypatch.setattr("os.sched_getaffinity", get_affinity, raising=False)
    ocr = RapidOcr()
    assert ocr.workers == expected
    assert ocr.max_side is None
    assert constructed == [dict(intra_op_num_threads=1, inter_op_num_threads=1)]
    forked = ocr.fork()
    assert forked._ocr is not ocr._ocr
    assert forked.max_side is None and forked.workers == expected
    assert constructed == [dict(intra_op_num_threads=1, inter_op_num_threads=1)] * 2
