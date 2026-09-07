import pytest

from keepframe.analyze.device import gpu_status, resolve_device


def test_gpu_status_has_cuda_flag():
    st = gpu_status()
    assert "cuda" in st and "device" in st and "name" in st
    assert st["cuda"] in (True, False)
    if not st["cuda"]:
        assert st["device"] == "cpu"


def test_resolve_device_cpu_explicit():
    assert resolve_device("cpu") == "cpu"


def test_keepframe_device_cuda_does_not_fallback(monkeypatch):
    monkeypatch.setenv("KEEPFRAME_DEVICE", "cuda")
    monkeypatch.setattr("keepframe.analyze.device._cuda_available", lambda: False)
    with pytest.raises(RuntimeError, match="KEEPFRAME_DEVICE=cuda"):
        resolve_device()


def test_keepframe_device_unset_falls_back_to_cpu(monkeypatch):
    monkeypatch.delenv("KEEPFRAME_DEVICE", raising=False)
    monkeypatch.setattr("keepframe.analyze.device._cuda_available", lambda: False)
    assert resolve_device() == "cpu"
