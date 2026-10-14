# kf-script P5 · editing through kf-script — Implementation Plan (task level)

> **For agentic workers:** this plan is task level. Refine each task into Deliverable / Files / Interfaces / Gates / Tests against the code at that commit before starting. One commit per task.

## Context

- Spec §Format and §LLM view: the agent reads kf-script; `scene.json` stays canonical; edits are patches by element id.
- MoVer: predicate-level feedback lifts animation correctness 58.8 % → 93.6 %; P5 adds that loop to edits.
- Depends on P3's parser and primitives, and on P1's A/B edit set.

## Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | Patches are typed operations on element ids, produced either as a tool call or by diffing a parsed edited kf-script against the scene; out-of-subset changes are refused with a fixable message. | Unedited elements stay exact; no document regeneration. |
| D2 | Verify-repair predicates come from the original scene (Allen intervals, directions, durations, keep predicates); ≤ 3 repair rounds. | MoVer; the cap is deliberate. |
| D3 | The default agent view switches only by the P1 pre-registered rule, rerun on the production model. | |

## Tasks

| Task | Deliverable |
|---|---|
| 1 | Tolerant parse with precise error lists (line, construct, reason). |
| 2 | `diff(parse(edited), scene)` → typed `Intent` targets (`edit/intent.Target`), applied through `edit/apply`; ids stable; unknown ids/props rejected. |
| 3 | `edit/apply.py` handles the P3 primitives (shape colour/stroke/size, split retime/retext, counter values, cursor timing, shot intent). |
| 4 | Agent tool `patch_kfscript` and SYSTEM wording; `tools._edit` stays the execution path. |
| 5 | Verify-repair: predicates from `verify/` (instruction predicates + keep predicates) checked after each edit; failures returned as predicate-level messages. |
| 6 | AE export of shapes, parenting, text animators, hold keyframes, counters (source text), mattes; fake-AE tests; the live AE check is a user step. Lottie exports shapes/parents and refuses the rest with a warning. |
| 7 | A/B rerun with a production `make_llm` client and real tool calls; decide the default view by the pre-registered rule. |

## Verification

P1 A/B edit set through `scripts/ab_views.py run --llm`; `scripts/eval_prompts.py` with `KEEPFRAME_AGENT_VIEW=kfscript`; fake-AE spec tests; live AE check (user).
