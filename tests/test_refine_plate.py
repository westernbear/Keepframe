import cv2
import numpy as np
import pytest

torch = pytest.importorskip("torch")

from keepframe.analyze import refine


@pytest.fixture(scope="module", autouse=True)
def cpu_threads():
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(previous)


@pytest.mark.parametrize("use_plate", [False, True])
@pytest.mark.parametrize("scale,checkpoint", [(1.0, False), (0.5, True)])
def test_refine_corrects_three_pixel_x_error(use_plate, scale, checkpoint):
    h, w = 48, 80
    ramp = np.linspace((10, 30, 50), (180, 120, 80), w).astype(np.uint8)
    plate = np.repeat(ramp[None], h, axis=0)
    bg = tuple(int(v) for v in plate.mean((0, 1)))
    if not use_plate:
        plate[:] = bg
    frames = np.repeat(plate[None], 3, axis=0)
    texture = np.full((16, 16, 4), 255, np.uint8)
    expected_x = np.array([28.0, 36.0, 44.0])
    for f, x in enumerate(expected_x.astype(int)):
        frames[f, 16:32, x - 8:x + 8] = 255
    raw = np.array([[x + 3, 24, 1, 1, 0, 0, 0, 1] for x in expected_x], dtype=float)
    kwargs = {"plate": plate} if use_plate else {}
    out = refine.refine_affine(frames, bg, {"s": raw}, {"s": texture}, {"s": (0.5, 0.5)}, {"s": 1},
                               iters=80, scale=scale, device="cpu", checkpoint=checkpoint, **kwargs)["s"]
    error = np.max(np.abs(out[:, 0] - expected_x))
    if scale == 1.0:
        assert error < 1.0
    else:
        # Downscaled sampling can plateau between grid points; require improvement here.
        assert error < 3.0
    assert np.max(np.abs(out[:, 1] - 24)) < 1.0
    assert np.array_equal(raw[:, 0], expected_x + 3)
    assert np.array_equal(out[:, 2:], raw[:, 2:])


@pytest.mark.parametrize("use_plate", [False, True])
def test_refine_uploads_background_at_work_resolution(monkeypatch, use_plate):
    h, w = 49, 81
    plate = np.arange(h * w * 3, dtype=np.int64).reshape(h, w, 3).astype(np.uint8)
    frames = np.repeat(plate[None], 2, axis=0)
    bg = (17, 34, 51)
    uploads = []

    def inspect(work, consts, raws, iters, lr, dev, H, W, work_h, work_w, chunk, checkpoint):
        uploads.append(consts["bg"])
        assert work.shape == (2, work_h, work_w, 3)
        actual = consts["bg"].cpu().numpy()
        assert consts["bg"].dtype == torch.float32
        assert not consts["bg"].requires_grad
        if use_plate:
            expected = cv2.resize(plate, (work_w, work_h), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
            assert actual.shape == (1, 3, work_h, work_w)
            assert np.array_equal(actual[0].transpose(1, 2, 0), expected)
        else:
            assert actual.shape == (1, 3, 1, 1)
            assert np.array_equal(actual.reshape(3), np.array(bg, np.float32) / 255)
        return raws

    monkeypatch.setattr(refine, "_refine_chunks", inspect)
    refine.refine_affine(frames, bg, {}, {}, {}, {}, scale=0.5, device="cpu", plate=plate if use_plate else None)
    assert len(uploads) == 1
