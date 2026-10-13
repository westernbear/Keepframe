# kf-script round trip after the fixes (T11)

Machine: cloud container (4 CPU, no GPU). The run uses the 8 synthetic kinds at seed 1, analysed and measured at `3f0b149` (all analysis fixes, the brief fixes and the view changes), from a pinned worktree. It is compared with `rt-head`, a fresh analysis at the pre-fix commit. Full numbers: `p2-after-fixes.json`; per-change records: `fixes.md`.

prompt_typing-1 is not like-for-like: its template was fixed in `07fcde7` (a shorter line that fits the bar; a readable button label), so the page differs from the baseline's.

| Case | L1 kf before → after | Text ΔE | Text ratio | Pieces | Structure found / missed | View / page chars |
|---|---|---|---|---|---|---|
| kinetic_title-1 | 0.0177 → 0.0173 | 0.2 → 0.2 | 1.00 → 1.00 | 15.0 → 15.0 | 0 / 0 | 7.90 → 8.40 |
| logo_sting-1 | 0.0283 → 0.0282 | 1.0 → 1.0 | 1.00 → 1.00 | 4.2 → 4.2 | 1 / 1 → 1 / 1 | 5.20 → 4.28 |
| chat_ui-1 | 0.0109 → **0.0152** | **34.5 → 2.8** | 1.00 → 1.00 | 2.0 → 2.1 | — | 9.58 → 6.96 |
| scroller-1 | 0.0686 → 0.0681 | 0.5 → 0.5 | 0.49 → 0.49 | 5.6 → 5.6 | — | 20.35 → 20.34 |
| block_transition-1 | 0.0498 → 0.0497 | 0.6 → 0.6 | 0.50 → 0.50 | 3.5 → 2.7 | — | 4.86 → 4.19 |
| prompt_typing-1 (new page) | 0.0118 → 0.0113 | 0.8 → 0.8 | 0.99 → **1.00** | 2.0 → 2.0 | 0 / 1 → 0 / 1 | 3.05 → 2.56 |
| counter-1 | 0.0263 → 0.0250 | **73.7 → 1.9** | 1.00 → 1.00 | 6.7 → 6.7 | — | 12.12 → 10.12 |
| stepped_zoom-1 | 0.0199 → 0.0199 | 0.5 → 0.5 | 0.97 → 0.97 | 1.3 → 1.3 | — | 2.29 → 2.06 |

- **`kf_parity`** stays ≤ 0.00002 on every case.
- **L1 kf improves on 6 of 8 cases.** chat_ui is the recorded B1 exception: the bubble's hole under the text, which the old wrong fill had painted over.
- **Text colour on coloured surfaces is fixed:** ΔE ≈ 98 → < 7 on every such text.
- **Views are shorter on 7 of 8 cases**, even with the new summary line per element and the group lines. Holds, pixel scale tolerance and sub-tolerance changes now show as holds. kinetic_title is the exception: its 30 short pieces each gain a summary line.
- **Typing on the new prompt_typing page.** B2's surface measure works here: the ink is the glyphs, not the bar, and the onsets spread over 0–35 frames across 17 glyphs. But the glyph model's fit is 0.168 against `GLYPH_FIT` 0.05, so the line stays in box mode. On the old page it passed. The likely cause is the caret moving along the line, together with glyph columns from the uniform fallback, so a column rises in two steps. `GLYPH_FIT` was not tuned.
  - Follow-up (P4): take each typed glyph's column from its onset, not from glyph centres, and model the caret.
  - For the A/B both views print glyph reveals now, so this changes the truth only for the round trip's structure metric.

Gold: mk571 and e24 hold (`fixes.md`, T6). Not re-checked after the median-hold revision; mk571 had passed (0.0191 against its 0.0192 limit) with the start-value holds, which were worse on every synthetic case.
