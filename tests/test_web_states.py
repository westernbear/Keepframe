import json
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"


def test_library_has_empty_copy():
    ko = json.loads((SRC / "i18n" / "ko.json").read_text(encoding="utf-8"))
    assert "프로젝트가 없습니다" in ko["library.empty"]
    assert "T('library.empty')" in (SRC / "pages" / "library" / "index.tsx").read_text(encoding="utf-8")


def test_no_retry_override_control():
    for path in [SRC / "pages" / "analyze" / "index.tsx", *(SRC / "pages" / "review").glob("*.ts*")]:
        # the cap is code policy (admin.policy copy says so); no page offers a field to raise it
        assert "retry_cap" not in path.read_text(encoding="utf-8"), path.name
