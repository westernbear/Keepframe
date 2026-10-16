# Two upgrade paths from fixes5: never-seen holes, and sprites across a content change

The user asked for both (2026-10-16): fill a hole no frame shows from the surrounding ground (glow_cut's regression), and cut a sprite where its content changes (envato1 f0–24). The rules are as in `fixes5.md`:
- one change per comparison;
- accepted on the synthetic round trip plus gold references;
- nothing tuned on the eval clips.

The real clips are re-checked afterwards, report only, in `clips.md` §8.

## B12: a never-seen hole takes the ground of its rows

**Diagnosis.** After the f140 fix, glow_cut's second shot is its own scene, and its huge word never moves.
- The plate's hole behind the word ([11,0,777,286]) reaches the glow, so its ring is not smooth enough for the degree-2 fit (RMS 2.92 > 2.0).
- Telea inpainting then dragged the glow up into the hole, as a pink cone.
- In the single scene before the split, the hole was smaller ([39,0,773,255], ring RMS 1.24) and the degree-2 fit took it.

**Fix (`e2909f4`).** Before Telea, each row of such a hole whose visible ground agrees along the whole row takes that row's median.
- "Agrees" means at least MIN_RING pixels, with RMS ΔE to their median ≤ POLY_MAX_RMS (the degree-2 fit's own tolerance).
- Telea fills only the rest.
- Holes the degree-2 fit takes, and holes with no agreeing row, are unchanged.
- `shortcut:` rows only; a ground that changes across a row keeps Telea.
- Unit test: a hole from the top edge down to a glow. Old fill: max ΔE 24 in the top rows; new: 0.0.

| Id | Commit | Target before → after | Other changes | Decision |
|---|---|---|---|---|
| B12 | `e2909f4` | glow_cut kf L1 **0.0191 → 0.0067** (before the split: 0.0110); its own reconstruction 0.0118 → 0.0072 | glow_cut ghost ΔE 3.22 → 3.97, explained below. Nothing else moved. mk571 unchanged; e24's typing keeps its 23 glyphs. (`b12-row-ground.json`, `rt5-fix` → `rt6-g1`) | **accepted** |

**The ghost ΔE exception.** The metric fits a degree-2 surface to a 6–16 px ring around the text box. glow_cut's word box runs from the top edge down to the glow, so the ring sits on the glow's top, and the fit expects glow inside the box: what the cone looked like.

Against the true ground (the median of source frames f0–5, before any title appears), over the word's box:

| Fill | Mean ΔE | p95 ΔE |
|---|---|---|
| old (Telea cone) | 10.75 | 19.6 |
| new (row ground) | 0.02 | 0.0 |

## B13: a sprite track is cut where its content changes

**Diagnosis (envato1 f0–24).** The tracker matches one region to one track. envato1's bottom glow (track 2) is joined by blurred letter regions:
- at f15, the top of its box jumps from 894 to 418;
- at f23, it widens to the full frame.

`_merge_adjacent_tracks` had already joined its pieces across gaps of 3 frames. The sprite's one texture comes from its largest frame (f23, glow plus letters), so the letters show from f0.

**Measuring stick (`6c7aad9`): `glow_merge`.** A glow along the bottom of an empty dark frame, then a big word whose letters come out of a slow blur just above it.
- At B12's code, the glow's sprite spans the whole clip: the gap merge joins it to a speck that appears later (x jumps 398 → 66 at f44), so the glow's texture is drawn where the speck is.
- kf L1 0.0274.
- A first draft with a quick blur kept glow and letters apart (0.0086) and was set aside.

| Id | Commit | Change | Target before → after | Other changes | Decision |
|---|---|---|---|---|---|
| B13 | `a196b3e` | `split_merges`, after the gap merge. A track is cut where its region takes in, or leaves behind, half or more of another region at least a quarter of its size (the size `track_regions` starts a track at). It is also cut where, across a gap, one region is under a quarter of the other. | glow_merge **0.0274 → 0.0109** | logo_sting 0.0282 → **0.0101**; scroller 0.0621 → 0.0604 (matched 0.50 → 0.625); mk571 0.0180 → **0.0094**. **wipe 0.0066 → 0.0152: flag.** On the clips (§8), ig2 s1 elements go 55 → 122: letters coming out of a blur break up and rejoin every frame, and each frame read as a merge. (`b13-track-split.json`) | **revised** |
| B13b | `6289d0e` | A joining or leaving region counts only when it belongs to another track seen in at least 3 frames (as `split_swaps` asks of a read). The rule then needs only the tracks. | glow_merge 0.0274 → 0.0109 | Same gains; fewer pieces (scroller 11.4 → 8.9, big_scroll 42 → 39, hard_cut stray 0.19 → 0.05); ig2 s1 has 71 elements. **wipe 0.0066 → 0.0153: flag.** The cut is the gap rule's: wipe's circle has no regions at f44–45 and grows about 2× a frame, so across the gap it grew 4.3×, and with the track cut nothing drew those two frames. (`b13b-object-merges.json`) | **revised** |
| B13c | `a91105a` | Across a gap, the size is compared with what the track's own growth over its two frames before the gap predicts. Still more than 4× off is something else (glow_merge's speck is 350× off). | glow_merge 0.0274 → **0.0109** | **No flags.** wipe unchanged (L1 kf; stray 0.30 → 0.46, one more piece). Kept: logo_sting 0.0282 → 0.0101, scroller 0.0621 → 0.0607, big_scroll 0.0745 → 0.0740, mk571 0.0180 → 0.0094. hard_cut 0.0054 → 0.0056, within the bound. e24 keeps its 23 glyphs. (`b13c-growth-gap.json`) But the fast suite fails 3 tests (`test_sliding_card_is_not_a_mover`, `test_rerun_from_keyframes_appends_version`, `test_guard_keeps_a_good_merge`): a striped card sliding 7 px a frame has its 6 px stripes overlap their neighbours' last places by more than half, so every stripe was cut every frame. | **revised** |
| B13d | `354e66e` | A region taken in counts only when its track has no region on the other side of the cut (it went into this one); a region left behind only when its track has none before (it came out of it). A neighbour that goes on beside it is not taken in. envato1's opening keeps its cuts (track 1 ends at f14, track 7 at f20). | (pending) | The 3 tests and the stripe test pass. | (pending) |

**Stray area 1.00 on logo_sting and mk571 (B13 to B13c).** Each opens with a full-frame layer (`e1`). It is now cut where a half-frame panel takes over (f7 in logo_sting, f4 in mk571). The first piece (f0–6, f0–3) comes before any truth box, so the diff counts it as a full-frame stray. Their kf L1 roughly halves.

**envato1 f0–24 at B13c** (kf L1 every 4th frame; §7 → B13c, from `clips.md` §8):

| f0 | f4 | f8 | f12 | f16 | f20 | f24 |
|---|---|---|---|---|---|---|
| 0.037 → **0.006** | 0.035 → **0.008** | 0.034 → **0.012** | 0.058 → 0.038 | 0.066 → 0.057 | 0.084 → 0.081 | 0.095 → 0.086 |

- The ghost letters at f0–8 are gone, and those frames are below §6's values (0.030, 0.009, 0.026).
- The glow's first piece (f0–14) takes its texture from a glow-only frame.
- Under B13, the f0–12 piece still held a "B": the "B" grows out of a blur inside the glow's region, and B13's cuts left it in that piece.
- What remains at f12–24 is the letters' blur-in itself (§5's blurred-letter limit).

## Run notes

- **Containers.** The container restarted twice during the runs. Each time, the finished analyses were reused (same analysis code) and the step in progress was rerun.
- **Disk.** Two round trips ran out of disk at mk571's gold analysis, because the cases' stage caches stay until a round trip ends. The later scripts analyse the cases, prune their caches, then add mk571 with `--reuse` (`eval/out/rt6-g3.sh`, `rt6-g4.sh`).

## Final checks

- Browser tests of the synthetic kinds: 2 passed.
- Real clips (report only), `clips.md` §8, at B13c:
  - ig1 0.0332 → **0.0308**, ig2 0.0390 → **0.0383**, ig3 0.0250 → **0.0246**.
  - envato1 0.0639 → 0.0643: its first shot is better (0.0615 → 0.0583, f0–8 fixed), its second shot worse (0.0975 → 0.148, B12's disagreement, explained there).
