# Pre-registration: brief vs kf-script view on the real eval clips, after the fixes

**Status: frozen** (2026-10-13). The commit that adds this file is the freeze. At that commit:
- the read boxes (`ab-real/read_boxes.json`) and the question code (`scripts/ab_views.py`) are fixed;
- no analysis of the eval clips at any post-fix commit exists.

The result is reported whatever it is. The decision rule below is the user's (2026-10-13): if kf-script fails it, the agent's default view reverts to `brief`.

## Question

kf-script became the agent's default after the synthetic A/B (`ab-test.md`, `78e9bc9`). Is it at least as good as the brief on real clips, once the real-clip fixes of `fixes2.md` are in?

Both views come from the same `scene.json` of each scene.

## Test set

- **Clips:** the four eval clips (ig1, ig2, ig3, envato1), source frames 0–149, exactly as `clips.md`.
- **Analysis command:** `keepframe gate-m2-real --clips eval/clips --out eval/out/clips-after --render-check`.
- **Analysis code:** the head of the fix series once Phase B is accepted; the report names that commit. The analysis splits each clip at its cuts and transitions. Its run is also the "after fixes" report in `clips.md`.
- **Units:** every scene the analysis makes (`clip:sN`); both views per scene.
- **Exclusions**, decided now: a scene is dropped from both views only if its analysis crashes. A read role whose box holds no IR element gets no question; such roles are listed in the report.

## Questions (generator frozen at this commit)

1. `scripts/ab_views.py real-gold --root eval/out/clips-after --boxes docs/qa/kfscript/ab-real/read_boxes.json --out docs/qa/kfscript/ab-real`
2. `scripts/ab_views.py packets --root eval/out/clips-after --out docs/qa/kfscript/ab-real`

| Category | Questions | Gold |
|---|---|---|
| **auto** and **motion** | the generators of the synthetic test (`ab-test-prereg.md`), unchanged | from each scene's IR |
| **read** | one per role the committed read names (`clips.md` §1, written before any IR at `6ce4003`): "Which element id(s) make up the …?" | the IR elements of the scene that holds the role's frame, visible there, whose box at that frame lies ≥ 60 % (`CONTAIN`) inside the role's box; each element belongs to the smallest box that holds it |
| **edit** | one per role (a text → its text, anything else → its colour), plus one timing edit per scene | the same ids |

Read scoring is Jaccard. An edit is correct when every answered id is in the gold set and the property matches.

**Read boxes.** There are 34 roles (`ab-real/read_boxes.json`), at named source frames. They were measured on the source frames, never on an IR:
- **ink (4 text roles):** the bounding box of pixels at least ΔE 12 from the local ground, inside the visual box padded by 4 %. Used when it does not touch that padding.
- **visual (the rest):** the role's edges read off a 10 % coordinate grid on the source frame, about ±10 px. Faint surfaces (white cards on near-white, the pill, the globe) and text over gradients or glows defeat the ink measure, so they use this method.

Packets never name the view and carry the production SYSTEM prompt.

## Answerer protocol

As in `ab-test-prereg.md`:
- one fresh Claude subagent (Sonnet) per packet;
- it reads only its packet and writes `answers/<hash>.json`;
- compliance is checked from each run's tool-use count;
- one attempt per packet; a missing or invalid answer scores 0.

The production model has no credentials in this container, so Sonnet is the proxy.

## Analysis and decision rule

Command: `scripts/ab_views.py score --dir docs/qa/kfscript/ab-real --by-scene`.

The statistic is the paired difference per question (kf-script − brief), with a 95 % bootstrap: 10 000 resamples, numpy seed 0, **resampling scenes**. Four clips are too few clusters for a percentile interval. Scenes of one clip are correlated, so this interval is narrower than a clip-level one would be; the clip-clustered interval is reported next to it.

- **Keep** (`keep_kfscript_default`): comprehension (auto + motion + read) lower bound **> −10 points** and edit-target accuracy lower bound **> −10 points**. This is non-inferiority with the margin pre-registered for edits in the synthetic test.
- **Outcome:**
  - *Keep holds:* the default stays `kfscript`.
  - *Otherwise:* `_scene_summary` (`keepframe/session/agent.py`) defaults to `brief` again, `KEEPFRAME_AGENT_VIEW=kfscript` opts in, and the tests and docs flip back.
  - **No re-run** on these clips either way.
- **Also reported, not deciding:**
  - the superiority test (`h1`);
  - the clip-clustered intervals;
  - per-category and per-scene accuracies;
  - each view's size in characters.

**Power, stated now.** No pilot exists on real clips. The synthetic runs' clustered SE ranged from 2.2 to 6.0 points. With about 8–10 scene clusters and dense scenes (the first gate's ig1 had 101 elements), a half-width near 10 points is likely. So the rule keeps kf-script only if it is roughly level with the brief or better on these clips. A true difference of −5 has a real chance of failing it.

## Before the freeze (disclosed)

- **Pre-fix IR seen.** The pre-fix IR of these clips (`eval/out/clips-78e9bc9`) was inspected for `clips.md` (`e392534`). The roles come from the read committed before that IR was opened. The named frames and the boxes were chosen and measured on the source frames for this test.
- **Fixes.** The fixes (`fixes2.md`) were accepted on the synthetic round trip and gold references only, never on these clips (CLAUDE.md §3).
- **Not seen yet:** no post-fix analysis, view, question or answer of these clips exists at the freeze.
