from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ROOT / ".stitch" / "designs"
STATIC = ROOT / "refstudio" / "web" / "static"
FORBIDDEN = (
    "cdn.tailwindcss.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "lh3.googleusercontent.com",
    "material-symbols",
)


def test_analyze_stitch_source_exists():
    html = (DESIGNS / "analyze.html").read_text(encoding="utf-8")
    assert "<html" in html.lower()
    assert (DESIGNS / "analyze.png").stat().st_size > 1000


def test_ported_pages_are_offline():
    for name in ("library.html", "ingest.html", "analyze.html", "review.html"):
        text = (STATIC / name).read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{name} still loads {bad}"
        assert 'href="/static/css/app.css"' in text
        assert 'src="/static/js/i18n.js"' in text
        assert 'src="/static/js/api.js"' in text


def test_i18n_has_ko_and_en_keys():
    src = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    assert "keepframe.lang" in src
    for key in ("ingest.start", "review.keepSave", "error.liveaction"):
        assert key in src


def test_css_tokens_match_design():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "#090909" in css and "#0099ff" in css and "#ffffff" in css
