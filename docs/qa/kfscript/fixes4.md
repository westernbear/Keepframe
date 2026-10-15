# The two defects left by fixes3: blurred letters in the background video, ig2's doubled title

The user asked for both to be fixed (2026-10-15, "둘 다 전부 고쳐"). The rules are as in `fixes3.md`:
- one change per comparison;
- accepted on the synthetic round trip plus gold references;
- nothing tuned on the eval clips.

The real clips are re-checked afterwards, report only, in `clips.md` §6.

## Diagnosis

**1. Blurred letters in the background video** (envato1's "Build", `clips.md` §5). Two causes:
- **Before the first read.** The region of a title's still-blurred letters has no text box yet, so B8b2's surface guard, which needs a common frame, misses it, and B8b puts the region into the video. Example: envato1 tracks 11 and 13, at f11–17. The text "sull" starts at f18, inside them.
- **While the line builds.** The text cover takes each frame's read box. Letters not yet read lie outside it and stay in the video. This is older than B8b; B8a's cover never reached them.

**2. ig2's doubled title** (s1 L1 kf 0.0560 → 0.0626). A bisect of the `fixes3` commits on ig2 (gate options, f0–149, kf fidelity per scene):

| Commit | s1 elements | s1 L1 kf |
|---|---|---|
| `a9972e2` (before fixes3) | 58 | 0.05597 |
| `ad56ed5` B8a | 58 | 0.05618 |
| `8a635ee` B8b | 43 | 0.06259 |
| `7f18152` (fixes3 head) | 43 | 0.06259 |

The cause is B8b, but indirectly:
- The solid search had always found a "solid" in ig2 s1: a long-lived component of the scrolling title's unread letters. Its 13 short-lived member pieces made it pass as a fragmented spinning object.
- Before B8b, the fidelity choice kept its fragments (fragments 0.123, still 0.202).
- B8b moved the gradient region at f0–9 into the video. The component then tracked cleanly from f3, and the still beat its fragments within the slack (0.157 against 0.155).
- A solid is one texture moved as a unit, so the passing letters showed twice (f12–20).

Two other ideas were checked and set aside:
- An outline-stability rule (area within ±25 % on ≥ 80 % of frames, as for established movers) would keep it (84 %).
- Comparing the alternatives over the per-frame video plate instead of the poster still picks the still (0.154 against 0.161).

**The rule (B11).** A candidate whose box holds a read text box on at least half its frames is the rest of that title. The threshold was set before measuring ig2. ig2's solid holds one on 57 % of its frames, a thin margin, reported as is.

## Measuring stick (`5a59c50`)

`glow_letters` is a title over glow_title's pulsing glow (a video background). Its letters come out of a blur one by one as the line slides in. No synthetic kind forms a solid; three trials put big_scroll's headline over the glow, and none did, because OCR reads these letters while ig2's are mostly unread. So B11 is accepted on its unit test, the unchanged round trip, the gold references, and the ig2 re-check.

**Baseline `rt4-base`.** These are `rt3-b5a2`'s analyses: the analysis code is unchanged since `7f18152`. Only `glow_letters` is analysed fresh. The reuse check is identical except for render noise (chat_ui's compose, parities under 2e-4).

| Case | Defect | Baseline |
|---|---|---|
| glow_letters-1 | blurred letters | Ghost ΔE 1.688 in the title box. The region of the first, still-blurred letters (f0–16) is in the video, and so are the unread letters while the line builds (f16–56). Stray 0.0012, L1 kf 0.00797. |
| mk571, e24 (gold) | — | as `fixes3` (L1 kf 0.0180; `e13` 23 glyphs) |

## Fixes

| Id | Commit | Change | Target before → after | Other changes | Decision |
|---|---|---|---|---|---|
| B8c | `02a17ef` | A soft region also holds a text when a text box lies inside it on the region's nearest frame up to 0.5 s away, which covers a title's letters still blurred before its first read or after its last. | glow_letters ghost ΔE 1.69 → **1.44**: the region of the first, blurred letters (f0–16) stays out of the video | glow_letters stray area 0.0012 → 0.175 and pieces 2 → 3. That region is a sprite again, as before B8b, and counts as stray because its middle frame (f8) precedes the title's annotated start (f12). L1 kf unchanged. Nothing else moved. e24 keeps 23 glyphs. | **accepted** |
| B8d | `4d5c748` | A line whose box grows is covered at its full width while it builds, widened on each side in proportion to how far that edge moved over its life. | glow_letters ghost ΔE 1.44 → **0.886** (the letters not yet read, f16–56) | Nothing else moved. e24 keeps 23 glyphs. | **accepted** |
