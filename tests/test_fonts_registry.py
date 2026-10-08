import hashlib, json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

FONTS = Path(__file__).resolve().parents[1] / "keepframe" / "fonts"
CATEGORIES = {"geometric_sans", "neo_grotesque", "humanist_sans", "rounded_sans", "condensed_sans", "display",
              "serif", "slab", "script", "handwriting", "mono"}


def _manifest():
    return json.loads((FONTS / "manifest.json").read_text())["families"]


def _coverage(path, wght):
    font = ImageFont.truetype(str(path), 120)
    font.set_variation_by_axes([wght])
    img = Image.new("L", (900, 200), 0)
    ImageDraw.Draw(img).text((10, 10), "Hamburg", font=font, fill=255)
    return float((np.asarray(img) > 127).mean())


def test_woff2_variable_weight_in_pillow():
    path = FONTS / "files" / "Inter.woff2"
    ratio = _coverage(path, 800) / _coverage(path, 300)
    assert ratio >= 1.4, ratio


def test_manifest_files_exist_hash_and_licence():
    fams = _manifest()
    assert fams
    for fam in fams:
        assert fam["category"] in CATEGORIES and fam["license"] == "OFL-1.1" and fam["scripts"]
        assert (FONTS / "LICENSES" / fam["license_file"]).stat().st_size > 100, fam["family"]
        assert fam["source"] and fam["files"]
        for f in fam["files"]:
            p = FONTS / "files" / f["file"]
            assert hashlib.sha256(p.read_bytes()).hexdigest() == f["sha256"], f["file"]
            lo, hi = f["axes"]["wght"]
            assert 1 <= lo <= hi <= 1000


def test_bundled_set_coverage():
    from keepframe.fonts.registry import FontRegistry
    fams = _manifest()
    assert 60 <= len(fams) <= 80
    assert {f["category"] for f in fams} == CATEGORIES
    reg = FontRegistry.for_project(None)
    for name in ("Pretendard", "Noto Sans KR", "Noto Serif KR"):
        assert reg.covers(name, "가"), name
    assert not reg.covers("Inter", "가") and reg.covers("Inter", "Hello 123")
    assert "Pretendard" in reg.families(script="hangul") and "Inter" not in reg.families(script="hangul")


def test_registry_precedence_uploaded_bundled_system(tmp_path, monkeypatch):
    from keepframe.fonts import registry as R
    reg = R.FontRegistry.for_project(tmp_path)
    assert reg.face("Inter", 400).source == "bundled"
    src = FONTS / "files" / "Lora.woff2"
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / f"{sha}.woff2").write_bytes(src.read_bytes())
    entry = {"family": "Inter", "original_family": "Lora Mine", "style": "Regular", "weight_range": [400, 700],
             "postscript": "Lora-Regular", "category": "serif", "ext": "woff2", "sha256": sha,
             "bytes": src.stat().st_size, "latin": True, "hangul": False, "file": f"{sha}.woff2"}
    (tmp_path / "fonts" / "index.json").write_text(json.dumps({"fonts": [entry]}))
    up = R.FontRegistry.for_project(tmp_path).face("Inter", 500)
    assert up.source == "uploaded" and up.path == tmp_path / "fonts" / f"{sha}.woff2" and up.weight_range == (400, 700)
    assert R.FontRegistry.for_project(tmp_path).face("inter", 500).source == "uploaded"
    monkeypatch.setattr(R.FontRegistry, "_system_face", lambda self, family: R.FontFace(
        family, "system", Path("/x.ttf")) if family == "SysOnly" else None)
    assert reg.face("SysOnly", 400).source == "system"
    assert reg.face("Nope Nope", 400) is None


def test_bad_or_missing_index_means_no_uploads(tmp_path, caplog):
    from keepframe.fonts.registry import FontRegistry
    assert FontRegistry.for_project(tmp_path).face("Inter", 400).source == "bundled"
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "index.json").write_text("{nope")
    with caplog.at_level("WARNING"):
        assert FontRegistry.for_project(tmp_path).face("Inter", 400).source == "bundled"
    assert "index" in caplog.text
    (tmp_path / "fonts" / "index.json").write_text(json.dumps({"fonts": [{"family": "X", "file": "../../etc/passwd"}]}))
    assert FontRegistry.for_project(tmp_path).face("X", 400) is None


def test_hangul_fallback_map():
    from keepframe.fonts.registry import FontRegistry
    reg = FontRegistry.for_project(None)
    assert reg.hangul_fallback("Montserrat", 640) == ("Pretendard", 600)
    assert reg.hangul_fallback("Anton", 400) == ("Pretendard", 400)
    assert reg.hangul_fallback("Nunito", 700) == ("Noto Sans KR", 700)
    assert reg.hangul_fallback("Lora", 749) == ("Noto Serif KR", 700)
    assert reg.hangul_fallback("Bitter", 400) == ("Noto Serif KR", 400)
    assert reg.hangul_fallback("Caveat", 400) == ("Nanum Pen Script", 400)
    assert reg.hangul_fallback("JetBrains Mono", 400) == ("Pretendard", 400)
    assert reg.hangul_fallback("Dancing Script", 400) == ("Pretendard", 400)
    assert reg.hangul_fallback("Pretendard", 650) == ("Pretendard", 700)
    assert reg.hangul_fallback("Unknown Family", 300) == ("Pretendard", 300)


def test_safe_alias():
    from keepframe.fonts.registry import safe_alias
    assert safe_alias("Plus Jakarta Sans") == "kf-plus-jakarta-sans"
    assert safe_alias('A"; } body{x:y} /*') == "kf-a-body-x-y"
    assert safe_alias("한글").startswith("kf-font-") and safe_alias("한글") != safe_alias("글")
    assert safe_alias("  ") == "kf-font"
    assert safe_alias("Inter") == safe_alias("inter")
