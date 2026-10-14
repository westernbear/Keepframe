# kf-script P4 · fitting the script to the video — Implementation Plan (task level)

> **For agentic workers:** this plan is task level. Refine each task into Deliverable / Files / Interfaces / Gates / Tests against the code at that commit before starting. One commit per task.

## Context

- Spec §Redesigned analysis: the pipeline's job is the shortest kf-script whose render matches the reference.
- Research §5/§5b: the IR spends dozens of elements and invented transforms where the read sees a few structured things (mk571 frame split into 4 sprites; mk544 glyph fragments; mk543 per-line scroll repeated 5–6 times; e07 counters as 13 texts; e11 typing as "moves right, scales ×1.94").
- P3 provides the vocabulary; P2 provides the measuring stick.

## Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | One objective: render residual + λ · description length (view chars). A merge, split, retype or re-ease is kept only if it lowers the objective. | Replaces per-case thresholds (CLAUDE.md §3). |
| D2 | numpy renders inside the loop; Chromium confirms the final scene. | CPU budget. |
| D3 | The read pass never overrides numbers; disagreements go into `report.json` and the QA docs. | CLAUDE.md §1, §4. |
| D4 | P2 baselines become gates for this phase; gold and clip L1 must not regress. | |

## Tasks

| Task | Deliverable |
|---|---|
| 1 | Shots inside a scene: cut/transition classification per frame range into `Scene.shots`; elements cannot span a cut. |
| 2 | Shape typing: rect/ellipse fit (border, fill, radius) against the plate; kept when its residual ≤ the texture's (fewer parameters win ties). Size tracks (`w`, `h`, `stroke_w`) instead of scale for shapes. |
| 3 | Hold and step detection in `analyze/keyframes.py` before `fit_ease` (a jump becomes a `hold` key, not a 1-frame ramp). |
| 4 | Stage `structure` after `keyframes` (in `_finish`, beside `text_groups`/`group_by_motion`): stagger (the same tween shifted across siblings), repetition (template + instances), containers (seeded from "moves together" groups), counters (a numeric OCR series in one box), typing + caret, masks (edge-limited reveals). Updates `STAGES` and `web/static/js/analyze.js:26`. |
| 5 | Render–compare refit loop with an iteration budget and per-change logging. |
| 6 | Read pass: keyframe sheets with burned-in frame numbers → closed-vocabulary roles, shot intents, choreography labels (≤ 3 VLM calls; Claude subagents in evaluation); `data-src="read"`; disagreement table in `report.json`. |
| 7 | Gates: P2 round trip structure F1 and matched rate beat the baseline; `l1_kf` no worse; gold mk571 no worse; eval clips (when present) no worse on `render_l1` and `title_whole`. |

## Verification

`keepframe eval-roundtrip` against the P2 baseline; gold rows; `eval-edits` on clips when `eval/clips/` exists; the read → measurement → render table per reference.
