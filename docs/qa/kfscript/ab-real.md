# Real-clip A/B: brief vs kf-script view, after the fixes

**Pre-registration:** `ab-real-prereg.md`, frozen at `b8780ce`, before any post-fix analysis of these clips existed.
**Decision:** kf-script is **non-inferior** on both measures, so **the default view stays `kfscript`**. It was not shown superior.

## Run

| | |
|---|---|
| Analysis | `keepframe gate-m2-real --clips eval/clips --out eval/out/clips-after --max-frames 150 --render-check` at `5ce591a`, the head after Phase B (`fixes2.md`). The first attempt filled the disk and was discarded; nothing of it was used. |
| Questions | `real-gold` and `packets` at `ef5bafb`; the question code is unchanged since the freeze |
| Scenes (units) | 6: envato1 `s1`; ig1 `s1` (f0–25) and `s2` (f26–149); ig2 `s1` (f0–58) and `s2` (f59–149); ig3 `s1` |
| Questions per view | 121: 91 comprehension (auto 47, motion 20, read 24) and 30 edit |
| Answerers | 12 fresh Sonnet subagents, one per packet. Each made 2 or 3 tool calls (one packet read, sometimes in two parts, and one write), so all 12 complied. No answer was missing or invalid. |

**Read roles without a question.** 10 of the 34 roles had no IR element ≥ 60 % inside their box, so they got no question:
- ig2: globe, suitcase, palm island, landscape photo, plane, pin icon;
- ig3: cursor, close-up "just", white pill, "Your idea".

These are the pieces the analysis keeps inside bigger sprites, or splits into pieces the box does not hold.

## Result

| Category | brief | kf-script |
|---|---|---|
| auto | 95.7 | **100.0** |
| motion | **75.0** | 70.0 |
| read (Jaccard) | 40.3 | 40.7 |
| **comprehension** | 76.6 | **77.8** |
| **edit target** | 63.3 | **66.7** |

| Interval (kf − brief, points, 95 %, 10 000 resamples, seed 0) | Scenes (pre-registered) | Clips (reported) |
|---|---|---|
| comprehension | **+1.2 [−0.73, +4.03]** | +1.2 [−0.73, +5.02] |
| comprehension without motion (P1/P2 set) | +2.95 [+0.28, +5.72] | — |
| edit | **+3.33 [0.00, +6.98]** | +3.33 [0.00, +6.67] |

- **Keep rule:** both lower bounds > −10 → **holds**. kf-script stays the default; nothing flips back.
- **Superiority (h1, comprehension lower bound > 0):** not shown. The P1 rule (+10 points) also fails.
- **14 of the 121 paired questions differ; 9 favour kf-script.** In detail:
  - **auto:** kf-script wins two questions about the largest text (envato1, ig3).
  - **read:** kf-script wins the ig1 card ids, and the brief wins three ig3 ids.
  - **motion:** split 1 / 2.

Per scene (brief / kf-script accuracy; view size in characters):

| Scene | auto | motion | read | edit | brief chars | kf chars |
|---|---|---|---|---|---|---|
| envato1 s1 | 88 / 100 | 67 / 67 | 40 / 42 | 80 / 80 | 7 383 | 48 095 |
| ig1 s1 | 100 / 100 | 67 / 67 | 100 / 100 | 100 / 100 | 1 385 | 9 803 |
| ig1 s2 | 100 / 100 | 100 / 67 | 27 / 34 | 36 / 45 | 10 977 | 47 988 |
| ig2 s1 | 100 / 100 | 67 / 67 | 12 / 12 | 100 / 100 | 8 446 | 47 976 |
| ig2 s2 | 100 / 100 | 80 / 80 | 67 / 78 | 75 / 75 | 3 718 | 30 289 |
| ig3 s1 | 88 / 100 | 67 / 67 | 46 / 24 | 67 / 67 | 6 764 | 48 028 |

## Caveats

- **Few clusters.** Six scenes from four clips. The scene interval is narrower than a clip-level interval would be, because scenes of one clip are correlated. The clip-clustered interval gives the same decision.
- **The views still hit the budget.** Four of the six kf-script views reach the 48 000-character budget. Their summary rows now carry motion words (V1).
- **Read accuracy is low in both views (about 40 %).** The IR still splits titles and cards into pieces (B5 was not done), so a role's id set is long and partly missed by both.
- **Proxy model.** Sonnet stands in for the production model, as in P1, P2 and the synthetic test.
