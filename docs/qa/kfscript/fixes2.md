# Real-clip fixes: one change per round trip

These fix the defects the real-clip gate found (`clips.md`, `e392534`); the plan is `/root/.claude/plans/progress-cryptic-wilkinson.md`.

**Setup.**
- Machine: cloud container (4 CPU, 15 GB, no GPU), Chromium 1194.
- Each row compares the 12-case synthetic round trip (seed 1) at the change's commit with the run before it. The case list adds four kinds to P2's eight: `hard_cut`, `wipe`, `word_build` and `whip_pan` (Phase A, `1bcad90`).
- Gold rows: mk571 (via `--gold`) and e24 (`keepframe analyze` f120–269, glyph typing).
- Every run analyses afresh on the same renders (`eval/out/rt2-after.sh`), from the main checkout pinned at that commit.
- Before/after JSON: `fixes2/<id>.json`.

**Accept rule** (as `fixes.md`), applied to every case:
- `kf_parity` ≤ 0.001;
- the targeted metric moves as predicted;
- L1 kf worsens by no more than 0.001;
- other metrics are unchanged or explained;
- no case's shot F1 drops;
- gold: mk571's L1 kf is within +0.001 of its base, and e24's `e13` keeps its 23-glyph typing.

**Noise floor:** L1 ±0.0002 (`fixes.md`).

**Baseline correction.** The S1 commit (`9a09950`) added a footer bar to the `hard_cut` template, so that its title swap stays inside one shot as the reads require. The baseline run (`rt2-base`, `329a3b9`) had rendered the template without the footer. Its `hard_cut` row was replaced by `329a3b9`'s analysis of the footer render, which every later run uses (`rt2-base-fair`; that row alone was analysed again). Before this correction, B1 seemed to cost hard_cut +0.0112 in L1 kf; on the same video it gains −0.0096.

| Id | Commit | Change | Target | Result | Other cases | Decision |
|---|---|---|---|---|---|---|
| b1-shots | `9a09950` + `afe7e2c` | A clip splits at its hard cuts (S1) and gradual transitions (S2) when no scenes are given (`analyze`, the gate, the web estimate) | Shot F1 = 1 on hard_cut, wipe and block_transition; no split elsewhere | Shot F1 **0 → 1.00** on all three. Every other kind and mk571 stays one scene (F1 1.00). L1 kf: block_transition 0.0497 → **0.0217**, wipe 0.0136 → **0.0066**, hard_cut 0.0352 → **0.0256**. hard_cut text ΔE 24.7 → **0.8**: the ig1 symptom on synthetic data | Other kinds unchanged. View/page (DL) ratio rises on the split cases: two views, each with its own header. mk571 0.0187 = 0.0187; e24 `e13` keeps 23 glyphs | **Accepted.** Exposed a defect: hard_cut's footer, still for its whole shot, renders as an outline with a hole (fixed in b1b) |
| t1-reveal | `c2d72d1` | A reveal clips only while its read grows (fraction from the frame's own left edge); a frame that reads the whole string takes the box | `shrink` found on word_build (l2 shrinks about its centre after its build) | word_build structure found `build` → **`build`, `shrink`**; still missed: l1's centred build (T2). centre px 2.2 → 1.8 | word_build L1 kf +0.0003 (0.0056 → 0.0059; the shrink is now a scale, before a slide plus clip). hard_cut: s1's title t1 overlaps one more piece (5 → 6), keys/el 10.9 → 12.6; L1 unchanged. Rest unchanged; mk571 0.0187 = 0.0187; e24 23 glyphs | **Accepted** |
| t2-centred-build | `25d0d53` | A build may grow from its left edge, centre or right edge (same baseline); the reveal follows each frame's left edge | `build` found on word_build's centred, re-centring l1 | word_build structure missed `build` → **none missed** (found build, build, shrink). L1 kf 0.0059 → **0.0046**, recon 0.0041 → 0.0023 (l1 was a scale ×0.22 → 1) | hard_cut back to B1's pieces and keys (2.4 pieces, 10.8 keys/el); rest unchanged; mk571 0.0187 = 0.0187; e24 23 glyphs | **Accepted** |
| k1-noise-keys | `894e5fd` + fix `01937c1` (measured as `a10dc78` = T2 + both) | A track's key tolerance is at least its own noise bound σ·√(2 ln n), σ the smaller of the first- and third-difference MADs | Fewer keys where measured boxes jitter; no L1 cost; steps kept | keys/el: scroller 23.9 → **19.6**, counter 9.4 → **8.3**, logo_sting 11.4 → **10.9**; clean tracks unchanged | L1 kf unchanged on every case; mk571 0.0187 = 0.0187; e24 23 glyphs. hard_cut's s1 title overlaps 5 or 6 pieces from run to run (T1 6, T2 5, K1 6): analysis variation, not this change | **Accepted.** The first version (`894e5fd`, third differences only) read a letter-by-letter staircase as noise and smoothed it into a ramp (`test_letter_by_letter_ocr_becomes_one_reveal_without_sprite_fragments`); it was never measured on its own |
| v1-view-motion | `cb8f9df` | A summary row carries the brief's motion words; packing continues past a block that does not fit; the brief (and those words) counts a one-frame step the keys carry | Every element's motion in the view; stepped_zoom's brief not 'static' | Dense views (pre-fix eval-clip projects): summary rows with motion words 0 → **60 / 94** (ig1), **16 / 39** (envato1), **15 / 41** (ig3), **3 / 10** (ig2); the rest read 'static'. stepped_zoom's word: 'scales ×1.01' → **'scales ×0.28 0.30s; scales ×2.03 0.67s; …'** | Round trip unchanged (its views rarely reach the budget; scroller DL 22.02 → 22.08). The longer rows cost markup: elements kept in full ig1 8 → 7, ig3 25 → 22, envato1 14 → 16 | **Accepted** (view and brief only: no analysis change) |

## Notes

Shot detection on the references (dry run, no analysis; gold in `gold/shots.json`):
- every synthetic kind is correct;
- e07 [87], e11 [40, 84], mk800 [97] (zoom-blur) and mk544 [97] (blocks) are correct;
- mk571 and mk543 do not split.

Known limit: mk544's step zoom at f149, about 5× with the big side cropped by the frame, reads as a cut, so mk544 scores F1 0.67.
