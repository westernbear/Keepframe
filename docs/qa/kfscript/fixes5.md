# envato1's missed cut at f140

The user asked for envato1's shot split to be fixed (2026-10-15, "envato1 컷 분할 수정"). The rules are as in `fixes4.md`:
- one change per comparison;
- accepted on the synthetic round trip plus gold references;
- nothing tuned on the eval clips.

The real clips are re-checked afterwards, report only, in `clips.md` §7.

## Diagnosis

envato1 cuts at f139→f140 (the read in `clips.md` §1). This shot is a small "Build SaaS Promo" line; the next is a huge blurred word over the same bottom glow. Measured on the source frames at the head (`9497b35`):

| Step of `detect_boundaries` / `_is_cut` | Value | Rule | Result |
|---|---|---|---|
| change score f139→f140 | 0.496 | spike ≥ 3× neighbours and ≥ FLOOR 0.063 | passes |
| `_kept_pixels` (a's off-ground pixels left as they were) | 0.626 | ≥ KEPT 0.5 → not a cut | **stops here** |
| its components: the glow | area 21 406 px (97 %), peak structure 2.1, kept 0.64 | — | — |
| its components: the title line | area 775 px, peak structure 32.3, kept 0.12 | — | — |
| `_kept(a,b)`, `_kept(b,a)` (structure correlation) | 0.40, 0.17 | both < 0.5 → a cut | would cut |
| ground | [3,1,6] → [19,5,28] | ΔE ≥ 20 → new ground | not new |

The cause: `_kept_pixels` counted the glow, a region with no edge anywhere that goes on through the cut, as content that stayed. `_kept` already ignores it, because its mask needs structure above 4.0.

This miss also caused `fixes4`'s one real-clip regression. Sprite o34 (f111–149) crossed the cut and its texture took the second shot's letters (`clips.md` §6).

## Measuring stick (`c1fd238`)

`glow_cut`: a small three-word title line on a dark gradient, above a pulsing glow (glow_letters' parts). At a whole-tenth frame (42, 45 or 48) it hard-cuts to one huge word. The glow goes on through the cut, and the ground stays.
- Before committing, at the head:
  - seeds 0–3 all miss the cut: spike 0.20–0.30, kept pixels ≈ 0.95. The glow and the gradient's lower part are one component with peak structure ≈ 1.
  - The round-trip seed, 1, has structure kept 0.26 / 0.14.
- A first draft with two similar titles and a ground shade change was set aside:
  - Its cut set at 44/30 s landed a frame late (the time is written rounded up).
  - Its titles were similar enough that `_kept` read 0.6–0.7, so it did not isolate this defect.
  - The final template follows envato1's read (small line → huge word).

**Baseline `rt5-base`.**
- These are `rt4-b8d2`'s analyses (code `de74495`, unchanged until this fix), re-measured with `--reuse`; glow_cut is analysed fresh.
- The re-measure matches except for render noise: chat_ui L1 kf 0.01443 → 0.01427; scroller and hard_cut parities fall to ≤ 5e-6.
- glow_cut-1 at baseline: 1 scene, shot F1 **0**, L1 kf 0.0110 (worst f48, the cut, 0.077).

## Fix

| Id | Commit | Change | Target before → after | Other changes | Decision |
|---|---|---|---|---|---|
| F140 | `b288490` | In `_kept_pixels`, an off-ground region whose structure peaks at 4.0 or less (a soft glow or gradient: light on the ground) leaves the count. 4.0 is the function's own structure tolerance and `_kept`'s floor. With nothing left, the new-ground rule decides, as for an empty ground. `shortcut:` a glow touching a title is one region and still counts. | glow_cut shot F1 **0 → 1** (cut at f48 found) | **glow_cut L1 kf 0.0110 → 0.0191 (+0.008): breaks the L1 bound** (explained below). Every other case unchanged. mk571 unchanged; e24's `e13` keeps its 23 glyphs. | **accepted, with the glow_cut L1 exception explained** |

**The glow_cut L1 exception.** The split is right: the truth has two shots. The L1 rise is the background plate's, not the shot rule's:
- In its own scene (f48–119), the huge word never moves, so no frame shows the ground behind it.
- The plate's fill behind the word pulls the glow's colour up into a cone (residual sheet `eval/out/rt5-fix/sheets/glow_cut-1.s2.png`).
- Before the split, the single scene borrowed the clean ground from f0–47, across the undetected cut. That works only because both shots share a ground.
- Measured over f48–119, every 4th frame, the kf render's L1 in two areas:
  - outside the word's box (padded 24 px): 0.0016 → 0.0017;
  - inside it: 0.0132 → **0.0266**. All of the rise is there.
- worst kf: 0.077 (f48) → 0.007 (s1) and 0.035 (s2).

This is a limit of the plate, now exposed: a title that stays for its whole scene leaves a hole that the fill invents from its neighbours. Upgrade path: fill a never-seen hole from the ground model (per row of the visible ground) instead of from its edges. That is a separate change with its own comparison; not done here.

## Gold shots (`gold-shots-base.json`, `gold-shots-fix.json`)

`detect_boundaries` / `auto_scenes` on `gold/shots.json`'s ranges, at `c1fd238` and at `b288490`: **identical.**

| Ref | Gold | Found | Shot F1 |
|---|---|---|---|
| e07 | cut 87 | cut 87 | 1.0 |
| e11 | cuts 40, 84 | cuts 40, 84 | 1.0 |
| mk800 | transition → 97 | transition 97 | 1.0 |
| mk544 | blocks 82–148 | transition 97, cut 149 (the known step-zoom limit) | 0.67 |
| mk571, mk543 | one shot | none | 1.0 |

## Final checks (`4b9e548`, code as `b288490`)

- **Fast suite:** 2946 passed, 2 skipped, **0 failed** (the font-budget timing test passed this time).
- **Shot tests:** `test_cut_under_a_soft_glow` fails at the head (`[] == [8]`) and passes with the fix; the other five tests pass.
- **Browser tests** of the synthetic kinds: 2 passed.
- **Run note:** `rt5-fix` first ran out of disk in mk571's gold analysis, because the fast suite was running alongside. Its case analyses were already fresh at the fix commit. It was resumed with `--reuse` of those (`eval/out/rt5-fix-resume.sh`), with mk571 analysed again.
- **Real clips** (report only, `clips.md` §7):
  - envato1 now splits at f140, as read. o34's regression is gone: f112–136 fall from 0.04–0.07 to 0.01–0.03.
  - envato1 L1 kf 0.0742 → **0.0639**, below §5's 0.0710 and §4's 0.0722.
  - ig1, ig2 and ig3 are unchanged.
