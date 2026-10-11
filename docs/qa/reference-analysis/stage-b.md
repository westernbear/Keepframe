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
