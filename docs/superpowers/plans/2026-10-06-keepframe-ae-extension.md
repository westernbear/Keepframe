# Keepframe After Effects Extension — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. **Implementer = Codex** (`run-codex.sh` = `codex exec -m gpt-6.1-sol -c model_reasoning_effort=xhigh -s workspace-write -c sandbox_workspace_write.network_access=true -C <worktree>`; the sandbox keeps `.git` read-only, so the controller runs the full suite outside the sandbox and commits). Reviews = Claude subagents (task: sonnet, re-review: haiku, final: opus). Steps use `- [ ]`.

**Goal:** Replace the round-2 After Effects path (Python connector → MCP helper → file bridge → ScriptUI panel → relay port), which never completed a live run, with a signed CEP extension that pairs once with a code and keeps an AE comp in sync with Keepframe's scene: build, verify, final MP4 + project zip, and live agent control.

**Spec (binding):** `docs/superpowers/specs/2026-10-06-keepframe-ae-extension-design.md` (commit `2c1c8d1`). Every section approved in conversation 2026-10-06.

**Architecture:** New `keepframe/ae/` (spec, devices, jobs, api, verify) on the existing server and port; `extension/` (CSXS manifest, `index.html` + `panel.js` + a Node-testable `core.js`, `host/keepframe.jsx`). The server sends a declarative `CompSpec`; one ExtendScript routine (`kfSync`) makes AE match it by stable layer tags. HTTP long-poll. The old `keepframe/after_effects/` (~26k lines) is deleted in slice 1.

**Tech stack:** Python 3.12 stdlib server (`ThreadingHTTPServer`), pydantic 2, existing compose/render (Playwright Chromium) for verify; CEP 11 extension in vanilla JS + Node built-ins (no npm deps; `window.__adobe_cep__.evalScript` instead of vendoring CSInterface); ExtendScript ES3; Node 25 for the fake AE and the headless extension tests; Adobe `ZXPSignCmd` under Wine in Docker for signing.

## Context

On 2026-10-05 the live check never got past pairing: user-site imports hidden from the MCP child, ExceptionGroup-wrapped errors, a stale-heartbeat deadlock, an icacls step that emptied file DACLs, an ES3 engine without `JSON`, session cookies that locked projects, `SimpleCookie` dropping cookies, and a ctypes struct that corrupted the heap. Each layer was only tested against fakes of the layer below. The user chose to rebuild everything AE, server side included (decisions table in the spec).

## Global Constraints

- No new core runtime dependencies (`pyproject.toml` `dependencies`). Remove the `ae = ["mcp>=2.2,<3"]` extra. The extension has no npm dependencies.
- The extension only runs fixed entry points (`kfInfo`, `kfSync`, `kfRender`, `kfPackage`) with JSON data; nothing received from the server is evaluated as code.
- Device tokens are stored hashed; pairing codes are single use, 10 minutes. Never print or log tokens, codes after use, HF/OAuth tokens, or the signing certificate password. The certificate lives outside git (`~/.config/keepframe/`).
- Browser routes keep the server's same-origin check; extension routes take only `Authorization: Bearer <device token>`.
- No silent failure: every error ends as job `error` text (web + panel) or a panel status line; ExtendScript errors carry the line number.
- UI copy ko + en (`i18n.js`); one cache-buster stamp for all pages.
- Don't commit `eval/`, `.superpowers/`, `.impeccable/`, `keepframe/ae/static/keepframe.zxp`, or any certificate.
- Tests: `.venv/bin/python -m pytest -p no:cacheprovider -q -m 'not browser and not gpu and not ocr'`; Node tests: `node --test extension/test tests/ae_fake`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. After merge: `graphify update .` in the main checkout.
- Each slice ends with a live check in the user's AE (Windows, AE 24.1+, Korean UI). The controller runs it with the user (AskUserQuestion), records results and screenshots in `docs/qa/ae-extension/README.md`, and turns every finding into fake-AE behaviour plus a test before fixing.

## Review Focus

1. Re-sending the same version changes nothing in AE and adds no second undo step → Task 8 `test_sync_twice_is_a_no_op`.
2. A user's own (untagged) layer in the Keepframe comp survives every sync, including deletion of Keepframe layers around it → Task 8 `test_untagged_layers_are_never_touched`.
3. The network drops mid-upload: the partial file never becomes downloadable and the job fails with "AE disconnected" → Tasks 6/15 `test_partial_upload_is_discarded`, Task 5 `test_running_job_fails_after_device_silence`.
4. Two agent edits in quick succession with Live on: AE ends on the newest version; the older queued sync never runs → Task 19 `test_live_coalesces_to_newest_version`.
5. A job asks for an asset name that is not in its spec, or contains `..`/absolute paths: the server refuses, and the extension never writes outside its asset folder → Task 6 `test_asset_outside_spec_is_refused`, Task 9 `test_asset_cache_paths_stay_inside_folder`.

---

## Task 0: Workspace

- [ ] `git worktree add .worktrees/ae-ext -b feature/ae-extension` (base: master `2c1c8d1`); venv `python3.12 -m venv .venv && .venv/bin/pip install -e '.[ocr,llm,dev]' && .venv/bin/python -m playwright install chromium`.
- [ ] Plan copy at `docs/superpowers/plans/2026-10-06-keepframe-ae-extension.md`; SDD workspace + ledger; `run-codex.sh` (command above).
- [ ] Record the baseline test count. Commit `docs: add AE extension plan`.

## Slice 1 — Foundation + comp build

### Task 1: Signing spike (decides the build route)
**Files:** Create `scripts/build_zxp.sh`, `extension/CSXS/manifest.xml` (minimal), `extension/index.html` (hello).
- [ ] Run Adobe `ZXPSignCmd` (CEP-Resources, Windows build) under Wine in Docker (pattern of the public `zxpsigncmd-docker` images): `-selfSignedCert` once to `~/.config/keepframe/zxp-cert.p12` (password from `~/.config/keepframe/zxp-cert.pass`, 0600), then `-sign extension keepframe/ae/static/keepframe.zxp`, then `-verify`.
- [ ] If Wine/Docker fails: a `.github/workflows/build-zxp.yml` Windows job, with the certificate as a repository secret; record the ruling in the ledger.
- [ ] Done = a signed hello-world `.zxp` that `-verify` accepts. Commit `build(ae): sign the extension with ZXPSignCmd`.

### Task 2: Remove the old AE path
**Files:** Delete `keepframe/after_effects/`, `tests/test_ae_*.py`, `tests/ae_panel_host.js`, `docs/qa/round2/ae-live-check.md`, `scripts/ae_live_check.py`. Modify `keepframe/cli.py` (drop `ae-install`, `ae-connect`, `_ae_relay_config`, relay thread), `keepframe/web/server.py` (drop `/api/ae/status|pairings|artifacts|sessions`, `_authorize_ae_browser`, controller cookie, `_ae_execution_plan`, `prepare_ae_final_successor`, `ae_relay_url`/`workflow` params, AE branches in render-plan routes), `keepframe/render/plan.py` (`RenderBackend = Literal["native", "lottie"]`, drop AE fields and branches), `keepframe/session/tools.py` (enums without `after_effects`), `keepframe/web/static/agent.html` + `js/agent.js` (AE option, pairing block, substitutions, direction field), `pyproject.toml` (drop the `ae` extra), README (old AE section), tests that referenced AE (`test_web_render_plans.py`, `test_render_plan.py`, `test_cli.py`, `test_session_tools.py`, `test_web_agent.py`, `test_reveal_track.py`, `test_ir_spin.py`, `test_chatgpt_client.py`; delete the AE-only cases, keep the rest).
- [ ] `grep -rn "after_effects\|ae_relay\|ae-connect\|ae-install" keepframe tests scripts README.md` returns nothing except the spec/plan docs.
- [ ] Full suite green. Commit `refactor(ae): remove the connector, MCP helper, file bridge, ScriptUI panel and relay`.

### Task 3: `comp_spec`
**Files:** Create `keepframe/ae/__init__.py`, `keepframe/ae/spec.py` · Test `tests/test_ae_spec.py`
**Interfaces:** `comp_spec(scene: Scene, scene_dir: Path, *, project: str, scene_id: str, version: str, fonts: list[dict] | None = None) -> dict` (spec §`spec.py`); `ease_to_ae(ease, v0, v1, dt) -> dict` (`{out_influence, out_speed, in_influence, in_speed}`); `png_size(path) -> (w, h)` read from the IHDR (no Pillow); assets resolved with `keepframe/ir/paths.py:scene_asset_path`.
- [ ] Tests: each row of the spec's kind table and property table; a `scale_fix` for a 2× PNG; text anchor deferred (`anchor: null`, `anchor_fraction` given); `z` changing over time → one warning + order by first visible frame; font match by family/candidates/nearest weight, else `ArialMT` + `substituted`; `reveal` always 1 → `effects.reveal: null`; `rx/ry` only on `model`; 3D without GLB → `image` + `3D candidate`; image background → bottom `kf:background`; determinism (same scene → byte-identical JSON); asset names never contain path separators.
- [ ] Commit `feat(ae): describe a scene version as an AE comp`.

### Task 4: Devices and pairing codes
**Files:** Create `keepframe/ae/devices.py` · Test `tests/test_ae_devices.py`
**Interfaces:** `Devices(workspace)`: `create_code(now=None) -> (code, expires_at)`, `pair(code, info, now=None) -> (device_id, token)`, `authenticate(token) -> Device | None`, `touch(device_id, status, now=None)`, `revoke(device_id)`, `list(now=None) -> list[dict]` (`connected` = seen < 40 s). Storage `<workspace>/.ae/devices.json`, atomic, 0600, guarded by a lock.
- [ ] Tests: `KF-XXXX-XXXX` format; expiry; single use; at most 5 live codes; only `sha256(token)` on disk; revoke; fonts list capped (5,000 × 256 chars); wrong code → `None` without timing leaks (`hmac.compare_digest`).
- [ ] Commit `feat(ae): pair devices with one-time codes`.

### Task 5: Job queue
**Files:** Create `keepframe/ae/jobs.py` · Test `tests/test_ae_jobs.py`
**Interfaces:** `Jobs(workspace)`: `enqueue(device, kind, project, scene, version, params=None) -> Job` (adds a prerequisite `sync` for `render_frames|render_final|package` when `last_synced(device, project, scene) != version`; replaces a queued `sync` for the same device+scene); `next(device, wait=25.0) -> Job | None` (condition variable; marks `running`); `finish(job_id, ok, result=None, error=None)`; `sweep(devices_last_seen, now=None)` (running job with device silent ≥ 60 s → `failed: "AE disconnected"`); `last_synced(...)`; `state(project, scene)`. Storage `<workspace>/.ae/jobs/<id>.json`; prune finished > 7 days at start.
- [ ] Tests: wake-up latency < 0.2 s after enqueue; one running job per device; `test_running_job_fails_after_device_silence`; coalescing; prerequisite sync; persistence across restart (running → failed "server restarted").
- [ ] Commit `feat(ae): per-device job queue with long-poll wake-up`.

### Task 6: HTTP API + server wiring
**Files:** Create `keepframe/ae/api.py` (`AERoutes(workspace)` with `handle_get/post/put/delete(handler, u) -> bool`, mirroring `keepframe/admin/http.py:AdminRoutes`), Modify `keepframe/web/server.py` (construct `AERoutes` in `make_server`; delegate in `do_GET/do_POST/do_PUT/do_DELETE`; a sweeper thread every 10 s), `keepframe/cli.py` (`serve` unchanged otherwise) · Test `tests/test_ae_api.py` (real server via `tests/test_web_server.py:start`)
- [ ] Routes exactly as the spec's two tables (`/api/ae/codes`, `devices`, `send`, `state`, `pair`, `next`, `jobs/<id>/spec|assets|files|result|progress`, `/ae/keepframe.zxp`); `426` on extension major mismatch; upload allowlist + caps; temp file → rename on completion.
- [ ] Tests: browser routes reject cross-origin; extension routes reject missing/revoked tokens; `test_asset_outside_spec_is_refused`; `test_partial_upload_is_discarded` (client closes mid-body); long-poll returns 204 after `wait` and 200 immediately after `send`; `send` with no connected device → 409; zxp served with `application/zip` (404 + readable error when not built).
- [ ] Commit `feat(ae): extension and browser routes on the main server`.

### Task 7: Fake After Effects in Node
**Files:** Create `tests/ae_fake/ae.js` (object model), `tests/ae_fake/run.js` (CLI: `node run.js <state.json> <jsx> <entry> <json-args…>` → loads state, runs the entry, writes state back, prints the result), `tests/ae_fake/README.md` (supported API surface), `tests/ae_fake/test_fake.test.js`; Python helper `tests/ae_fake_runner.py:run_jsx(state_path, entry, *args) -> dict`.
- [ ] Model: `app.project` (items, `FolderItem`, `FootageItem` with `mainSource.file`, `CompItem` with `layers`, `comment`, `width/height/frameRate/duration`), `importFile(ImportOptions)`, layers `add/addText/addSolid/addNull`, `AVLayer` (`comment`, `inPoint/outPoint`, `moveBefore/moveAfter`, `remove`, `property("ADBE Transform Group")`, `Effects.addProperty(matchName)`), `Property` (`setValue`, `setValueAtTime`, `numKeys`, `removeKey`, `keyValue`, `setTemporalEaseAtKey`, `separationDimensions`), `TextDocument`, `app.beginUndoGroup/endUndoGroup` (counted), `app.fonts.allFonts`, `app.version`, `$.getenv`, `File/Folder` (from tonight's host), `app.project.renderQueue` (slice 2), **no global `JSON`**. Unknown API calls throw `"fake AE: unsupported <name>"` so gaps are loud.
- [ ] Commit `test(ae): fake After Effects object model for ExtendScript tests`.

### Task 8: `host/keepframe.jsx` — `kfInfo` and `kfSync`
**Files:** Create `extension/host/keepframe.jsx` (JSON fallback moved from the old panel), Test `tests/test_ae_host_sync.py` (via `run_jsx`)
- [ ] Implement the spec's 7-step algorithm. Layer comment `keepframe:<id>;fp=<fingerprint>`; comp comment = `comp.tag`; footage comment = sha256; easing via `KeyframeEase`; Linear Wipe (`ADBE Linear Wipe`, completion/angle/feather) and Transform effect (`ADBE Geometry2`, skew/axis); model layers via `importFile` of the GLB; text anchor from `sourceRectAtTime(0)` × `anchor_fraction`.
- [ ] Tests: create from empty; `test_sync_twice_is_a_no_op` (no property writes, one undo group per call); update changes only changed layers; kind change recreates; deleted element removes its layer; `test_untagged_layers_are_never_touched`; hand edit → `applied: false, hand_edited: [...]`, then `force` applies; reveal/skew/model/ease/order mapping; any thrown error → `{ok:false, error, line}`.
- [ ] Commit `feat(ae): ExtendScript sync that makes a comp match its description`.

### Task 9: Extension panel
**Files:** Create `extension/CSXS/manifest.xml` (spec values), `extension/index.html`, `extension/css/panel.css`, `extension/js/core.js` (pure logic, Node-testable: URL policy, pairing, loop with backoff, job runner with injected `http`, `evalScript`, `fs`), `extension/js/panel.js` (DOM + CEP glue: `window.__adobe_cep__.evalScript`, `require('https'|'http'|'fs'|'zlib'|'os')`), `extension/test/core.test.js` (`node --test`).
- [ ] UI: server URL, pairing code, status line (`Connected · <server>` / `Not connected: <reason>`), current job + stage, 200-line log with **Copy log**, extension version; ko/en by AE locale.
- [ ] Tests: URL policy (https anywhere; http only loopback, `100.64.0.0/10`, `*.ts.net`); backoff 1→30 s; 426 shows the update link; `test_asset_cache_paths_stay_inside_folder`; sync job: download only missing hashes → `kfSync` → post result; hand-edit result surfaces in status.
- [ ] Commit `feat(ae): CEP panel that pairs, long-polls and runs sync jobs`.

### Task 10: Web UI — After Effects card
**Files:** Modify `keepframe/web/static/agent.html`, `js/agent.js` (or a new `js/ae.js`), `css/app.css`, `js/i18n.js`, cache-buster stamp · Test `tests/test_web_static.py`
- [ ] Card: **Download extension** + install hint (UPIA one-liner), **Connect AE** → code with copy button and expiry, devices list (connected/AE version/open project/**Disconnect**), **Send to AE** for the current version, last synced version, job state/error, hand-edit prompt (**Overwrite them** → `send` with `force`).
- [ ] Commit `feat(web): After Effects card for pairing and sync`.

### Task 11: End-to-end in CI
**Files:** Create `tests/test_ae_e2e.py` (spawns `node tests/ae_fake/extension_runner.js`, which runs `extension/js/core.js` against the fake AE and the real server).
- [ ] Pair → send → comp built → resend no-op → hand edit in the fake → warning → force → disconnect mid-job → "AE disconnected".
- [ ] Commit `test(ae): pair and sync end to end against the fake AE`.

### Task 12: Build, docs, live check 1
**Files:** `scripts/build_zxp.sh` (final), `docs/qa/ae-extension/live-check.md`, `docs/qa/ae-extension/README.md`, README AE section.
- [ ] Doc: download → install (`UnifiedPluginInstallerAgent.exe /install keepframe.zxp` or aescripts ZXP Installer) → **Window > Extensions > Keepframe** → server URL (`sudo tailscale serve --https=8766 http://127.0.0.1:8765` replaces the old relay mapping; the user runs it) → pair → **Send to AE** on the three live-check scenes (reveal text, image background, spinning GLB).
- [ ] Controller runs it with the user. Checks: comp and layers appear and are editable; reveal wipes left→right (angle sign); background at the bottom; GLB model rotates on the right axes; fonts/substitution notes; resend is a no-op; hand edit warning. Findings → fake AE + tests → fixes. Record in README.
- [ ] Slice-1 review (opus) on `master..HEAD`; merge locally after the user approves (finishing-a-development-branch).

## Slice 2 — Verify

### Task 13: Frame rendering
**Files:** `extension/host/keepframe.jsx` (`kfRender({mode:"frames", frames, folder})`: render-queue item for the tagged comp, PNG-sequence output module chosen by format, half resolution, wait for completion, return paths), `extension/js/core.js` (`render_frames` job: render → upload `frame_<NNNN>.png` for the sampled frames), fake AE render queue (writes PNGs from a test fixture), tests.
- [ ] Commit `feat(ae): render sampled frames in AE and upload them`.

### Task 14: `verify.py` + report UI
**Files:** Create `keepframe/ae/verify.py` (spec §verify; reuses `keepframe/compose/composer.py:compose` and `keepframe/render/renderer.py:render`, metric as `keepframe/gates.py:render_fidelity`), `keepframe/ae/api.py` (`POST /api/ae/verify`, report + images), web card section (pass/fail, notes, three worst frames AE/Keepframe/diff) · Tests `tests/test_ae_verify.py` (synthetic frames; thresholds; font notes; job prerequisite sync).
- [ ] Commit `feat(ae): compare AE frames with Keepframe's render`.

### Task 15: Live check 2 + calibration
- [ ] Run Verify on the three live-check scenes and one real clip (ig2); set `VERIFY_MEAN_MAX`/`VERIFY_FRAME_MAX` from the observed numbers (rule unchanged); `test_partial_upload_is_discarded` re-run against a real AE upload interruption if the user can (pull the network). Record in README. Slice review + merge.

## Slice 3 — Final render + package

### Task 16: Final MP4
**Files:** `kfRender({mode:"final"})` choosing the H.264 output module by its format setting, not its (localized) template name; `render_final` job uploads `final.mp4` streamed; web **Download MP4** per version; tests (fake AE output modules with Korean template names).
- [ ] Commit `feat(ae): final H.264 render from AE`.

### Task 17: Project package
**Files:** `kfPackage` (saved project required; returns `.aep` path + footage paths), `extension/js/zip.js` (store + `zlib.deflateRawSync`, ZIP64 not needed under 4 GB), `package` job uploads `project.zip`; web **Download project zip**; tests (Python `zipfile` opens the result; unsaved project → "Save the AE project first").
- [ ] Commit `feat(ae): package the AE project with its footage`.

### Task 18: Live check 3
- [ ] Korean AE: H.264 selection works; MP4 plays and matches; zip opens on another path with footage relinked. Record; slice review + merge.

## Slice 4 — Live agent control

### Task 19: Version listener + Live flag
**Files:** Modify `keepframe/ir/store.py` (`on_new_version(callback)` registry, called after `new_version` saves; listener errors logged, never raised), `keepframe/ae/jobs.py` (Live flags in `<workspace>/.ae/live.json`; enqueue coalescing `sync` per connected device; hand-edit result pauses Live), `keepframe/ae/api.py` (`POST /api/ae/live`), web card (**Live** toggle, "Live paused: N layers edited by hand" with **Overwrite them** / **Keep my AE edits**) · Tests.
- [ ] `test_live_coalesces_to_newest_version`; pause on hand edit; Live off → no jobs; listener failure does not break `new_version`.
- [ ] Commit `feat(ae): live sync of new scene versions`.

### Task 20: Agent tools
**Files:** Modify `keepframe/session/tools.py` (`ae_status` read-only; `send_to_ae` with `needs_confirm=True` unless Live is on, confirmed through the existing pending/confirm flow in `js/agent.js`; `ae_verify` waits up to 120 s for the job then returns the summary), `keepframe/session/agent.py` (SYSTEM: AE tools and when to use them) · Tests `tests/test_session_tools.py`.
- [ ] Commit `feat(agent): After Effects status, send and verify tools`.

### Task 21: Live check 4 + final review
- [ ] With Live on, an agent edit appears in AE within ~1 s of the new version; two quick edits end on the newest; hand edit pauses Live and the agent reports it through `ae_status`.
- [ ] Final whole-branch review (opus) → fixes → finishing-a-development-branch (user chooses merge/push). `graphify update .`. Update memory (`keepframe-round2.md` → new AE status).

---

## Verification

1. Python suite + `node --test extension/test tests/ae_fake` green; `tests/test_ae_e2e.py` green.
2. `grep` shows no references to the old AE path outside docs.
3. `scripts/build_zxp.sh` produces a `.zxp` that `ZXPSignCmd -verify` accepts; `/ae/keepframe.zxp` serves it.
4. Live checks 1–4 in the user's AE pass and are recorded in `docs/qa/ae-extension/README.md` with screenshots: pairing, sync of the three scenes, no-op resend, hand-edit warning, Verify pass with calibrated thresholds, MP4 + zip downloads, Live edits within ~1 s.
