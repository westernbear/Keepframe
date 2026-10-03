# Core-flow evaluation

Run `keepframe gate-m2-real --clips eval/clips --out eval/out/<name> --max-frames 150`, replacing `<name>` with the evaluation run name.

Baseline (2026-10-03, commit 6b5732f, CPU only — no GPU/torch so sprite refine skipped, RapidOCR on CPU):

| clip | size | frames | live_action | elements | kinds | mean_l1 | low_conf | keep_on/constraints | seconds | messages |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| envato1 | 2560×1440 @59.94 | 150 | — | 67 | text 40, sprite 27 | 0.0848 | 55 | 0/7327 | 1412 | — |
| ig1 | 1916×1080 @30 | 150 | — | 103 | text 76, sprite 27 | 0.0597 | 80 | 0/36155 | 1616 | — |
| ig2 | 1920×1080 @60 | 150 | — | 46 | text 38, sprite 8 | 0.0798 | 36 | 0/3208 | 1405 | — |
| ig3 | 1920×1080 @30 | 150 | — | 84 | text 33, sprite 51 | 0.0176 | 39 | 0/10878 | 630 | — |

Clips: envato1 is a watermarked Envato preview ("Build" kinetic type over a dark purple gradient); ig1–ig3 are Instagram reels (kinetic typography over gradient/white backgrounds, ig2 a globe with a moving label). Clips live in `eval/clips/` and are not committed.

Observations:
- Fragmentation dominates: a few words become 33–76 text tracks and 8–51 sprites; most elements score below 0.7 confidence.
- Predicate explosion: 3k–36k predicates per 5 s window, none kept (keep defaulted off before Task 2).
- Only ig3 (white background, solid type) meets the M2 reconstruction target (mean L1 ≤ 0.02); gradient backgrounds (envato1, ig1, ig2) are 3–4× worse.
- Cost: 10–27 min per 150 frames on 4 CPU cores (OCR every frame ≈ 7 min at 2560×1440; ECC sprite fitting ≈ 6 min).

