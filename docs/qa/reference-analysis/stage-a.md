# Reference analysis · Stage A results

Measured with `keepframe eval-edits` (Task 2). The analysis code is the branch state after Task 1b (`cbb64f0`); later tasks re-run the same command and add an "after" section.

## Before · 2026-10-08

```sh
keepframe eval-edits --out eval/out/ra-before --renderer numpy      # --synthetic 6 is the default
```

- **Set:** six 640×360 × 60-frame synthetic references, seeds 1–6. Plates cycle gradient / flat / animated gradient / picture. Even seeds add a 100–140 px striped mover behind the title, and seeds 3 and 6 add a subtitle. Every scene has a static corner logo that always covers the same spot, plus a title in a named font (DejaVu / Liberation) that eases in and then holds still for about 75 % of the shot.
- **Options** (recorded in `metrics.json`): synthetic analysis `AnalyzeOptions(ocr=True, refine=False, generate_3d=False)`, RapidOCR, no captioner, so 0 VLM calls. Refine is off by ruling R13 (CPU refine costs about 17 min per scene here).
- **Renderer:** `numpy`. References are rendered by the numpy compositor and encoded as H.264 yuv444p crf 8, and edited scenes are rendered the same way. The edit agent's own verification still renders in Chromium.
- **Cost:** 398 s wall for six scenes (each analysis 44–59 s) at 1.7 GB peak RSS.
- **Raw numbers:** [`stage-a-before.json`](stage-a-before.json), the run's `metrics.json`.
- **Comparison rule:** an "after" run must use the same command, renderer and options. `--reuse` re-scores existing analyses without re-running them.

### Gates

| gate | threshold | mean | worst | failed / samples | pass |
|---|---|---|---|---|---|
| title_outside_glyph_delta | ≤ 1.0 | 1.379 | 3.785 | 4 / 6 | no |
| title_smear_score | ≤ 3.0 | 9.587 | 33.306 | 3 / 6 | no |
| title_glyph_de | < 5.0 | 14.314 | 32.109 | 4 / 6 | no |
| hide_plate_de | ≤ 2.0 | 10.404 | 27.061 | 7 / 10 | no |
| hide_hf_ratio | ∈ [0.5, 2.0] | 1.657 | 4.179 | 2 / 10 | no |
| hide_residue | ≤ 0.02 | 0.316 | 0.664 | 7 / 10 | no |
| recolour_halo_ring | < 2.0 | 30.166 | 59.537 | 6 / 6 | no |
| alpha_sad_edge | ≤ 0.03 | 0.300 | 0.426 | 6 / 6 | no |
| f_de_interior | < 3.0 | 2.787 | 5.561 | 2 / 6 | no |
| f_de_edge | < 6.0 | 15.853 | 26.409 | 6 / 6 | no |
| font_top3 | ≥ 0.9 | 0.375 | — | — / 8 | no |

Every sample must pass a gate. A sample is one scene, or one hidden element, or one title for font top-3, which is a hit rate. "—" means the check could not be measured, and that counts as a failure: for example, the edited title never showed on screen.

### Per scene

| seed | plate | s | matched | title whole | title ΔE | font top-3 | α SAD | F ΔE int / edge | bg leak | leak slope | plate ΔE under title / logo | title outside / smear / glyph | hide ΔE / hf / residue (truth ΔE) | halo | render L1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | gradient | 48 | 4/5 | yes | 23.49 | 0.00 | 0.324 | 2.35 / 22.33 | 0.662 | 0.49 | 15.56 / 19.36 | 3.79 / 2.57 / 23.50 (done) | e2: 8.61 / 1.20 / 0.386 (10.85) | 30.31 | 0.0115 |
| 2 | flat | 44 | 4/6 | yes | 0.63 | 1.00 | 0.174 | 5.56 / 7.28 | 0.0000 | 0.00 | 0.00 / 0.00 | 0.0000 / 0.02 / 1.30 (done) | e3: 0.13 / 0.98 / 0.000 (0.00); e1: 0.00 / 1.00 / 0.000 (0.00) | 7.09 | 0.0129 |
| 3 | animated | 59 | 4/6 | yes | 75.16 | 0.00 | 0.356 | 1.41 / 16.96 | 0.457 | 0.40 | 22.25 / 25.96 | — / — / — (done) | e5: 10.93 / 1.91 / 0.293 (21.26); e13: 27.06 / 1.31 / 0.664 (27.59) | 33.23 | 0.0592 |
| 4 | image | 51 | 5/6 | yes | 32.62 | 1.00 | 0.270 | 3.64 / 26.41 | 0.447 | 0.16 | 25.30 / 14.20 | 1.73 / 33.31 / 32.11 (done) | e6: 17.65 / 2.78 / 0.574 (23.08); e5: 7.79 / 1.16 / 0.354 (12.68) | 59.54 | 0.0259 |
| 5 | gradient | 48 | 3/5 | yes | 0.89 | 0.00 | 0.426 | 2.71 / 15.44 | 0.555 | 0.35 | 12.73 / 29.96 | 0.00 / 2.45 / 0.35 (done) | e6: 8.10 / 1.05 / 0.332 (11.66) | 34.76 | 0.0159 |
| 6 | flat | 48 | 6/7 | yes | 1.60 | 0.50 | 0.250 | 1.05 / 6.70 | 0.000 | 0.00 | 0.93 / 0.93 | — / — / — (done) | e19: 23.77 / 4.18 / 0.554 (25.12); e1: 0.00 / 1.00 / 0.000 (0.93) | 16.06 | 0.0307 |

The hide column names the analysed element: first the title, then the element paired with the logo. "Truth ΔE" compares the frame after hiding with the true plate.

### What the numbers say

- **Plates absorb what holds still.** The flat plates come out exact (0.0–0.9 ΔE). On gradient, animated and picture plates, the analysed plate is 13–25 ΔE off under the title and 14–30 ΔE off under the logo. After hiding the title, 29–57 % of the revealed pixels sit more than 10 ΔE from the surrounding ring (35–66 % under the logo).
- **Textures carry background.** On non-flat plates, 45–66 % of opaque texture pixels are the plate itself (`bg_leak_fraction`; leak slope 0.16–0.49). After a background replace, the 4-px ring around the true layers is 7–60 ΔE off the ideal recomposite.
- **Mattes are boxes.** The edge-band α error is 0.17–0.43 against the 0.03 gate, and foreground colour at edges is 7–26 ΔE off.
- **Titles are found but not owned.** All 8 titles come back as one text element with the right string, yet in seeds 3, 4 and 6 the held title is drawn by other layers (shapes reclassified from text, the mover). Retexting the text element there either does not show at the checked frames (3, 6) or leaves the old glyphs as a ghost (4, smear 33). The analysed title colour is 23–75 ΔE off in 3 of 6 scenes, and the true font is in the top 3 for 3 of 8 titles.
- **Pixel L1 hides all of this.** `render_l1` is 0.012–0.059.

### Calibration

- On the true plate, the ring fit predicts the hole under the title within 0–0.5 ΔE (`ring_floor` in the JSON; the picture plate is highest). `hide_plate_de ≤ 2` is therefore reachable.
- The double render recovers each layer's α within 1/255 and F within ΔE 0.5. The browser-marked test recomposes Chromium's own frame from its layers to a mean error under 0.3 levels.
- Constructed cases pin each metric (`tests/test_qa_metrics.py`, `tests/test_eval_edits.py`):
  - plate residue: clean < 0.5 vs smeared > 8;
  - binary crop leak: > 0.2 vs < 0.01;
  - leak slope: equals the mixed-in share;
  - white cut-out halo: > 10 vs < 1;
  - title edit: smear, outside-glyph and glyph-colour cases;
  - a title kept alive by a second layer fails;
  - a title edit that never shows fails.

## Real clips: not measured

Real clips were not run in Stage A. Analysing one `eval/clips` clip peaks at 7–9 GB RSS, and this shared host OOMs (user ruling, 2026-10-08). Clip mode is built and unit-tested on a 640×360 synthetic stand-in with a gold file. Clip analysis uses the default `AnalyzeOptions()` (refine on), the same as `gate-m2-real`. To measure on a machine with at least 12 GB free:

```sh
keepframe eval-edits --clips eval/clips --gold docs/qa/reference-analysis/gold --out eval/out/ra-clips \
  --synthetic 0 --renderer browser [--baseline BASELINE.json] [--strict]
```

`BASELINE.json` is a `gate-m2-real --render-check` result (`{"rows": [{"clip", "seconds", "render_l1"}]}`) or `{"<clip stem>": {"seconds", "render_l1"}}`. Clip gates are:

- title colour vs gold ΔE < 10;
- `render_l1` ≤ baseline + 0.005;
- seconds ≤ 1.5 × baseline;
- 0 VLM calls.

Title integrity (whole / duplicates / fragments) and leak metrics against the analysed plate are reported but not gated. Without a baseline, the two baseline gates read "no baseline". `--strict` counts them as failures.

## What each check does

On a temporary copy of the analysed project, at three frames (settled title, middle, last):

| Check | How |
|---|---|
| Title → "Fall Drop Sale" | Through the edit agent, overflow `expand_box`. Each frame is scored three ways: **outside-glyph delta** (how much worse the pixels in the title box, outside the old and new glyphs, match the source than the unedited rebuild did), **smear** (ΔE where the old glyphs were, against the plate estimated from the neighbouring non-glyph pixels) and **glyph ΔE** (median colour of the solid new glyphs vs the analysed colour). The check fails if the new title is not on screen. |
| Hide | Removes the title (or the largest sprite), and the logo's layer on synthetic scenes, then fits a ring 6–16 px around the hole. Reported: ΔE of the low-passed plate vs that fit (mean / p95), residue (share of pixels more than 10 ΔE off), fine-detail ratio inside vs ring, and ΔE vs the true plate on synthetic scenes. |
| Background replace #1a2a6c | Through the edit agent. Halo = mean ΔE on the 4-px ring outside α > 0.5, vs the true layers over the new colour (synthetic) or the source with its plate swapped (clips). The `tint` check joins with Task 12 (`BACKGROUND_EDITS`). |
| Layers | Black/white double render of each layer (truth and analysis): α = 1 − mean(I_w − I_b)/255, F = I_b/α. Truth and analysed layers are paired by title text, then by α IoU. Reported: edge-band (3 px) α SAD/MSE, F ΔE interior/edge, `bg_leak_fraction` (α > 0.5 pixels within ΔE 5 of the plate) and leak slope. |

With ground truth, pixels that other *true* layers cover are excluded, so an analysed layer that still holds a hidden or retexted element's pixels counts against the check. On clips, the other analysed layers are excluded instead.
