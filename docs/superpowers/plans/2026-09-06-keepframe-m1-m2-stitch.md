# Keepframe M1+M2 Stitch UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the Stitch maker screens on the existing M1+M2 core so a local process can list projects, upload a clip (range or full, with a cost gate), watch analysis, and review keep/corrections.

**Architecture:** One Python package `keepframe`. Core IR/analyze/verify stays in `keepframe/{ir,compose,render,verify,analyze,review}` as built by `docs/superpowers/plans/2026-09-05-keepframe-m1-m2.md` Tasks 1–25. This plan adds `keepframe/web`: stdlib `http.server`, static HTML copied from `.stitch/designs/` and taken offline, plus JSON APIs. No React, no Next, no CDN at runtime.

**Tech Stack:** Python 3.11+, pydantic v2, numpy, opencv-python-headless, playwright (Chromium for renderer and browser smoke), pytest, stdlib `http.server`. Stitch HTML is the visual source; DESIGN.md is the token authority.

**Spec:** `docs/superpowers/specs/2026-09-05-keepframe-design.md` (§4 IR, §5 analyzer, §6 review, §9 cost gate, §10–§11 M2). Visual: `DESIGN.md`. Screen source: `.stitch/designs/` and Stitch project `17345649857730883248`.

## Global Constraints

- Do not edit `docs/superpowers/plans/2026-09-05-keepframe-m1-m2.md`.
- Python ≥ 3.11. Package name `keepframe`. Tests with `pytest`; browser tests `@pytest.mark.browser`.
- IR schema ids are exactly `keepframe.project/1` and `keepframe.scene/1`.
- Corrections are exactly four ops: reassign, mask, bbox, edit_text. Keep predicates are mandatory verifier checks. Retry cap 4, asset-generation cap 2 — display only; no UI control changes them.
- Local edition is one process (CLI + local web). Admin routes are Plan B; this plan must 404 `/admin` unless `--admin` is later wired.
- Korean default, English toggle via `localStorage("keepframe.lang")`. Every visible string goes through `T` / `data-i18n`.
- Primary CTA is a white pill with a black label. Accent `#0099ff` is focus/selection only. No emoji. Dark only.
- Runtime HTML/CSS/JS must not load `cdn.tailwindcss.com`, `fonts.googleapis.com`, `fonts.gstatic.com`, or `lh3.googleusercontent.com`.
- Icons are inline SVG. Material Symbols CDN is forbidden.
- Review must work at 1280×800: orig/recon panes, timeline, and side panel visible without scrolling the window.
- Full-video mode requires a confirm token from `POST /api/estimate`. Invented SLA or prices are forbidden. Estimate = `scene_count * SECONDS_PER_SCENE` with `SECONDS_PER_SCENE = 180` labeled as a guess.
- Live-action is rejected with a reason. Do not play or thumbnail the rejected footage. Quarantine persistence is Plan B.
- Commit after every task with the given message. Never commit `.venv`, frames, or MP4s.
- Out of this plan (M3): session-agent confirm, conflict chooser, verify-fail report, export MP4 screen.

## Prerequisite (existing plan, not copied)

Execute Tasks 1–25 of `docs/superpowers/plans/2026-09-05-keepframe-m1-m2.md` first, using that file as the brief. Those tasks produce the names below. This file starts at Task 1 of the web layer.

**Consumed from Tasks 1–25 (exact names):**

- `keepframe.ir.schema`: `FontGuess`, `Constraint`, `Scene`, `Project`, `Version`, `dump`, `load_scene_json`, `load_project_json`
- `keepframe.ir.store`: `save_scene(scene, path)`, `load_scene(path) -> Scene`, `scene_dir(root, scene_id) -> Path`, `load_project(root) -> Project`, `init_project(root, source, scene, note="initial analysis") -> Project`, `current_scene(root, scene_id) -> tuple[Scene, Version]`, `new_version(root, scene_id, scene, note, auto=True) -> Version`
- `keepframe.ir.tracks`: `eval_track`, `eval_props`, `element_bbox(el, f) -> tuple[float,float,float,float]`
- `keepframe.analyze.composite`: `composite_scene(scene, scene_dir, f) -> np.ndarray` RGB float 0..1
- `keepframe.analyze.pipeline`: `AnalyzeOptions`, `analyze(video: Path, start: int, end: int, out_root: Path, options: AnalyzeOptions | None = None, ocr=None) -> Project`, `rerun(root, scene_id, from_stage, note, options=None) -> Version`
- `keepframe.review.corrections`: `reassign_id`, `set_region_mask`, `add_bbox_prompt`, `edit_text` (each `-> Version`)
- CLI already has `synth`, `compose`, `render`, `verify`, `analyze`, `correct`, `gate-m1`, `gate-m2`

---

## File Structure

```
keepframe/web/__init__.py
keepframe/web/server.py          ThreadingHTTPServer, routes
keepframe/web/workspace.py       list/create projects under a workspace dir
keepframe/web/jobs.py            in-process JobStore (analyze + correct)
keepframe/web/estimate.py        range/full time estimate + confirm token
keepframe/web/liveaction.py      cheap live-action reject (no bypass)
keepframe/web/static/library.html
keepframe/web/static/ingest.html
keepframe/web/static/analyze.html
keepframe/web/static/review.html
keepframe/web/static/css/app.css
keepframe/web/static/js/i18n.js
keepframe/web/static/js/api.js
keepframe/cli.py                 add `serve`
tests/test_web_static.py
tests/test_web_server.py
tests/test_web_workspace.py
tests/test_web_estimate.py
tests/test_web_jobs.py
tests/test_web_ingest.py
tests/test_web_review.py
tests/test_web_states.py
tests/test_web_smoke.py
.stitch/designs/analyze.html     fetched in Task 1
```

---

### Task 1: Pin the analyze Stitch screen locally

**Files:**
- Create: `.stitch/designs/analyze.html`, `.stitch/designs/analyze.png`
- Modify: `.stitch/metadata.json`, `.stitch/SITE.md`

**Interfaces:**
- Consumes: Stitch project `17345649857730883248`, screen `45648a9fadb544d69ba92c9b7307a157`
- Produces: local files used by Task 3 as the analyze markup source. No Python package yet.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_static.py
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ROOT / ".stitch" / "designs"

def test_analyze_stitch_source_exists():
    html = (DESIGNS / "analyze.html").read_text(encoding="utf-8")
    assert "<html" in html.lower()
    assert (DESIGNS / "analyze.png").stat().st_size > 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_static.py::test_analyze_stitch_source_exists -v`
Expected: FAIL with `FileNotFoundError` for `.stitch/designs/analyze.html`

- [ ] **Step 3: Fetch the screen**

Use Stitch MCP `get_screen` with `name=projects/17345649857730883248/screens/45648a9fadb544d69ba92c9b7307a157`. Download `htmlCode.downloadUrl` to `.stitch/designs/analyze.html` and `screenshot.downloadUrl` to `.stitch/designs/analyze.png`. Add the screen to `.stitch/metadata.json` (`analyze.files`) and a row in `.stitch/SITE.md`. Do not regenerate the screen.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_static.py::test_analyze_stitch_source_exists -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_web_static.py .stitch/designs/analyze.html .stitch/designs/analyze.png .stitch/metadata.json .stitch/SITE.md
git commit -m "chore(stitch): pin analyze progress screen locally"
```

---

### Task 2: `serve` CLI and static routes

**Files:**
- Create: `keepframe/web/__init__.py`, `keepframe/web/server.py`
- Modify: `keepframe/cli.py`, `pyproject.toml` (package-data `web/static/*`)
- Test: `tests/test_web_server.py`

**Interfaces:**
- Consumes: nothing from analyze yet. Static files may be placeholders until Task 3.
- Produces:
  - `PAGES = {"/": "library.html", "/new": "ingest.html", "/analyze": "analyze.html", "/review": "review.html"}`
  - `make_server(workspace: Path, port: int = 8765, host: str = "127.0.0.1", admin: bool = False) -> ThreadingHTTPServer`
  - `GET /`, `/new`, `/analyze`, `/review` → corresponding HTML (404 JSON if file missing)
  - `GET /static/...` from `keepframe/web/static/` (`..` rejected)
  - `GET /admin` and `GET /admin/` → 404 `{"error": "로컬판에는 이 화면이 없습니다."}` when `admin` is False
  - CLI: `keepframe serve --workspace DIR [--port 8765]` prints `http://127.0.0.1:{port}/`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_server.py
import json
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

def start(tmp_path):
    from keepframe.web.server import make_server
    srv = make_server(tmp_path, port=0)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

def get(srv, path):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    try:
        with urlopen(url) as r:
            return r.status, r.headers.get_content_type(), r.read()
    except HTTPError as e:
        return e.code, e.headers.get_content_type(), e.read()

def test_admin_is_404_without_flag(tmp_path):
    srv = start(tmp_path)
    code, ctype, body = get(srv, "/admin")
    srv.shutdown()
    assert code == 404
    assert json.loads(body)["error"] == "로컬판에는 이 화면이 없습니다."

def test_missing_page_is_404_json(tmp_path):
    srv = start(tmp_path)
    code, _, body = get(srv, "/")
    srv.shutdown()
    assert code == 404
    assert "library.html" in json.loads(body)["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.web.server'`

- [ ] **Step 3: Write the server**

```python
# keepframe/web/server.py
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

STATIC = Path(__file__).parent / "static"
PAGES = {
    "/": "library.html",
    "/new": "ingest.html",
    "/analyze": "analyze.html",
    "/review": "review.html",
}
ADMIN_OFF = {"error": "로컬판에는 이 화면이 없습니다."}


def make_server(workspace: Path, port: int = 8765, host: str = "127.0.0.1", admin: bool = False) -> ThreadingHTTPServer:
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/admin", "/admin/") and not admin:
                return self._json(404, ADMIN_OFF)
            if u.path in PAGES:
                p = STATIC / PAGES[u.path]
                if not p.exists():
                    return self._json(404, {"error": f"{PAGES[u.path]} missing"})
                return self._send(200, p.read_bytes(), "text/html; charset=utf-8")
            if u.path.startswith("/static/"):
                rel = u.path[len("/static/"):]
                if ".." in rel:
                    return self._json(400, {"error": "bad path"})
                p = STATIC / rel
                if not p.is_file():
                    return self._json(404, {"error": "not found"})
                ctype = "text/css" if p.suffix == ".css" else "application/javascript" if p.suffix == ".js" else "application/octet-stream"
                return self._send(200, p.read_bytes(), ctype)
            return self._json(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), H)
```

Add to `keepframe/cli.py` a `serve` subparser with `--workspace` (required) and `--port` (default 8765) that calls `make_server` and `serve_forever`. Add `keepframe/web/static` to package-data.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_server.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/web/__init__.py keepframe/web/server.py keepframe/cli.py pyproject.toml tests/test_web_server.py
git commit -m "feat(web): stdlib serve with maker routes and admin 404"
```

---

### Task 3: Offline-port the four Stitch pages

**Files:**
- Create: `keepframe/web/static/library.html`, `ingest.html`, `analyze.html`, `review.html`, `css/app.css`, `js/i18n.js`, `js/api.js`
- Test: `tests/test_web_static.py` (extend)

**Interfaces:**
- Consumes: `.stitch/designs/{library,ingest,analyze,review}.html` as visual source
- Produces: offline pages that keep the Stitch layout (header, side list / two panes / filmstrip / table) and load only `/static/css/app.css` and `/static/js/{i18n,api}.js`. `i18n.js` exports `T`, `applyI18n()`, `toggleLang()`. `api.js` exports `api(path, opts)` → `fetch` JSON.

`app.css` tokens (copy from DESIGN.md, these exact values):

```css
:root {
  --canvas: #090909;
  --surface-1: #141414;
  --surface-2: #1c1c1c;
  --hairline: #262626;
  --ink: #ffffff;
  --muted: #999999;
  --accent: #0099ff;
  --on-primary: #000000;
}
```

Primary button: white background, black text, pill (`border-radius: 9999px`). Focus ring: `1px solid rgba(0,153,255,0.15)`.

`i18n.js` must include both `ko` and `en` for at least: `app.name`, `nav.library`, `nav.new`, `nav.analyze`, `nav.review`, `lang.toggle`, `ingest.start`, `ingest.range`, `ingest.full`, `ingest.confirm`, `library.search`, `analyze.running`, `review.keepSave`, `review.reassign`, `review.mask`, `review.bbox`, `review.text`, `error.liveaction`, `empty.elements`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_web_static.py
import re

STATIC = ROOT / "keepframe" / "web" / "static"
FORBIDDEN = (
    "cdn.tailwindcss.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "lh3.googleusercontent.com",
    "material-symbols",
)

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_static.py -v`
Expected: FAIL with missing `keepframe/web/static/library.html` (analyze source test from Task 1 still passes)

- [ ] **Step 3: Port the four pages**

Copy structure from `.stitch/designs/*.html`. Replace Tailwind CDN with `app.css`. Replace Material icons with inline 20×20 SVG (stroke currentColor). Replace remote filmstrip `<img src="https://lh3...">` with empty `<div data-filmstrip>` filled later from `/api/filmstrip`. Keep Korean visible copy as `data-i18n` defaults. Do not invent marketing sections.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_static.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/web/static tests/test_web_static.py
git commit -m "feat(web): offline-port Stitch library ingest analyze review"
```

---

### Task 4: Workspace project list and create

**Files:**
- Create: `keepframe/web/workspace.py`
- Modify: `keepframe/web/server.py`
- Test: `tests/test_web_workspace.py`

**Interfaces:**
- Consumes: filesystem only (a project is a directory containing `project.json` OR, before analyze, `source.mp4` + `meta.json`)
- Produces:
  - `list_projects(workspace: Path) -> list[dict]` each `{id, title, status, updated, version, confidence}` sorted by `updated` desc
  - `create_project(workspace, title: str, video: Path, mode: str, range: tuple[int,int] | None) -> dict` writes `workspace/{id}/meta.json` and copies video to `workspace/{id}/source.mp4`. `id` is `p` + 8 hex chars. `status` starts as `"uploaded"`. `mode` is `"range"` or `"full"`.
  - `project_dir(workspace, project_id) -> Path`
  - `GET /api/projects` → `{projects: [...]}`
  - `POST /api/projects` JSON `{title, filename}` is not enough; upload is Task 5. This task's POST accepts `{title}` only and returns 400 `{"error": "video required"}`. GET works.

`status` values: `uploaded` | `analyzing` | `review` | `error` | `rejected`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_workspace.py
import json
from pathlib import Path
from keepframe.web.workspace import list_projects, create_project, project_dir

def test_empty_workspace(tmp_path):
    assert list_projects(tmp_path) == []

def test_create_requires_video(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        create_project(tmp_path, "Autumn", tmp_path / "nope.mp4", "range", (0, 30))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_workspace.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.web.workspace'`

- [ ] **Step 3: Implement workspace + GET /api/projects**

```python
# keepframe/web/workspace.py
from __future__ import annotations
import json, secrets, shutil
from datetime import datetime, timezone
from pathlib import Path

def project_dir(workspace: Path, project_id: str) -> Path:
    return Path(workspace) / project_id

def list_projects(workspace: Path) -> list[dict]:
    rows = []
    for p in Path(workspace).iterdir() if Path(workspace).exists() else []:
        meta = p / "meta.json"
        if not meta.exists():
            continue
        rows.append(json.loads(meta.read_text()))
    rows.sort(key=lambda r: r.get("updated", ""), reverse=True)
    return rows

def create_project(workspace: Path, title: str, video: Path, mode: str, range_: tuple[int, int] | None) -> dict:
    video = Path(video)
    if not video.exists():
        raise FileNotFoundError(video)
    if mode not in ("range", "full"):
        raise ValueError(mode)
    pid = "p" + secrets.token_hex(4)
    root = project_dir(workspace, pid)
    root.mkdir(parents=True)
    shutil.copy2(video, root / "source.mp4")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {"id": pid, "title": title, "status": "uploaded", "updated": now,
           "version": None, "confidence": None, "mode": mode, "range": list(range_) if range_ else None}
    (root / "meta.json").write_text(json.dumps(row, indent=2, sort_keys=True))
    return row
```

Wire `GET /api/projects` in `server.py` to `{"projects": list_projects(workspace)}`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_workspace.py tests/test_web_server.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/web/workspace.py keepframe/web/server.py tests/test_web_workspace.py
git commit -m "feat(web): workspace project list under serve --workspace"
```

---

### Task 5: Upload, filmstrip, estimate, confirm

**Files:**
- Create: `keepframe/web/estimate.py`, `keepframe/web/liveaction.py`
- Modify: `keepframe/web/server.py`, `keepframe/web/static/ingest.html`, `keepframe/web/static/js/api.js`
- Test: `tests/test_web_estimate.py`, `tests/test_web_ingest.py`

**Interfaces:**
- `probe_video(path: Path) -> dict` `{width, height, fps, frames, duration_s}` via cv2.
- `SECONDS_PER_SCENE = 180`
- `estimate(mode: str, frames: int, fps: float) -> dict`  
  range → `scene_count=1`; full → `scene_count = max(1, ceil(duration_s / 4.0))` (4s shot guess, labeled).  
  `seconds = scene_count * SECONDS_PER_SCENE`.  
  `confirm_token` = 16 hex chars stored in memory on the server for 30 minutes.  
  Returns `{scene_count, seconds, seconds_per_scene: 180, note: "추정. SLA 아님.", confirm_token}`.
- `looks_live_action(path: Path) -> str | None` — if the first frame's Laplacian variance is `> 120` **and** a skin-tone pixel fraction is `> 0.08`, return `"실사 푸티지. 평면 2D MG·UI 녹화만 받음."` else `None`. This is a coarse gate, not a claim of accuracy.
- `POST /api/projects` `multipart/form-data` fields: `title`, `video`, `mode`, `start`, `end`. On live-action: 422 `{error, code:"live_action"}` and do not keep the file. Else `create_project` and 201 `{project}`.
- `GET /api/projects/{id}/filmstrip?n=8` → PNG sprite or JSON list of `/api/projects/{id}/frame/{i}` (JSON list of URLs is enough).
- `GET /api/projects/{id}/frame/{i}` → JPEG of source frame `i`.
- `POST /api/estimate` JSON `{project_id}` or `{mode, frames, fps}` → estimate dict. Full mode without a later token is rejected in Task 6.
- ingest.html: range/full toggle, filmstrip from the API, start button posts estimate then (if full) shows confirm copy with seconds and `장면 N개` before enabling analyze navigation.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_estimate.py
from keepframe.web.estimate import SECONDS_PER_SCENE, estimate

def test_range_is_one_scene():
    e = estimate("range", frames=90, fps=30)
    assert e["scene_count"] == 1
    assert e["seconds"] == SECONDS_PER_SCENE
    assert e["note"] == "추정. SLA 아님."
    assert len(e["confirm_token"]) == 16

def test_full_uses_four_second_shots():
    e = estimate("full", frames=300, fps=30)  # 10s → 3 scenes
    assert e["scene_count"] == 3
    assert e["seconds"] == 3 * SECONDS_PER_SCENE
```

```python
# tests/test_web_ingest.py
import json, io
from pathlib import Path
import cv2, numpy as np
from tests.test_web_server import start, get

def _mp4(path: Path, solid=True):
    w, h, n = 64, 36, 10
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    wr = cv2.VideoWriter(str(path), fourcc, 10, (w, h))
    for i in range(n):
        if solid:
            fr = np.full((h, w, 3), (20, 40, 80), np.uint8)
        else:
            fr = np.random.randint(0, 255, (h, w, 3), np.uint8)
        wr.write(fr)
    wr.release()

def test_upload_range_project(tmp_path):
    vid = tmp_path / "a.mp4"
    _mp4(vid)
    # POST multipart is tested via workspace.create_project here if server multipart lands in Step 3
    from keepframe.web.workspace import create_project, list_projects
    row = create_project(tmp_path / "ws", "Card", vid, "range", (0, 9))
    assert row["status"] == "uploaded"
    assert list_projects(tmp_path / "ws")[0]["id"] == row["id"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_estimate.py tests/test_web_ingest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.web.estimate'`

- [ ] **Step 3: Implement estimate, liveaction, upload routes, bind ingest.html**

`estimate.py` keeps tokens in a module-level `dict[str, float]` of token → expiry `time.time()`. `consume_token(token: str) -> bool`.

`liveaction.py`:

```python
def looks_live_action(path: Path) -> str | None:
    import cv2, numpy as np
    cap = cv2.VideoCapture(str(path))
    ok, bgr = cap.read()
    cap.release()
    if not ok:
        return None
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
    ycrcb = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    skin = cv2.inRange(ycrcb, (0, 133, 77), (255, 173, 127))
    frac = float(np.mean(skin > 0))
    if sharp > 120 and frac > 0.08:
        return "실사 푸티지. 평면 2D MG·UI 녹화만 받음."
    return None
```

Wire multipart POST on `/api/projects`. Bind ingest start button to estimate + (full → confirm) + `location = /analyze?job=`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_estimate.py tests/test_web_ingest.py tests/test_web_server.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/web/estimate.py keepframe/web/liveaction.py keepframe/web/server.py keepframe/web/static/ingest.html keepframe/web/static/js/api.js tests/test_web_estimate.py tests/test_web_ingest.py
git commit -m "feat(web): upload filmstrip estimate and live-action reject"
```

---

### Task 6: Analyze job and progress page

**Files:**
- Create: `keepframe/web/jobs.py`
- Modify: `keepframe/web/server.py`, `keepframe/web/static/analyze.html`
- Test: `tests/test_web_jobs.py`

**Interfaces:**
- `Job` dataclass: `id: str`, `kind: str` (`analyze`|`correct`), `status: str` (`queued`|`running`|`done`|`error`), `project_id: str`, `scene_id: str | None`, `stage: str | None`, `error: str | None`, `eta_s: int | None`, `result: dict | None`
- `JobStore`: `submit(kind, fn, **meta) -> Job` runs `fn` on a daemon thread; `get(id) -> Job`; `list() -> list[Job]`
- `POST /api/analyze` JSON `{project_id, confirm_token?}`. If project `mode=="full"` and `consume_token` is False → 400 `{"error": "confirm required"}`. Else set meta status `analyzing`, submit a job that calls `analyze(source.mp4, start, end, project_dir)`, writes `status=review` or `error`, returns 202 `{job}`.
- `GET /api/jobs/{id}` → job dict.
- analyze.html polls every 1000 ms, shows `stage` and `eta_s` as text (not a full-page spinner). On `done` navigate to `/review?project={id}&scene=s1`. On `error` show `job.error` with `role="status"`.

Range start/end come from `meta.json.range` or `(0, frames-1)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_jobs.py
import time
from keepframe.web.jobs import JobStore

def test_job_runs_and_finishes():
    store = JobStore()
    j = store.submit("analyze", lambda: {"ok": True}, project_id="p1")
    assert j.status in ("queued", "running")
    for _ in range(50):
        if store.get(j.id).status == "done":
            break
        time.sleep(0.02)
    got = store.get(j.id)
    assert got.status == "done" and got.result == {"ok": True}

def test_job_surfaces_error():
    store = JobStore()
    def boom():
        raise RuntimeError("gpu missing")
    j = store.submit("analyze", boom, project_id="p1")
    for _ in range(50):
        if store.get(j.id).status == "error":
            break
        time.sleep(0.02)
    assert "gpu missing" in store.get(j.id).error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_jobs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.web.jobs'`

- [ ] **Step 3: Implement JobStore and /api/analyze**

```python
# keepframe/web/jobs.py
from __future__ import annotations
import secrets, threading, traceback
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

@dataclass
class Job:
    id: str
    kind: str
    status: str
    project_id: str
    scene_id: str | None = None
    stage: str | None = None
    error: str | None = None
    eta_s: int | None = None
    result: dict | None = None

    def to_json(self) -> dict:
        return asdict(self)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, jid: str) -> Job:
        return self._jobs[jid]

    def list(self) -> list[Job]:
        return list(self._jobs.values())

    def submit(self, kind: str, fn: Callable[[], Any], **meta) -> Job:
        j = Job(id="j" + secrets.token_hex(4), kind=kind, status="queued", **meta)
        self._jobs[j.id] = j

        def work():
            j.status = "running"
            try:
                j.result = fn() or {}
                j.status = "done"
            except Exception as e:
                j.status = "error"
                j.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()

        threading.Thread(target=work, daemon=True).start()
        return j
```

`POST /api/analyze` uses one process-wide `JobStore` on the server. `fn` calls `analyze` from the prerequisite pipeline.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_jobs.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/web/jobs.py keepframe/web/server.py keepframe/web/static/analyze.html tests/test_web_jobs.py
git commit -m "feat(web): analyze job store and progress page"
```

---

### Task 7: Review APIs bound to Stitch review.html

**Files:**
- Modify: `keepframe/web/server.py`, `keepframe/web/static/review.html`, `keepframe/web/static/js/api.js`
- Test: `tests/test_web_review.py`

**Interfaces:**
- Consumes: `current_scene`, `new_version`, `composite_scene`, `corrections.*`, `scene_dir`, `load_project`
- Produces (same contracts as old Task 26, scoped by `?project=`):
  - `GET /api/state?project=&scene=s1&v=` → `{project, version, scene, report, job}`
  - `GET /api/job?project=` → current correct job
  - `GET /frame/orig/{f}?project=&scene=` PNG from `stages/frames.npy` or source video
  - `GET /frame/recon/{f}?project=&scene=&v=` PNG from `composite_scene`
  - `GET /assets/{name}?project=&scene=` PNG only
  - `POST /api/keep` `{project, scene, changes:[{pred, keep}], note}`
  - `POST /api/correct` `{project, scene, op, args}` ops `reassign|mask|bbox|text` → 202
- review.html behavior (from old Task 27, keep these exact interactions):
  - Load `/api/state` on start; `?v=` selects version
  - Two panes `#orig` `#recon`; Space play/pause; arrows frame-step; `[` `]` error peaks
  - Timeline with error strip; side panel keep checkboxes + four correction forms
  - Poll `/api/job` every 700 ms; disable forms while running; on done reload `?v=`
  - Empty elements: "인식된 요소가 없습니다. 원본 화면에서 박스를 그려 첫 요소를 지정하세요."
  - 1280×800: panes + timeline + panel on screen without window scroll

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_review.py
import json
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import init_project, scene_dir
from tests.test_web_server import start, get

def test_state_from_synthetic(tmp_path):
    root = tmp_path / "ws" / "p1"
    scene = make_synthetic_scene(root / "gold", seed=11, with_text=False)
    # init_project expects the scene files under root; copy via init_project
    from keepframe.ir.store import save_scene
    from keepframe.ir.schema import Scene
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, scene.frames - 1]}, scene)
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "t", "status": "review"}))
    srv = start(tmp_path / "ws")
    import urllib.request
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/state?project=p1&scene={scene.id}"
    with urllib.request.urlopen(url) as r:
        body = json.loads(r.read())
    srv.shutdown()
    assert body["scene"]["id"] == scene.id
    assert body["version"]["id"] == "v1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_review.py -v`
Expected: FAIL (404 on `/api/state`)

- [ ] **Step 3: Implement review routes and bind review.html**

Port the handler bodies from `docs/superpowers/plans/2026-09-05-keepframe-m1-m2.md` Task 26 (`ReviewState`, `/api/state`, frames, keep, correct) into `keepframe/web/server.py`, keyed by `project` under the workspace. Wire review.html to those endpoints. Do not add a fifth correction.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_review.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/web/server.py keepframe/web/static/review.html keepframe/web/static/js/api.js tests/test_web_review.py
git commit -m "feat(web): bind Stitch review desk to keep and four corrections"
```

---

### Task 8: Library binding and empty / reject / error states

**Files:**
- Modify: `keepframe/web/static/library.html`, `keepframe/web/static/ingest.html`, `keepframe/web/static/analyze.html`, `keepframe/web/static/review.html`
- Test: `tests/test_web_states.py`

**Interfaces:**
- library.html on load fetches `/api/projects` and renders one row per project (title, status, version, confidence). Empty workspace: "프로젝트가 없습니다. 새 프로젝트에서 레퍼런스를 올리세요." plus a link to `/new`.
- Status pills: `uploaded` → "업로드됨", `analyzing` → "분석 중", `review` → "검수 필요", `error` → "실패", `rejected` → "거부됨".
- Row click: `analyzing` → `/analyze?job=` if `meta.job_id` else `/review?project=`; `review` → `/review?project=&scene=s1`; `rejected` stays on library (reason in the row, no video).
- ingest live-action 422 shows `error.liveaction` in a `role="status"` banner; no filmstrip of the rejected file.
- analyze error shows the job error string; no retry-cap override control.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_states.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_states.py -v`
Expected: FAIL (empty copy missing)

- [ ] **Step 3: Bind library.js behavior and banners**

Add a `<script>` at the bottom of library.html that calls `api("/api/projects")` and fills `#project-list`. Keep DESIGN.md pills.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_states.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/web/static tests/test_web_states.py
git commit -m "feat(web): library rows and reject empty error states"
```

---

### Task 9: Browser smoke for the maker loop

**Files:**
- Create: `tests/test_web_smoke.py`
- Modify: `scripts/m2_gate.py` only if it already exists from the prerequisite plan — add a note in stdout that web smoke is `pytest tests/test_web_smoke.py`. Do not change M2 numeric gates.

**Interfaces:**
- `@pytest.mark.browser` test using playwright: start `make_server` on a workspace with one uploaded synthetic mp4 (from `render_scene_video` if analyze pipeline is present; otherwise skip with `pytest.importorskip`).
- Visit `/`, expect "새 프로젝트" or `data-i18n="nav.new"`.
- Visit `/new`, expect range toggle.
- Visit `/review?project=` after `init_project` of a synthetic scene; expect `#orig` and `#recon` in the DOM.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_smoke.py
import pytest
from pathlib import Path

pytest.importorskip("playwright")

@pytest.mark.browser
def test_library_and_review_render(tmp_path):
    from playwright.sync_api import sync_playwright
    from keepframe.ir.synth import make_synthetic_scene
    from keepframe.ir.store import init_project
    from keepframe.web.server import make_server
    import json, threading

    root = tmp_path / "p1"
    scene = make_synthetic_scene(tmp_path / "gold", seed=3, with_text=False, frames=12)
    init_project(root, {"file": "ref.mp4", "fps": scene.fps, "size": list(scene.size), "mode": "range", "range": [0, 11]}, scene)
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "Smoke", "status": "review"}))
    srv = make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with sync_playwright() as p:
            page = p.chromium.launch().new_page(viewport={"width": 1280, "height": 800})
            page.goto(base + "/")
            assert page.locator("body").count() == 1
            page.goto(base + "/review?project=p1&scene=" + scene.id)
            page.wait_for_selector("#orig, [data-pane=orig]")
    finally:
        srv.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_smoke.py -v`
Expected: FAIL until review.html has `#orig` or `[data-pane=orig]` (add one of those ids in Step 3 if missing)

- [ ] **Step 3: Ensure review.html has `#orig` and `#recon`**

If the ported markup used different ids, add `id="orig"` and `id="recon"` to the two viewer images. Do not change layout.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_smoke.py -v`
Expected: PASS (needs Chromium)

- [ ] **Step 5: Commit**

```bash
git add tests/test_web_smoke.py keepframe/web/static/review.html
git commit -m "test(web): browser smoke for library and review at 1280"
```

---

## Self-review

- Spec §6 four pages: Tasks 2–8. Cost gate §9: Task 5–6. Four corrections §6: Task 7. Live-action reject: Task 5, 8. Admin absent locally: Task 2. M3 screens not included.
- No TBD/TODO placeholders. Types used in later tasks are defined in Prerequisite or earlier tasks.
- `JobStore.submit` / `estimate` / `create_project` names are stable across Tasks 4–7.
