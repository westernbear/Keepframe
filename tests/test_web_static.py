from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ROOT / ".stitch" / "designs"
STATIC = ROOT / "keepframe" / "web" / "static"
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


def test_landing_stitch_source_exists():
    html = (DESIGNS / "landing.html").read_text(encoding="utf-8")
    assert "<html" in html.lower()
    assert "유지할 것과 바꿀 것을 지정하세요" in html
    assert (DESIGNS / "landing.png").stat().st_size > 1000


def test_ported_pages_are_offline():
    for name in ("landing.html", "library.html", "ingest.html", "analyze.html", "review.html"):
        text = (STATIC / name).read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{name} still loads {bad}"
        assert "/static/css/app.css" in text
        assert "/static/js/i18n.js" in text
        assert "/static/js/api.js" in text


def test_analyze_main_clears_icon_rail():
    html = (STATIC / "analyze.html").read_text(encoding="utf-8")
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "sidebar--icon" in html
    assert "main--icon" in html
    assert "main--no-sidebar" not in html
    assert ".main--icon { margin-left: 80px; }" in css


def test_analyze_steps_follow_job_stage():
    html = (STATIC / "analyze.html").read_text(encoding="utf-8")
    assert 'data-step="shots"' in html
    assert "updateSteps" in html
    assert "/api/jobs/" in html
    assert "li--active" not in html


def test_pages_include_logo_and_favicon():
    assert (STATIC / "logo.png").stat().st_size > 100
    assert (STATIC / "icon.png").stat().st_size > 100
    for name in ("landing.html", "library.html", "ingest.html", "analyze.html", "review.html"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert 'src="/static/logo.png"' in text
        assert 'href="/static/icon.png"' in text


def test_ingest_sends_selected_range_to_analyze():
    ingest = (STATIC / "ingest.html").read_text(encoding="utf-8")
    analyze = (STATIC / "analyze.html").read_text(encoding="utf-8")
    assert "selectedWindow" in ingest
    assert "mode: win.mode" in ingest
    assert "payload.start" in analyze
    assert "payload.end" in analyze


def test_ingest_gpu_status_is_not_hardcoded():
    text = (STATIC / "ingest.html").read_text(encoding="utf-8")
    assert "data-gpu" in text
    assert "/api/status" in text
    assert "<span>ON</span>" not in text


def test_i18n_has_ko_and_en_keys():
    src = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    assert "keepframe.lang" in src
    for key in ("ingest.start", "review.keepSave", "error.liveaction", "library.empty", "landing.headline"):
        assert src.count(f'"{key}"') >= 2


def test_i18n_markup_keys_exist_in_both_langs():
    import re
    src = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    ko = src.split("en: {", 1)[0]
    en = src.split("en: {", 1)[1]
    used = set()
    for html in STATIC.glob("*.html"):
        used.update(re.findall(r'data-i18n="([^"]+)"', html.read_text(encoding="utf-8")))
    missing = [k for k in sorted(used) if f'"{k}"' not in ko or f'"{k}"' not in en]
    assert missing == []


def test_css_tokens_match_design():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "#090909" in css and "#0099ff" in css and "#ffffff" in css


def test_hidden_wins_over_display():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "[hidden] { display: none !important; }" in css


def test_review_empty_project_has_library_exit():
    html = (STATIC / "review.html").read_text(encoding="utf-8")
    assert 'id="review-missing"' in html
    assert "is-empty" in html
    assert 'href="/library"' in html
    assert "review.backLibrary" in html


def test_landing_and_library_link_to_demo():
    landing = (STATIC / "landing.html").read_text(encoding="utf-8")
    library = (STATIC / "library.html").read_text(encoding="utf-8")
    assert 'href="/demo"' in landing
    assert 'href="/demo"' in library
    assert "nav.demo" in landing
    assert "p.scene" in library


def test_library_filters_are_wired():
    html = (STATIC / "library.html").read_text(encoding="utf-8")
    assert "data-search" in html
    assert 'data-filter="review"' in html
    assert "visibleProjects" in html
    assert 'library.filter.export' not in html
