# B2, B5, B8, B9: the remaining real-clip defects, one comparison each

The plan is `/root/.claude/plans/progress-cryptic-wilkinson.md`, summarised here. These four defects were left open by `fixes2.md` (master `a9972e2`):

| Id | Defect |
|---|---|
| B2 | A text that changes mid-track |
| B5 | Title pieces not joined |
| B8 | Ghost titles and glow sprites in a background video |
| B9 | Fonts past the work cap kept as sans-serif |

**Rules:**
- One change per comparison.
- **Accept rule** (as `fixes2.md`):
  - parity ≤ 0.001;
  - the target metric moves;
  - L1 kf within +0.001 on every case;
  - no shot F1 drop;
  - every other change explained.
- **Gold:**
  - the mk571 row is recorded;
  - e24's `e13` keeps its 23-glyph typing.
- No threshold is fitted to the eval clips.

**Machine:** cloud container, 4 CPU, no GPU.

**Runs:**
- Every run analyses afresh at the named commit (`eval/out/rt3-after.sh <name>`).
- Each run reuses the renders of `rt2-base3`.
- Each JSON in `fixes3/` holds the per-case before/after values, the e24 glyph texts and the flags.

## Measuring stick (`467024d`, `2915676`)

**Four new kinds** in `kfscript/synth.py`. Each is a generic pattern seeded per run, and each shows one defect:

| Kind | Pattern | Defect |
|---|---|---|
| `word_swap` | A three-word line built word by word. After a while its last word is replaced in place by a round icon. | B2 |
| `big_scroll` | A huge, tracked-out headline crossing the frame fast, slow, fast. Its letters come out of a blur one by one as it enters; the whole line blurs as it leaves. | B5 |
| `glow_title` | A title blurring in and fading out over a dark gradient, with a pulsing radial glow. The background is a video. | B8 |
| `label_grid` | 24 cells: a bold heading in font F over a regular label in font G. That is more texts than the scene's font budget. | B9 |

**Four new metrics** in `kfscript/diff.py` and `qa/roundtrip.py`:

| Metric | What it measures |
|---|---|
| `font_rate` | Share of matched texts whose family is the truth's |
| `units_mean` | Fragments per truth element, with grouped members counted once |
| `stray_area` | Frame share of IR elements that match nothing and lie inside no truth box |
| `ghost_de` | Mean ΔE of the background video inside the truth text boxes, every 4th frame |

**Reuse check.** The 12 older cases and mk571 were re-measured from `rt2-b1b3`'s analyses. Every older metric is identical except chat_ui, which is render noise:
- its L1 kf moved by −0.00016 in one run;
- in the next run its compose render moved by +0.00016 and its parity went from 0.000001 to 0.000166.

That is inside the ±0.0002 noise floor and under the parity gate. The same check moved counter's page render by 5e-7.

**big_scroll template change.** The first `big_scroll` (a plain headline) did not show B5:
- OCR read one box at a time (`'d Work'`);
- its other pieces were blurred sprites before and after that box.

On the eval clips, the pieces sit side by side, overlap in time and start at different frames:
- ig2: `'eel'` f8–49 next to `'e'` f22–45;
- envato1: `'B'` f11–68 next to `'sull'` f18–40.

The template was changed, not any rule (`2915676`). The headline is now tracked out (0.15 em) and its letters blur in one after another. It now reads as `'Wc'` f24–67 next to `'ork'` f36–77, unmatched. Two variants were tried and set aside:
- a slower stagger alone still gave one box;
- 400 px type split too, but less cleanly.

### Baseline (`rt2-base3`, analyses at `467024d`; big_scroll at `2915676`)

| Case | Defect | Baseline |
|---|---|---|
| word_swap-1 | B2 | `short` and `icon` unmatched (matched 0.33). `full` lives 42 frames too long. Text 0.50. |
| big_scroll-1 | B5 | Matched 0, fragments 2, units 2, pieces 35, text 0, stray 0.135, L1 kf 0.0745 |
| glow_title-1 | B8 | Ghost ΔE 0.76, stray 0.266 (glow sprites e1 and e2), title in 3 pieces |
| hard_cut-1 | B8 | Ghost ΔE 0.56 (0.83 before `fb73b58`, see below), stray 0.768 |
| label_grid-1 | B9 | Font rate 0.46. Nine "font not matched (scene work cap reached); kept sans-serif" messages. |
| mk571 (gold) | — | L1 kf 0.0187, matched 0.80 |
| e24 (gold) | — | `e13` 23 glyphs, "Makemynextlaunchhappen." |

`fixes3/base.json` holds the full metrics.

**`ghost_de` corrected (`fb73b58`).** A truth text's box is held outside the text's life. So in a multi-shot case, `ghost_de` also scored the boxes of texts from other shots, which never appear in the scene. On hard_cut, after B8b put the glow into the video, this read the glow's own shading inside two second-shot boxes as residue (sub2 1.23 → 2.96). The metric now measures only the texts living in the scene's frames. Every run's `ghost_de` was re-measured from its saved background video (`metrics.orig.json` keeps the first value). Only hard_cut changes: 0.83 → 0.56 at baseline.

## Fixes

| Id | Commit | Change | Target before → after | Other changes | Decision |
|---|---|---|---|---|---|
| B9 | `911604e` | Past the work cap, a text borrows the family and weight of its nearest matched style twin (cap height within 1.25×, fill within ΔE 20, stroke ratio within `RATIO_TOL`), at its own size and confidence ≤ 0.5. One text per style is matched first. | label_grid font rate 0.458 → **0.479**. All 11 capped texts borrowed, so no text keeps sans-serif. But the 16 px labels' own matches spread over nine families, so nearest-twin borrowing mostly copied a wrong guess (Unbounded 6, Geist 4, Poppins 4). | label_grid L1 kf 0.0194 → 0.0192. Nothing else moved. e24 `e13` keeps 23 glyphs. | **accepted** (the target moved, no flag); the weak effect leads to B9b |
| B2 | `6aafdba` | A track whose whole read was seen for ≥ 3 frames, then reads only its first words for ≥ 0.5 s, splits in two. The pieces share a `cut`, so the title merge keeps them apart. | word_swap matched 0.33 → **1.00**, text 0.50 → **1.00**, `full` visible error 42 → **0**. `short` and `icon` are both matched with visible error 0. L1 kf 0.0032 → 0.0018. | scroller L1 kf 0.0679 → **0.0621**: "STEPS CLEAR" (f0–89) became "STEPS CLEAR" f0–6 + "STEPS" f7–89, because "CLEAR" scrolls out while "STEPS" stays (the read does change). Pieces 5.62 → 5.75. Nothing else moved. e24 keeps 23 glyphs. | **accepted** |
| B8a | `ad56ed5` | Each text's box is kept out of the background video, interpolated across OCR gaps, and for 0.3 s before and after its reads. | Ghost ΔE: glow_title 0.756 → **0.666** (the blur-in no longer leaves the title in the video), but hard_cut 0.561 → **0.884**. hard_cut's "bigger team" pops in at f6. In the pad frames f0–8, the still plate's polynomial fill replaced the true background (residue 0.5 → 1.6 there, unchanged from f12). | L1 kf unchanged everywhere. e24 keeps 23 glyphs. | **rejected as is** (the target moved both ways); revised by B8a2 |
| B8b | `8a635ee` | When the background is a video, an object track or mover that is soft light in place (median box ≥ 4 % of the frame, centre within half its diagonal, outline gradient < 1 ΔE/px) stays in the video and becomes no sprite. | Stray area: hard_cut 0.768 → **0.031**, glow_title 0.266 → **0.053**. L1 kf: hard_cut 0.0063 → 0.0056, glow_title 0.0092 → 0.0059. glow_title pieces 3 → 2. | Ghost ΔE hard_cut 0.884 → 1.285. All of it falls in B8a's pad frames f0–8 (a still patch now against a live glow); from f12 the runs are equal. glow_title 0.666 → 0.684. **e24 gold fails:** its prompt bar, a soft surface holding the typed prompt, went into the video. The prompt's glyph timing, measured against the plate behind it, was lost (`e13` 23 glyphs → none; "A brighterway to think,create and" gained 29). | **rejected as is** (gold); revised by B8b2 |
| B5a | `936ecf2` | Side-by-side pieces join when their left edges, right edges or centres keep one velocity over 0.1 s steps; before, it was centres only, per frame. | big_scroll matched 0 → **1**, units 2 → **1**, text 0 → 0.27, L1 kf 0.0745 → **0.0583** ("Wc" + "ork" became one title, "Wor!"). | mk571 text 0.92 → 1.00, L1 kf 0.0187 → 0.0180. **scroller L1 kf 0.0621 → 0.0770 (flag)**, though its matches rose 0.50 → 0.88 and its reconstruction improved 0.0575 → 0.0439. Its lines, "STEPS" + "CLEAR", are one line in the truth and now merged. But the merge re-read them as "STEPS CLEEAR" and placed one stretched across the frame at f0. The guard compares raster stills, so it kept them. | **rejected as is** (L1 bound); revised by B5a2 |
| B9b | `55c3b9e` | A text past the work cap takes the family most of its matched twins got (ties: summed confidence), from the nearest twin with that family. | label_grid font rate 0.479 → **0.604** (6 more labels in their true family) | label_grid L1 kf +0.0002 (noise). Nothing else moved. e24 as in the B5a run. | **accepted** |
| B8b2 | `43164f9` | A soft region that holds half a text box or more on a common frame is that text's surface, so it stays a sprite. | **e24 gold restored:** `e13` "Makemynextlaunchhappen." 23 glyphs again; the spurious 29-glyph line is gone. | No synthetic case moved: glow_title's and hard_cut's glows hold no text and still go to the video (stray 0.053, 0.031). | **accepted** |
| B8a2 | `2464546` | In the 0.3 s pads around a text's reads, its box is kept out of the video only while something shows in it: ≥ 2 % of the box more than ΔE76 8 off the still plate. Keeping out only the changed pixels was tried first; it left a patchwork (glow_title 0.86) and was dropped before any run. | Ghost ΔE hard_cut 1.285 → **0.602** (baseline 0.561); glow_title stays 0.685 (baseline 0.756) | Nothing else moved. e24 keeps 23 glyphs. | **accepted** |
| B5a2 | `7f18152` | Pieces merge only when the test before B5a joins them. A group that only B5a's steps or edges joined is bound as one `text_fragments` group around its merges and pieces. | Units: big_scroll 2 → **1** ("Wc" + "ork" one group), scroller 1.50 → **1.12** | Against the B8a2 run, which still carried B5a's merges: scroller L1 kf 0.0770 → 0.0621 (B5a's regression gone), but **big_scroll 0.0583 → 0.0745 (flag)**, matched 1 → 0, because its pieces are no longer merged. Against the series without B5a, both render exactly as before (big_scroll 0.0745, scroller 0.0621) and only the units changed. mk571's B5a gain (text 1.00) stays: its pieces join under the old test. e24 keeps 23 glyphs. | **accepted**, with the flag recorded: it is B5a's merge gain given back, not a regression against the code without B5a. No rule was found that keeps big_scroll's merge but not scroller's misread (big_scroll's merged read "Wor!" was wrong too). |

## Net effect (`final-vs-base.json`: `rt2-base3` → `rt3-b5a2`, analyses at `7f18152`)

No flags against the baseline: parity, L1 kf on every case, and shot F1 all hold. Gold: mk571 L1 kf 0.0187 → 0.0180 and text 0.92 → 1.00; e24's `e13` keeps its 23-glyph typing.

| Defect | Case | Baseline → final |
|---|---|---|
| B2 (a text that changes) | word_swap | matched 0.33 → **1.00**, text 0.50 → **1.00**, visible error 42 → **0**, L1 kf 0.0032 → 0.0018 |
| | scroller | L1 kf 0.0679 → **0.0621** ("CLEAR" scrolls out of "STEPS CLEAR") |
| B5 (title pieces) | big_scroll | units 2 → **1** (grouped, not merged), L1 unchanged |
| | scroller | units 1.50 → **1.12** |
| B8 (glows, ghosts) | glow_title | stray area 0.266 → **0.053**, ghost ΔE 0.756 → **0.685**, L1 kf 0.0092 → **0.0059**, pieces 3 → 2 |
| | hard_cut | stray area 0.768 → **0.031**, L1 kf 0.0063 → **0.0056**, ghost ΔE 0.561 → 0.602 (+0.04) |
| B9 (fonts past the cap) | label_grid | font rate 0.458 → **0.604**; no capped text keeps sans-serif |

**Not done, with reasons:**
- B5b, B5c and B5d, as planned. There is still no `merge_failed` anywhere.
- A merged title's string re-read stays as it is. That re-read is where B5a broke.
- The title guard compares raster stills, not the live-text render the pages use. That is why it kept scroller's bad merges. Upgrade path: guard on the rendered text.
