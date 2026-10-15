# Real-clip A/B 2: brief vs kf-script view, after B2/B5/B8/B9

**Pre-registration:** `ab-real2-prereg.md`, frozen at `686e914`, before any analysis of these clips at a `fixes3.md` commit.

**Decision:** the keep rule **fails**. The edit interval's lower bound is −10.34 against a margin of −10. So the agent's default view **reverts to `brief`** (`keepframe/session/agent.py`; `KEEPFRAME_AGENT_VIEW=kfscript` opts in). Comprehension, separately, favours kf-script and clears zero.

## Run

| | |
|---|---|
| Analysis | `keepframe gate-m2-real --max-frames 150 --render-check`, one clip at a time (`eval/out/clips-after2.sh`), at `686e914` (code as `b6d3a4a`; last code change `7f18152`). All four clips analysed without error. |
| Questions | `real-gold` and `packets` with the frozen commands. The question code is unchanged since `b8780ce`. |
| Scenes (units) | 6, the same split as the first A/B: envato1 `s1`; ig1 `s1` (f0–25) and `s2` (f26–149); ig2 `s1` (f0–58) and `s2` (f59–149); ig3 `s1` |
| Questions per view | 121: 91 comprehension (auto 47, motion 20, read 24) and 30 edit, as before |
| Answerers | 12 fresh Sonnet subagents, one per packet, new to the test. Each made 3 or 4 tool calls (one packet read, sometimes in two parts, and one write), so all 12 complied. No answer was missing or invalid. |

**Read roles without a question:** 10 of 34, the same count as the first A/B. These roles have no IR element ≥ 60 % inside their box.

## Result

| Category | brief | kf-script | first A/B (brief / kf) |
|---|---|---|---|
| auto | 95.7 | **100.0** | 95.7 / 100.0 |
| motion | 75.0 | 75.0 | 75.0 / 70.0 |
| read (Jaccard) | 39.6 | **43.7** | 40.3 / 40.7 |
| **comprehension** | 76.4 | **79.7** | 76.6 / 77.8 |
| **edit target** | 66.7 | 66.7 | 63.3 / 66.7 |

| Interval (kf − brief, points, 95 %, 10 000 resamples, seed 0) | Scenes (pre-registered) | Clips (reported) |
|---|---|---|
| comprehension | **+3.28 [+0.80, +6.03]** | +3.28 [+1.13, +7.46] |
| comprehension without motion | +4.21 [+1.06, +7.73] | +4.21 [+1.47, +9.24] |
| edit | **0.00 [−10.34, +6.82]** | 0.00 [−12.50, +6.67] |

- **Keep rule:** both lower bounds must exceed −10. Comprehension holds; edit fails, by 0.34 points. The default reverts to `brief`, as registered, and there is no re-run.
- **Superiority (h1, comprehension lower bound > 0):** it **holds** this time, unlike the first A/B.
- **Why the edit bound fails:**
  - Only 2 of the 30 paired edit questions differ, one each way: ig1 `s2` Q23 favours kf-script, ig3 `s1` Q20 favours the brief.
  - So the mean difference is 0. But with six small clusters, the scene bootstrap puts the lower bound just past the margin.
  - The first A/B's edit interval was [0.00, +6.98].
- **14 of the 121 paired questions differ; 9 favour kf-script:**
  - auto: 2 for kf (the largest text in envato1 and ig3);
  - motion: 1 each way in ig2 `s2`;
  - read: kf 5, brief 3, in ig1 `s2`, ig2 `s2`, ig3 and envato1;
  - edit: 1 each way.

Per scene (brief / kf-script accuracy; view size in characters):

| Scene | auto | motion | read | edit | brief chars | kf chars |
|---|---|---|---|---|---|---|
| envato1 s1 | 88 / 100 | 67 / 67 | 42 / 40 | 80 / 80 | 6 888 | 47 900 |
| ig1 s1 | 100 / 100 | 67 / 67 | 100 / 100 | 100 / 100 | 1 033 | 7 716 |
| ig1 s2 | 100 / 100 | 100 / 100 | 30 / 33 | 36 / 45 | 10 972 | 48 001 |
| ig2 s1 | 100 / 100 | 67 / 67 | 18 / 18 | 100 / 100 | 7 474 | 47 815 |
| ig2 s2 | 100 / 100 | 80 / 80 | 67 / 78 | 75 / 75 | 3 582 | 28 746 |
| ig3 s1 | 88 / 100 | 67 / 67 | 33 / 41 | 83 / 67 | 6 746 | 48 054 |

## Caveats

- **The rule is mechanical, and it was applied as registered.** By point estimates the two views are level on edits, and kf-script leads on comprehension. The decision turns on a bootstrap bound 0.34 points past the margin, with six clusters and 30 edit questions. A larger test (more clips) would settle whether kf-script is non-inferior on edits. This one cannot.
- **Second test on the same clips.** The first result was known at the freeze (disclosed in the pre-registration).
- **Few clusters.** Six scenes from four clips. The clip-clustered interval is wider and gives the same decision.
- **Budget.** Four of the six kf-script views still reach the 48 000-character budget.
- **Proxy model.** Sonnet stands in for the production model, as in every earlier test.
