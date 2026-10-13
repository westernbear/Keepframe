# Pre-registration: brief vs kf-script view, fresh round-trip clips (seeds 2–5)

**Status: draft.** The freeze is the commit that changes this line to "frozen"; the test run uses exactly that commit's code, and that commit comes before any analysis, view, question or answer of the test clips exists. Only the decision rule's numbers below decide the outcome; the result is reported whatever it is (user decision, 2026-10-13: report and stop if it fails).

## Question

Does the kf-script view help the session agent understand an analysed motion-graphics scene better than the brief, without losing edit-target accuracy? Both views are generated from the same `scene.json`. The brief's three bugs (moving-axis ease, clearest-frame size, typing) are fixed (`80e9c33`), so kf-script is compared with a fair brief.

## Test set

- 32 clips: the 8 round-trip kinds × seeds 2, 3, 4, 5 (`keepframe/kfscript/synth.py`).
- Pages for these seeds were rendered and their contact sheets read for template defects before this commit (P2 practice). Two prompt_typing defects were fixed in `07fcde7` (line overflowing the bar; white-on-white button label). Nothing else about these clips has been looked at.
- Analysis is run from a git worktree pinned at the freeze commit, with `AnalyzeOptions(refine=False, generate_3d=False)` and no captioner. Disk allows about 8 cases of stage caches at once, so it runs seed by seed:
  1. `keepframe eval-roundtrip --out eval/out/rt-test --first-seed S --seeds 1 --reuse` for S = 2, 3, 4, 5;
  2. after each seed, delete its `stages/frames.npy`, `stages/*.pkl` and `analysis/sources/*.npy`;
  3. once more over all seeds, `keepframe eval-roundtrip --out eval/out/rt-test --first-seed 2 --seeds 4 --reuse`, which re-analyses nothing and writes the 32-case `metrics.json` the questions use.
- **Exclusions**, decided now: a clip is dropped from both views only if its analysis crashes or its `kf_parity` exceeds 0.001.

## Questions (generator frozen at this commit)

- `scripts/ab_views.py synth-gold --rt eval/out/rt-test --out docs/qa/kfscript/ab-test`, then `packets --root eval/out/rt-test/proj --out docs/qa/kfscript/ab-test`.
- **auto:** visibility at t, end of the last movement, glyph-by-glyph ids, ease of the biggest move, largest text and its colour, which is in front. These are P1's generator, gold from the IR.
- **motion:** entrance time (±2 f); which of two appears first; duration of the biggest move (±max(2 f, 10 %)); centre at a held frame (±8 px); opacity at that frame (±0.05). Gold from the IR.
- **read:** for each truth element whose role is unique in its clip, the IR ids inside it over its life. Each id belongs to the smallest truth element that contains it; scored by Jaccard.
- **edit:** one per unique-role element (a text → its text, a shape → its colour) plus one timing edit per clip. Scored correct when every answered id is in the gold set and the property matches.
- Packets never name the view and carry the production SYSTEM prompt.

## Answerer protocol

- One fresh Claude subagent (Sonnet) per packet, 64 packets in all.
- The packet text goes inline in the prompt, with instructions to use no tools and to answer with one JSON object. The subagent never sees file paths, `index.json` or the other view.
- One attempt per packet. The answer is the first `{…}` object in the reply; a missing or invalid answer scores 0.
- The production model (`gpt-6.1-sol`) has no credentials in this container, so Sonnet is a proxy, as in P1 and P2.

## Analysis and decision rule

`scripts/ab_views.py score --dir docs/qa/kfscript/ab-test`. The interval statistic is the paired difference per question (kf-script − brief), with a 95 % bootstrap that resamples clips: 10 000 resamples, numpy seed 0. Questions within a clip are correlated (Miller 2024, arXiv 2411.00640).

- **H1, primary.** Comprehension (auto + motion + read): lower bound of the kf − brief interval **> 0**.
- **H2, safety.** Edit-target accuracy: lower bound of the kf − brief interval **> −10 points** (non-inferiority margin, fixed now).
- **Decision:**
  - H1 and H2 hold → "A/B success": the session agent's default scene view becomes `kfscript` (`keepframe/session/agent.py`).
  - Otherwise the default stays `brief`, and the result is reported. **No re-run on seeds 2–5**; any later confirmation uses seeds ≥ 6.
- **Also reported, not deciding:**
  - comprehension on the P1/P2 question set alone (auto + read);
  - per-category and per-clip accuracies;
  - the P1 rule (point estimate ≥ +10 and edit ≥ brief).

Power (from the 8-clip confirmation run's clip-clustered SE of 6.0 points, scaled to 32 clips, SE ≈ 3.0): 38 % at a true +5, 76 % at +8, 92 % at +10, with false positives held at 2.5 %.

## Reporting

`docs/qa/kfscript/ab-test.md` will hold:
- the per-category and per-clip tables, the intervals and the decision;
- the raw answers (`ab-test/answers/`), questions and index;
- the read → measurement → render notes for the clips where the views disagree most.
