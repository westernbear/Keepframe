# Reference analysis · Stage A results

Measured with `keepframe eval-edits` (Task 2). The analysis code is the branch state after Task 1b (`cbb64f0`); the "After" section below re-runs it on master (2026-10-10).

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

## After · 2026-10-10

Measured on the `home` machine: WSL2 Ubuntu 24.04 with 25 GB RAM, 16 cores (Ryzen 9 7950X3D) and an RTX 4070 SUPER (torch 2.14.0+cu130, so refine runs on the GPU; OCR runs on the CPU). Python packages are pinned to this repo's venv. Every before/after pair ran back to back on that machine.

Headless Chromium under WSLg needs `DISPLAY` and `WAYLAND_DISPLAY` unset. Otherwise it waits up to about 100 s on the WSLg Wayland socket, and Playwright's 30 s timeouts fire at random. The runs here unset both.

### Synthetic

```sh
keepframe eval-edits --out eval/out/ra-after --renderer numpy --synthetic 8
```

- **Seeds 1–6** are the before set. Seeds 7 and 8 (ruling R26) add a striped disc that spins a full turn in place behind the title, on an animated and on a picture plate.
- **Before** is the same command at `996ad67` (Task 2) with the R26 seeds patched in. On seeds 1–6 it reproduces the 2026-10-08 numbers to the digit.
- **After** is master `9aff0a8`. The text-fill fix (`696ebde`) leaves every synthetic number unchanged.
- **Raw numbers:** [`stage-a-before8.json`](stage-a-before8.json) and [`stage-a-after.json`](stage-a-after.json).

| seed | plate | mover | plate ΔE under title | under logo | halo | α SAD | bg leak | title whole | render L1 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | gradient | — | 15.6 → 0.1 | 19.4 → 0.0 | 30.3 → 7.0 | 0.469 → 0.334 | 0.662 → 0.000 | yes → yes | 0.0115 → 0.0067 |
| 2 | flat | crossing | 0.0 → 0.0 | 0.0 → 0.0 | 7.7 → 7.1 | 0.443 → 0.519 | 0.000 → 0.000 | yes → yes | 0.0153 → 0.0131 |
| 3 | animated | — | 22.2 → 0.0 | 26.0 → 0.2 | 32.4 → 7.8 | 0.686 → 0.264 | 0.020 → 0.000 | yes/yes → yes/yes | 0.0791 → 0.0062 |
| 4 | image | crossing | 25.3 → 1.5 | 14.2 → 3.1 | 59.5 → 11.6 | 0.392 → 0.431 | 0.447 → 0.000 | yes → no | 0.0259 → 0.0150 |
| 5 | gradient | — | 12.7 → 0.4 | 30.0 → 0.8 | 34.8 → 7.9 | 0.672 → 0.414 | 0.555 → 0.000 | yes → yes | 0.0159 → 0.0108 |
| 6 | flat | crossing | 0.9 → 0.9 | 0.9 → 0.9 | 16.1 → 9.5 | 0.362 → 0.542 | 0.000 → 0.001 | yes/yes → no/yes | 0.0307 → 0.0284 |
| 7 | animated | in place | 21.8 → 6.9 | 36.5 → 9.8 | 53.6 → 29.8 | 0.573 → 0.518 | 0.801 → 0.254 | yes → yes | 0.0365 → 0.0519 |
| 8 | image | in place | 22.8 → 3.7 | 29.7 → 3.2 | 38.5 → 17.9 | 0.523 → 0.751 | 0.469 → 0.135 | no → yes | 0.0334 → 0.0195 |

- **Plates no longer absorb what holds still.** The plate under the title drops from 13–25 ΔE to 0.0–1.5 on seeds 1–6, and under the logo from 14–30 to 0.0–3.1.
- **Textures no longer carry the plate.** The bg leak fraction goes from 0.45–0.66 to 0.000. Halo after a background replace goes from 30–60 to 7–12.
- **Remaining failures:**
  - **α SAD** is still 0.26–0.54, against the 0.03 gate.
  - **Seeds 4 and 6:** the title "New Arrivals" over the crossing mover comes back split by OCR (font set changed at Task 10). Merging the fragments is Stage B work.
  - **In-place mover (seeds 7–8):** plate and halo are much better, but on the animated plate (seed 7) the mover is still not found. On the picture plate (seed 8) it is found, but it absorbs three sprites and the logo (α SAD 0.52 → 0.75).

### Real clips

```sh
keepframe gate-m2-real --clips eval/clips --out eval/out/ra-baseline --max-frames 150 --render-check   # at 12da25a
keepframe eval-edits --clips eval/clips --gold docs/qa/reference-analysis/gold --out eval/out/ra-clips \
  --synthetic 0 --renderer browser --baseline baseline.json --strict                                  # at master
```

- **Before:** [`baseline.json`](baseline.json), from master before Stage A (`12da25a`, the D7 baseline). Each clip analysis peaked at 6.2 GB RSS.
- **After:** master with the text-fill fix (`696ebde`). Raw numbers are in [`stage-a-clips-fix.json`](stage-a-clips-fix.json); the merged Stage A without the fix is in [`stage-a-clips.json`](stage-a-clips.json). Peak RSS was 13.6 GB for the whole run.

| clip | analysis s (before → after) | render L1 (before → after) | title colour ΔE vs gold (merged → fixed) | titles whole |
|---|---|---|---|---|
| envato1 | 411 → 514 (×1.25) | 0.0794 → 0.0673 | 92.60 → 7.18 | 1/2 |
| ig1 | 618 → 665 (×1.08) | 0.0458 → 0.0341 | 0.94, 1.78 → 0.94, 1.78 | 2/2 |
| ig2 | 250 → 288 (×1.15) | 0.0725 → 0.0793 | 0.79 → 0.79 | 1/2 |
| ig3 | 170 → 184 (×1.09) | 0.0226 → 0.0224 | —, 5.54 → —, 5.54 | 1/2 |

Clip gates with the fix:

| gate | result |
|---|---|
| seconds ≤ 1.5 × before | pass, 4/4 (×1.08–1.25) |
| `render_l1` ≤ before + 0.005 | 3/4. ig2 fails by 0.0018 (0.0793 vs 0.0775) |
| title colour ΔE < 10 | 5/6. ig3 "You just speak" is unmeasured because OCR splits it into "You just", "ust" and "st" |
| VLM calls | 0 |

Other checks on master:

- `gate-m1`: 20/20.
- `gate-m2 --n 20`: frame L1 20/20, tracking ok, temporal mean 0.720, so it passes.
- The whole pytest suite, every marker: 2683 passed and 236 skipped. One test failed only because node was missing on that machine, and it passes once node is installed.
- Node tests: 133/133.

Sheets (source | before Stage A | after Stage A with the fix, frames 75/120/149): [envato1](stage-a-envato1.jpg), [ig1](stage-a-ig1.jpg), [ig2](stage-a-ig2.jpg), [ig3](stage-a-ig3.jpg).

**ig2 is the ig2demo source.**

- **Fixed:** the title reads dark red, ΔE 0.79 from gold #ab0004, where Stage A's motivating bug gave #fb9d9d. It is the right size, and the red smear is gone. Retexting it to "Fall Drop Sale" leaves no outside-glyph change and a smear score of 4.0.
- **Still wrong:** the globe does not become its own layer; it rebuilds as a pale still shape. The small stickers, the suitcase and the plane are missing, both before and after. The leak against the analysed plate is 0.214. These are why `render_l1` rises.

**envato1.**

- **Text-fill bug, fixed:** the merged Stage A drew all eight light-on-black texts near-black. The matte kept the black around the glyphs, and the plate estimate under them was a purple haze. The text-fill fix (R66/R67) uses the pre-Stage-A core colour to break that tie, on the texture's own evidence.
- **Still wrong:** the colours are right now, but the matched fonts are not. A condensed display face and a slab face are matched where the source uses a geometric sans.

**ig1.** Better than before: the title matches its font, and the plate behind the cards is cleaner.

**ig3.**

- **Fixed:** the colours are right, including "and it builts for you", which the merged Stage A drew pale blue because it took the line's minority colour.
- **Still wrong:** the fonts. A serif is matched for a sans, and "Your idea" also gets a false 14° shear and a black shadow.

**Follow-ups from the real clips** (not fixed here):
- Font matching on real text picks the wrong family, and on ig3 also a false shear and shadow.
- A mover that turns in place on a picture plate absorbs neighbouring layers (seed 8).
- The ig2 globe is not its own layer, and ig2 `render_l1` is 0.0018 over its gate.
- OCR-split titles (ig3, seeds 4 and 6) belong to Stage B.

The clip gates are those of `eval-edits` (title colour vs gold ΔE < 10, `render_l1` ≤ baseline + 0.005, seconds ≤ 1.5 × baseline, 0 VLM calls). Title integrity and leak against the analysed plate are reported but not gated. `BASELINE.json` may be a `gate-m2-real --render-check` result (`{"rows": [...]}`) or `{"<clip stem>": {"seconds", "render_l1"}}`.

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
