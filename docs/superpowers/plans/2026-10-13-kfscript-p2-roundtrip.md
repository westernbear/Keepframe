# kf-script P2 · synthetic round trip — Implementation Plan

> **For agentic workers:** steps use `- [ ]`. One commit per task on `claude/quirky-fermat-uyx2k0`, pushed after each task. Tests and measurements run in the cloud container (CLAUDE.md §5).

## Context

- Spec: `docs/superpowers/specs/2026-10-13-kf-script-design.md` (§Evaluation).
- Stage B's fix wave fitted rules to four clips; every round fixed one case and broke another. P3/P4 need a measuring stick that cannot be fitted: random reference pages with known truth, rendered, analysed, re-emitted (P1) and diffed.
- Depends on P1's `emit_page` and `fidelity`.

## Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | Templates are hand-authored GSAP pages written from the reads (`gold-reads.md`, research §5b), not from analyser features. | The truth must not know how the analyser works. |
| D2 | Templates are free GSAP pages (DOM props, `__seek/__ready/__bbox`), like `gold/mk571.html`; not emitted kf-script. | Truth may use structure the IR cannot express yet. |
| D3 | 960×540, 30 fps, 90–150 frames, text ≥ 28 px. | SaaS-like, CPU-feasible; 540p OCR loses small text. |
| D4 | Truth level (a), IR-expressible: per element id, role, kind (text/shape/image), string, colour, font, visible range, boxes probed by Chromium through `__bbox`. Level (b), structure: `[{type, targets, params, ir: "now"|"P3"}]`. | (a) is scored now; (b) items marked `P3` are recorded `unsupported`, never failed. |
| D5 | Only gate: `kf_parity` (|l1_kf − l1_compose| ≤ 0.001). Everything else is recorded as the baseline for P3/P4. | Analysis quality is what P3/P4 change. |
| D6 | Analysis runs with `AnalyzeOptions(refine=False, generate_3d=False)`, no captioner; stage caches reused with `--reuse`; finished caches pruned. | CPU and disk in the container. |

## Global constraints

As P1. Never commit rendered cases or mp4s (`eval/out/`).

## Review focus

1. **Truth that encodes analyser behaviour** (the harness becomes fit-able). D1; reviewed per template.
2. **Non-deterministic cases** (baselines move between runs). Pinned by `test_templates_deterministic`.
3. **Box truth from hand maths drifting from the render.** Boxes come from `__bbox` in Chromium (`test_render_template_mp4_and_boxes`).
4. **Matching that rewards fragmentation** (many small analysed pieces matching one truth element). Pinned by `test_fragmentation_counted`.

## Facts checked in code

- `render(html, scene, out_dir, frames, probe)` probes `__bbox(id)` for every scene element id when `probe=True` (`render/renderer.py`).
- `analyze.video.write_video(frames_u8, fps, out)` writes the H.264 reference as `render_reference` does (`ir/synth.py:398`).
- `ir.tracks.element_bbox(el, f)` gives analysed boxes; `scipy.optimize.linear_sum_assignment` is used in `qa/edits.py`.
- `qa/sheets.comparison_sheet(rows, labels, out)` draws source | render | diff sheets.

## Task overview

| Task | Deliverable | Depends | Browser |
|---|---|---|---|
| 1 | Template generator, 8 kinds | P1 | |
| 2 | `render_template` → mp4 + truth | 1 | yes |
| 3 | Structural diff | — | |
| 4 | `keepframe eval-roundtrip` | 2, 3, P1 | yes |
| 5 | mk571 gold truth in the harness | 4 | yes |
| 6 | Baseline run, QA doc, review (controller) | all | yes |

## Task 1: template generator

**Deliverable**: `make_template(kind, seed) -> Template(doc, truth, size, fps, frames)`; seeded strings, palette, bundled fonts (file URIs), timings, stagger `each`/`from`.

Kinds: `kinetic_title` (word/glyph stagger through a mask, reversed exit); `logo_sting` (centre-out clip, border frame growing in w/h with constant stroke, mark pop, name rising word by word); `chat_ui` (N bubbles from one template, alternating sides, list stepping every k frames); `scroller` (stacked lines, inverse flash); `block_transition` (k blocks wiping shot A → B); `prompt_typing` (bar fade-in, chars with pauses, caret blink, cursor glide + press); `counter` (snap, "K" suffix, slow–fast–slow); `stepped_zoom` (holds).

**Files**: new `keepframe/kfscript/synth.py`.

**Tests**: `test_templates_deterministic`, `test_truth_levels`, `test_every_kind_renders` (browser).

## Task 2: render a template

**Deliverable**: `render_template(kind, seed, out) -> (mp4, truth_json)`; truth boxes probed per sampled frame.

**Files**: `keepframe/kfscript/synth.py`.

**Tests**: `test_render_template_mp4_and_boxes` (browser).

## Task 3: structural diff

**Deliverable**: `diff(truth, scene, view_chars=None, truth_chars=None) -> dict`: Hungarian matching on box IoU at each truth element's mid-visible frame; element counts; fragmentation (analysed elements inside each truth element); matched rate (IoU ≥ 0.5); text ratio (difflib); text colour ΔE; centre error px; visible-range error frames; structure found/missed/unsupported; `dl_ratio`.

**Files**: new `keepframe/kfscript/diff.py`.

**Tests**: `test_identity_perfect`, `test_fragmentation_counted`, `test_unsupported_recorded`, `test_dl_ratio`.

## Task 4: `keepframe eval-roundtrip`

**Deliverable**: `eval-roundtrip --out D [--kinds …] [--seeds N] [--reuse] [--gold NAME=CLIP,ROOT]`: generate → analyse → `emit_page` → `fidelity` (`l1_kf`), report `l1_recon`, compose L1 → diff → `metrics.json`, `summary.md`, sheets.

**Files**: new `keepframe/qa/roundtrip.py`; `keepframe/cli.py`.

**Tests**: `test_fake_analysis_end_to_end` (analysis monkeypatched), `test_cli_eval_roundtrip_args`.

## Task 5: mk571 gold

**Deliverable**: `docs/qa/kfscript/gold/mk571.truth.json` from `gold-reads.md` (bg reveal, frame, mark, name, url; structure: centre-out mask, frame w/h with constant stroke and the 1076 → 1012 settle, mark pop, word stagger through a mask, url drift 24 px); `window.__bbox` added to `gold/mk571.html`; the gold row reports gold L1 (the ceiling), pipeline `l1_kf` and the diff.

**Tests**: `test_gold_mk571_truth_ids`, `test_gold_mk571_bbox` (browser).

## Task 6: baseline (controller)

- [x] 8 kinds × seed 1 (seeds 2–3 not run) + mk571 gold → `docs/qa/kfscript/p2-baseline.{json,md}` and sheets, with commit, timings and the read → measurement → render table.
- [x] A rerun with `--reuse` gives identical pages, truth and metrics.
- [x] Review; push.

## Time budget

| Task | Estimate |
|---|---|
| 1–2 | 1 day |
| 3–4 | 0.5 day |
| 5–6 | 0.5 day |

Cut order: kinds 8 → 4 (kinetic_title, logo_sting, chat_ui, prompt_typing) → sheets → gold box probing.

## Verification (end to end)

```bash
.venv/bin/python -m keepframe.cli eval-roundtrip --out eval/out/rt-base --seeds 1 --gold mk571=eval/refs/mk571.mp4,eval/kf/mk571
.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_kfscript_synth.py tests/test_kfscript_diff.py tests/test_roundtrip.py
```
