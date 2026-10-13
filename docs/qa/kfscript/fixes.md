# Analysis fixes: one change per round trip

Machine: cloud container (4 CPU, no GPU), Chromium 1194. Each row compares the 8-case synthetic round trip (seed 1; renders reused from `eval/out/rt-base`) at the change's commit with the run before it. Each run is analysed from a git worktree pinned at that commit (`keepframe eval-roundtrip --seeds 1 --reuse`). Before/after JSON for every case is in `fixes/<id>.json`.

Accept rule (plan, 2026-10-13):
- `kf_parity` ≤ 0.001 on every case;
- the targeted metric moves as predicted;
- L1 kf worsens by no more than 0.001 on any case;
- untargeted metrics are unchanged, or each change is explained.

**Noise floor.** A fresh analysis at the unchanged commit (`rt-head` vs `rt-base`) differs by up to 0.0002 in L1 kf, 0.4 in ΔE and 0.02 px in centre error, and its views are 3–7% shorter.

| Id | Commit | Change | Target | Result | Other cases | Decision |
|---|---|---|---|---|---|---|
| b1-text-colour | `15fe836` | Text on a kept surface takes the glyph colour | Text-colour ΔE on chat_ui and counter below 10 | chat_ui 34.5 → **2.4** (t1 97.9 → 5.8, t3 98.3 → 0.6); counter 73.7 → **1.8** (l1, l2 and n1 98.3 → 0.6–6.5); text ratio unchanged | **L1 kf chat_ui +0.0040** (0.0109 → 0.0149, above the 0.001 limit); counter −0.0009; prompt_typing +0.0006; the rest within noise | **Kept, as a recorded exception.** The regression is a separate defect: the bubble sprite has a hole where the text box was cut out of the regions (`pipeline.py` regions stage, OCR box + 2 px). The old wrong fill (blue Bebas 56 px with a white glow) painted over the hole; the right fill (white Manrope 29.8 px, no effect) leaves it showing as a dark box. Reverting would restore a ΔE ≈ 98 colour on every text on a bubble or card. Follow-up: fill a sprite's hole under a text that sits on its surface (P4). |
| b3-caret | `e0b46f0` | A blinking caret is not text | prompt_typing typed text exact | "Ship channel build launch.**l**" → "Ship channel build launch." (text ratio 0.981 → **1.00**) | prompt_typing L1 kf +0.0007 (the caret blinks in the source; the line no longer draws it as a permanent "l"); the rest within noise | **Accepted** |
| b2-typing | `f54ff7e` | Typing on a surface is a glyph reveal | prompt_typing "typing" found | missed → **found**: `e10` gets a 23-glyph reveal (one per non-space character) | prompt_typing L1 kf **−0.0013**, view/page 2.96 → 2.42; the rest within noise | **Accepted** |

## Gold check at the final analysis code (T6)

Run from a worktree at `f900bba` (all analysis fixes): `keepframe analyze --no-captions`, default options as in P1.

| Reference | Before (P1/P2) | After | Verdict |
|---|---|---|---|
| mk571 (1080p, 482 f) | L1 kf 0.0187, recon 0.0165, matched 0.80, text 0.92, pop and drift found, view/page 4.89 | L1 kf **0.0191** (+0.0004, limit 0.0192), recon 0.0170, matched 0.80, text 0.92, pop and drift found, view/page **4.21** | no regression; 865 s |
| e24 (Envato range f120–269) | typed prompt `e13` has a 23-glyph reveal | `e13` keeps its 23-glyph reveal | B2 does not disturb typing on the plate; 197 s |
