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
| hard_cut-1 | B8 | Ghost ΔE 0.83, stray 0.768 |
| label_grid-1 | B9 | Font rate 0.46. Nine "font not matched (scene work cap reached); kept sans-serif" messages. |
| mk571 (gold) | — | L1 kf 0.0187, matched 0.80 |
| e24 (gold) | — | `e13` 23 glyphs, "Makemynextlaunchhappen." |

`fixes3/base.json` holds the full metrics.

## Fixes

| Id | Commit | Change | Target before → after | Other changes | Decision |
|---|---|---|---|---|---|
