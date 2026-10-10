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
| hide_plate_de | ≤ 2.0 | 10.713 | 27.061 | 15 / 19 | no |
| hide_hf_ratio | ∈ [0.5, 2.0] | 2.139 | 5.734 | 7 / 19 | no |
| hide_residue | ≤ 0.02 | 0.331 | 0.843 | 14 / 19 | no |
| hide_truth_de | ≤ 2.0 + ring floor | 13.337 | 27.587 | 14 / 19 | no |
| plate_de_title | ≤ 2.0 + ring floor | 12.796 | 25.304 | 4 / 6 | no |
| plate_de_logo | ≤ 2.0 + ring floor | 15.068 | 29.964 | 4 / 6 | no |
| recolour_halo_ring | < 2.0 | 30.166 | 59.537 | 6 / 6 | no |
| alpha_sad_edge | ≤ 0.03 | 0.480 | 0.672 | 6 / 6 | no |
| f_de_interior | < 3.0 | 2.787 | 5.561 | 2 / 6 | no |
| f_de_edge | < 6.0 | 15.853 | 26.409 | 6 / 6 | no |
| font_top3 | ≥ 0.9 | 0.375 | — | — / 8 | no |

Every sample must pass a gate. A sample is one scene, one hide (the title, the logo's layer, and every true layer the analysis missed), or one title for font top-3, which is a hit rate.

- **Unmeasured fails.** "—" means the check could not be measured — for example, the edited title never showed on screen. A None or NaN sample fails, and a gate with no samples fails.
- **Exact-truth gates.** `hide_truth_de`, `plate_de_title` and `plate_de_logo` compare with the true plate, at ≤ 2.0 + that scene's ring floor.
- **Missed layers in the α gate.** `alpha_sad_edge` counts missed true layers at full error (SAD 1 over their band).

### Per scene

| seed | plate | s | matched | title whole | title ΔE | font top-3 | α SAD | F ΔE int / edge | bg leak | leak slope | plate ΔE under title / logo | title outside / smear / glyph | hide ΔE / hf / residue (truth ΔE) | halo | render L1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | gradient | 48 | 4/5 | yes | 23.49 | 0.00 | 0.469 | 2.35 / 22.33 | 0.662 | 0.61 | 15.56 / 19.36 | 3.79 / 2.57 / 23.50 (done) | e2: 8.61 / 1.20 / 0.386 (10.85); missed logo: 14.04 / 3.00 / 0.636 (14.91) | 30.31 | 0.0115 |
| 2 | flat | 44 | 4/6 | yes | 0.63 | 1.00 | 0.417 | 5.56 / 7.28 | 0.0000 | — | 0.00 / 0.00 | 0.0000 / 0.02 / 1.30 (done) | e3: 0.13 / 0.98 / 0.000 (0.00); e1: 0.00 / 1.00 / 0.000 (0.00); missed mover: 1.19 / 1.10 / 0.000 (0.97); missed s3: 3.24 / 1.45 / 0.0016 (1.31) | 7.09 | 0.0129 |
| 3 | animated | 59 | 4/6 | yes | 75.16 | 0.00 | 0.571 | 1.41 / 16.96 | 0.457 | 0.45 | 22.25 / 25.96 | — / — / — (done) | e5: 10.93 / 1.91 / 0.293 (21.26); e13: 27.06 / 1.31 / 0.664 (27.59); missed s1: 17.16 / 2.27 / 0.465 (19.50); missed s3: 5.92 / 1.12 / 0.267 (18.52) | 33.23 | 0.0592 |
| 4 | image | 51 | 5/6 | yes | 32.62 | 1.00 | 0.392 | 3.64 / 26.41 | 0.447 | 0.16 | 25.30 / 14.20 | 1.73 / 33.31 / 32.11 (done) | e6: 17.65 / 2.78 / 0.574 (23.08); e5: 7.79 / 1.16 / 0.354 (12.68); missed mover: 8.35 / 4.13 / 0.249 (12.51) | 59.54 | 0.0259 |
| 5 | gradient | 48 | 3/5 | yes | 0.89 | 0.00 | 0.672 | 2.71 / 15.44 | 0.555 | 0.35 | 12.73 / 29.96 | 0.00 / 2.45 / 0.35 (done) | e6: 8.10 / 1.05 / 0.332 (11.66); missed s2: 12.99 / 1.97 / 0.370 (13.69); missed logo: 26.24 / 3.28 / 0.843 (26.02) | 34.76 | 0.0159 |
| 6 | flat | 48 | 6/7 | yes | 1.60 | 0.50 | 0.362 | 1.05 / 6.70 | 0.000 | — | 0.93 / 0.93 | — / — / — (done) | e19: 23.77 / 4.18 / 0.554 (25.12); e1: 0.00 / 1.00 / 0.000 (0.93); missed mover: 10.37 / 5.73 / 0.305 (12.80) | 16.06 | 0.0307 |

The hide column names the analysed element: first the title, then the element paired with the logo. "missed X" is a true layer with no analysed pair; it is scored where it was with nothing removed, so whatever the analysis baked there counts. "Truth ΔE" compares the frame after hiding with the true plate.

### What the numbers say

- **Plates absorb what holds still.** The flat plates come out exact (0.0–0.9 ΔE). On gradient, animated and picture plates, the analysed plate is 13–25 ΔE off under the title and 14–30 ΔE off under the logo; `plate_de_title` and `plate_de_logo` fail in 4 of 6 scenes. After hiding the title, five of six scenes leave 29–57 % of the revealed pixels more than 10 ΔE from the surrounding ring, and 11–25 ΔE from the true plate.
- **Layers go missing and get baked.** The analysis has no layer for 1–2 true layers per scene: the logo in seeds 1 and 5, the mover in 2, 4 and 6, a sprite in 2, 3 and 5. Where such a layer was baked into the plate or other layers, the frame there is 12.5–26 ΔE from the true plate. On seed 2's flat plate the missed layers just vanish (1.0–1.3 ΔE), so they fail only in the α gate.
- **Textures carry background.** On non-flat plates, 45–66 % of opaque texture pixels are the plate itself (`bg_leak_fraction`; leak slope 0.16–0.61). After a background replace, the 4-px ring around the true layers is 7–60 ΔE off the ideal recomposite.
- **Mattes are boxes.** The edge-band α error is 0.36–0.67 against the 0.03 gate, with missed layers at full error (0.17–0.43 over matched layers only). Foreground colour at edges is 7–26 ΔE off.
- **Titles are found but not owned.** All 8 titles come back as one text element with the right string, yet in seeds 3, 4 and 6 the held title is drawn by other layers (shapes reclassified from text, the mover). Retexting the text element there either does not show at the checked frames (3, 6) or leaves the old glyphs as a ghost (4, smear 33). The analysed title colour is 23–75 ΔE off in 3 of 6 scenes, and the true font is in the top 3 for 3 of 8 titles.
- **Pixel L1 hides all of this.** `render_l1` is 0.012–0.059.

### Calibration

- On the true plate, the ring fit predicts the hole under the title within 0–0.5 ΔE (`ring_floor` in the JSON; the picture plate is highest). `hide_plate_de ≤ 2` is therefore reachable, and the exact-truth gates allow 2.0 on top of that floor.
- The double render recovers each layer's α within 1/255 and F within ΔE 0.5. The browser-marked test recomposes Chromium's own frame from its layers to a mean error under 0.3 levels.
- Constructed cases pin each metric (`tests/test_qa_metrics.py`, `tests/test_eval_edits.py`):
  - plate residue: clean < 0.5 vs smeared > 8;
  - binary crop leak: > 0.2 vs < 0.01;
  - leak slope: equals the mixed-in share;
  - white cut-out halo: > 10 vs < 1;
  - title edit: smear, outside-glyph and glyph-colour cases;
  - a title kept alive by a second layer fails;
  - a title edit that never shows fails;
  - a missed layer baked into the plate fails the hide check and counts at full α error;
  - unmeasurable metrics return None;
  - gate aggregation fails on None / NaN / no samples, and `passed` is false when no gate ran.

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
| Hide | Removes the title (or the largest sprite), and the logo's layer on synthetic scenes, then fits a ring 6–16 px around the hole. On synthetic scenes, every true layer with no analysed pair is also scored at its true mask, with nothing removed. Reported: ΔE of the low-passed plate vs that fit (mean / p95), residue (share of pixels more than 10 ΔE off), fine-detail ratio inside vs ring, and ΔE vs the true plate on synthetic scenes. Frames where the element shows nowhere are skipped; a shown frame that cannot be measured voids the check. |
| Background replace #1a2a6c | Through the edit agent. Halo = mean ΔE on the 4-px ring outside α > 0.5, vs the true layers over the new colour (synthetic) or the source with its plate swapped (clips). Tint is not measured by eval-edits: since 2026-10-09 it is an explicit mode the LLM agent chooses (`mode: "tint"`), not a choice Keepframe makes (`BACKGROUND_EDITS` holds the replace only). |
| Layers | Black/white double render of each layer (truth and analysis): α = 1 − mean(I_w − I_b)/255, F = I_b/α. Truth and analysed layers are paired by title text, then by α IoU. Reported: edge-band (3 px) α SAD/MSE (a true layer with no pair counts as SAD = MSE = 1), F ΔE interior/edge, `bg_leak_fraction` (α > 0.5 pixels within ΔE 5 of the plate) and leak slope. |

With ground truth, pixels that other *true* layers cover are excluded, so an analysed layer that still holds a hidden or retexted element's pixels counts against the check. On clips, the other analysed layers are excluded instead.

A metric with nothing to measure returns None, never 0. A check where any shown frame is unmeasurable reports None. `passed` is true only when at least one gate ran and none failed; with `--strict`, unevaluated gates also fail.
