# Eval clips: read → measurement → IR → render (CLAUDE.md §1)

**Clips.** Downloaded 2026-10-13 into `eval/clips/` (not committed) from the sources in `docs/superpowers/plans/2026-10-03-keepframe-core-flow.md`:
- ig1–ig3 with `uvx yt-dlp`, no login needed;
- envato1 with `curl`.

Size and fps match `docs/qa/core-flow/README.md`:

| Clip | Size | fps | Source frames |
|---|---|---|---|
| envato1 | 2560×1440 | 59.94 | 3186 |
| ig1 | 1916×1080 | 30 | 3691 |
| ig2 | 1920×1080 | 60 | 900 |
| ig3 | 1920×1080 | 30 | 642 |

**Window.** The gate's window: source frames 0–149, i.e. 2.5 s for envato1 and ig2, 5 s for ig1 and ig3.

**Order of work.**
1. The read and the measurement below were written and committed **before any IR of these clips was opened**. The gate (`keepframe gate-m2-real … --render-check`, commit `78e9bc9`) was already running in the background, but its output was not looked at.
2. Sheets: `eval/out/clips-read/` (not committed). Every 6 frames over f0–149, then 1–2-frame steps around the events the measurement found. Source frame numbers are burned in before `select`.
3. Measurement: `measure_clips.py` (scratch). For consecutive frames at 480 px wide it computes mean luma, the mean absolute difference, and the global shift (OpenCV phase correlation). Shifts are given in source px (×4 for the 1920-wide clips, ×5.33 for envato1).

## 1. Read (sheets, before the IR)

### envato1 — "Build SaaS Promo", dark background, purple/orange glow at the bottom
- **One shot f0–139, a hard cut at f140.**
- f0–8: empty dark frame with the glow.
- f9–37: a huge "Build" (white top → lavender bottom gradient) is revealed **letter by letter, left to right, each letter fading in from a blur**, while the word slides in from the right.
- f38–60: "Build" holds, almost still.
- f62–77: a "<" chevron enters from the right edge. "Build<" slides left, faster and faster, with growing motion blur.
- f78: the frame jumps to a zoomed-out view: "Build SaaS Promo" on one line, small and blurred.
- f84–102: the words come into focus one at a time (Build → SaaS → Promo), lavender.
- f114–126: the line turns white.
- f128–139: the line shrinks to about a third.
- f140–149: a new huge title "…ered fo…" (same gradient as "Build") enters blurred and sharpens while drifting left.
- Glow: rises (orange at the centre bottom, f84–90) and falls back (f120+).

### ig1 — "Every day, ideas are born inside your walls." (Instagram reel)
- **Two scenes joined by a circle wipe f18–26.**
- f0–17: white text on a dark-blue gradient. Word by word: "Every" (f0), "Every day," (f6).
- f18–26: a white dot appears between "Every" and "day," (f18) and grows into a circle that covers the frame (f25–26). The light scene shows inside it.
- f26–38: light scene, white with a blue gradient at the bottom, navy text. "Every day," plus "ideas" in a blurred blue highlight box. The box sharpens (f27) and fades (f28–35), leaving bold navy "ideas".
- f38–66: words appear one at a time: "are" (f38), "born", then line 2 "inside your" (f60), "walls." (f66). The text block re-centres as it grows.
- f56–66: UI cards fly in around the text, about eight of them: "Conversations" list, "Sara" chat, line chart, two handwritten sticky notes, "Research Projects" bar chart, "Prof. Shemiz" message.
- f96: the text swaps to "And"; "And every day," builds word by word f96–108.
- f126: "too"; "too many of them" builds word by word f126–138.
- f128–149: big blurred blue card shapes fly in over the scene.

### ig2 — "A Weekend Away" (Instagram reel, 60 fps)
- **Two scenes, a hard cut at f59.**
- f0–58: a huge red "A Weekend Away" scrolls left across a pink-to-white gradient (only 1–2 words visible at a time).
- f59–149: a pink globe on white, a few degrees of rotation visible.
- From f59, a small red title "A Weekend Away" types on letter by letter to the right of the globe's centre; it is complete about f84.
- From about f62, stickers and photos pop in around the globe: suitcase, "Lets Goooo" badge, landscape photos, palm island, plane, a green chip, a pin icon above.
- f90–149: the composition holds, with small drifts.

### ig3 — "You just speak … Your idea … and it builts for you" (Instagram reel)
- **Hard cut at f57; whip pans at f87–122 and f123–149.**
- f0–35: on white, purple "You just speak" fades in word by word, letters left to right.
- f36: "speak" glitches ("sp)ak").
- f42: "speak" is replaced by a small mic icon.
- f48–56: a cursor moves onto the mic, which gets a lavender ring.
- f57: hard cut to a close-up (about ×2.5): navy "just" and a white pill with a mic button.
- f58–68: click. A blue disc grows from the cursor and fades.
- f69–90: waveform bars animate in the pill. f96: the bars are gone.
- f87–122: whip pan left (pill out, "Your idea" in, motion blur at the peak).
- f108–122: "Your idea" letters go from light blue to navy, left to right.
- f123–149: whip pan left again. "and it builts for you" fades in word by word, light blue → black. The string really reads "builts".

## 2. Measurement (source frames, simple CV)

| Clip | Event | Measured |
|---|---|---|
| envato1 | "Build" entrance | content shifts left f9–37; speed −79 → 0 px/f, decaying (**ease-out**); total ≈ −640 px |
| envato1 | slide out | f39–77, speed 0 → −120 px/f, growing (**ease-in**); total ≈ −960 px. Difference spikes f60–78 (motion blur) |
| envato1 | cut | **f140** (difference 0.193, 27× the median); the read's "f140" agrees |
| envato1 | brightness | mean luma 0.04 (f0) → 0.20 (f149) |
| ig1 | circle wipe | difference spikes f23–26 (peak **f25**, 0.373); luma 0.03 → 0.81 across the wipe |
| ig1 | text drift | f1–23: −470 px (word-by-word growth of a centred line); f26–35: −176 px |
| ig2 | scroll | f1–57, speed −259 → −28 px/f (f17–21) → −268 px/f (f57): **fast, slow, fast**; total ≈ −4 090 px |
| ig2 | cut | **f59** (difference 0.206) |
| ig2 | globe scene | f63–104: content drifts left ≈ −544 px (the title and stickers; the read said "holds, small drifts") |
| ig3 | cut | **f57** (an isolated spike, 0.048) |
| ig3 | whip pan 1 | f84–122: speed peaks **f102** (−222 px/f), symmetric in-out; total ≈ −970 px |
| ig3 | whip pan 2 | f123–149: peaks f128 (−290 px/f) and f133 (−127 px/f); total ≈ −1 300 px |

Read vs measurement:

| Clip | Read | Measurement | Owner |
|---|---|---|---|
| envato1 | "Build" revealed in place, then holds | it slides in, decelerating (ease-out), while revealing | measurement (direction, timing) |
| ig2 | globe scene "holds, small drifts" | content drifts ≈ 544 px left over f63–104 | measurement (magnitude) |
| ig3 | f57 "zoom in (camera push)" at 6 frames | one-frame change: a cut | measurement (timing) |

## 3. IR and render

*(added after the gate finished; see below)*
