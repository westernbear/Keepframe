import hashlib
import sys
import types

import numpy as np
import pytest

from keepframe.analyze import text
from keepframe.analyze.text import RapidOcr, ocr_models


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_table(monkeypatch, payloads):
    table = {k: (name, f"http://x/{name}", hashlib.sha256(payloads[k]).hexdigest()) for k, (name, _, _) in text.OCR_V5.items()}
    monkeypatch.setattr(text, "OCR_V5", table)
    return table


def test_ocr_models_downloads_once_and_checks_sha256(tmp_path, monkeypatch):
    monkeypatch.delenv("KEEPFRAME_OCR_MODELS", raising=False)
    payloads = {"det_model_path": b"det", "rec_model_path": b"rec"}
    table = _fake_table(monkeypatch, payloads)
    calls = []

    def fetch(url, timeout=None):
        calls.append(url)
        return _Resp(next(payloads[k] for k, (_, u, _) in table.items() if u == url))

    bad = tmp_path / "bad"
    assert ocr_models(bad, fetch=lambda url, timeout=None: _Resp(b"tampered")) is None
    assert not list(bad.glob("*.onnx")) and not list(bad.glob("*.part"))

    good = tmp_path / "good"
    paths = ocr_models(good, fetch=fetch)
    assert {k: open(v, "rb").read() for k, v in paths.items()} == payloads
    n = len(calls)
    assert n == 2 and ocr_models(good, fetch=fetch) == paths and len(calls) == n


def _fake_rapid(monkeypatch, seen):
    class Fake:
        def __init__(self, **kw):
            seen.append(kw)

    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", types.SimpleNamespace(RapidOCR=Fake))
    monkeypatch.setattr(text, "ocr_cuda", lambda: False)
    monkeypatch.setattr(text, "ocr_cuda_expected", lambda: False)


def test_rapidocr_gets_v5_paths_and_fork_keeps_them(monkeypatch):
    paths = {"det_model_path": "/m/det.onnx", "rec_model_path": "/m/rec.onnx"}
    monkeypatch.setattr(text, "ocr_models", lambda *a, **k: paths)
    seen = []
    _fake_rapid(monkeypatch, seen)
    ocr = RapidOcr()
    ocr.fork()
    assert len(seen) == 2 and all(kw["det_model_path"] == "/m/det.onnx" and kw["rec_model_path"] == "/m/rec.onnx" for kw in seen)
    assert ocr.models == paths


def test_v4_when_models_unavailable(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("KEEPFRAME_OCR_MODELS", raising=False)
    monkeypatch.setenv("KEEPFRAME_OCR_DIR", str(tmp_path))
    seen = []
    _fake_rapid(monkeypatch, seen)

    def boom(url, timeout=None):
        raise OSError("offline")

    monkeypatch.setattr(text.ocr_models, "__kwdefaults__", {"fetch": boom})   # the default is bound at def time
    with caplog.at_level("WARNING"):
        ocr = RapidOcr()
    assert ocr.models is None and not any("model_path" in k for k in seen[0])
    assert sum("ocr_models_missing" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.ocr
def test_v5_reads_rendered_titles(monkeypatch):
    from keepframe.edit.textraster import render_lines
    from keepframe.ir.synth import TITLES

    fonts = ("Inter", "Roboto", "Playfair Display", "Montserrat", "Oswald", "Lato")
    frames = []
    for i, (title, family) in enumerate(zip(TITLES, fonts)):
        glyphs = render_lines([title], 44 + 2 * i, (240, 240, 240), family)
        if glyphs is None:
            continue
        h, w = glyphs.shape[:2]
        plate = np.zeros((h + 80, w + 120, 3), np.uint8)
        plate[:] = (40, 60, 90) if i % 2 else (120, 90, 50)
        if i % 2:
            plate[:] = np.linspace((40, 60, 90), (160, 90, 60), plate.shape[1])[None].astype(np.uint8)
        a = glyphs[..., 3:] / 255.0
        y, x = 40, 60
        plate[y:y + h, x:x + w] = (glyphs[..., [2, 1, 0]] * a + plate[y:y + h, x:x + w] * (1 - a)).astype(np.uint8)
        frames.append((title, plate))   # RGB, as RapidOcr expects
    assert len(frames) >= 4

    def exact(mode):
        monkeypatch.setenv("KEEPFRAME_OCR_MODELS", mode)
        ocr = RapidOcr()
        assert (ocr.models is None) == (mode == "v4")
        return sum(any(t.casefold() == title.casefold() for t, _, _ in ocr(f)) for title, f in frames)

    v4, v5 = exact("v4"), exact("v5")
    assert v5 >= v4
