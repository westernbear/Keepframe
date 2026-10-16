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

## 3. IR and render (opened after section 1–2 were committed at `6ce4003`)

**Runs.**
- Gate: `keepframe gate-m2-real --clips eval/clips --out eval/out/clips-78e9bc9 --max-frames 150 --render-check` at `78e9bc9`. Cloud container: 4 CPU, 15 GB, no GPU, Chromium 1194. The analysis took 36 minutes for the four clips.
- kf-script fidelity: for each scene, `emit_page` + `kfscript.check.fidelity` (every 4th frame), as in the round trip.
- Rows: `clips.json`.

| Clip | Elements (sprite / text) | mean_l1 (recon) | render_l1 (compose, every 10th f) | **L1 kf** (every 4th f) | worst kf frame | kf_parity | view: full / summaries | s | Context: round 2 final mean / render (other code) |
|---|---|---|---|---|---|---|---|---|---|
| envato1 | 55 (47 / 8) | 0.0686 | 0.0683 | **0.0722** | f144 (0.148) | 0.000006 | 14 / 41 | 833 | 0.0674 / 0.0791 |
| ig1 | 101 (58 / 43) | 0.0305 | 0.0320 | **0.0379** | f24 (0.259) | 0.000005 | 8 / 93 | 548 | 0.0402 / 0.0455 |
| ig2 | 29 (16 / 13) | 0.0457 | 0.0514 | **0.0474** | f40 (0.131) | 0.000016 | 19 / 10 | 596 | 0.0646 / 0.0712 |
| ig3 | 63 (56 / 7) | 0.0164 | 0.0231 | **0.0245** | f112 (0.068) | 0.0000005 | 25 / 38 | 208 | 0.0183 / 0.0228 |

- Every clip analyses as one scene, `s1` covering f0–149. No shot boundary was found: not envato1's cut at f140, ig2's cut at f59, ig3's cut at f57, nor ig1's wipe.
- `kf_parity` ≤ 0.00002 everywhere (gate 0.001), so the kf-script page renders like compose.

**kf-script view on real clips.** All four views reach the 48 000-character budget (44 596–47 846 chars). Only 8–25 elements keep markup and motion; the rest are one-line summaries "motion not shown". The kept ones are the main titles:
- envato1: "B", "sull", "Build SaaSPromo", "ere"/"e"/"f";
- ig1: "Every day.", "Every day, ideas are born", "inside your walls.", "And every day,";
- ig2: "A Weekend Away";
- ig3: "You just speak", "Your idea", "and it builts for you".

The synthetic A/B clips had a median view of 19 228 chars, so the budget rarely bit there. **The A/B result does not cover clips this dense.** On ig1 the brief carries a motion summary for 40 elements; the kf-script view, for 8.

### envato1

| Item | Read | Measurement | IR | Render (kf page) |
|---|---|---|---|---|
| shots | one shot, cut at f140 | cut f140 (difference spike) | one scene. The new title starts at f141 as fragments "ere", "e", "f" (e44, e45, e49) | f144 is the worst frame (0.148): the pieces overlap and miss "ered fo" |
| "Build" | one word, letters revealed one by one with blur, sliding in | slides left, ease-out f9–37, still f38, accelerating left f39–77 | **fragmented**: text "B" (e4, f11–68), text "sull" (e8, OCR of "uild", f18–40) and about 10 letter-sized sprites. e4's `x` keys follow the measured profile: 974 → 578 decelerating f11–32, flat, then 578 → 194 accelerating. The brief prints it as "moves left 781px linear" | f48 (0.099): "B" text plus sprite pieces of "uild", a stray stroke; ghost "Build" in the background video at f0 |
| "<" chevron | enters f62, slides with "Build" | — | not identified as its own element (inside the moving sprites) | — |
| "Build SaaS Promo" | one line; word focus Build → SaaS → Promo f84–102; whitens; shrinks f128–139 | width 1447 → 913 px (f111 → f135) **about a fixed centre** (cx 1284) | e37 "Build SaaSPromo" (space lost) plus duplicates e38 "Saas" and e40 "Promo" (#7c758a: the blurred word). **The shrink is modelled as `x` +358 px to the right, `reveal` 1 → 0.75 and opacity 0.98 → 0.52**, with no scale track | f100 (0.035): "Build SaaSPr…" cut by the reveal; ghost "Build Saa" in the background video |
| glow | rises and falls; part of the background | luma 0.04 → 0.20 | background video **plus** about 8 near-full-frame gradient sprites (e1, e10, e13, e33, e53, e54) with rotations | glow shape differs (purple patches) |

### ig1

| Item | Read | Measurement | IR | Render |
|---|---|---|---|---|
| shots | dark scene, circle wipe f18–26, light scene | difference peak f25; luma 0.03 → 0.81 | one scene with a background video. **No element for the circle** | f24, the worst frame (0.259): the circle is missing |
| "Every day," (dark) | white, word by word | line grows and re-centres: −470 px f1–23 | e1 "Every day." (comma read as a period), **colour #010005 (black)** on a white box; the growth is modelled as moves left 679 px + scale ×0.48 | f0 (0.072): a white box with black condensed "EVERY…", where the source has white text on dark |
| "Every day, ideas are born / inside your walls." | words one at a time; "ideas" in a blue box that fades | — | e7 "Every day, ideas are born" (one string, from f25), e10 "ideas" #6677ea (the box, f28–32), e13 "inside your walls." (reveals left → right f55–60) | f48 (0.015): right string; bold "ideas" lost (one weight) |
| UI cards | about 8 cards fly in f56–66, then drift | — | OCR texts of the cards, with misreads: "hhhh" (the bar chart), "Seomm", "What if wecreatea", "ashboard". Sprites for the card bodies. Several cards are **split in time** (e18/e68 "2d ago", e22/e67, e31/e66: one card, two elements) | f100 (0.019): cards in place, handwriting re-typeset in a sans font |
| "And every day," / "too many of them" | built word by word f96–108 / f126–138 | — | e61 and e80 as whole strings from their first frame; the build is modelled as scale ×2.58 / ×2.51 | f100: the full "And every day," while the source shows "And every" |

### ig2

| Item | Read | Measurement | IR | Render |
|---|---|---|---|---|
| shots | scroll scene, cut f59, globe scene | cut f59 | one scene with a background video. The globe sprite e2 is **visible f0–149** (covered by full-frame sprites before f59) | — |
| scrolling "A Weekend Away" | huge red text scrolls left | speed fast → slow → fast; ≈ −4 090 px f1–57 | text fragments "eel", "e", "ken", "n" (e5, e6, e8, e12), each moving left about 720–1 366 px linearly, plus sprites; **not grouped as one title** | f40, the worst frame (0.131): "e"/"eke" pieces; the page background is white where the source is a pink gradient |
| title on the globe | types on letter by letter f59–84 | content drifts −544 px f63–104 | e20 "A Weekend Away" #ad0104 from f59 (entrance right). The type-on is modelled as **scale ×6.11** + left move 564 px (the drift magnitude agrees) | f100 (0.017) and f148 (0.010): close |
| stickers | suitcase, "Lets Goooo", photos, "Party Time", green chip, plane | — | OCR "Gaaaaao", "Panty"/"Paintye"/"Tine"/"Time" (grouped as one text_fragments title "Time Time Panty Paintye"), "Visitor"; photos as sprites | stickers mostly present |

### ig3

| Item | Read | Measurement | IR | Render |
|---|---|---|---|---|
| shots | cut f57 (close-up), whip pans f87–122 and f123–149 | cut f57; pan peaks f102 and f128/133 | one scene, colour background #fdfefe | — |
| "You just speak" | word by word; "speak" glitches (f36) and becomes a mic (f42) | re-centring shifts f9–14, f21–26 | e1 "You just speak" f2–73: **the string never loses "speak"**, and runs past the cut. The mic is read as "U" (e14) | f48 (0.010): "You just spe…" where the source shows "You just 🎤" + cursor; mic and cursor missing |
| close-up and click | navy "just" and a pill with a mic; blue disc on click f58–68; waveform f69–90 | — | "1sr" (e23) and "ust" (e33): OCR of "just". Waveform bars as thin sprites (e26, e27, e29) f73–79; cursor and disc as sprites | f100 (0.029): **the white pill is missing**; pale mic |
| "Your idea" | slides in with pan 1; letters light blue → navy f108–122 | pan ≈ −970 px, peak f102 | e49 "Your idea" #17325b f103–128, moves left 384 px, one colour | f112, the worst frame (0.068): letter spacing too tight ("Youridea"); the colour gradient is lost |
| "and it builts for you" | word by word, light blue → black | pan 2 ≈ −1 300 px | e56 "and" + e59 "and it builts for you" (exact, including "builts"), #303030 | f148 (0.021): one grey, condensed |

### Disagreements (none resolved here)

1. **Shots.** No clip was split at its cut or wipe (f140, f59, f57, ig1's wipe f18–26), so a single scene spans two compositions. In ig2 a sprite is visible across the cut. ig1's circle is not an element.
2. **Word-by-word and letter type-on builds** come out as scale and translation of the whole string; the string appears whole from its first frame (ig1 e1 ×0.48, e61 ×2.58, e80 ×2.51; ig2 e20 ×6.11). ig3's "speak" → mic swap is not in the IR.
3. **A centred shrink becomes a slide plus a clip** (envato1 e37: `x` +358, `reveal` 0.75, no scale), where the measurement shows a fixed centre and width ×0.63.
4. **Fragmented titles.** "Build" = "B" + "sull" + sprites; the scrolling "A Weekend Away" = 4 pieces, not grouped. Card texts are split in time (ig1).
5. **Colours.** ig1's white-on-dark "Every day," is stored black on a white box. Colour gradients across words are one colour (ig3).
6. **Background.** The glow (envato1) and the pink gradient (ig2) are split between the background video and big gradient sprites. The background video keeps ghost titles (envato1 f0, f100).
7. **Agrees with measurement.**
   - envato1 "B": two-phase motion (ease-out in, hold, accelerating out) in its keys.
   - ig2 title drift (564 px vs 544 px measured).
   - Entrance times at the cuts (f141 vs f140; f59 vs f59; ig3 "Your idea" f103 vs f102).
   - Exact strings "A Weekend Away" and "and it builts for you".
8. **View budget.** On these clips the kf-script view keeps motion for 8–25 elements; the rest are summaries without motion (see above).

## 4. After the fixes (`fixes2.md`), report only

**Run.**
- Gate: `keepframe gate-m2-real --clips eval/clips --out eval/out/clips-after --max-frames 150 --render-check` at `5ce591a`, the head after Phase B.
- kf-script fidelity: per scene, as in §3.
- Rows: `clips-after.json`.
- Nothing was tuned on these clips (CLAUDE.md §3). The fixes were accepted on the synthetic round trip and gold references. Section 1's read (committed at `6ce4003`) is the reference here as before.

| Clip | Scenes (from the analysis) | Elements | mean_l1 | render_l1 | **L1 kf** before → after (frame-weighted) | worst kf frame before → after | kf_parity | s |
|---|---|---|---|---|---|---|---|---|
| envato1 | s1 f0–149 | 55 | 0.0675 | 0.0684 | 0.0722 → **0.0722** | f144 0.148 → f144 0.148 | 0.000004 | 716 |
| ig1 | **s1 f0–25, s2 f26–149** | 7 + 96 | 0.0285 | 0.0300 | 0.0379 → **0.0318** (s1 0.0263, s2 0.0329) | f24 **0.259** → s1 f24 0.075, s2 f112 0.107 | ≤ 0.0000014 | 369 |
| ig2 | **s1 f0–58, s2 f59–149** | 58 + 18 | 0.0363 | 0.0358 | 0.0474 → **0.0392** (s1 0.0560, s2 0.0283) | f40 0.131 → s1 f4 0.073, s2 f28 0.044 | ≤ 0.000044 | 333 |
| ig3 | s1 f0–149 | 63 | 0.0164 | 0.0232 | 0.0245 → **0.0244** | f112 0.068 → f112 0.068 | 0.000001 | 170 |

**Disagreements of §3, after the fixes** (read and measurement as in §1–2):

| # | Item | Read / measurement | IR after the fixes | Status |
|---|---|---|---|---|
| 1 | shots | ig1 wipe f18–26 (peak f25); ig2 cut f59; envato1 cut f140; ig3 cut f57 | ig1 splits at **f26** and ig2 at **f59**. **envato1 and ig3 stay one scene.** envato1 f140 is a clear spike (0.496), but 63 % of f139's off-ground pixels, mostly the glow continuing across the cut, are unchanged: `_content` takes the border colour for the ground, a recorded shortcut. ig3's f57 spike is 0.048, below FLOOR (0.063) | 2 of 4 fixed |
| 2 | word / letter builds | ig1 "Every day," and "And every day," word by word; ig2 title types on | ig1 e1 sx 0.15 → 1, e59 0.23 → 1, e75 0.21 → 1; ig2 e3 sx 0.09 → 1: **still scale**. Fixed on the synthetic centred build (T2). These tracks never became reveals. Not diagnosed: the OCR caches were pruned | not fixed on these clips |
| 3 | centred shrink (envato1 e37) | width ×0.63 f111–135 about a fixed centre (cx 1284) | **sx 0.94 → 0.62 over f116–135, x 1297 → 1284** (reveal only during the build f83–106) | **fixed; agrees with measurement** |
| 4 | fragmented titles | "Build" one word; scrolling "A Weekend Away" one title | "B" + "sull" (envato1); "eel", "e", "ken", "n" (ig2 s1) | unchanged (B5 not done) |
| 5 | colours | ig1 dark-scene text white | ig1 s1 e1 **#f8f7fc** (before #010005 on a white box); the white full-frame sprite is gone with the split | **fixed** |
| 6 | background | glow and gradient split into sprites; ghost titles | unchanged (B8 not done) | unchanged |
| 8 | view budget | — | Summary rows now carry motion words: envato1 10 / 31, ig1 s2 58 / 87, ig2 s1 23 / 36, ig3 12 / 28 (the rest are static). ig1 s1 and ig2 s2 fit in full | **fixed (V1)** |

**Also new:**
- ig3 "and it builts for you" (e59) is a reveal 0.22 → 1.
- The ig2 globe scene `s2` begins at the cut; its sprites no longer cover the scroll scene.

The real-clip A/B on these projects is in `ab-real.md`.

## 5. After B2/B5/B8/B9 (`fixes3.md`), report only

**Run.**
- Gate: `keepframe gate-m2-real --max-frames 150 --render-check`, one clip at a time (`eval/out/clips-after2.sh`), at `686e914` (code as `b6d3a4a`).
- kf-script fidelity: per scene, as in §3.
- Measurements: `eval/out/clips5.json` (not committed: eval outputs).
- Nothing was tuned on these clips. The fixes were accepted on the synthetic round trip and gold references (`fixes3.md`). Section 1's read is the reference.

| Clip | Scenes | Elements §4 → §5 | mean_l1 | render_l1 | **L1 kf** §4 → §5 (frame-weighted) | worst kf frame | kf_parity | s |
|---|---|---|---|---|---|---|---|---|
| envato1 | s1 f0–149 | 55 → 50 | 0.0636 | 0.0698 | 0.0722 → **0.0710** | f144 0.148 → 0.155 | 0.000002 | 903 |
| ig1 | s1 f0–25, s2 f26–149 | 103 → 98 | 0.0286 | 0.0310 | 0.0318 → **0.0330** (s1 0.0261, s2 0.0344) | s2 f112 0.107 → 0.110 | ≤ 0.0000013 | 532 |
| ig2 | s1 f0–58, s2 f59–149 | 76 → 60 | 0.0382 | 0.0385 | 0.0392 → **0.0412** (s1 0.0560 → 0.0626, s2 0.0283 → 0.0273) | s1 f4 0.073 → f12 0.084 | ≤ 0.000044 | 396 |
| ig3 | s1 f0–149 | 63 → 64 | 0.0169 | 0.0235 | 0.0244 → **0.0250** | f112 0.068 → 0.068 | 0.000001 | 190 |

The shot splits are unchanged from §4.

**The four items, against the read and measurement:**

| # | Item | Read / measurement | IR after `fixes3` | Status |
|---|---|---|---|---|
| B2 | ig3 "You just speak" → "You just" + mic icon | The word is replaced by the mic button (read f25, f50) | e1 "You just speak" f2–40, then e6 "You just" f41–72 (before: one text f2–72) | **fixed** |
| B5 | fragmented titles | "Build" one word (envato1); the scrolling "A Weekend Away" one title (ig2) | Unchanged: "B" f11–68 + "sull" f18–40; "eel", "e", "ken", "n", with no group. ig2's pieces differ by 4.9 px/frame, measured over the new 0.1 s steps at 41 px/frame; B5a2's grouping needs ≤ 2. envato1's left edges agree within 1.6 px/frame, but another gate keeps the pair apart. Not diagnosed further, so as not to tune on the clip. | **not fixed** |
| B8 | glow sprites, ghost titles | envato1: purple/orange glow at the bottom; ghost "Build" in the background video at f0 (§3) | Moved into the background video: envato1 tracks 2, 11, 13, 24, 30; ig1 4, 14 / 3, 29, 32, 40, 41; ig2 1, 6, 82 / 3, 7. On envato1, tracks 2 and 30 are the bottom glow (correct), but **11, 13 and 24 are "Build"'s own letters while they blur in** (f11–17, f56–59). They are soft and in place, and no text box covers them yet. Residue in envato1's title read boxes rose (Build 10.25 → 13.60), but those boxes cover most of the frame and now hold the glow too, so the number does not separate glow from ghost. ig2 s1 renders worse: its title shows twice at f12–20. Its background video holds the same title ink as before (red share 0.104 in both), so the doubling comes from sprites. Cause not established (stage caches pruned). | **mixed**: glows moved (correct); blurred letters moved too (new defect) |
| B9 | fonts past the work cap | — | ig1 s2: texts kept sans-serif 19 → **4**, 17 borrow a twin's family. envato1, ig2, ig3: no text reached the cap. | **fixed** (the borrowed families' correctness is not measurable here) |

**Disagreements recorded, not resolved:**
- B8b's soft-region rule also takes blurred letters before OCR reads them. The B8b2 text guard does not see them, because no text box exists yet. Upgrade path: hold out any soft region that a text track later occupies (its box within a short window), and give the synthetic glow_title a blur-in that is not yet read.
- ig2 s1's doubled title (L1 kf +0.0066) is unexplained.

The real-clip A/B on these projects is `ab-real2.md`.

## 6. After B8c/B8d/B8d2/B11 (`fixes4.md`), report only

**Run.** `eval/out/clips-after4.sh`: the gate one clip at a time at `0fcb341` (code as `de74495`), kf fidelity per scene. Measurements: `eval/out/clips7.json` (not committed). Nothing was tuned on these clips.

| Clip | Elements §5 → §6 | mean_l1 | render_l1 §5 → §6 | **L1 kf** §5 → §6 | worst kf frame | kf_parity | s |
|---|---|---|---|---|---|---|---|
| envato1 | 50 → 54 | 0.0696 | 0.0698 → 0.0694 | 0.0710 → **0.0742** | f144 0.155 → 0.149 | 0.000001 | 794 |
| ig1 | 98 → 98 | 0.0288 | 0.0310 → 0.0312 | 0.0330 → 0.0332 (s1 0.0261, s2 0.0347) | s2 f112 0.110 → 0.111 | ≤ 0.000001 | 439 |
| ig2 | 60 → 72 | 0.0364 | 0.0385 → 0.0357 | 0.0412 → **0.0390** (s1 0.0626 → **0.0572**, s2 0.0273) | s1 f12 0.084 → 0.072 | ≤ 0.000044 | 382 |
| ig3 | 64 → 64 | 0.0169 | 0.0235 → 0.0235 | 0.0250 → 0.0250 | f112 0.068 | 0.000001 | 204 |

**The two defects of §5:**

| # | Item | IR after `fixes4` | Status |
|---|---|---|---|
| B8 | envato1's "Build" letters moved into the background video while they blur in | Only one envato1 region still goes into the video (track 11, 5 before). Ghost residue in the title read boxes: "Build" 13.60 → **9.93**, "Build SaaS Promo" 13.51 → **9.51**, second-shot title 18.01 → **12.82**. The first two are now below §4's values (10.25, 8.32, 13.24) except "Build SaaS Promo". | **fixed** |
| ig2 | doubled scrolling title (s1 L1 kf +0.0066) | No 3D candidate in ig2 s1 (B11). s1 has 55 elements again (§4: 58) and L1 kf 0.0572 (before fixes3: 0.0560) | **fixed** |

**New disagreement, recorded, not resolved: envato1 L1 kf +0.003.**
- Per frame (every 4th) against §5, f0–56 improve (−0.001 to −0.014) and f140 improves −0.042.
- But f76–96 (+0.005 to +0.019) and f112–136 (+0.018 to +0.030) get worse.
- **f112–136:** sprite `o34` spans f111–149, across envato1's cut at f140, which is not detected (§4 item 1). Its single texture now holds the second shot's letters (alpha coverage 0.10 → 0.34), because those letters are no longer in the background video for matting to remove. So the letters show from f111.
- **f76–96:** the words of "Build SaaS Promo" that are still blurred before their reads are kept out of the video (B8d, as intended), and the text element does not draw them yet.
- The cause of the f112–136 part is the missing shot split, not B8. Upgrade path: split envato1 at f140 (the content test in `_content` takes the border colour for the ground), or cut sprites at a detected cut.

## 7. After the f140 cut fix (`fixes5.md`), report only

**Run.**
- `eval/out/clips-after5.sh`: the gate one clip at a time at `4b9e548` (code as `b288490`), then kf fidelity per scene.
- Per-frame kf L1 of envato1, every 4th frame: `perframe.py` (scratch), against §6's projects.
- Nothing was tuned on these clips.

| Clip | Scenes | Elements §6 → §7 | mean_l1 | render_l1 §6 → §7 | **L1 kf** §6 → §7 (frame-weighted) | worst kf frame | kf_parity | s |
|---|---|---|---|---|---|---|---|---|
| envato1 | **s1 f0–139, s2 f140–149** (§6: one scene) | 54 → 39 + 8 | 0.0696 → 0.0562 | 0.0694 → 0.0593 | 0.0742 → **0.0639** (s1 0.0615, s2 0.0975) | f144 0.149 → s1 f36 0.142, s2 f144 0.116 | ≤ 0.000004 | 546 |
| ig1 | s1 f0–25, s2 f26–149 | 98 → 98 | 0.0288 | 0.0312 → 0.0312 | 0.0332 → 0.0332 | s2 f112 0.111 | ≤ 0.000001 | 300 |
| ig2 | s1 f0–58, s2 f59–149 | 72 → 72 | 0.0364 | 0.0357 → 0.0357 | 0.0390 → 0.0390 | s1 f12 0.072 | ≤ 0.000044 | 269 |
| ig3 | s1 f0–149 | 64 → 64 | 0.0169 | 0.0235 → 0.0235 | 0.0250 → 0.0250 | f112 0.068 | 0.000001 | 134 |

**Shots against the read (§1):**

| Clip | Read | §6 | §7 |
|---|---|---|---|
| envato1 | cut f140 | one scene (missed) | **cut f140** |
| ig1 | wipe f18–26 | f26 | f26 |
| ig2 | cut f59 | f59 | f59 |
| ig3 | cut f57 | one scene (spike 0.048 < FLOOR) | one scene: unchanged, not this fix's case |

**envato1 per frame (kf L1, §6 → §7):**
- **f112–136: 0.04–0.07 → 0.01–0.03.** §6's sprite o34 (f111–149, `e38` there) is gone; no element crosses f140. This was §6's recorded disagreement, and it is resolved.
- f140–148: 0.104 / 0.149 / 0.141 → 0.067 / 0.116 / 0.110.
- f28–68: mostly better (f48 −0.016, f52 −0.018, f64 −0.031); f36 +0.011, f40 +0.005.

**New disagreement, recorded, not resolved: envato1 f0–24 and f72 got worse.**
- f4 rose 0.009 → 0.035, f12 0.043 → 0.058, f24 0.080 → 0.095, and f72 0.043 → 0.090. f0–8 is an empty dark frame with the glow (read).
- At f0 the page shows pieces of "Build" (`eval/out/clips-after5-kf/sheets/envato1.s1.png`). Sprite `e1` now spans f0–25 (§6: f0–15), and its one texture holds letters that appear only later.
- Cause:
  - The shorter scene has a different background video (background tracks 7 and 26, §6: 11), so the residual regions are tracked differently.
  - It is the same class as o34: one texture across a content change.
- Upgrade path: as in §6, cut a sprite where its content changes, not only at a detected cut.

**The glow_cut exception** (`fixes5.md`): a title that stays for its whole scene has no visible ground behind it. envato1's s2 title drifts, so it is not this case.
