"""The React frontend's unit tests (Vitest) and its committed build, run from the fast suite."""
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
APP = Path(__file__).resolve().parents[1] / "keepframe" / "web" / "static" / "app"


def test_built_bundle_is_committed():
    html = (APP / "index.html").read_text(encoding="utf-8")
    assert '<div id="root">' in html and "/static/app/assets/" in html
    assert "https://" not in html  # offline: no CDN


def test_vitest_suite_passes():
    npx = shutil.which("npx")
    if not npx or not (FRONTEND / "node_modules").is_dir():
        pytest.skip("frontend deps not installed (cd frontend && npm ci)")
    r = subprocess.run([npx, "vitest", "run"], cwd=FRONTEND, text=True, capture_output=True, timeout=600)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
