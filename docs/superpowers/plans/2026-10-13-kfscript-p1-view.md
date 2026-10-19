# kf-script P1 · the view: Implementation Plan

> **For agentic workers:** steps use `- [ ]`. One commit per task on `claude/quirky-fermat-uyx2k0`, pushed after each task. Tests and measurements run in the cloud container (CLAUDE.md §5).

## Context

- Spec: `docs/superpowers/specs/2026-10-13-kf-script-design.md` (§Format, §LLM view).
- Research: `docs/research/2026-10-13-ai-legible-format/`. The brief hides IR facts (e24's typed line has glyph reveals; the brief shows none), and an executable page can be checked against the source (mk571 gold 0.0066).
- P1 gives every analysed scene an executable, readable kf-script that renders like `compose()`, lets the agent read it, and measures both: fidelity against the source and an A/B against the brief.
- Inputs: seven analysed reference projects in `eval/kf/` (mk543, mk544, mk571, mk800, e07, e11, e24; sources in `eval/refs/`), the gold page `docs/research/2026-10-13-ai-legible-format/gold/mk571.html`.

## Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | kf-script = `compose()` with an explicit timeline. The template keeps its runtime; only the loop that builds tweens from `SCENE.elements[].tracks` is replaced by emitted tweens on per-element state objects `S["id"]`. | Exact by construction; no duplicated runtime (video seek, models, glyph masks, gradient keys, UI states stay shared). |
| D2 | IR prop names in tweens (`x y sx sy rot skx rx ry opacity reveal`). | GSAP's native 3D order differs from Keepframe's; state objects keep `apply()`'s order. The header's `conventions` line explains the names. |
| D3 | Merge segments sharing `(t0, t1, ease)` per element; seconds to 4 dp with a frame comment. | Readability; 4 dp at ≤ 60 fps moves a time by < 0.003 frame. |
| D4 | Eases: `CustomEase.create(name, path)` once per used preset (names from `PRESET_EASES`), other beziers `ease1…`, linear `"none"`. | Named eases are what an LLM reads; values stay exact. |
| D5 | Header: id, size, fps, frames, duration, `shots: sh1 0.00–D`, palette (bg + element colours with ids, merged at ΔE < 3), type (family/weight/size with ids), `intent: (unread)`, conventions. | Spec §Format 1. |
| D6 | Fidelity source frames via `qa/edits._source_frames` (stage cache, else the project's video at its range). | Range projects (e24 f120–269) start mid-video. |
| D7 | View budget: rank by `rank_elements`, drop the lowest until under `VIEW_CHARS`, list omitted ids. | Same ranking as the brief's 40-element cap. |
| D8 | Agent switch by `KEEPFRAME_AGENT_VIEW` (default `brief`); any view error falls back to the brief. | No behaviour change until the A/B decides. |
| D9 | A/B is blind and pre-registered: kf-script becomes the default only if edit-target accuracy ≥ brief **and** comprehension ≥ brief + 10 points. Subagents answer now; `run --llm` uses `make_llm()` when credentials exist. | No LLM credentials in the container. |

## Global constraints

- Ponytail full; no new dependency; schema unchanged; legacy `compose()` output renders bit-identically.
- Fail soft: emission, fidelity and the view never fail analysis or an agent turn.
- Script content is numbers and `json.dumps` ids only; text stays in escaped markup.
- Never commit `eval/` or media.

## Review focus

1. **kf-script renders differently from the composer** (an agent edits a view that is not the scene). Pinned by Task 3 parity tests.
2. **Legacy compose changes** (every existing render shifts). Pinned by `test_compose_without_timeline_is_unchanged`.
3. **Observed text breaks out of markup into the script or the prompt.** Pinned by `test_state_keys_are_json_escaped` and the composer's existing escape test.
4. **The agent loses targeting** (ids in the view differ from the ids `tools._edit` accepts). Pinned by `test_emit_header_and_ids_match_ir`, `test_agent_sends_kfscript_when_env`.

## Facts checked in code

- `compose/template.html:58-89`: per element a state `st` with `DEFAULTS`, `apply()` writes `translate rotateX rotateY rotate skewX scale`, opacity, reveal clip; per track segment `tl.fromTo(st, {[prop]: a.v}, {…, duration: (b.t-a.t)/FPS, ease, immediateRender: false, onUpdate: apply}, a.t/FPS)`.
- `__seek(f)` (161) runs `tl.time(f/FPS)` then every applier (`apply()`, visibility, z, glyph mask), so `onUpdate` is not needed for rendering.
- `compose()` (`composer.py:181`) fills placeholders by string replacement; markup from `_element_html` (91).
- `render(html, scene, out_dir, frames, probe)` (`render/renderer.py:45`) renders any page exposing `__ready`/`__seek` (and `__bbox` when probing).
- `ae/verify.compare_frames(a, b, masks)` returns `(l1 0..1, diff×4)` for equal-size uint8 frames.
- `session/agent._scene_summary` (59-72) builds the second system message; `tests/test_brief.py:43` asserts it is the brief.
- `PRESET_EASES` (`ir/tracks.py:6`): linear, in_quad, out_quad, in_out_quad, in_cubic, out_cubic, in_out_cubic, out_back.

## Task overview

| Task | Deliverable | Depends | Browser |
|---|---|---|---|
| 0 | Rules, spec, plans; disk; refs; baseline (controller) | — | |
| 1 | Template hook + `compose(..., timeline=None, header="")` | 0 | yes |
| 2 | Emitter `keepframe/kfscript/emit.py` | 1 | |
| 3 | Parity tests | 2 | yes |
| 4 | Fidelity helper, CLI, measurements | 3 | yes |
| 5 | LLM view | 2 | |
| 6 | Agent switch | 5 | |
| 7 | Blind A/B harness and run | 5 | |
| 8 | Final run, QA doc, review (controller) | all | yes |

## Task 0: rules, spec, plans, disk, baseline (controller)

- [x] `CLAUDE.md`, the spec and the five plans committed and pushed.
- [x] Analysed projects copied to `eval/kf/` without `stages/*.pkl`, `frames.npy`, `plate_frames.npy`, `analysis/`; `project.json` sources point at `eval/refs/`.
- [x] Fast-suite baseline recorded in `docs/qa/kfscript/p1.md`.
- [ ] Eval clips: the user uploads ig1 to ig3 and envato1 to `eval/clips/`; clip gates are "not run" until then.

## Task 1: template hook

**Deliverable**: the template exposes per-element state objects and an explicit-timeline slot; without a timeline it builds the same tweens as today.

**Files**: `keepframe/compose/template.html` (element loop), `keepframe/compose/composer.py` (`compose`).

**Interfaces**
```python
def compose(scene, scene_dir, out_html, *, fonts=None, font_wait=None, warnings=None,
            timeline: str | None = None, header: str = "") -> Path
    # timeline: JS run after the state objects exist (uses `tl`, `S`, CustomEase); None = today's per-track loop
    # header: an HTML comment placed before <html>
```
Template: `const S = {};` before the loop; `S[el.id] = st;` in it; the track loop is guarded by `if (!EXPLICIT)`; `{{TIMELINE}}` after the loop; `{{HEADER}}` at the top. `EXPLICIT` is `true` only when a timeline is given.

**Gates**: fast tests; `test_compose_without_timeline_is_unchanged` (browser): PNG hashes of a synthetic scene are equal before and after.

## Task 2: emitter

**Deliverable**: `emit_page(scene, scene_dir, out, fonts=None) -> Path` writes an executable kf-script page; `timeline_js(scene) -> str`, `header(scene) -> str`.

**Files**: new `keepframe/kfscript/__init__.py`, `keepframe/kfscript/emit.py`; `compose/composer.py` gains `data-role`, `data-src`, `data-conf`, `data-caption` attributes only when called for kf-script (`attrs=True`).

**Interfaces**
```python
def timeline_js(scene: Scene) -> str      # CustomEase registrations + one tl.fromTo per merged segment group
def header(scene: Scene) -> str           # the <!-- kf-script v0 … --> comment
def emit_page(scene, scene_dir, out, *, fonts=None) -> Path
```

**Gates**: fast tests. **Tests**: `test_emit_header_and_ids_match_ir`, `test_shared_segments_merge`, `test_preset_eases_registered_by_name`, `test_times_four_decimals`, `test_state_keys_are_json_escaped`.

## Task 3: parity

**Deliverable**: the kf-script page renders like the composer page.

**Files**: `tests/test_kfscript.py`, `tests/test_kfscript_browser.py`.

**Gates**: per sampled frame L1 ≤ 0.002 and mean ≤ 0.0005 on `make_synthetic_scene` seeds 1 to 3; `make_reference_scene` flat/gradient/animated/image plates with mover and logo; stagger (9) and plane (10) titles; a video-sprite scene. A shuffled frame order gives the same hashes as the forward order.

**Tests**: `test_parity_synthetic`, `test_parity_reference_plates`, `test_parity_glyph_and_plane`, `test_parity_video`, `test_seek_order_independent`.

## Task 4: fidelity, CLI, measurements

**Deliverable**: `fidelity(root, scene_id, html, every=4, max_frames=None, sheet=None) -> dict` and `keepframe kfscript --scene S --out O [--view V] [--check]`.

**Files**: new `keepframe/kfscript/check.py`; `keepframe/cli.py`; `docs/qa/kfscript/p1.md`.

**Gates**: on the seven `eval/kf` projects |L1_kf − L1_compose| ≤ 0.001; gold mk571 through the same helper 0.0066 ± 0.0005. Chars of brief / page / view and seconds recorded.

**Tests**: `test_fidelity_range_project` (browser), `test_cli_kfscript_page_and_view`.

## Task 5: LLM view

**Deliverable**: `view(scene, scene_dir, max_chars=VIEW_CHARS) -> str`.

**Files**: `keepframe/kfscript/emit.py`.

**Gates**: every reference's view ≤ `VIEW_CHARS`; `VIEW_CHARS` set from Task 4's sizes.

**Tests**: `test_view_strips_vendor_and_data_uris`, `test_view_budget_by_rank`.

## Task 6: agent switch

**Deliverable**: `KEEPFRAME_AGENT_VIEW=kfscript` sends the view; the brief otherwise and on any error.

**Files**: `keepframe/session/agent.py` (`_scene_summary`, SYSTEM wording), `tests/test_brief.py`.

**Tests**: `test_agent_sends_kfscript_when_env`, `test_kfscript_failure_falls_back_to_brief`; `test_brief.py:43` unchanged.

## Task 7: blind A/B

**Deliverable**: `scripts/ab_views.py` with `questions`, `packets`, `score`, `run --llm`; results in `docs/qa/kfscript/ab/`.

- Auto-gold questions from `scene.json` (ids visible at t; when X's last motion ends ± 1 f; which element has a glyph reveal; ease of X's y motion; string/colour of the largest text; z of A vs B).
- Read-gold questions from `gold-reads.md` mapped to acceptable id sets (brand name, url, frame, typed prompt, counter).
- Edit-target prompts scored like `scripts/eval_prompts.gold_match` (element + property).
- One packet per (clip, view) with production SYSTEM lines; file names hashed; one fresh subagent per packet.

**Tests**: `test_auto_gold_from_ir`, `test_scoring`, `test_packets_blind`.

## Task 8: final run (controller)

- [x] Fast suite (no new failures vs Task 0) and the touched browser tests.
- [x] `docs/qa/kfscript/p1.md`: parity, fidelity table, chars, A/B results with caveats, the read → measurement → render table for mk571.
- [x] Review the whole diff; push.

## Time budget

| Task | Estimate |
|---|---|
| 1–3 | 0.5 day |
| 4–6 | 0.5 day |
| 7–8 | 0.5 day |

Cut order: `run --llm` → layout-class hoist in the view.

## Verification (end to end)

```bash
.venv/bin/python -m keepframe.cli kfscript --scene eval/kf/mk571/scenes/s1/scene.v1.json --out eval/out/mk571.kf.html --view eval/out/mk571.view.txt --check
.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_kfscript.py tests/test_kfscript_browser.py tests/test_brief.py
```
