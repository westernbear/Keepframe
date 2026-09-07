from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static"


def test_library_has_empty_copy():
    html = (STATIC / "library.html").read_text(encoding="utf-8")
    assert "프로젝트가 없습니다" in html
    assert 'data-i18n="library.empty"' in html or "library.empty" in html


def test_no_retry_override_control():
    for name in ("analyze.html", "review.html"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert "재시도 상한" not in text or "올릴 수 없음" in text or "한도는 코드" in text
        assert 'name="retry_cap"' not in text
