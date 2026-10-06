# Keepframe After Effects Extension — Design

Date: 2026-10-06 · Status: approved in conversation, awaiting written-spec review

## Why

The round-2 After Effects path (Python connector on Windows → MCP stdio helper → files in
`%LOCALAPPDATA%\Keepframe\ae-bridge` → ScriptUI panel, plus a separate relay port) never completed a
live run. In one evening it failed on: the user site hidden from the MCP child, ExceptionGroup-wrapped
errors, a stale-heartbeat deadlock, an icacls step that left files with an empty DACL, an ES3 engine
without `JSON`, session cookies that locked projects, `SimpleCookie` dropping cookies, and a ctypes struct
that corrupted the heap. Each layer was tested only against fakes of the layer below. The rebuild removes
the layers instead of patching them.

## Decisions (user, 2026-10-05/06)

| Topic | Decision |
|---|---|
| Scope | Rebuild everything AE, server side included |
| v1 outcomes | Build the comp in AE · verify against Keepframe · final MP4 + project zip · live agent control |
| Ownership | Keepframe owns the comp: the scene is the master; a push overwrites Keepframe-made layers, after warning about hand edits |
| Install | Signed `.zxp` downloaded from the Keepframe web page |
| AE versions | 24.1 and newer (3D model layers, H.264 from the render queue) |
| Architecture | A — declarative sync over HTTP long-poll on the existing server |
| Delivery | Four slices, in order: (1) foundation + comp build, (2) verify, (3) final render + package, (4) live agent control |

## Goals and non-goals

Goals
- One install in AE, no Python or command window on the AE machine.
- Pair once per server with a short code; the panel and the web page both show the connection state.
- A scene version becomes an editable AE comp; re-sending updates only what changed.
- Every failure has readable text in the panel and on the web page. No silent exits.
- The AE-side code runs in CI against a fake AE object model; each slice ends with a short checklist in the user's AE.

Non-goals (v1)
- Reading hand edits in AE back into the scene (two-way sync).
- AE older than 24.1, macOS-specific testing (the extension is CEP and should load there, but only Windows is checked).
- Adobe Exchange distribution.
- Parenting from scene groups (groups carry no transform).

## Architecture

```
Keepframe server (existing process, existing port)        After Effects 24.1+ (Windows)
 keepframe/ae/                                             CEP extension "Keepframe"
  spec.py     comp_spec(scene) -> CompSpec                  index.html + panel.js (Chromium + Node.js)
  devices.py  pairing codes, device tokens                    pair, long-poll, download, upload, zip
  jobs.py     per-device job queue                          host/keepframe.jsx (ExtendScript)
  api.py      /api/ae/* routes, /ae/keepframe.zxp             kfInfo, kfSync, kfRender, kfPackage
  verify.py   AE frames vs Keepframe frames
        ^                                                          |
        |   HTTPS (or http over loopback / Tailscale)              |
        +---------- GET /api/ae/next (long-poll), results, files --+
```

The server sends data only (job parameters, `CompSpec`, assets). The extension maps each job kind to one
fixed ExtendScript entry point; it never evaluates code it receives.

## Server components (`keepframe/ae/`)

### `spec.py` — `comp_spec(scene, *, fonts=None) -> dict`

Pure function; no I/O. `fonts` is the connected device's font list (`[{family, style, postscript}]`).

```json
{
  "schema": "keepframe.ae-comp/1",
  "project": "ig2demo", "scene": "s1", "version": "v7",
  "comp": {"tag": "keepframe:ig2demo/s1", "name": "Keepframe · ig2demo · s1",
           "width": 1080, "height": 1920, "fps": 30, "frames": 150},
  "assets": [{"name": "e3.png", "sha256": "…", "bytes": 48213}],
  "layers": [
    {"id": "kf:e3", "kind": "image", "name": "e3 · logo", "label": 4,
     "in": 0, "out": 149, "order": 3,
     "source": {"asset": "e3.png", "scale_fix": [1.0, 1.0]},
     "anchor": [120.0, 40.0],
     "props": {"position_x": [[0, 540.0, null], [12, 560.0, [0.25, 0.1, 0.25, 1.0]]],
               "position_y": [[0, 960.0, null]],
               "scale": [[0, [100.0, 100.0], null]],
               "rotation": [[0, 0.0, null]],
               "opacity": [[0, 100.0, null]]},
     "effects": {"skew": null, "reveal": null},
     "warnings": []}
  ],
  "warnings": ["z-order of e7 changes over time; ordered by its first visible frame"]
}
```

Layer kinds and sources

| Scene element | AE layer `kind` | `source` |
|---|---|---|
| `text` | `text` | `{text, font: {postscript, family, style, substituted}, size_px, color}` |
| `sprite`, `ui` | `image` | `{asset, scale_fix}` |
| `3d` with `canonical.model` | `model` | `{asset}` (GLB) |
| `3d` without a model | `image` | `{asset}` + layer warning `3D candidate` |
| `group` with a texture | `image` | `{asset}` |
| `group` without a texture | `null` | — |
| background `color` | `solid` (bottom, full comp, `id: "kf:background"`) | `{color}` |
| background `image` | `image` (bottom, `id: "kf:background"`) | `{asset}` |

Property mapping (keys are `[frame, value, ease]`; `ease` is the scene's cubic-bezier `(x1, y1, x2, y2)` or
`null` for linear):

| Scene track | `props` / `effects` key | Value |
|---|---|---|
| `x`, `y` | `position_x`, `position_y` | comp pixels (separated dimensions) |
| `sx`, `sy` | `scale` | `[sx·100·scale_fix.x, sy·100·scale_fix.y]` |
| `rot` | `rotation` | degrees |
| `opacity` | `opacity` | `opacity·100` |
| `skx`, `sky` | `effects.skew` | Transform effect (`ADBE Geometry2`) Skew / Skew Axis; `null` when all zero |
| `reveal` | `effects.reveal` | Linear Wipe completion `(1−reveal)·100`, angle 270, feather 0; `null` when always 1 |
| `rx`, `ry` | `rotation_x`, `rotation_y` | degrees, `model` layers only |
| `visible` | `in`, `out` | frames |
| `z` | `order` | rank by `z` at the layer's first visible frame; a warning when `z` changes |

- `anchor` = `canonical.anchor × (canonical.width, canonical.height)` in layer pixels. For `image` layers whose
  PNG size differs from the canonical size, `scale_fix = canonical / png` and the anchor is expressed in PNG
  pixels. For `text`, the extension computes the anchor in AE from `sourceRectAtTime(0)` and the same fractions
  (AE's text box is only known inside AE).
- Fonts: choose the device font whose family matches `family_guess` (case-insensitive, then the candidates in
  order) with the nearest weight; otherwise Arial (`ArialMT`) with `substituted: true` and a layer warning.
  Without a font list, `postscript` is `null` and the extension picks the family by name.
- Easing conversion to AE: outgoing influence `x1·100`, incoming influence `(1−x2)·100`; speeds from the
  bezier slopes `y1/x1` and `(1−y2)/(1−x2)` times the segment's average speed. Clamped to AE's 0.1–100 %.
- Groups: members of a scene group share one AE label colour (`label`, 1–16, by group order).
- `CompSpec` must be deterministic: same scene → byte-identical JSON (sorted keys, rounded to 1e-4).

### `devices.py`

- `create_code(now) -> str` — `KF-XXXX-XXXX` (Crockford base32, 40 bits), stored hashed with a 10-minute expiry;
  single use; at most 5 live codes.
- `pair(code, info) -> (device_id, token)` — `token` is 32 random bytes (urlsafe); only `sha256(token)` is stored.
  `info` = `{ae_version, extension_version, os, fonts}` (fonts capped at 5,000 entries, 256 chars each).
- `authenticate(token) -> Device | None`, `revoke(device_id)`, `touch(device_id, status)` (last seen, open AE
  project name), `list()`.
- Storage: `<workspace>/.ae/devices.json`, written atomically, `0600`.
- Pairing is per server: a device can receive jobs for any project in the workspace.

### `jobs.py`

- Kinds: `sync`, `render_frames`, `render_final`, `package`. Fields: `id, device, kind, project, scene, version,
  params, state (queued|running|done|failed), error, result, created, started, finished`.
- One `running` job per device. `next(device, wait)` returns the oldest queued job or waits on a condition
  variable up to `wait` seconds (25 by default) — enqueueing wakes it immediately.
- A running job whose device has not been seen for 60 s becomes `failed: "AE disconnected"`. No automatic retry.
- `render_frames`, `render_final` and `package` enqueue a `sync` first when the device's last synced version of
  that scene differs from the requested version.
- Live coalescing: a queued (not running) `sync` for the same device and scene is replaced by a newer one.
- Storage: `<workspace>/.ae/jobs/<id>.json`; completed jobs older than 7 days are pruned at startup.

### `api.py` (routes on the existing server)

Browser routes (existing same-origin check):

| Route | Purpose |
|---|---|
| `POST /api/ae/codes` | new pairing code → `{code, expires_at}` |
| `GET /api/ae/devices` | devices with `connected` (seen < 40 s), AE version, open project |
| `DELETE /api/ae/devices/<id>` | revoke |
| `POST /api/ae/send` | `{project, scene, version, force?, device?}` → `sync` job for `device`, or for the most recently seen connected device; `409 "no connected After Effects"` when none |
| `POST /api/ae/verify` / `render` / `package` | enqueue the job (and its `sync`) |
| `GET /api/ae/state?project=&scene=` | last jobs, last synced version, live flag, hand-edit state, verify report, downloads |
| `POST /api/ae/live` | `{project, scene, on}` |
| `GET /ae/keepframe.zxp` | the signed extension |

Extension routes (`Authorization: Bearer <device token>`, `X-Keepframe-Extension: <semver>`):

| Route | Purpose |
|---|---|
| `POST /api/ae/pair` | `{code, info}` → `{device_id, token, server_name}` (no bearer) |
| `GET /api/ae/next?wait=25` | `200 {job}` or `204`; also updates last seen |
| `GET /api/ae/jobs/<id>/spec` | the `CompSpec` for `sync` jobs |
| `GET /api/ae/jobs/<id>/assets/<name>` | an asset named in the job's spec |
| `PUT /api/ae/jobs/<id>/files/<name>` | streamed upload; names allowlisted per kind |
| `POST /api/ae/jobs/<id>/result` | `{ok, result}` or `{ok: false, error, line?}` |
| `POST /api/ae/jobs/<id>/progress` | `{stage, done, total}` (optional) |

- A major-version mismatch in `X-Keepframe-Extension` → `426 {"error": "update the Keepframe extension", "download": "/ae/keepframe.zxp"}`.
- Upload allowlist: `render_frames` → `frame_<NNNN>.png` (≤ 25 MB each, ≤ 16 files); `render_final` →
  `final.mp4` (≤ 4 GB); `package` → `project.zip` (≤ 4 GB). Uploads stream to a temp file and are renamed into
  `<project>/ae/<scene>/<version>/` only when complete; partial files are deleted.
- Assets and specs are served only for the job's own project; names come from the spec, never from the path
  alone.

### `verify.py`

- `sample_frames(frames, n=16)` — evenly spaced, first and last included.
- `verify(scene, ae_frames: dict[int, Path]) -> Report` — renders the same frames with Keepframe's renderer,
  resizes AE frames to the scene size, computes per-frame mean |a−b|/255 (the `render_l1` metric).
- Pass: mean ≤ `VERIFY_MEAN_MAX = 0.02` and every frame ≤ `VERIFY_FRAME_MAX = 0.05` (calibrated in slice 2 on
  the live-check clips; the constants move, the rule does not).
- Text layers with a substituted font are listed as notes; their difference does not fail the check.
- The report keeps the three worst frames as AE / Keepframe / difference PNGs for the web page.

## Extension (`extension/`)

- `CSXS/manifest.xml`: bundle `com.keepframe.ae`, host `AEFT` `[24.1,99.9]`, CEP 11+, one panel
  "Keepframe", `--enable-nodejs`, `--mixed-context`.
- `index.html` + `panel.js`:
  - Settings: server URL (validated: `https://…`, or `http://` only for loopback, `100.64.0.0/10`, `*.ts.net`).
  - Pairing: code field → `POST /api/ae/pair`; the token is kept in the panel's `localStorage`.
  - Status line: `Connected · <server>` / `Not connected: <reason>`; the current job and its stage.
  - The long-poll loop with backoff (1 s → 30 s) on network errors; the job runner; a 200-line log with
    **Copy log**.
  - Downloads to `Documents/Keepframe/<project>/assets/<sha256>.<ext>` (reused when the hash matches); uploads
    with Node `https`/`http` streams; a small zip writer (store + `zlib.deflateRawSync`).
- `host/keepframe.jsx` (ExtendScript, ES3, with its own JSON fallback). Every entry point takes and returns a
  JSON string and never throws (returns `{ok:false, error, line}`):
  - `kfInfo()` → `{ae_version, project_name, project_saved, fonts:[{family, style, postscript}]}`.
  - `kfSync(specJson, assetDirJson, force)` — see below.
  - `kfRender(requestJson)` → render queue; `frames` (PNG sequence, half resolution, into a temp folder) or
    `final` (H.264 chosen by output-module format, not by the localized template name).
  - `kfPackage(requestJson)` → `{aep: path, footage: [paths]}` for the saved project; fails with
    "Save the AE project first" when unsaved.

### `kfSync` algorithm

Wrapped in `app.beginUndoGroup("Keepframe sync")`.
1. Find the comp whose comment is `comp.tag`; create it if missing. Update size, fps, duration, name if changed.
2. Ensure project folder `Keepframe/<project>`; import each asset file whose sha256 (kept in the footage item's
   comment) is new; replace the source of footage items whose hash changed.
3. Hand-edit check (skipped when `force`): for each existing tagged layer, recompute the fingerprint of its
   animated values (keys and values of every property and effect Keepframe set) and compare with the one stored
   in the layer comment at the last sync. Any mismatch → return `{ok: true, applied: false, hand_edited: [ids]}`
   without changing anything.
4. For each spec layer: same tag and kind → clear and rewrite its keys, settings and effects; kind changed →
   delete and recreate; missing → create. Delete tagged layers absent from the spec. Untagged layers are never
   touched.
5. Order the tagged layers by `order` among themselves (untagged layers keep their places).
6. Store each layer's new fingerprint in its comment (`keepframe:<id>;fp=<hash>`).
7. Return `{ok: true, applied: true, created, updated, deleted, keys: {id: count}, warnings, ae_version}`.

## Live agent control

- `POST /api/ae/live` stores a per-scene flag. While on, every new version of that scene (agent edit, review
  correction, keep change) enqueues a coalescing `sync` for each connected device.
- A `sync` result with `hand_edited` pauses Live for that scene; the web page offers **Overwrite them**
  (re-send with `force`) or **Keep my AE edits** (Live off).
- Agent tools (`keepframe/session/tools.py`):
  - `ae_status` — connected devices, last synced version, Live flag, hand-edited layers. Read-only.
  - `send_to_ae` — one `sync`; browser confirmation like `correct`/`set_keep`, except when Live is already on.
  - `ae_verify` — enqueue verify and, when it finishes, return the report summary (pass/fail, worst frames,
    font notes).
- Edits still go through scene versions and the existing edit verification; AE only shows accepted versions.

## Errors

- No silent path: every failure ends as a job `error` (shown on the web page and in the panel) or a panel status
  line. ExtendScript errors include the line number.
- Network loss: the panel shows `Not connected: <reason>` and keeps retrying; the server marks the running job
  failed after 60 s.
- Version skew: `426` with a download link, shown in the panel.

## Testing

- Python unit tests: `comp_spec` (each kind and property, ease conversion, order and its warning, font choice
  and fallback, determinism); devices (expiry, single use, hashing, revoke); jobs (states, wake-up, disconnect
  timeout, coalescing, sync-before-render); api (auth, allowlist, caps, partial-upload cleanup, 426); verify
  (synthetic frames, thresholds, font notes).
- Fake AE in Node (`tests/ae_fake/`): grows from `tests/ae_panel_host.js` into an object model — Project,
  FolderItem, FootageItem, CompItem, AVLayer/TextLayer/ShapeLayer, Property with keys and temporal ease,
  effects (`ADBE Linear Wipe`, `ADBE Geometry2`), RenderQueue writing placeholder PNG/MP4 files — with no
  built-in `JSON`. `kfSync` tests: create, update, delete, idempotence (second run changes nothing), hand-edit
  detection, untagged layers untouched.
- End-to-end in CI: the real server + `panel.js` run headless in Node against the fake AE: pair → sync →
  render_frames → verify; Live coalescing; disconnect.
- Live checklist per slice in the user's AE (`docs/qa/ae-extension/live-check.md`), with screenshots. Findings
  become fake-AE behaviour and tests.

## Build and signing

- `scripts/build_zxp.sh`: generates a self-signed certificate once (stored outside git, e.g.
  `~/.config/keepframe/zxp-cert.p12`), signs `extension/` with Adobe's `ZXPSignCmd`, writes
  `keepframe/ae/static/keepframe.zxp` (git-ignored; served by `/ae/keepframe.zxp`).
- Current `ZXPSignCmd` releases are Windows/macOS. On this Linux server the Windows binary runs under Wine (the
  approach of the public `zxpsigncmd-docker` images); if that fails, signing runs on a GitHub Actions Windows
  runner. The first plan task settles this.
- Install for the user: download from the web page, then either Adobe's UPIA that ships with Creative Cloud
  (`"C:\Program Files\Common Files\Adobe\Adobe Desktop Common\RemoteComponents\UPI\UnifiedPluginInstallerAgent\UnifiedPluginInstallerAgent.exe" /install keepframe.zxp`)
  or the free aescripts ZXP Installer.

## Removal (slice 1)

- Delete `keepframe/after_effects/` and its tests, the `ae-install` / `ae-connect` / relay CLI options and
  environment variables (`KEEPFRAME_AE_RELAY_*`), the old `/api/ae/pairings` and AE render-plan routes, and the
  AE backend in render plans (native and Lottie plans stay).
- Replace the agent page's AE card; remove `docs/qa/round2/ae-live-check.md` and the README's old AE section.
- Existing `ae-auth.json` files and AE session folders in workspaces are ignored, not migrated.
- Until slices 2–3 land there is no verify/MP4/zip from AE.

## Slices and done criteria

1. Foundation + comp build — extension builds and installs from the web page; pairing; connection status on both
   sides; `sync` of the live-check scenes (reveal text, image background, spinning GLB) creates an editable comp;
   re-sync is a no-op; hand-edit warning. Old AE code removed. Live checklist passes in the user's AE.
2. Verify — `render_frames` + `verify.py`; thresholds calibrated on the live-check clips; web report.
3. Final render + package — MP4 and project zip downloadable from the web page; Korean-AE H.264 selection checked.
4. Live agent control — Live toggle, coalescing, hand-edit pause, agent tools; an agent edit appears in AE
   within ~1 s of the new version.

## Open items

- `ZXPSignCmd` on Linux (Wine) vs a Windows runner — settled in slice 1, task 1.
- Linear Wipe angle sign and model-layer rotation axes — confirmed in the slice-1 live check.
- `VERIFY_*` thresholds — calibrated in slice 2.
