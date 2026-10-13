# A/B test: brief vs kf-script view, 32 fresh round-trip clips

**Result: A/B success.** Both pre-registered conditions hold (`ab-test-prereg.md`, frozen at `ab13953`), so the session agent's default scene view is now `kfscript` (`keepframe/session/agent.py`; `KEEPFRAME_AGENT_VIEW=brief` opts out, and a view error still falls back to the brief).

Order, from git:
1. Freeze `ab13953` (10:58).
2. Analysis of seeds 2–5 from a worktree pinned at the freeze.
3. Packets and questions `ab9b2a9` (11:46), before any answer.
4. Answers `72e181a` and this commit.
5. Scoring: `scripts/ab_views.py score --dir docs/qa/kfscript/ab-test` → `ab-test/results.json`.

No clip was excluded: none crashed, and the largest `kf_parity` was 0.0003, under the 0.001 limit.

## Protocol as run

- **Packets.** 64 packets: 32 clips (8 round-trip kinds × seeds 2–5) × 2 views, with 524 questions per view.
- **Answerers.** One fresh Sonnet subagent per packet, told to read only its packet file and write only its answer file. Nothing else was asked of them.
- **Compliance.** Checked from each run's tool calls:
  - 61 runs: one Read and one Write.
  - 3 runs (`b62c7da95240`, `ea67b874c8aa`, `f8628dd710cb`): two Reads of their own packet (offset/limit), then one Write.
  - No run opened another path.
- **Answers.** All 64 files are valid JSON and answer every question.
- **Deviation from the pre-registration text, disclosed.** The frozen generator emits read and edit questions only for clips with a truth element whose role is unique (`synth_gold`). The pre-registration says "one timing edit per clip". block_transition, counter and scroller have no unique role, so they have no read or edit questions. H2 therefore rests on 20 clips (76 edit questions), not 32. The generator was not changed.

## Decision

| | brief | kf-script | kf − brief, 95 % CI (clip bootstrap) | Rule | Holds |
|---|---|---|---|---|---|
| **H1** comprehension (auto + motion + read), n = 448 | 86.1 | **93.8** | **+7.73 [+4.72, +10.88]** | lower bound > 0 | **yes** |
| **H2** edit target, n = 76 (20 clips) | 88.2 | **94.7** | **+6.58 [0.00, +13.95]** | lower bound > −10 | **yes** |
| old question set (auto + read), n = 300 | | | +6.87 [+3.80, +10.10] | reported | — |
| P1 rule (comp ≥ brief + 10 and edit ≥ brief) | | | +7.7, edit ≥ | reported | not met |

The effect is near the dev smoke run's +3.75 [−0.81, +7.94] and P2's confirmation run's +6.2 [−4.7, +18.5]. Unlike both, the interval now excludes zero.

## Per category

| Category | n | brief | kf-script | kf right, brief wrong | brief right, kf wrong |
|---|---|---|---|---|---|
| auto | 244 | 92.2 | 99.6 | 19 | 1 |
| motion | 148 | 84.5 | 93.9 | 16 | 2 |
| read (Jaccard) | 56 | 63.4 | 68.1 | 8 | 0 |
| edit | 76 | 88.2 | 94.7 | 5 | 0 |

By question type (both views, all clips):

| Question | n | brief | kf-script |
|---|---|---|---|
| end of the last movement | 32 | 65.6 | **96.9** |
| centre at a held frame (±8 px) | 27 | 74.1 | **100** |
| ease of the biggest horizontal move | 16 | 62.5 | **100** |
| ease of the biggest vertical move | 14 | 85.7 | **100** |
| duration of the biggest horizontal move | 14 | 35.7 | **64.3** |
| duration of the biggest vertical move | 16 | 68.8 | 75.0 |
| opacity at a held frame | 27 | 92.6 | 100 |
| visibility, entrance time, appearance order, z order, largest text and its colour, glyph typing | 246 | 100 | 100 |

kf-script's gain is in motion detail: when a move ends, where an element is at a given time, and how a move eases. On structure (what is there, when it appears, its order and text) the fixed brief is as good. Durations stay weak for both views (see block_transition-4 below).

## Per kind and per clip

| Kind | auto b/kf | motion b/kf | read b/kf | edit b/kf |
|---|---|---|---|---|
| block_transition | 97/100 | 85/95 | – | – |
| chat_ui | 94/100 | 75/80 | 65/65 | 67/67 |
| counter | 89/96 | 100/100 | – | – |
| kinetic_title | 100/100 | 88/100 | 29/31 | 100/100 |
| logo_sting | 81/100 | 75/80 | 48/63 | 75/100 |
| prompt_typing | 97/100 | 89/100 | 87/87 | 100/100 |
| scroller | 100/100 | 93/100 | – | – |
| stepped_zoom | 77/100 | 74/100 | 75/75 | 100/100 |

| Clip | comp brief | comp kf | Δ | edit brief | edit kf |
|---|---|---|---|---|---|
| block_transition-2 | 92.3 | 100 | +7.7 | – | – |
| block_transition-3 | 84.6 | 100 | +15.4 | – | – |
| block_transition-4 | 100 | 92.3 | **−7.7** | – | – |
| block_transition-5 | 92.3 | 100 | +7.7 | – | – |
| chat_ui-2 | 78.9 | 85.6 | +6.7 | 33.3 | 33.3 |
| chat_ui-3 | 85.6 | 85.6 | 0 | 33.3 | 33.3 |
| chat_ui-4 | 80.0 | 93.3 | +13.3 | 100 | 100 |
| chat_ui-5 | 90.0 | 90.0 | 0 | 100 | 100 |
| counter-2 | 100 | 91.7 | **−8.3** | – | – |
| counter-3 | 91.7 | 100 | +8.3 | – | – |
| counter-4 | 91.7 | 100 | +8.3 | – | – |
| counter-5 | 91.7 | 100 | +8.3 | – | – |
| kinetic_title-2 | 85.4 | 85.4 | 0 | 100 | 100 |
| kinetic_title-3 | 93.7 | 93.7 | 0 | 100 | 100 |
| kinetic_title-4 | 87.1 | 94.6 | +7.5 | 100 | 100 |
| kinetic_title-5 | 77.6 | 85.7 | +8.0 | 100 | 100 |
| logo_sting-2 | 68.5 | 81.4 | +12.9 | 60 | 100 |
| logo_sting-3 | 70.4 | 87.1 | +16.7 | 100 | 100 |
| logo_sting-4 | 85.3 | 89.7 | +4.4 | 60 | 100 |
| logo_sting-5 | 62.3 | 83.8 | +21.6 | 80 | 100 |
| prompt_typing-2 | 97.2 | 97.2 | 0 | 100 | 100 |
| prompt_typing-3 | 96.9 | 96.9 | 0 | 100 | 100 |
| prompt_typing-4 | 85.2 | 96.3 | +11.1 | 100 | 100 |
| prompt_typing-5 | 88.9 | 94.4 | +5.6 | 100 | 100 |
| scroller-2 | 92.3 | 100 | +7.7 | – | – |
| scroller-3 | 100 | 100 | 0 | – | – |
| scroller-4 | 100 | 100 | 0 | – | – |
| scroller-5 | 100 | 100 | 0 | – | – |
| stepped_zoom-2 | 90.0 | 100 | +10.0 | 100 | 100 |
| stepped_zoom-3 | 66.7 | 100 | **+33.3** | 100 | 100 |
| stepped_zoom-4 | 65.4 | 96.2 | **+30.8** | 100 | 100 |
| stepped_zoom-5 | 82.1 | 96.4 | +14.3 | 100 | 100 |

## Where the views disagree most: read → measurement → render (CLAUDE.md §1)

These clips are synthetic, so the template's truth (`truth.json`) is the read's owner of truth. The contact sheets (`eval/out/rt-test/sheets/`) were read **after** the packets had been seen, because the test had to stay blind until scoring. That order is disclosed here. The golds come from the IR, so these questions test whether a view conveys the IR. Whether the IR is right is the round trip's job; the rows below answer both.

| Clip | Read (truth + sheet) | Measurement (IR) | Views | Render vs source |
|---|---|---|---|---|
| stepped_zoom-3 (+33) | "Orbit" in steps of scale 2.4 → 0.5 (f10) → 1.0 (f20); two "FASTER LESS" labels from f24 | `sx` 1 → 0.204 (f10) → 0.414 (f21), uniform truth 0.208 / 0.417. **`sy` 0.164 / 0.316, not uniform**; second step **f21, not f20**; labels read as "FASTERLESS" | **brief: e1 "static"**. `extract_motions` drops runs shorter than 2 frames (`verify/matrix._runs`), so a step vanishes. kf prints the keys, and all four e1 questions went to kf | L1 kf 0.0206; f28–f88 0.0247: "Orbit" is drawn squashed (the `sy` error) |
| stepped_zoom-4 (+31) | same template | same pattern (e2) | same: the brief says static, kf is right on all four | L1 kf 0.0264 |
| logo_sting-5 (+22) | centre mask reveal; a square purple frame that widens; logo = white "P" on a purple disc (pop); name; url drifts down 20 px | matched 0.6; pop **missed**, drift found. The logo is purple "P" text (e7, e10) with **no disc**, plus a 1-frame sprite e9. The square frame at f48 is missing (only the wide e8 from f72). f16 reveal: half the frame white vs a narrow slit | brief: ease "in_cubic" for a linear move, e13's end 3.00 vs 3.07 s, centre off by 16 px, logo = e6 (a sprite before the logo). kf: logo e7 + e10 (Jaccard 0.67), edit target right | L1 kf 0.0324; f16 0.46 (the mask), f48–f148 ≈ 0.008 |
| counter-2 (**−8**) | "125K" pops in with a small overshoot | e26 `sy` 1 → 1.0122 (f53) → 1.0244 (f119): two 1 px steps, together exactly the 2 px tolerance | kf showed the slow 2.17 s `sy` drift (the view shows a drift once it adds up to more than the tolerance; here it equals the tolerance and float rounding tips it over). The reader called it movement, ending 3.97 s; gold 1.83 s (per-segment rule) | L1 kf 0.0200 |
| block_transition-4 (**−8**) | "busywork Plan" slides right out of a block wipe | e2 `x` 491 → 611 over f35–f38 in two keys (0.067 s + 0.033 s) | "biggest horizontal move duration": gold 0.1 s (the whole run). The kf reader took one key segment (0.033 s); the brief prints the run | L1 kf 0.0339; matched 0.33 (the block transition is unsupported) |

Disagreements, not resolved here:
- **Brief.** It drops one-frame steps (`_runs`, runs < 2 frames), so stepped motion reads as "static".
- **View vs gold.** The view's drift-adds-up rule and the gold's per-segment rule split on a change that equals the tolerance.
- **Duration questions.** A "move" is a run of same-direction keys. kf-script readers must merge the keyframes themselves, and the brief states the run.
- **IR (stepped_zoom).** Non-uniform scale and the second step one frame late.
- **IR (logo_sting).** The logo disc and the early square frame are missing, and the logo glyph takes the disc colour.

## Limits

- The answers come from a proxy model (Sonnet subagents); the production model has no credentials in this container. When it does, `scripts/ab_views.py run` re-asks the same packets.
- The clips are synthetic round-trip templates, not the eval clips (ig1–ig3, envato1), which are still to be uploaded.
- **Cost.** The kf-script view is longer. The median is 19 228 characters against the brief's 2 777 (×6.9); the largest is 47 690 against 8 122.
- **Rule.** Any later confirmation uses seeds ≥ 6 (pre-registered); seeds 2–5 are not re-run.
