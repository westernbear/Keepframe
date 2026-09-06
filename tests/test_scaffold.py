import importlib, pathlib

def test_package_imports():
    assert importlib.import_module("refstudio").__version__ == "0.1.0"

def test_vendor_js_present():
    v = pathlib.Path("refstudio/compose/vendor")
    assert (v / "gsap.min.js").stat().st_size > 50_000
    assert (v / "CustomEase.min.js").stat().st_size > 5_000
