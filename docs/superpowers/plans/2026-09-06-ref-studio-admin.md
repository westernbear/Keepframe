# Ref Studio Admin Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the cloud/team admin desk (login, tenants, job queue, quarantine, audit log) behind `AdminService`, off by default on the local edition.

**Architecture:** `refstudio/admin` is a protocol plus an in-memory store for tests. `refstudio serve --admin` mounts `/admin/*`. Without the flag, `/admin` stays 404 with the Korean local-edition sentence from Plan A. Admin screens are Stitch HTML taken offline the same way as maker pages. Admin never edits scene IR and never raises retry/asset caps.

**Tech Stack:** Python 3.11+, pydantic v2 (if already in the package), stdlib `http.server` / `http.cookies`, pytest. No React. No billing prices.

**Spec:** `docs/superpowers/specs/2026-09-05-ref-studio-design.md` §13 (and §7.1 / §9 policy). Visual: `DESIGN.md`. Offline-port rules: `docs/superpowers/plans/2026-09-06-ref-studio-m1-m2-stitch.md` Global Constraints (CDN forbidden, white pill CTA, `#0099ff` signal only). Screen source: `.stitch/designs/admin-*.html`.

## Global Constraints

- Do not edit `docs/superpowers/plans/2026-09-05-ref-studio-m1-m2.md`.
- Local default: `/admin` → 404 `{"error": "로컬판에는 이 화면이 없습니다."}`.
- `RETRY_CAP = 4`, `ASSET_GEN_CAP = 2`. No function, route, or form field may change these.
- No endpoint that turns on live-action, skips keep checks, edits IR, swaps models, or sets prices.
- Admin session cookie name is `refstudio_admin`. Maker must not use that name.
- Audit log is append-only. `DELETE /admin/api/audit` must not exist.
- Quarantine rows expose filename, tenant, rejected_at, reason only. No video URL, no thumbnail.
- Korean default, `localStorage("refstudio.lang")`, `data-i18n`. No emoji. No invented SLA.
- Runtime admin HTML must not load `cdn.tailwindcss.com`, `fonts.googleapis.com`, `lh3.googleusercontent.com`, or Material Symbols.
- Commit after every task. Never commit `.venv`, frames, or MP4s.

## Prerequisite

Plan A (`2026-09-06-ref-studio-m1-m2-stitch.md`) Tasks 1–9 done. This plan consumes:

- `refstudio.web.server.make_server(workspace, port=8765, host="127.0.0.1", admin=False)`
- `refstudio.web.jobs.Job`, `JobStore` (queue screen lists these when `--admin`)
- `refstudio/web/static/css/app.css`, `js/i18n.js`, `js/api.js`

---

## File Structure

```
refstudio/admin/__init__.py
refstudio/admin/policy.py        RETRY_CAP, ASSET_GEN_CAP, display helpers
refstudio/admin/models.py        Tenant, Member, QuarantineItem, AuditEvent, AdminSession
refstudio/admin/service.py       AdminService protocol
refstudio/admin/memory.py        MemoryAdmin
refstudio/admin/auth.py          login/logout, cookie
refstudio/admin/http.py          /admin routes mixed into make_server when admin=True
refstudio/web/static/admin-login.html
refstudio/web/static/admin-tenants.html
refstudio/web/static/admin-queue.html
refstudio/web/static/admin-quarantine.html
refstudio/web/static/admin-audit.html
tests/test_admin_policy.py
tests/test_admin_memory.py
tests/test_admin_static.py
tests/test_admin_auth.py
tests/test_admin_http.py
tests/test_admin_local_off.py
```

---

### Task 1: Policy constants (display only)

**Files:**
- Create: `refstudio/admin/__init__.py`, `refstudio/admin/policy.py`
- Test: `tests/test_admin_policy.py`

**Interfaces:**
- Produces: `RETRY_CAP: int = 4`, `ASSET_GEN_CAP: int = 2`
- `policy_note() -> str` returns exactly `재시도 상한 4회 · 에셋 생성 2회 · 관리자가 올릴 수 없음.`
- `remaining_retries(used: int) -> int` is `max(0, RETRY_CAP - used)`. No setter.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_policy.py
import ast
from pathlib import Path
from refstudio.admin.policy import RETRY_CAP, ASSET_GEN_CAP, policy_note, remaining_retries

def test_caps_are_fixed():
    assert RETRY_CAP == 4 and ASSET_GEN_CAP == 2
    assert policy_note() == "재시도 상한 4회 · 에셋 생성 2회 · 관리자가 올릴 수 없음."
    assert remaining_retries(4) == 0
    assert remaining_retries(1) == 3

def test_policy_module_has_no_setter():
    src = (Path("refstudio/admin/policy.py")).read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert "set_retry_cap" not in names
    assert "set_asset_cap" not in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_admin_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'refstudio.admin.policy'`

- [ ] **Step 3: Implement**

```python
# refstudio/admin/policy.py
RETRY_CAP = 4
ASSET_GEN_CAP = 2

def policy_note() -> str:
    return "재시도 상한 4회 · 에셋 생성 2회 · 관리자가 올릴 수 없음."

def remaining_retries(used: int) -> int:
    return max(0, RETRY_CAP - used)
```

```python
# refstudio/admin/__init__.py
from .policy import ASSET_GEN_CAP, RETRY_CAP, policy_note, remaining_retries
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_admin_policy.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add refstudio/admin/__init__.py refstudio/admin/policy.py tests/test_admin_policy.py
git commit -m "feat(admin): fixed retry and asset-generation caps"
```

---

### Task 2: Models and MemoryAdmin

**Files:**
- Create: `refstudio/admin/models.py`, `refstudio/admin/service.py`, `refstudio/admin/memory.py`
- Test: `tests/test_admin_memory.py`

**Interfaces:**
- `Tenant(id, name, status: Literal["ok","quota","suspended"], members: int, analyze_min: int, analyze_quota_min: int, render_min: int, created: str)`
- `Member(email, role: Literal["admin","billing","review_lead","maker"], tenant_id)`
- `QuarantineItem(id, filename, tenant_id, rejected_at, reason)` — no `video` field
- `AuditEvent(ts, actor, action, target, detail: str | None = None)`
- `AdminService` protocol methods:
  - `list_tenants() -> list[Tenant]`
  - `get_tenant(id) -> Tenant`
  - `list_members(tenant_id) -> list[Member]`
  - `suspend_tenant(id, actor: str) -> Tenant` (status `suspended`; writes audit)
  - `list_jobs() -> list[dict]` (shape `{id, tenant, kind, target, status, retries, gpu}`)
  - `list_quarantine() -> list[QuarantineItem]`
  - `add_quarantine(item) -> QuarantineItem`
  - `list_audit() -> list[AuditEvent]` newest first
  - `append_audit(event) -> None`
  - `audit_csv() -> str`
- `MemoryAdmin(seed: bool = True)` implements the protocol. Seed tenants: Northwind (`ok`, 6 members, 41/120 analyze, 18 render), Hanbit (`quota`, 2, 120/120, 9), Solo (`ok`, 1, 7/30, 2), Closed shop (`suspended`, 0, 0/0, 0). Seed one quarantine row `clip_street.mp4` / Solo / live-action reason. Seed audit rows matching the Stitch table (keep, export, member, suspend attempt).
- There is no `delete_audit` method.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_memory.py
import pytest
from refstudio.admin.memory import MemoryAdmin
from refstudio.admin.models import AuditEvent, QuarantineItem
from refstudio.admin.policy import RETRY_CAP

def test_seed_tenants_and_no_delete():
    svc = MemoryAdmin()
    ids = [t.id for t in svc.list_tenants()]
    assert "org_northwind" in ids and "org_closed" in ids
    assert not hasattr(svc, "delete_audit")
    n = len(svc.list_audit())
    svc.append_audit(AuditEvent(ts="15:00", actor="mina@ref.studio", action="멤버 추가", target="Northwind", detail="x"))
    assert len(svc.list_audit()) == n + 1
    assert svc.list_audit()[0].action == "멤버 추가"

def test_quarantine_has_no_video_field():
    item = QuarantineItem(id="q1", filename="a.mp4", tenant_id="org_solo", rejected_at="2026-09-06T09:12:00Z",
                          reason="실사 푸티지. 평면 2D MG·UI 녹화만 받음.")
    assert not hasattr(item, "video")
    assert "mp4" not in item.__dataclass_fields__ or "video" not in item.__dataclass_fields__

def test_job_retries_cannot_exceed_cap_in_seed():
    svc = MemoryAdmin()
    for j in svc.list_jobs():
        assert j["retries"] <= RETRY_CAP
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_admin_memory.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement models, protocol, MemoryAdmin**

Use `@dataclass` models. `MemoryAdmin.list_jobs` seed includes `job_1838` Hanbit `correct` `실패` `retries=4`. `suspend_tenant` appends audit `테넌트 정지`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_admin_memory.py tests/test_admin_policy.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add refstudio/admin/models.py refstudio/admin/service.py refstudio/admin/memory.py tests/test_admin_memory.py
git commit -m "feat(admin): in-memory tenants jobs quarantine and audit"
```

---

### Task 3: Offline-port five admin Stitch pages

**Files:**
- Create: `refstudio/web/static/admin-login.html`, `admin-tenants.html`, `admin-queue.html`, `admin-quarantine.html`, `admin-audit.html`
- Test: `tests/test_admin_static.py`

**Interfaces:**
- Same offline rules as Plan A Task 3. Pages load `/static/css/app.css`, `/static/js/i18n.js`, `/static/js/api.js`.
- Source: `.stitch/designs/admin-*.html`.
- login: wordmark, `운영` badge, 관리자 로그인, 들어가기, 제작 도구로 돌아가기 (`href="/"`), footer `로컬판에는 이 화면이 없습니다.` only as muted note (the page itself is the cloud door).
- tenants/queue/quarantine/audit: left nav links `/admin/tenants` `/admin/queue` `/admin/quarantine` `/admin/audit`. No `+ New Project`. No version string like `v 3.4.0`.
- i18n keys: `admin.login`, `admin.enter`, `admin.tenants`, `admin.queue`, `admin.quarantine`, `admin.audit`, `admin.policy`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_static.py
from pathlib import Path

STATIC = Path("refstudio/web/static")
FORBIDDEN = ("cdn.tailwindcss.com", "fonts.googleapis.com", "lh3.googleusercontent.com", "material-symbols")
PAGES = ("admin-login.html", "admin-tenants.html", "admin-queue.html", "admin-quarantine.html", "admin-audit.html")

def test_admin_pages_offline():
    for name in PAGES:
        text = (STATIC / name).read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{name} {bad}"
        assert 'href="/static/css/app.css"' in text

def test_no_maker_cta_or_fake_version():
    for name in PAGES:
        text = (STATIC / name).read_text(encoding="utf-8")
        assert "+ New Project" not in text
        assert "v 3.4.0" not in text and "V 0.4.0" not in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_admin_static.py -v`
Expected: FAIL FileNotFoundError

- [ ] **Step 3: Port the five pages**

Copy layout from `.stitch/designs/`. Strip CDN and remote images. Inline SVG icons. Remove invented version badges and maker CTAs.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_admin_static.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add refstudio/web/static/admin-*.html tests/test_admin_static.py
git commit -m "feat(admin): offline-port Stitch admin screens"
```

---

### Task 4: Admin auth, separate cookie

**Files:**
- Create: `refstudio/admin/auth.py`
- Test: `tests/test_admin_auth.py`

**Interfaces:**
- `COOKIE = "refstudio_admin"`
- `MemoryAuth`: `login(email, password) -> str | None` (session id). Seed user `mina@ref.studio` / `dev-admin` (test only, documented in the test). `get(session_id) -> str | None` (email). `logout(session_id) -> None`.
- `cookie_header(session_id: str) -> str` → `refstudio_admin={sid}; HttpOnly; Path=/admin; SameSite=Lax`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_auth.py
from refstudio.admin.auth import COOKIE, MemoryAuth, cookie_header

def test_login_and_cookie_name():
    auth = MemoryAuth()
    assert auth.login("mina@ref.studio", "wrong") is None
    sid = auth.login("mina@ref.studio", "dev-admin")
    assert sid and auth.get(sid) == "mina@ref.studio"
    h = cookie_header(sid)
    assert h.startswith(f"{COOKIE}=")
    assert "Path=/admin" in h
    assert COOKIE == "refstudio_admin"
    auth.logout(sid)
    assert auth.get(sid) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_admin_auth.py -v`
Expected: FAIL ModuleNotFoundError

- [ ] **Step 3: Implement MemoryAuth**

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_admin_auth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add refstudio/admin/auth.py tests/test_admin_auth.py
git commit -m "feat(admin): session cookie separate from maker"
```

---

### Task 5: Mount /admin when --admin

**Files:**
- Create: `refstudio/admin/http.py`
- Modify: `refstudio/web/server.py`, `refstudio/cli.py`
- Test: `tests/test_admin_http.py`, `tests/test_admin_local_off.py`

**Interfaces:**
- `ADMIN_PAGES = {"/admin": "admin-login.html", "/admin/": "admin-login.html", "/admin/login": "admin-login.html", "/admin/tenants": "admin-tenants.html", "/admin/queue": "admin-queue.html", "/admin/quarantine": "admin-quarantine.html", "/admin/audit": "admin-audit.html"}`
- When `admin=True`, `make_server(..., admin=True, admin_svc=MemoryAdmin(), admin_auth=MemoryAuth())` serves those HTML files and JSON:
  - `POST /admin/api/login` `{email, password}` → 200 Set-Cookie or 401
  - `POST /admin/api/logout`
  - `GET /admin/api/tenants` (401 without cookie)
  - `GET /admin/api/tenants/{id}`
  - `GET /admin/api/jobs`
  - `GET /admin/api/quarantine` — each item keys only `id,filename,tenant_id,rejected_at,reason`
  - `GET /admin/api/audit`
  - `GET /admin/api/audit.csv` `text/csv`
  - `GET /admin/api/policy` `{retry_cap: 4, asset_gen_cap: 2, note: policy_note()}`
- Protected JSON (all except login + HTML login page) require the cookie.
- `GET /admin/api/quarantine/{id}/video` → 404
- No `PUT`/`PATCH` for policy. No `DELETE /admin/api/audit`.
- CLI: `refstudio serve --workspace DIR [--admin]`
- When `admin=False`, existing Plan A test still holds.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_local_off.py
import json
from tests.test_web_server import start, get

def test_admin_still_404_by_default(tmp_path):
    srv = start(tmp_path)
    code, _, body = get(srv, "/admin")
    srv.shutdown()
    assert code == 404
    assert json.loads(body)["error"] == "로컬판에는 이 화면이 없습니다."
```

```python
# tests/test_admin_http.py
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from refstudio.web.server import make_server
from refstudio.admin.memory import MemoryAdmin
from refstudio.admin.auth import MemoryAuth
import threading

def start_admin(tmp_path):
    srv = make_server(tmp_path, port=0, admin=True, admin_svc=MemoryAdmin(), admin_auth=MemoryAuth())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

def post(srv, path, obj, cookie=None):
    req = Request(f"http://127.0.0.1:{srv.server_address[1]}{path}",
                  data=json.dumps(obj).encode(), method="POST",
                  headers={"content-type": "application/json"})
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urlopen(req) as r:
            return r.status, r.headers, json.loads(r.read())
    except HTTPError as e:
        return e.code, e.headers, json.loads(e.read())

def test_login_and_tenants(tmp_path):
    srv = start_admin(tmp_path)
    code, headers, body = post(srv, "/admin/api/login", {"email": "mina@ref.studio", "password": "dev-admin"})
    assert code == 200
    cookie = headers.get("Set-Cookie")
    assert "refstudio_admin=" in cookie
    req = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/policy")
    req.add_header("Cookie", cookie.split(";")[0])
    with urlopen(req) as r:
        pol = json.loads(r.read())
    srv.shutdown()
    assert pol["retry_cap"] == 4 and pol["asset_gen_cap"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_admin_http.py -v`
Expected: FAIL (make_server does not accept admin_svc)

- [ ] **Step 3: Implement http.py and wire make_server**

Keep `admin=False` path unchanged. Only extend kwargs.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_admin_http.py tests/test_admin_local_off.py tests/test_web_server.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add refstudio/admin/http.py refstudio/web/server.py refstudio/cli.py tests/test_admin_http.py tests/test_admin_local_off.py
git commit -m "feat(admin): mount admin routes only with --admin"
```

---

### Task 6: Bind tenant, queue, quarantine, audit pages

**Files:**
- Modify: `refstudio/web/static/admin-tenants.html`, `admin-queue.html`, `admin-quarantine.html`, `admin-audit.html`, `admin-login.html`, `js/i18n.js`
- Test: extend `tests/test_admin_static.py`

**Interfaces:**
- login form POST `/admin/api/login` then `location=/admin/tenants`
- tenants: render `/admin/api/tenants`; detail pane shows `policy_note()`; buttons 멤버 보기 / 정지 (`POST /admin/api/tenants/{id}/suspend` — add this POST in this task if missing; it only sets status and audit)
- queue: table from `/admin/api/jobs`; selected failure shows retries `n/4` and the policy sentence; no input to change cap
- quarantine: table from `/admin/api/quarantine`; no `<video>`, no `<img>` of footage
- audit: table from `/admin/api/audit`; link 내보내기 → `/admin/api/audit.csv`; no delete button

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_admin_static.py
def test_pages_call_admin_api():
    assert "/admin/api/tenants" in (STATIC / "admin-tenants.html").read_text(encoding="utf-8")
    assert "/admin/api/jobs" in (STATIC / "admin-queue.html").read_text(encoding="utf-8")
    assert "/admin/api/quarantine" in (STATIC / "admin-quarantine.html").read_text(encoding="utf-8")
    q = (STATIC / "admin-quarantine.html").read_text(encoding="utf-8")
    assert "<video" not in q.lower()
    assert "/admin/api/audit" in (STATIC / "admin-audit.html").read_text(encoding="utf-8")
    assert "delete" not in (STATIC / "admin-audit.html").read_text(encoding="utf-8").lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_admin_static.py::test_pages_call_admin_api -v`
Expected: FAIL

- [ ] **Step 3: Bind fetch/render scripts**

Small inline `<script>` per page using `api()`. Add `POST /admin/api/tenants/{id}/suspend` in `http.py` if Task 5 did not.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_admin_static.py tests/test_admin_http.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add refstudio/web/static/admin-*.html refstudio/web/static/js/i18n.js refstudio/admin/http.py tests/test_admin_static.py
git commit -m "feat(admin): bind Stitch admin pages to AdminService APIs"
```

---

### Task 7: Quarantine and audit API contracts

**Files:**
- Modify: `refstudio/admin/http.py` if needed
- Test: `tests/test_admin_http.py` (extend)

**Interfaces:**
- Quarantine JSON items must equal exactly the key set `{id, filename, tenant_id, rejected_at, reason}`.
- `GET /admin/api/audit.csv` first line `ts,actor,action,target,detail`.
- `DELETE /admin/api/audit` → 404 (even with cookie).
- `GET /admin/api/quarantine/q1/video` → 404.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_admin_http.py
def test_quarantine_keys_and_no_video(tmp_path):
    srv = start_admin(tmp_path)
    code, headers, _ = post(srv, "/admin/api/login", {"email": "mina@ref.studio", "password": "dev-admin"})
    cookie = headers.get("Set-Cookie").split(";")[0]
    req = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/quarantine")
    req.add_header("Cookie", cookie)
    with urlopen(req) as r:
        rows = json.loads(r.read())["items"]
    assert set(rows[0]) <= {"id", "filename", "tenant_id", "rejected_at", "reason"}
    req2 = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/quarantine/q1/video")
    req2.add_header("Cookie", cookie)
    try:
        urlopen(req2)
        assert False, "video must 404"
    except HTTPError as e:
        assert e.code == 404
    req3 = Request(f"http://127.0.0.1:{srv.server_address[1]}/admin/api/audit", method="DELETE")
    req3.add_header("Cookie", cookie)
    try:
        urlopen(req3)
        assert False
    except HTTPError as e:
        assert e.code == 404
    srv.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_admin_http.py::test_quarantine_keys_and_no_video -v`
Expected: FAIL until routes exist

- [ ] **Step 3: Enforce key set and 404s**

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_admin_http.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add refstudio/admin/http.py tests/test_admin_http.py
git commit -m "feat(admin): quarantine metadata only and immutable audit"
```

---

## Self-review

- Spec §13 screens: Tasks 3–6. Roles that cannot raise caps or enable live-action: Tasks 1, 5, 7. Local off: Task 5 + Plan A. No prices, no model swap, no IR editor.
- Cookie name distinct. Audit append-only. Types match across tasks (`Tenant.id`, `retry_cap`).
