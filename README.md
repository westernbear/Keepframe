<p align="center">
  <img src="docs/assets/logo.png" width="128" height="128" alt="Keepframe">
</p>

# Keepframe

Keepframe measures a flat 2D motion-graphics clip or UI screen recording into an editable scene and renders MP4. You name what to keep and what to change. Live-action is rejected.

## Requirements

- Python 3.11+ (3.12 for `[ocr]`; RapidOCR stops at 3.12)
- [ffmpeg and ffprobe](https://ffmpeg.org/) on `PATH` (both are required by final render verification)
- Chromium via Playwright, for compose and render

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install '.[ocr]'
playwright install chromium
```

`pip install .` skips OCR. GPU refine: `pip install '.[gpu]'`.
NVIDIA OCR needs `[ocr]` plus a CUDA 12 `onnxruntime-gpu` wheel (`<1.27`; 1.27+ is CUDA 13). Uninstall **both** ORT packages or a leftover CPU wheel breaks `GraphOptimizationLevel`. Do not run `pip install '.[ocr]'` again afterwards (it pulls CPU `onnxruntime` back).

```bash
pip install -e '.[ocr]'
pip uninstall -y onnxruntime onnxruntime-gpu
pip install 'onnxruntime-gpu>=1.19,<1.27'
python -c "import torch; from onnxruntime import GraphOptimizationLevel, get_available_providers, get_device; print(get_device(), get_available_providers())"
python -c "import torch; from rapidocr_onnxruntime import RapidOCR; print('ocr ok')"
```

## Usage

```bash
keepframe serve --workspace ./data/workspace
```

http://127.0.0.1:8765/ landing. Maker UI at `/library` and `/new` (Korean by default; header toggle for English). `/demo` seeds a synthetic video, detects its graphic regions, and opens reference analysis. Demo text boxes are labelled fixtures with no OCR confidence. `/admin` is on by default and requires an explicitly configured account; set `KEEPFRAME_ADMIN_EMAIL` and `KEEPFRAME_ADMIN_PASSWORD` (or `KEEPFRAME_ADMIN_USERS` as comma-separated `email:password` pairs). `--no-admin` turns it off.

CLI-generated project directories placed under the workspace appear in the library even without `meta.json`; the first metadata change creates that file. Analysis creates `overrides.json` without overwriting existing overrides, and frame-stage reruns reuse the recorded source range.

In review, choose a keep preset before approving the analysis:

| Preset | Keeps |
| --- | --- |
| `content_only` (default) | Motion type, direction, distance, duration and temporal relations |
| `motion_shape` | Motion type, direction and order |
| `all` | All motion and spatial predicates |
| `none` | No keep predicates |

After approval, ask the agent for an edit. Confirm its preview in the browser.

| Edit | Changes |
| --- | --- |
| `text` | Text copy |
| `color` | Element color |
| `background` | Scene background color |
| `font` | Font family or weight |
| `timing` | Speed or element delay |
| `texture` | Texture from a PNG/JPEG attachment |
| `model` | 3D model |

ChatGPT login uses a direct Responses client with `gpt-6.1-sol` by default. VLM labels and captions are suggestions; timing, position and size stay measured.

The optional After Effects relay is a second, connector-only listener. Set
`KEEPFRAME_AE_RELAY_URL`, `KEEPFRAME_AE_RELAY_HOST`,
`KEEPFRAME_AE_RELAY_PORT`, and `KEEPFRAME_AE_RELAY_TOKEN` together; partial
configuration fails startup. Expose only that relay listener through an HTTPS
reverse proxy. Plain HTTP relay URLs are accepted only for loopback hosts.

The first same-origin pairing establishes a project-scoped, host-only controller
cookie. Re-pairing and unpairing require that cookie. Replacement and unpair
stop new connector leases, allow the active lease to settle, then rotate or
revoke the hashed device credential. Browser artifact downloads require the
same controller cookie and same-origin request.

Install the optional Windows connector with `pip install 'keepframe[ae]'`.
`keepframe ae-install [--ae-path ...]` installs the ScriptUI panel for After
Effects 2022 or newer; restart After Effects, enable **Allow Scripts to Write
Files and Access Network**, then open **Window > Keepframe Panel**. With
`KEEPFRAME_AE_RELAY_TOKEN` set only in the connector environment, run
`keepframe ae-connect --url https://relay.example`; the command prompts for the
one-use pairing code without echoing it. `--code CODE` is available for
non-interactive pairing. Later starts use
`keepframe ae-connect --url https://relay.example --project PROJECT_ID` and the
current-user DPAPI-protected device record. The MCP child receives neither
relay credential.

After Effects plans pin the active capability manifest, operation schemas, scene
assets, source locks, and any acknowledged compatibility substitutions.
Unsupported fonts or converter semantics produce a non-approvable draft until
the proposed lost semantics are acknowledged; capability changes require a new
successor plan.

Approved After Effects sessions first build and verify deterministic checkpoint
0, then send at most 12 bounded preview frames to the configured LLM for typed
polish operations. Every baseline, agent, and manual candidate retains its AEP,
preview, frames, inspection, operations, model response, manifests, and verifier
report. Failed candidates remain inspectable and roll back to the last passing
checkpoint. The loop has no iteration cap; one no-op or two identical model
plans pauses it as `no_progress`. Manual AE edits sync as new checkpoints and
never modify Keepframe IR.

Finalization is a separate approval-bound step for the selected passing
checkpoint. The connector rematerializes and re-verifies that immutable AEP,
renders a full-resolution PNG sequence through the After Effects Render Queue,
and encodes `final.mp4` with fixed H.264/yuv420p settings. It retains
`project.aep`, collected content-addressed media, and `dependencies.json`
locally, uploads only to coordinator-reserved artifact slots, and publishes a
ZIP containing exactly those package files. Font and plugin binaries are never
collected.

The five admin pages share the Korean/English toggle. Entity values and timestamps stay verbatim. The admin queue combines seeded examples with live jobs from the running server; its status and GPU counts reflect those rows.

```bash
keepframe analyze --video ref.mp4 --start 0 --end 90 --out ./out
keepframe analyze --video ui.mp4 --start 0 --end 90 --out ./ui-out --ui --no-captions
keepframe compose --scene ./out/scenes/s1/scene.json --out ./out/comp.html
keepframe render --scene ./out/scenes/s1/scene.json --html ./out/comp.html --out ./out/frames --mp4
keepframe verify --scene ./out/scenes/s1/scene.json --render-json ./out/frames/render.json
keepframe correct --root ./out --scene s1 --op text --args '{"element_id":"e3","text":"New title"}'
```

`keepframe verify --reference` compares motion by element id.

`--end` is inclusive. `correct` ops: `reassign`, `mask`, `bbox`, `text`.
`analyze --ui` enables UI parsing. `--no-captions` skips optional VLM captions.

Real-clip evaluation caps each clip with `--max-frames`. Prompt evaluation previews typed edits on project copies; it does not execute them.

```bash
keepframe gate-m2-real --clips eval/clips --out eval/out/final --max-frames 150
python scripts/eval_prompts.py --project eval/out/final/ig2 --scene s1 --workspace ./data/workspace
```

Results: [core-flow evaluation](docs/qa/core-flow/README.md).

## CLI

| Command | Role |
| --- | --- |
| `serve` | Maker UI. `/admin` on by default; `--no-admin` turns it off |
| `analyze` | Video to IR |
| `compose` | Scene JSON to HTML + GSAP |
| `render` | Chromium frames, optional MP4 |
| `verify` | Schema, keep predicates, frame compare |
| `correct` | Review ops on a scene |
| `ae-install` | Windows-only After Effects ScriptUI panel installer |
| `ae-connect` | Pair or resume the Windows connector and local MCP bridge |
| `synth` | Synthetic scene |
| `gate-m1` / `gate-m2` | Synthetic gates |
| `gate-m2-real` | Real-clip evaluation; `--max-frames` defaults to 150 |

## Docker

```bash
cp .env.example .env
docker compose up -d
```

Image `westernbear/keepframe:0.1.0` (`KEEPFRAME_IMAGE` / `KEEPFRAME_TAG`). Port 8765. Volume `./data/workspace`. Analyze and render run in-process (`KEEPFRAME_JOB_RUNNER=thread`).

```bash
docker compose --profile admin up -d
```

GPU refine needs an NVIDIA GPU, the NVIDIA Container Toolkit, and the `gpu` image layer.
Intel iGPU is not used. The overlay sets `KEEPFRAME_DEVICE=cuda` so sprite refine will not silently fall back to CPU.

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

## Limits

- Flat 2D motion graphics and UI recordings only
- Background plates assume a static camera
- Font candidates come only from installed fonts
- Agent edits are preview-only; confirm in the browser
- The default `content_only` preset keeps motion predicates; choose `content_only`, `motion_shape`, `all` or `none`. `none` disables keep checks
- Repair retries: 4. Asset generation: 2. Caps are display-only
- After Effects work is frozen until the core flow is validated

### Reference analysis review

Review displays the original frame with per-object detection masks and OCR boxes. Select a region, object row, or appearance track to inspect it. The analysis layer starts at 25% opacity; use **Edit region** to draw a correction box, then apply it. Approving the analysis opens the existing AI recreation workflow. Reconstruction diagnostics and render APIs remain available internally.

`GET /api/analysis-overlay?project=<id>&scene=<id>&frame=<index>&v=<version>` returns the requested frame/version, original size, snapshot ID, object IDs, contour rings (including holes and separate components), and OCR observations. A successful response with `available: false` means that version has no recoverable analysis; `objects: []` with `available: true` means the frame has no detections. Only actual OCR scores are labelled as OCR confidence.

Analysis manifests and per-frame observations live under `scenes/<scene>/analysis/<content-hash>/`; immutable source arrays are deduplicated in `analysis/sources/`. Each scene version references its manifest through `analysis_file`. Ordinary edits inherit the reference; reanalysis and region corrections publish observations before adding a new version. Existing stage caches are migrated once to the latest version only. Historical versions without evidence show no analysis layer. These snapshots must be retained when archiving a project.
