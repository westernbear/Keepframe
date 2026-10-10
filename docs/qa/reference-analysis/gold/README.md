# Gold titles for `keepframe eval-edits` (Stage A)

One file per clip in `eval/clips` (first 150 frames, the range `eval-edits` analyses):
`{"titles": [{"text", "color", "role", "frame", "box": [x0, y0, x1, y1], "points": [[x, y], ...]}]}`.
`role` is `title` (settled, read by the eval's colour and integrity checks) or `kinetic` (large moving text; integrity is reported, colour is informative).

| Clip | Frame | Role | Text | Colour |
|---|---|---|---|---|
| envato1 | 120 | title | Build SaaS Promo | #f0ebfe |
| envato1 | 60 | kinetic | Build | #beacee (vertical white→violet fill; sampled where the plate is dark) |
| ig1 | 90 | title | Every day, ideas are born | #333138 |
| ig1 | 90 | title | inside your walls. | #313237 |
| ig2 | 120 | title | A Weekend Away | #ab0004 |
| ig2 | 30 | kinetic | Weekend | #fb0000 (only "Week" in frame; red→pink fade) |
| ig3 | 30 | title | You just speak | #14018a ("speak" still fading in) |
| ig3 | 115 | title | Your idea | #18325c ("idea" fading in) |

How the values were made (2026-10-08): boxes from RapidOCR on the source frame (envato1 "Build" and ig2 "Weekend" by eye, OCR returns single letters). Colour = per-channel median of the 60 glyph pixels listed in `points`: pixels inside the box whose CIELAB ΔE76 from the median of a 12 px ring around the box exceeds 25 (40 for "Build"), eroded 5 px (7 px), kept at or above the median ΔE, then 60 evenly spaced picks. `contact.jpg` shows each box, its points and the colour swatch.
