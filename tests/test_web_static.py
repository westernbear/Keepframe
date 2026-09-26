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


def static_src(*names):
    return "\n".join((STATIC / name).read_text(encoding="utf-8") for name in names)


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


def test_analyze_uses_primary_navigation_without_duplicate_icon_rail():
    html = (STATIC / "analyze.html").read_text(encoding="utf-8")
    assert 'class="header__nav maker-nav"' in html
    assert "sidebar--icon" not in html
    assert "main--icon" not in html


def test_analyze_steps_follow_job_stage():
    src = static_src("analyze.html", "js/analyze.js", "js/api.js")
    assert 'data-step="shots"' in src
    assert "updateSteps" in src
    assert "/api/jobs/" in src
    assert "li--active" not in src


def test_pages_include_logo_and_favicon():
    assert (STATIC / "logo.png").stat().st_size > 100
    assert (STATIC / "icon.png").stat().st_size > 100
    for name in ("landing.html", "library.html", "ingest.html", "analyze.html", "review.html"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert 'src="/static/logo.png"' in text
        assert 'href="/static/icon.png"' in text


def test_ingest_sends_selected_range_to_analyze():
    ingest = static_src("ingest.html", "js/ingest.js")
    analyze = static_src("analyze.html", "js/analyze.js")
    assert "selectedWindow" in ingest
    assert "mode: win.mode" in ingest
    assert "payload.start" in analyze
    assert "payload.end" in analyze


def test_ingest_gpu_status_is_not_hardcoded():
    text = static_src("ingest.html", "js/ingest.js", "js/api.js")
    assert "data-gpu" in text
    assert "/api/status" in text
    assert "<span>ON</span>" not in text


def test_i18n_has_ko_and_en_keys():
    src = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    assert "keepframe.lang" in src
    for key in ("ingest.start", "review.keepSave", "review.approve", "review.openAgent", "review.editRun", "error.liveaction", "library.empty", "landing.headline"):
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


def test_api_prefetches_review_frames():
    src = (STATIC / "js" / "playback.js").read_text(encoding="utf-8")
    assert "function createPreviewCache" in src
    assert "function createFrameTransport" in src
    assert "img.decode" in src
    assert "PREFETCH_AHEAD" in src
    assert "PREVIEW_CACHE_LIMIT" in src


def test_css_tokens_match_design():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "#090909" in css and "#0099ff" in css and "#ffffff" in css


def test_hidden_wins_over_display():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "[hidden] { display: none !important; }" in css


def test_review_empty_project_has_library_exit():
    src = static_src("review.html", "js/review.js")
    assert 'id="review-missing"' in src
    assert "is-empty" in src
    assert 'href="/library"' in src
    assert "review.backLibrary" in src


def test_landing_and_library_link_to_demo():
    landing = static_src("landing.html")
    library = static_src("library.html", "js/library.js")
    assert 'href="/demo"' in landing
    assert 'href="/demo"' in library
    assert "isApprovedStatus" in library
    assert "nav.demo" in landing
    assert "p.scene" in library
    assert "/agent?project=" in library
    assert "isReviewStatus" in library


def test_library_filters_are_wired():
    src = static_src("library.html", "js/library.js")
    assert "data-search" in src
    assert 'data-filter="review"' in src
    assert "visibleProjects" in src
    assert 'library.filter.export' not in src

def test_review_empty_copy_uses_the_exact_korean_guidance():
    source = static_src("review.html", "js/i18n.js")
    assert 'data-i18n="review.empty">인식된 요소가 없습니다. 원본 화면에서 박스를 그려 첫 요소를 지정하세요.' in source
    assert '"review.empty": "인식된 요소가 없습니다. 원본 화면에서 박스를 그려 첫 요소를 지정하세요."' in source

def test_all_linked_runtime_assets_exist_locally():
    import re
    for page in STATIC.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        for asset in re.findall(r"(?:src|href)=\"(/static/[^\"?#]+)", text):
            assert (STATIC / asset.removeprefix("/static/")).is_file(), f"{page.name} links missing {asset}"
