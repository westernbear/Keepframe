# Reference analysis · Stage B results

## OCR models

Task 1 switched RapidOCR (`rapidocr_onnxruntime` 1.4.4) from the bundled PP-OCRv4 det/rec models to the PP-OCRv5 mobile ones (RapidAI/RapidOCR tag v3.9.2 on ModelScope, pinned by sha256) and measured v4 against v5. **Decision: revert to v4.** The switch is commit `23d72d8`, reverted by the next commit; the numbers below are the record.

- **Files.** `ch_PP-OCRv5_det_mobile.onnx` (sha256 `4d97c44a…78ae`, 4.8 MB) and `ch_PP-OCRv5_rec_mobile.onnx` (sha256 `5825fc7e…42c5`, 16.6 MB). Both hashes matched the pins. The v5 rec ONNX carries its character list as `character` metadata, so no `rec_keys_path` was needed.
- **Machine.** `home` (WSL2, CPU OCR, 4 workers). Every v4/v5 pair ran back to back, twice; the results were identical both times.

### Real clips (OCR-only probe)

For each clip (150 frames): `ocr_frames` → `merge_reveals(track_text)` → `split_junk_text`, then `title_integrity` against the gold titles. Title colour ΔE is `text_props` on the matched track against the gold colour. The probe is scratch (`~/kfx/rb-t1/probe.py` on home), not in the repo. Run with `KEEPFRAME_OCR_MODELS=v4|v5`.

| clip | gold title | v4 whole / fragments | v5 whole / fragments | v4 ΔE | v5 ΔE | OCR s v4 / v5 |
|---|---|---|---|---|---|---|
| envato1 | Build SaaS Promo | yes / 3 | no (ratio .48) / 3 | 6.96 | – | 53.9 / 48.9 |
| ig1 | Every day, ideas are born | yes / 2 | no (.57) / 8 | 1.29 | – | 67.3 / 60.5 |
| ig1 | inside your walls. | yes / 0 | no (.50) / 4 | 2.81 | – | |
| ig2 | A Weekend Away | yes / 4 | yes / 0 | 3.18 | 8.25 | 53.5 / 48.2 |
| ig3 | You just speak | no / 4 | no / 2 | – | – | 49.8 / 47.4 |
| ig3 | Your idea | yes / 1 | no (.62) / 1 | 0.79 | – | |
| **titles whole** | | **5 / 6** | **1 / 6** | | | mean 56.1 / 51.2 |
| **fragments (sum, title role)** | | **14** | **18** | | | |

(OCR seconds are the mean of the two runs; the "kinetic" gold rows, "Build" and "Weekend", are not in the title counts. v5 finds "Build" whole in envato1, v4 does not.)

### Synthetic (seeds 1–8, `eval-edits --renderer numpy --synthetic 8`)

Against `stage-a-follow-ups-synthetic.json` (v4, same code):

| | v4 | v5 |
|---|---|---|
| titles whole | 8 / 10 | 2 / 10 |
| scene `font_top3` hit rate (gate ≥ 0.9) | 0.6 | 0.0 (the titles that come back whole are not the font's top 3) |
| title colour ΔE (titles found) | 9.17, 5.89, 0.00, 2.69, 6.87, 1.33, 0.46, 0.54 | 2.15, 0.46 (6 of 8 seeds have no matched title) |
| analysis seconds per scene | 13–21 | 14–23 |

### Why

PP-OCRv5's detector splits a line at word gaps ("Summer" / "Sale", "Big" / "Launch", "Fresh" / "Picks"), where v4 returns the line as one box. That also shows in the rendered-title check (`test_v5_reads_rendered_titles`: 6 bundled-font titles at 44–56 px on flat and gradient plates, exact casefolded reads: v4 5, v5 3). Reveal merging and title assembly built for v4's line boxes then join fewer words: the clip titles drop from 5 to 1 whole and the synthetic ones from 8 to 2. Recognition per box is not worse (v5 reads "Summer" and "A Weekend Away" at 0.98–1.0 confidence); the loss is in the box granularity.

### Gate

| gate | rule | measured | holds |
|---|---|---|---|
| clip titles whole | v5 ≥ v4 | 1 vs 5 | no |
| clip fragments | v5 ≤ v4 + 1 | 18 vs 14 | no |
| synthetic titles whole | v5 ≥ v4 | 2 vs 8 | no |
| mean title colour ΔE | no worse by more than 1.0 | ig2, the only title v5 matches: 8.25 vs 3.18 (+5.1); the other four are unmatched | no |
| scene `font_top3` | not lower | 0.0 vs 0.6 | no |
| OCR seconds per clip | ≤ 1.3 × v4 | 0.91 × | yes |

Only the speed gate holds. v4 stays.

### Follow-up

If a later task merges adjacent same-line boxes before tracking (a word-gap line merge), v5 is worth measuring again with the same probe: its per-box reads are as good as v4's and it is about 9 % faster on the CPU. The model files, hashes and the `ocr_models()` fetch are in `23d72d8`.

## Font prior

Task 2 extended the matcher's sans prior (`SANS`, margin 0.05 in `_fit_kept`) from geometric, neo-grotesque and humanist to the two other sans categories. **Decision: add `rounded_sans`, leave `condensed_sans` out.** Condensed fails a gate.

The `real` rows below came from `font_guesses` on the `tests/data/text_fill` cases (`_analyse_real`) with `SANS` patched per column, on `home`; the other numbers from the eval.

| text_fill case | old prior | + condensed | + rounded | both |
|---|---|---|---|---|
| envato1 | Urbanist (geometric) | same | same | same |
| envato1_title | Outfit (geometric) | **Oswald (condensed)** | Outfit | Oswald |
| ig1_t24 | Onest (neo-grotesque) | same | same | same |
| ig2 | Urbanist (geometric) | **Barlow Condensed (condensed)** | Urbanist | Barlow Condensed |
| ig2_t21 | Noto Sans (humanist) | same | same | same |
| ig3 | Source Sans 3 (humanist) | **Oswald (condensed)** | Source Sans 3 | Oswald |
| ig3_t24 | Sora (geometric) | same | same | same |

Condensed turns `envato1_title` and `ig3` (both gate cases of `test_real_sans_titles_match_a_sans_unsheared_without_shadow`, whose category tuple stays geometric, neo-grotesque or humanist) into Oswald, and `ig2` into Barlow Condensed: a condensed face 0.05 behind a wide one was kept ahead of it, which the real titles' truth contradicts. Rounded moves no case.

| gate | rule | measured (rounded only) | holds |
|---|---|---|---|
| `font_set(60)` top-3 | ≥ 0.95 | 0.983 (59 / 60; the miss is seed 39, as in the follow-ups) | yes |
| real sans titles | category tuple unchanged | `test_real_sans_titles_match_a_sans_unsheared_without_shadow` passes | yes |
| ig1 gold titles | family stays in its category | "Every day, ideas are born" Onest, "inside your walls." Onest (neo-grotesque), original = old prior = new prior | yes |
| synthetic seeds 1–8 | identical to `stage-a-follow-ups-synthetic.json` | every row field identical (title whole, colour ΔE, font hits); font set top-3 / weight / size / tracking identical | yes |

The ig1 run is `rerun(root, "s1", "sprites")` on a copy of the follow-ups analysis (`~/kfx/rb-t2/ig1` on home). The only font-set difference is seed 41 (Zilla Slab): its top 3 is now Zilla Slab, Nunito, Lora instead of Zilla Slab, Lora, Merriweather, still a hit. The synthetic run is `~/kf-b/eval/out/rb-t2` on home; its rows are the seeds 1–8 identity reference for Task 6.

Rounded stays because it costs nothing on any measure and covers the bundled rounded faces (Fredoka, Comfortaa, Nunito, Quicksand) the same way as the other sans. The Bebas Neue → Jost swap seen on ig1 stays: the condensed fix that targeted it fails the fixture gate, and ig1 has no font truth to say which side is right.

## Synthetic seeds 9 and 10 (before merge)

Seed 9 reveals its title glyph by glyph (step 2-3 frames, ramp 4-6, opacity constant 1); seed 10 yaws it (ry -35 to -18, small rot, 60-72 px,
two-stop gradient fill, `Fade(90, 1 -> 0.3)`). Run: `eval-edits --synthetic 10 --renderer numpy`, home `~/kf-b/eval/out/rb-t6` (commit 677a2c7,
numpy renderer, v4 OCR). Seeds 1-8 rows equal `rb-t2/metrics.json` (seconds and sheet paths excluded).

| seed | title | whole | title colour ΔE | font top-3 | α SAD | F ΔE int / edge | matched | elements truth/analysed |
|---|---|---|---|---|---|---|---|---|
| 9 stagger | "A Weekend Away" (+ subtitle) | yes (e3), subtitle yes (ratio 0.92) | 0.54 (subtitle 16.2) | 0.5 | 0.455 | 3.17 / 5.59 | 4 | 6 / 14 |
| 10 plane | "New Arrivals" | yes (e4) | 94.1 | 0.0 | 0.498 | 1.34 / 13.6 | 3 | 5 / 13 |

Gate `title_whole`: 11/13 titles whole over seeds 1-10 (0.846 < 0.9, fails). Seed 9 title edit: smear 0.70, glyph ΔE 2.5 at frame 30 but not visible at 39 (stagger
unsettled). Seed 10 residue: s1/s3 unmatched, hide residue 0.6. The analysis splits both titles into pieces elsewhere (14 and 13 analysed
elements), which Stage B's merge, plane fit and glyph measurement target.

## Final results (merged to master 2026-10-12)

### Clips at bdc447d (`stage-b-clips-bdc447d.json`)

Run: home, `eval-edits --clips … --renderer browser --strict`. Code at bdc447d: all Stage B tasks, before the visual fix wave.

| Gate | Value | Result |
|---|---|---|
| `title_whole` | 6/8 (0.75) | Passes. ig1 restored; ig3 "You just speak" whole. |
| fragments | 19 → 15 (vs follow-ups) | |
| `render_l1` | 4/4 | Passes. |
| seconds | 4/4 | Passes. |
| `vlm_calls` | 0 | Passes. |
| `title_colour_de` | 1/6 fails | Fails on ig3: gradient fill, ΔE 11.74. |

Suite at bdc447d: 3041 passed / 1 skipped. Node 210/210. gate-m1 20/20. gate-m2 20/20 (temporal .720).

### Visual fix wave: what was kept

The contact sheets showed five defects that the gates do not catch, V1–V5.

The fix wave went through three rounds, and each round fixed one case and broke another. Its V1/V5 rules had become thresholds fitted to the eval's own clips and seeds. The user ruled to keep only the fixes that rest on a stated principle (2026-10-12):

| Kept | Principle | Effect |
|---|---|---|
| V4 (b137e76) | A text's per-frame opacity is read on pixels on its colour's line, so a sprite crossing its box does not count. | Seed 9's glyph title is fully opaque at f39. |
| V3 (3509d93) | rx/ry are kept only when the perspective is observable: convergence ≥ 0.8° on a cap ≥ d·tan 0.8°. An in-plane turn alone is carried by `rot`. | ig1's turned notes have no false 3D. Seed 10's ry stays within 3° of the truth. |
| V2, tilted part (bd2bd0a) | A tilted title's font job reads its binary rectified canon. The fill coverage has no plate under it to model a fade. | Seed 10's retext is whole (dx −149 → −1.8). |
| e0765c5 | A text group lists its longest-lived piece first, so a group retext lands on the title, not on a short piece. | |
| 9afa887 | A fitted pen more than 0.3 em left of the ink starts at the ink. | A retext is no longer clipped on the left (envato1 e35 dx −150 → ink). |

Reverted:

- V1: the canonical frame chosen by read length.
- V5: pieces folded inside a title, the dominance rule, the title-colour contrast.
- The review rounds' rules: prefix reveal, cut split, blob cut, the merged-title canonical frame and colour.
- V2's texture-alpha coverage for two-colour lines (c507033). Ablation: on seed 4 it turned Barlow Condensed (0.90, the truth) into Bebas Neue (0.35), because the coverage reads a crossing sprite as glyph.

### Synthetic at c507033 (`stage-b-synthetic.json`, vs bdc447d)

| Gate | bdc447d | c507033 |
|---|---|---|
| `title_whole` | 13/13 | 13/13 |
| `font_top3` | 0.769 | 0.769 |
| `title_smear` failures | 6 | 5 (seed 9 measured, 0.34) |
| `title_glyph_de` failures | 4 | 2 |
| `f_de_edge` failures | 3 | 4 (seed 9: 5.85 → 7.32) |
| `render_l1` | | better on seeds 9 and 10 |

Seeds 1 and 2 are identical. Seeds 3–10 move only through V4.

### Still open

- ig3 e1:
  - Frames whose read holds less than the line are squeezed into its box: typing in (f10), the word turning into a mic (f39–56), and a cut (f57–72).
  - The title colour ΔE is 11.74 against gold. The line has three colour zones, and the fill is a two-stop gradient.
- Seeds 4 and 6: a retext of the whole title leaves the separately tracked pieces (smear 20.1 and 7.8). Seed 4's title box shrinks over f55–59.
- Seed 7: smear 7.2.
- Seed 10: its texture has a hole where the mover crossed at cf.
- R11: tilt recall on short words.
- envato1 e35 at f120: the retext keeps the original's reveal fraction (0.94).
- The VLM paths are unmeasured (D1).
- Live AE check (user step): X/Y rotation sign and order, perspective against the default camera, the glyph wipe.

### Clips at the merged code

The clip eval and the suite are re-running on home at the merged code. The home WSL filesystem turned read-only mid-run twice on 2026-10-12.
