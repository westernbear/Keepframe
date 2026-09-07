import importlib, pathlib

def test_package_imports():
    assert importlib.import_module("keepframe").__version__ == "0.1.0"

def test_vendor_js_present():
    v = pathlib.Path("keepframe/compose/vendor")
    assert (v / "gsap.min.js").stat().st_size > 50_000
    assert (v / "CustomEase.min.js").stat().st_size > 5_000


def test_dockerfile_has_gpu_stage():
    text = pathlib.Path("Dockerfile").read_text(encoding="utf-8")
    assert "AS base" in text and "AS gpu" in text and "AS runtime" in text
    assert "download.pytorch.org/whl" in text
    overlay = pathlib.Path("docker-compose.gpu.yml").read_text(encoding="utf-8")
    assert "target: gpu" in overlay and "gpus: all" in overlay
    assert "KEEPFRAME_DEVICE: cuda" in overlay
