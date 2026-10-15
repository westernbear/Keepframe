# Pre-registration: brief vs kf-script on the real eval clips, after B2/B5/B8/B9

**Status: frozen** (2026-10-15). The commit that adds this file is the freeze.

At that commit, the read boxes, the question code and the decision rule are fixed, and no analysis of the eval clips exists at any commit carrying B2, B5, B8 or B9. The result is reported whatever it is.

The decision rule is the user's (2026-10-15): **if kf-script fails it, the agent's default view reverts to `brief`.**

## Question

The first real-clip A/B (`ab-real.md`) found kf-script non-inferior after the `fixes2.md` fixes. Does that still hold after the fixes in `fixes3.md`? Those fixes change what both views show:
- texts that change mid-track (B2);
- title pieces joined, by merging or as one group (B5a, B5a2);
- soft glows moved into the background video, and fading titles kept out of it (B8a, B8a2, B8b, B8b2);
- fonts borrowed by text past the per-scene work limit (B9, B9b).

Both views come from the same `scene.json` of each scene.

## Test set

- **Clips:** the four eval clips (ig1, ig2, ig3, envato1), source frames 0–149, exactly as `ab-real-prereg.md`.
- **Analysis command:** `keepframe gate-m2-real --clips eval/clips --out eval/out/clips-after2 --max-frames 150 --render-check`, one clip at a time, with stage caches pruned after each clip (disk).
- **Analysis code:** `b6d3a4a`, the head once every `fixes3.md` row was decided (the last code change is `7f18152`, B5a2). The freeze is this file's commit, a child of `b6d3a4a` with no code change. The same run is `clips.md` §5.
- **Units:** every scene the analysis makes (`clip:sN`); both views per scene.
- **Exclusions,** decided now:
  - A scene is dropped from both views only if its analysis crashes.
  - A read role whose box holds no IR element gets no question; such roles are listed in the report.

## Questions (unchanged since `ab-real-prereg.md`)

1. `scripts/ab_views.py real-gold --root eval/out/clips-after2 --boxes docs/qa/kfscript/ab-real/read_boxes.json --out docs/qa/kfscript/ab-real2`
2. `scripts/ab_views.py packets --root eval/out/clips-after2 --out docs/qa/kfscript/ab-real2`

Everything else is as in `ab-real-prereg.md`:
- the categories: auto, motion, read, edit;
- the gold for each;
- the 34 read roles and their boxes (`ab-real/read_boxes.json`, measured on source frames);
- Jaccard scoring for reads;
- the edit rule;
- packets that never name the view and carry the production SYSTEM prompt.

`scripts/ab_views.py` has not changed since that freeze.

## Answerer protocol

As before:
- one fresh Claude subagent (Sonnet) per packet;
- it reads only its packet and writes `answers/<hash>.json`;
- compliance is checked from each run's tool-use count;
- one attempt per packet; a missing or invalid answer scores 0.

The answerers are new and have not seen the first A/B.

## Analysis and decision rule

Command: `scripts/ab_views.py score --dir docs/qa/kfscript/ab-real2 --by-scene`.

The statistic is the paired difference per question (kf-script − brief), with a 95 % bootstrap: 10 000 resamples, numpy seed 0, resampling scenes. The clip-clustered interval is reported next to it.

- **Keep** (`keep_kfscript_default`): comprehension (auto + motion + read) lower bound **> −10 points**, and edit-target accuracy lower bound **> −10 points**. The rule is unchanged.
- **Outcome:**
  - *Keep holds:* the default stays `kfscript`.
  - *Otherwise:* `_scene_summary` (`keepframe/session/agent.py`) defaults to `brief`, `KEEPFRAME_AGENT_VIEW=kfscript` opts in, and the tests and docs flip.
  - No re-run of this test either way.
- **Also reported, not deciding:**
  - superiority (`h1`);
  - the clip-clustered intervals;
  - per-category and per-scene accuracies;
  - view sizes;
  - the change from the first A/B per category.

**Power.** It is as in the first test. Its scene half-widths were about 3–4 points, but the scenes may differ now (B2 adds elements, B8 removes sprites).

## Before the freeze (disclosed)

- **The first test is known.** Its packets, answers and result (`ab-real.md`, non-inferior) were seen. This is the second test on the same four clips, at new analysis code. Testing twice on the same clips raises the chance of a false "keep". The margin stays as registered, and both results are reported.
- **IR seen.** The eval-clip IR at `5ce591a` (`eval/out/clips-after`) was inspected for `clips.md` §4. Its defect list motivated B2, B5, B8 and B9. The fixes were accepted on the synthetic round trip and gold references only (`fixes3.md`, CLAUDE.md §3). Two numbers were measured on eval-clip IR for the record only, after the rules were fixed:
  - B5a: the velocity difference of ig2's and envato1's split title pieces. The 0.1 s step had already been chosen from the synthetic big_scroll.
  - B8b: none.
  No threshold was set from them. The B5a, B8a and B8b revisions (B5a2, B8a2, B8b2) were made on the synthetic round trip and the e24 gold only.
- **Not seen yet:** no analysis, view, question or answer of these clips at any commit carrying the `fixes3.md` changes exists at the freeze.
