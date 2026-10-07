# Keepframe AE Extension, Slice 2 (Verify) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. **Implementer = Codex** (`run-codex.sh` = `codex exec -m gpt-6.1-sol -c model_reasoning_effort=xhigh -s workspace-write -c sandbox_workspace_write.network_access=true -C <worktree>`; the sandbox keeps `.git` read-only, so the controller runs the suites outside the sandbox and commits). Reviews = Claude subagents (task: sonnet, re-review: sonnet, final: opus). Steps use `- [ ]`.

**Goal:** Press **Verify against AE** on the web page and get a pass/fail report that compares frames rendered in the user's After Effects with Keepframe's own render of the same scene version, with the three worst frames shown side by side.

**Architecture:** The web page enqueues a `render_frames` job (the job queue already adds a `sync` first when AE has another version). The panel asks AE for 16 sampled frames as PNGs (`kfRender`), uploads them through the existing upload route, and posts the frame list. The server then renders the same frames with Keepframe's renderer in a background thread, compares them (`keepframe/ae/verify.py`), and stores a report that `/api/ae/state` and the web card show. Slice 2 starts by making large syncs fast (one `setValuesAtTimes` per property), found in live check 2.

**Tech stack:** as slice 1 — Python 3.12 stdlib server, existing `compose` + `render` (Playwright Chromium) and OpenCV; CEP panel in vanilla JS with Node built-ins; ExtendScript ES3; the Node fake AE in `tests/ae_fake/`.

**Spec (binding):** `docs/superpowers/specs/2026-10-06-keepframe-ae-extension-design.md` — §`verify.py`, §`api.py` (`POST /api/ae/verify`, upload allowlist), §Extension (`kfRender`), §Testing, §Slices item 2. Slice-1 rulings live in `.superpowers/sdd/2026-10-06-keepframe-ae-extension/progress.md` (lines starting `Ruling:`); they still apply.

## Context

Slice 1 is merged (master `58bf842`); live checks 1 and 2 passed all 17 items. Already in place and reused here: the `render_frames` job kind with its `sync` prerequisite and hand-edit failure (`keepframe/ae/jobs.py`); `PUT /api/ae/jobs/<id>/files/frame_<NNNN>.png` with the 25 MB / 16-file caps and partial-file cleanup (`keepframe/ae/api.py:_put`); `compose()` + `render()` and the L1 metric used by `keepframe/gates.py:render_fidelity`. Missing: `kfRender`, the panel's `render_frames` runner (it fails such jobs as unsupported, `extension/js/core.js:362`), file uploads from the panel, `verify.py`, the verify route/report, and the web UI.

Live check 2 finding 9: syncing ~120k keys took more than 10 minutes because every key costs three ExtendScript calls (`setValueAtTime`, `setTemporalEaseAtKey`, `setInterpolationTypeAtKey`, `extension/host/keepframe.jsx:360-376`), and the panel's 10-minute limit then blamed "a dialog". Task 1 fixes both.

## Decisions for this slice (rulings; the controller copies them into the new ledger)

1. **Frames come from `CompItem.saveFrameToPng(time, file)`, not the render queue.** Same outcome as the spec's "PNG sequence" (PNG files of chosen frames) without adding render-queue items to the user's project or choosing an output module by localized name; the Higgsfield bridge already exports frames this way in the user's Korean AE 26.5. The file may appear asynchronously, so `kfRender` waits for each file (Task 4). Frames are written at the comp's own resolution; `verify.py` resizes to the scene size anyway (the spec's "half resolution" was a speed choice that 16 frames do not need). Cost if wrong: switch `kfRender` to the render queue; nothing else changes.
2. **Verification runs on the server after the upload**, in one background thread at a time; the `render_frames` job ends when the frames are uploaded, so the device is free while Keepframe renders.
3. **`render_frames` keeps the spec's prerequisite rule** (sync first only when the device's last applied sync of that scene is another version). A comp hand-edited after its last sync is verified as it is; the report then shows the user's differences, which is a true answer.
4. **Substituted-font text is masked, not dropped:** pixels inside that text element's Keepframe bounding box (+4 px) are excluded from the pass/fail metric for that frame (spec: "their difference does not fail the check"), but the report still measures them: each note carries that region's worst L1 across the sampled frames, so missing or wrong-colour text shows as a large number instead of passing silently.
5. **Upload names allow 4–6 digits** (`frame_[0-9]{4,6}\.png`), so scenes longer than 9,999 frames can be verified.

## Global Constraints

- No new runtime dependencies (Python `dependencies`, npm). OpenCV, numpy, Pillow and Playwright are already installed.
- The extension only runs fixed entry points (`kfInfo`, `kfSync`, `kfRender`) with JSON data; nothing received from the server is evaluated as code. Host-call literals use the existing `literal()` escaping in `core.js`.
- Browser routes keep the existing checks (GET: Host allowlist `_host_allowed`; POST/DELETE: `_same_origin`); extension routes take only `Authorization: Bearer <device token>`.
- Never log or display tokens, pairing codes, or local file paths outside the panel's own log (server errors name files by basename only — slice-1 rule).
- No silent failure: every error ends as job `error` text (web card + panel) or a visible verify-report error.
- ExtendScript stays ES3 (the regex check near `tests/test_ae_host_sync.py:629`: no `const`/`let`/`=>`/backticks) and runs without built-in `JSON`.
- UI copy ko + en in `keepframe/web/static/js/i18n.js` (`ae.*` keys); bump the one cache-buster stamp on every page when static files change.
- Don't commit `eval/`, `.superpowers/`, `.impeccable/`, `graphify-out/`, `uv.lock`, `:memory:.ses`, `keepframe/ae/static/keepframe.zxp`, or any certificate.
- Tests: `.venv/bin/python -m pytest -p no:cacheprovider -q -m 'not browser and not gpu and not ocr'`; browser tests `-m browser <files>`; Node: `node --test 'tests/ae_fake/*.test.js' 'extension/test/*.test.js'` (Node 25 needs globs, not directories).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. After merge: `graphify update .` in the main checkout.
- User-facing replies in Korean; code, commits, specs and prompts in English.

## Review Focus

1. AE writes a frame PNG late (async `saveFrameToPng`) or never → `kfRender` waits for a complete file up to 60 s per frame, never returns a half-written file, and fails with "AE did not write frame N" → Task 4 `test_render_waits_for_late_frame_and_fails_on_missing`.
2. The user edits the scene (new version) while a verify is running → the report compares against the job's version, not the newest, and says which version it checked → Task 3 `test_verify_uses_the_job_version`.
3. The server restarts while Keepframe is rendering its frames → the web card shows "verification was interrupted — verify again", never a report stuck on "running" → Task 3 `test_interrupted_verification_is_reported`.
4. A frame upload breaks halfway or a frame is missing from the result → the job fails naming the missing frames, and no verification starts → Task 3 `test_result_with_missing_frames_fails_without_verifying`.
5. A scene of 10,000+ frames or an AE frame at a different size (comp resolution half/quarter) → frames still upload and compare after resizing → Task 2 `test_ae_frames_are_resized_to_scene_size`, Task 3 `test_upload_names_allow_six_digits`.

---

## Task 0: Workspace (controller)

- [ ] `git worktree add .worktrees/ae-verify -b feature/ae-verify` from master `58bf842`; `python3.12 -m venv .venv && .venv/bin/pip install -e '.[ocr,llm,dev]' && .venv/bin/python -m playwright install chromium`.
- [ ] Carry the uncommitted live-check-2 results: copy the main checkout's `docs/qa/ae-extension/README.md` into the worktree, commit `docs(qa): live check 2, items 12–17 pass`, then `git checkout -- docs/qa/ae-extension/README.md` in the main checkout.
- [ ] Commit this plan (`docs: add AE verify plan`). New SDD workspace (`bash scripts/sdd-workspace <plan>`), ledger first line, the five decisions above as `Ruling:` lines, `run-codex.sh` copied from the slice-1 workspace. Record the baseline suite count.

## Task 1: Fast key writes and an honest timeout message

**Files:** Modify `extension/host/keepframe.jsx` (`writeKeys`, ~360–376), `extension/js/core.js` (`hostCall` timeout text, ~175–190), `tests/ae_fake/ae.js` (`setValuesAtTimes`, call counts), `tests/ae_fake/run.js` (print `calls`), `tests/ae_fake/README.md` · Test `tests/test_ae_host_sync.py`, `tests/ae_fake/fake.test.js`, `extension/test/core.test.js`.

**Interfaces:**
- Consumes: the key shape `[frame, value, out_ease, in_ease]` (eases `null` = linear) and `eases()` / `paddedValue()` in `keepframe.jsx`.
- Produces: `run_jsx(...)` payloads gain `"calls": {"<method>": count}` (from the fake's existing `calls` map) for later perf assertions.

- [ ] **Step 1: Fake support.** `Property.setValuesAtTimes(times, values)` in the fake: same validation as `setValueAtTime` for each pair (array lengths must match, else throw like AE: `"fake AE: setValuesAtTimes needs equal arrays"`), new keys get `inInterpolation = outInterpolation = LINEAR` and AE's default temporal ease (`influence 16.666667, speed 0` per dimension), counts as one write. `run.js` adds `calls` to its JSON line. Fake test: 3 keys via `setValuesAtTimes` → `numKeys === 3`, `keyInInterpolationType(2) === LINEAR`.
- [ ] **Step 2: Failing host test.**

```python
def test_linear_keys_are_written_in_one_call_per_property(tmp_path):
    keys = [[f, 320.0 + f, None, None] for f in range(600)]
    spec, assets = solid_spec_with_x_keys(tmp_path, keys)   # helper: one solid layer, position_x = keys
    result = sync(tmp_path / "ae.json", spec, assets)
    assert result["value"]["applied"]
    calls = result["calls"]
    assert calls.get("setValuesAtTimes", 0) >= 1
    assert calls.get("setValueAtTime", 0) == 0
    assert calls.get("setTemporalEaseAtKey", 0) == 0          # linear keys keep AE's defaults
    assert calls.get("setInterpolationTypeAtKey", 0) == 0

def test_eased_keys_still_get_ease_and_bezier(tmp_path):
    ease = [0.25, 0.1, 0.25, 1.0]
    keys = [[0, 0.0, ease, None], [12, 100.0, None, ease], [24, 50.0, None, None]]
    ...  # assert key 1 out = BEZIER, key 2 in = BEZIER, key 3 LINEAR/LINEAR; ease values as before
```

Also keep `test_sync_twice_is_a_no_op` and the fingerprint tests green (fingerprints are read from AE after writing, so defaults on linear keys are stable).
- [ ] **Step 3: Implement** in `writeKeys`:

```js
function writeKeys(p, keys, fps, factor, modelScale) {
    var i, k, value, times = [], values = [], dimensions = propertyDimensions(p, true);
    clear(p);
    for (i = 0; i < keys.length; i += 1) {
        k = keys[i];
        value = k[1];
        if (modelScale) { value = [value[0] * factor, value[1] * factor]; }
        times.push(k[0] / fps); values.push(paddedValue(p, value, modelScale));
    }
    if (keys.length === 1) { p.setValue(values[0]); return; }
    // One call for all keys; per-key calls only where an ease is set (linear keys keep AE's defaults).
    p.setValuesAtTimes(times, values);
    for (i = 0; i < keys.length; i += 1) {
        k = keys[i];
        if (k[2] || k[3]) {
            p.setTemporalEaseAtKey(i + 1, eases(k[3], dimensions, factor), eases(k[2], dimensions, factor));
            p.setInterpolationTypeAtKey(i + 1, k[3] ? KeyframeInterpolationType.BEZIER : KeyframeInterpolationType.LINEAR,
                k[2] ? KeyframeInterpolationType.BEZIER : KeyframeInterpolationType.LINEAR);
        }
    }
}
```

Guard (Step 3b): AE's default temporal interpolation for script-made keys is expected to be LINEAR; if `p.keyInInterpolationType(1)` is not LINEAR after `setValuesAtTimes`, set LINEAR on every unset key (one extra read per property). Fake test with a fake whose default is BEZIER (`createAE({defaultInterpolation: "BEZIER"})`) proves the guard.
- [ ] **Step 4: Timeout text.** `core.js` host timeout message becomes `After Effects did not finish within <n> min. It may still be working on a large scene or waiting for a dialog — wait until AE responds, then send again.` (EN) and the panel's ko translation `After Effects가 <n>분 안에 끝내지 못했습니다. 큰 장면을 아직 처리 중이거나 대화상자를 기다리는 중일 수 있습니다. AE가 응답하면 다시 보내세요.`; update the matching `core.test.js` / `panel.js` translate tests.
- [ ] **Step 5:** Run host-sync, fake and core tests, then the full suite. Commit `perf(ae): write keys per property in one call; timeout says AE may be busy`.

## Task 1b: Agent page — the preview keeps its space (user request, 2026-10-07)

Found while reviewing the agent page: at 1440×900 the right column (`.agent-viewer`, 852 px) holds the render-plan card (293 px), the After Effects card (338 px), the transport (56 px) and the element list (168 px), all `flex: 0 0 auto`, so `.compare-panes` (`flex: 1 1 0%; min-height: 0`) shrinks to its 32 px padding and both preview images (`#agent-orig`, `#agent-recon`, 960×540 frames that load fine) are 0 px tall; at 1920×1080 they are 130×73. The transport sits under the two cards, far from the preview. User decision: both fixes.

**Files:** Modify `keepframe/web/static/agent.html` (order), `keepframe/web/static/css/app.css` (`.agent-viewer`, `.compare-panes`, card collapse rules), `keepframe/web/static/js/agent.js` and/or `keepframe/web/static/js/ae.js` (collapse toggles), `keepframe/web/static/js/i18n.js` (toggle labels if any), every page's cache stamp · Test `tests/test_web_static.py`, a browser test (new `tests/test_agent_layout_browser.py`, marked `browser`).

- [ ] **Step 1: Failing browser test** with a seeded project (reuse the helpers of `tests/test_ae_card_browser.py`): at 1440×900 and 1920×1080, `#agent-orig` and `#agent-recon` are each at least 40 % of the viewer's height tall (or limited only by their 16:9 width), the transport (`.agent-transport`) is directly below `.compare-panes` (its top within 24 px of the panes' bottom), and the render-plan card, the After Effects card and the element list are reachable by scrolling `.agent-viewer` (their `scrollIntoView` brings them into view; nothing is clipped). At 390×844 (mobile) the existing column layout still shows the preview at its 16:9 size.
- [ ] **Step 2: Failing collapse test:** the render-plan card and the After Effects card each have a header toggle (a `<button aria-expanded>` in the card header, text label, keyboard operable); collapsing hides the body and keeps the header and the AE status chip visible; the state survives a reload (`localStorage` keys `kf.agent.renderCardOpen`, `kf.agent.aeCardOpen`, read/written inside try/catch; default open). A collapsed AE card still shows the hand-edit prompt line when one is pending (so an Overwrite request is never hidden).
- [ ] **Step 3: Implement.** Viewer becomes a scroll container (`overflow-y: auto`); `.compare-panes` gets `flex: 0 0 auto; min-height: max(45vh, 240px)` inside the viewer (keep the mobile rule); move `.agent-transport` right after `.compare-panes` in `agent.html`. No change to the transport or preview JS beyond what the move needs.
- [ ] **Step 4:** Static + browser tests, then the full suite. Commit `fix(web): agent preview keeps its space; render and AE cards fold`.

## Task 2: `keepframe/ae/verify.py`

**Files:** Create `keepframe/ae/verify.py` · Test `tests/test_ae_verify.py` (pure tests unmarked; one integration test marked `browser`).

**Interfaces:**
- Consumes: `keepframe.compose.composer.compose(scene, scene_dir, out_html) -> Path`; `keepframe.render.renderer.render(html, scene, out_dir, frames=list[int], probe=True) -> RenderResult` (frames at `frames_dir / f"f_{i:05d}.png"`, `bboxes[eid][i] = [left, top, right, bottom]` in scene pixels, from `window.__bbox` in `keepframe/compose/template.html:104`).
- Produces (used by Task 3):
  - `VERIFY_MEAN_MAX = 0.02`, `VERIFY_FRAME_MAX = 0.05`, `MASK_PAD = 4`
  - `sample_frames(frames: int, n: int = 16) -> list[int]`
  - `compare_frames(ae: np.ndarray, kf: np.ndarray, masks: list[tuple[int,int,int,int]] = ()) -> tuple[float, np.ndarray]` — inputs RGB uint8 HxWx3 of equal size; returns (L1 in 0..1 over unmasked pixels, diff image uint8 = `clip(|a−b|·4)`).
  - `verify(scene: Scene, scene_dir: Path, ae_frames: dict[int, Path], out_dir: Path, *, masked: dict[str, str] = {}) -> dict` — `masked` maps element id → note text; the report's note for each masked element is `f"{text} — text region differs by {pct:.1f}%"` where `pct` is the worst per-frame L1 inside that element's padded box (frames where the box is empty or off-frame are skipped); writes `out_dir/f<NNNN>_{ae,kf,diff}.png` for the three worst frames and returns the report:

```python
{"passed": bool, "mean": float, "max": float,
 "thresholds": {"mean": VERIFY_MEAN_MAX, "frame": VERIFY_FRAME_MAX},
 "frames": [{"frame": int, "l1": float}, ...],          # sorted by frame
 "worst": [{"frame": int, "l1": float, "ae": "f0042_ae.png", "kf": "f0042_kf.png", "diff": "f0042_diff.png"}],  # ≤3, worst first
 "notes": ["<note>", ...],
 "masked": [{"id": "e1", "worst_l1": float}]}
```

- [ ] **Step 1: Failing tests** (no browser):

```python
def test_sample_frames_spreads_evenly_with_both_ends():
    assert sample_frames(150) == sorted(set(sample_frames(150)))
    assert sample_frames(150)[0] == 0 and sample_frames(150)[-1] == 149 and len(sample_frames(150)) == 16
    assert sample_frames(5) == [0, 1, 2, 3, 4]
    assert sample_frames(1) == [0]

def test_compare_frames_l1_and_mask():
    a = np.zeros((10, 10, 3), np.uint8); b = a.copy(); b[:5] = 255
    l1, diff = compare_frames(a, b)
    assert l1 == pytest.approx(0.5) and diff.shape == a.shape
    assert compare_frames(a, b, [(0, 0, 10, 5)])[0] == 0.0     # masked rows ignored

def test_ae_frames_are_resized_to_scene_size(tmp_path, monkeypatch):
    # ae frame 2x the scene size, same content → l1 ≈ 0 (monkeypatch render to write known PNGs)
    ...

def test_pass_rule_and_worst_three(tmp_path, monkeypatch):
    # five frames with l1 0.0, 0.01, 0.06, 0.02, 0.03 → passed False (max 0.06 > 0.05),
    # worst == frames [2, 4, 3] in that order, files written for each
    ...

def test_substituted_text_is_masked_and_noted(tmp_path, monkeypatch):
    # difference only inside element "t1"'s bbox; masked={"t1": "e1 · title: Arial Bold instead of Pretendard"}
    # → passed True; report["masked"] == [{"id": "t1", "worst_l1": <region L1>}] and the note ends with "text region differs by N.N%"
    # also: the text missing from the AE frame (region blank) still passes but shows a large worst_l1
    ...
```

The non-browser tests monkeypatch `keepframe.ae.verify.render` and `compose` to write synthetic PNGs and bboxes, so they need no Chromium.
- [ ] **Step 2: Implement.** `sample_frames`: `frames <= n → list(range(frames))`, else `sorted({round(i * (frames - 1) / (n - 1)) for i in range(n)})`. `verify`: compose into a `tempfile.TemporaryDirectory`, `render(..., frames=sorted(ae_frames), probe=True)`, load both with `cv2.imread(..., IMREAD_COLOR)` (BGR is fine as long as both are BGR; save diff/kf/ae images from the same arrays), resize AE to `scene.size` with `cv2.INTER_AREA` when larger and `INTER_LINEAR` when smaller, masks from `bboxes[eid][i]` padded by `MASK_PAD` and clipped to the frame. A missing or unreadable AE frame raises `ValueError(f"frame {f} from AE is missing or not an image")`.
- [ ] **Step 3: Browser integration test** (`@pytest.mark.browser`): a 64×36, 10-frame scene with a color background and one sprite; AE frames = Keepframe's own render of those frames → `passed` and `mean == 0`; then blank the sprite region in one AE frame → that frame is `worst[0]` and `passed` is False.
- [ ] **Step 4:** Run `tests/test_ae_verify.py` (unmarked and `-m browser`), then the full suite. Commit `feat(ae): compare AE frames with Keepframe's render`.

## Task 3: Verify route, report storage and state

**Files:** Modify `keepframe/ae/api.py` (`POST /api/ae/verify`, render-frames result handling, background verification, `GET /api/ae/verify-image`, `state["verify"]`, upload regex), `keepframe/ae/jobs.py` only if a helper is needed · Test `tests/test_ae_api.py`.

**Interfaces:**
- Consumes: Task 2 (`sample_frames`, `verify`), `self.jobs.enqueue(device, "render_frames", project, scene, version, params)` (adds the `sync` prerequisite itself), `self._scene(project, scene, version)`, `self._uploaded[job.id]`, `comp_spec(scene, scene_dir, project=, scene_id=, version=, fonts=<device fonts>)` (`keepframe/ae/spec.py:342`) for substitution notes — a text layer's `source.font` is `{postscript, family, style, substituted}`.
- Produces (used by Tasks 4–6):
  - `POST /api/ae/verify {project, scene, version?, device?}` → `202 {"job": <render_frames job>}`; `409 "no connected After Effects"` like `/send`. Job `params = {"frames": sample_frames(scene.frames), "tag": "keepframe:<project>/<scene>"}`.
  - Extension result for `render_frames`: `{"ok": true, "result": {"frames": [int, ...]}}`. The server requires `set(result.frames) == set(params.frames)` and every `frame_<NNNN>.png` (`f"frame_{f:04d}.png"`) uploaded for this job; otherwise the job fails with `frames missing from AE: 12, 40`.
  - Report directory `<workspace>/<project>/ae/<scene>/<version>/verify/`; report file `verify.json` = Task 2's report plus `{"job": id, "version": v, "finished": ts}`.
  - `GET /api/ae/state` adds `"verify"`: `null`, or for the newest `render_frames` job of that scene `{"job", "version", "state": "rendering"|"verifying"|"done"|"failed"|"interrupted", "error"?, ...report fields when done}` — `rendering` while the job is queued/running, `verifying` while the server thread runs, `failed` with the job's or verifier's error, `interrupted` when the job is done but there is neither a report nor a running thread (server restarted).
  - `GET /api/ae/verify-image?project=&scene=&version=&name=` (browser GET, Host check) → PNG; `name` must match `f[0-9]{4,6}_(ae|kf|diff)\.png`; 404 otherwise.

- [ ] **Step 1: Failing tests** in `tests/test_ae_api.py` (reuse its server/seed/pair helpers; monkeypatch `keepframe.ae.api.verify` with a fake that writes a report so these tests need no Chromium):
  - `test_verify_enqueues_render_frames_with_sampled_frames` (and a `sync` prerequisite when AE has another version; none when it has this one).
  - `test_render_frames_result_starts_verification_and_state_reports_it` (upload all frames, post result → state goes `verifying` → `done` with the fake report; the image route serves `f0000_ae.png`).
  - `test_result_with_missing_frames_fails_without_verifying` (one frame not uploaded → job `failed`, error names it, verify never called).
  - `test_verify_uses_the_job_version` (new scene version created after enqueue → report `version` is the job's).
  - `test_interrupted_verification_is_reported` (report missing, no thread → `interrupted`).
  - `test_verifier_error_is_shown` (fake verify raises `ValueError("frame 3 from AE is missing or not an image")` → `state: failed`, that text).
  - `test_verify_image_rejects_other_names` (`../verify.json`, `f1_ae.png`, other project) → 404.
  - `test_upload_names_allow_six_digits` (`frame_012345.png` accepted for render_frames; `frame_1234567.png` refused).
- [ ] **Step 2: Implement.** Verification thread: one `threading.Lock` so only one verification renders at a time; `self._verifying: dict[job_id, version]` under a lock for state; daemon thread joins nothing on `close()` but `close()` sets a flag so a finished thread does not write after shutdown. The verifier receives `masked` from the job's comp spec: text layers whose `source.font.substituted` is true → `{element_id: f"{layer name}: {source.font.family} instead of {element.canonical.font.family_guess}"}` (element id = layer id without `kf:`; the hidden `~text` companions are skipped). Errors: `ValueError`/`FileNotFoundError` → report `{"error": str(exc)}` with `state: failed`; anything else logs `log.exception` and stores `"verification failed: <ExceptionType>"`. Log every verification start/finish at INFO (project, scene, version, mean, passed).
- [ ] **Step 3:** Run `tests/test_ae_api.py`, `tests/test_ae_jobs.py`, then the full suite. Commit `feat(ae): verify route, report and state`.

## Task 4: The panel renders and uploads frames

**Files:** Modify `extension/host/keepframe.jsx` (`kfRender`), `extension/js/core.js` (`render_frames` runner, streamed upload in `request()`, status texts), `extension/js/panel.js` (ko translations for the new statuses), `tests/ae_fake/ae.js` (`CompItem.saveFrameToPng`, `Folder.temp`, `Folder.create`, `File.remove`, `File.length`, `$.sleep`), `tests/ae_fake/README.md` · Test `tests/test_ae_host_sync.py`, `extension/test/core.test.js`, `tests/ae_fake/fake.test.js`.

**Interfaces:**
- Consumes: job `params = {frames, tag}` (Task 3), `findComp(tag)` in `keepframe.jsx`, `PUT /api/ae/jobs/<id>/files/frame_<NNNN>.png`, `POST .../progress`, `POST .../result`.
- Produces: `kfRender(requestJson)` with `requestJson = {"job": "j_<16 hex>", "tag": "keepframe:<project>/<scene>", "frames": [int]}` → `{"ok": true, "frames": [{"frame": 0, "path": "<abs path>"}], "width": W, "height": H}` or the usual `{"ok": false, "error", "line"}`.

- [ ] **Step 1: Fake.** `CompItem.saveFrameToPng(time, file)` writes a valid PNG of `width × height` filled with the RGB color of the bottom-most enabled layer whose source is a solid (black when none) — enough for an all-background scene to match Keepframe's render. Options for tests: `createAE({frameWriteDelayMs: n})` writes the file `n` ms later (async like AE), `createAE({frameWriteFails: true})` never writes. `$.sleep(ms)` blocks (Atomics.wait on a SharedArrayBuffer). `Folder.temp` points inside the run's documents dir. The PNG writer is a small function with `zlib.deflateSync` + CRC32 (no deps). Fake tests for each.
- [ ] **Step 2: Failing host tests.**

```python
def test_render_writes_requested_frames(tmp_path, full_spec):          # after a sync
    out = run_jsx(path, HOST, "kfRender", json.dumps({"job": "j_" + "0" * 16, "tag": spec["comp"]["tag"], "frames": [0, 5, 29]}))
    assert [f["frame"] for f in out["value"]["frames"]] == [0, 5, 29]
    assert all(Path(f["path"]).name == f"frame_{f['frame']:04d}.png" and Path(f["path"]).stat().st_size > 0 for f in out["value"]["frames"])

def test_render_waits_for_late_frame_and_fails_on_missing(tmp_path, full_spec):
    # frameWriteDelayMs=300 → ok; frameWriteFails → ok False, error "AE did not write frame 0 within 60 s"
    # (make the wait limit injectable for the test, e.g. request "wait_ms": 500, capped at 60000)

def test_render_without_the_comp_says_send_first(tmp_path):
    # empty project → ok False, "the Keepframe comp is missing; send the scene to AE first"
```

- [ ] **Step 3: Implement `kfRender`** (ES3): validate `job` (`^j_[0-9a-f]{16}$`), `frames` (ints, `0 ≤ f < comp frames`, ≤ 16, unique), find the comp by tag, `folder = new Folder(Folder.temp.fsName + "/keepframe-" + job)` (create; remove stale files inside), for each frame `file = new File(folder.fsName + "/frame_" + pad(f, 4) + ".png")`, `comp.saveFrameToPng(f / comp.frameRate, file)`, then wait until `file.exists` and `file.length > 0` and the length is unchanged across two checks 100 ms apart, up to `wait_ms` (default 60000) → else throw `"AE did not write frame " + f + " within 60 s"`. No undo group (nothing in the project changes).
- [ ] **Step 4: Failing core tests** (`extension/test/core.test.js`, with the existing fake deps): a `render_frames` job → `kfRender` called with `{job, tag, frames}` from params; each returned file uploaded with `PUT` and the right `Content-Length`, streamed from disk (not read whole into memory); progress `{stage: "uploading", done: i, total: n}`; result `{frames}`; temp folder removed afterwards (also on failure); a path outside the job's temp folder or a frame not requested → job fails `Invalid frame from AE` without uploading anything; a 413/409 on upload → job fails with the server's text.
- [ ] **Step 5: Implement in `core.js`.** Extend `request()` with an `upload` option `{path, size}` that pipes `deps.fs.createReadStream(path)` into the request with `Content-Type: image/png` and `Content-Length: size` (keep redaction and the 32 MiB response cap). Runner: replace the `job.kind !== 'sync'` guard with a dispatch `{sync, render_frames}`; statuses `Rendering <n> frames of <project> / <scene> <version>…`, `Uploading frame <i>/<n>…`, final `Rendered <n> frames — Keepframe is comparing them` (panel ko: `<project> / <scene> <version> 프레임 <n>개 렌더링 중…`, `프레임 업로드 중 <i>/<n>…`, `프레임 <n>개 렌더링 완료 — Keepframe에서 비교 중`).
- [ ] **Step 6:** Run host, fake and core tests, then the full suite. Commit `feat(ae): render sampled frames in AE and upload them`.

## Task 5: Web card — Verify button and report

**Files:** Modify `keepframe/web/static/js/ae.js`, `keepframe/web/static/js/i18n.js`, `keepframe/web/static/css/app.css` (only `ae-card__verify*` rules), every page's cache stamp · Test `tests/test_web_static.py`, `tests/test_ae_card_browser.py`.

**Interfaces:**
- Consumes: `POST /api/ae/verify`, `state.verify` (Task 3 shape), `GET /api/ae/verify-image`.

- [ ] **Step 1: Failing tests.** Static: `ae.js` calls `/api/ae/verify` and `/api/ae/verify-image` and still nothing else new under `/api/ae/`; every `ae.verify*` key exists in ko and en; one stamp on all pages. Browser (`tests/test_ae_card_browser.py`, real kfSync/verify shapes, verify monkeypatched to write a report): **Verify against AE** (ko `AE와 비교`) is disabled without a connected device (with the reason text, like Send); pressing it posts `{project, scene, version}`; states render as text — `rendering` "AE is rendering frames…", `verifying` "Keepframe is comparing frames…", `done` pass "Matches Keepframe (v4): mean 0.3%, worst frame 42 at 1.1%" / fail "Differs from Keepframe (v4): …, 3 frames over 5%", `failed` shows the error, `interrupted` "Verification was interrupted — verify again"; a details toggle shows the worst three rows as three images (AE, Keepframe, difference) with `loading="lazy"` and alt text; notes listed.
- [ ] **Step 2: Implement** (percent with one decimal; the threshold numbers come from `report.thresholds`, not hard-coded). Polling stays as in slice 1 (2 s while any job is queued/running **or** `verify.state` is `rendering`/`verifying`).
- [ ] **Step 3:** Run static + browser card tests, full suite. Commit `feat(web): verify against AE from the After Effects card`.

## Task 6: End to end — pair, sync, render, verify

**Files:** Modify `tests/ae_fake/extension_runner.js` (render jobs use the fake's `saveFrameToPng`), `tests/test_ae_e2e.py`.

- [ ] **Step 1:** Scenario (marked `browser`, since Keepframe's renderer needs Chromium): background-only scene → `POST /api/ae/verify` → `run_ext --jobs 2` (sync + render_frames) → wait for `state.verify.state == "done"` → `passed` True, `mean == 0` (fake frames are the solid colour).
- [ ] **Step 2:** Scenario: scene with one sprite → same flow → `passed` False, the worst frames contain the sprite's visible frames, three image files exist and `/api/ae/verify-image` serves them.
- [ ] **Step 3:** Scenario (unmarked): render job when the device has a hand-edited comp → prerequisite sync refuses → `render_frames` failed with the hand-edit text; `state.verify.state == "failed"`.
- [ ] **Step 4:** Run `tests/test_ae_e2e.py` (both marks), full suite. Commit `test(ae): pair, sync, render and verify end to end`.

## Task 7: Build, docs, live check 3, calibration (controller with the user)

**Files:** `docs/qa/ae-extension/live-check.md` (new "Slice 2" section), `docs/qa/ae-extension/README.md` (results), README "After Effects" section (verify is available; precompose note), `keepframe/ae/verify.py` constants.

- [ ] Codex writes the docs part: live-check steps for slice 2 — install the new zxp; **Verify against AE** on the three live-check scenes and ig2demo; record mean/max per scene; the report images open; re-run the 120k-key `ae-live-long` sync and time it (target < 2 min); confirm key interpolation with the Higgsfield bridge (`ae_get_keyframes` on an eased and a linear key); a Korean-AE run where AE shows a dialog during `kfRender`.
- [ ] Controller: `scripts/build_zxp.sh`, restart the live server from the worktree, run the checks with the user (AskUserQuestion for any decision; mutating AE actions through the Higgsfield bridge are allowed). **Calibration:** set `VERIFY_MEAN_MAX` / `VERIFY_FRAME_MAX` to 2× the worst observed value on scenes that look right (rounded up to 0.005), never below the observed noise; record the numbers and the rule in the README; update tests that pin the constants.
- [ ] Findings → fake-AE behaviour + tests → fixes (one Codex dispatch per finding batch).
- [ ] Final whole-branch review (opus) on `master..HEAD`, one fix wave, scoped re-review; then finishing-a-development-branch with the user's choice.

---

## Self-review notes

- Spec coverage: §verify.py (sample_frames, verify, thresholds, font notes, three worst frames) → Tasks 2–3, 5; §api `POST /api/ae/verify` + uploads → Task 3; §Extension `kfRender` frames → Task 4 (decision 1 records the mechanism change); §Testing e2e "pair → sync → render_frames → verify" → Task 6; §Slices 2 "thresholds calibrated on the live-check clips; web report" → Tasks 5, 7. Not in this slice: `render_final`, `package`, Live, agent tools (slices 3–4).
- Live check 2 finding 9 (slow large syncs; misleading timeout text) → Task 1. Finding 10 (precompose recreates) stays in slice 4; Task 7 adds the README note.
