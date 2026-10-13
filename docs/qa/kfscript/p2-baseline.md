# kf-script P2 · synthetic round trip — baseline

Machine: cloud container (4 CPU, no GPU, 15 GB), Chromium 1194 via Playwright 1.56. Code: branch `claude/quirky-fermat-uyx2k0` at `a8394f5`. Command:

```bash
keepframe eval-roundtrip --out eval/out/rt-base --seeds 1 --gold mk571=eval/refs/mk571.mp4,eval/kf/mk571
```

Inputs: the eight reference-page kinds of `keepframe/kfscript/synth.py` at seed 1 (960×540, 30 fps, 90–150 frames), analysed with `AnalyzeOptions(refine=False, generate_3d=False)` and no captioner. The gold row is mk571 (1920×1080, 59.94 fps, 482 frames) with `docs/qa/kfscript/gold/mk571.truth.json`, the hand-written gold page and the P1 analysis in `eval/kf/mk571` (refine on, `--no-captions`). Full numbers: `p2-baseline.json`; sheets (source | kf-script page | diff ×4): `p2-sheets/`.

Time: analysis 567 s for the eight cases (26–125 s each), measurement 273 s (9–18 s per synthetic case, 172 s for the 1080p gold row).

## Harness checks

- **Gate** `kf_parity` (|L1 kf-script page − L1 compose page| ≤ 0.001): passes on all nine cases. The largest value is 0.000029, and it moves by about 1e-5 between runs (render noise).
- **Determinism.** A rerun with `--reuse` gives identical metrics for every case. The one change came from a declared-truth edit made between the two runs (logo_sting gained the `pop` and `drift` items), and `--reuse` re-keys on a hash of the page and its truth.
- Two page bugs were found and fixed while building the harness:
  - GSAP read CSS `translate(-50%)` as fallback-font pixels when it touched centred text before the face loaded. One run in four shifted the text by about 12 px; pages now build their timeline after `document.fonts.load`. The mk571 gold page had the same race on its url.
  - The 1080p gold row ran out of memory: 482 probe frames were stacked (6 GB peak), and fidelity kept float32 source frames.
- **L1 page** (the reference page against its own decoded video) is 0.0001–0.0025: the codec floor. For mk571 it is the gold ceiling, 0.0066, the same as in P1.

## Results

| Case | L1 page | L1 recon | L1 kf | Truth / IR els | Matched | Fragments | Pieces | Text | ΔE | Centre px | Visible err f | Structure found / missed / unsupported | DL ratio |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| kinetic_title-1 | 0.0001 | 0.0062 | 0.0177 | 2 / 30 | 1.00 | 1.00 | 15.0 | 1.00 | 0.2 | 2.5 | 3.5 | 0 / 0 / 4 | 8.38 |
| logo_sting-1 | 0.0012 | 0.0256 | 0.0283 | 5 / 13 | 0.80 | 1.60 | 4.2 | 1.00 | 0.9 | 1.2 | 22.8 | 1 / 1 / 5 | 5.39 |
| chat_ui-1 | 0.0012 | 0.0062 | 0.0110 | 12 / 19 | 0.83 | 1.75 | 2.0 | 1.00 | 34.1 | 2.1 | 4.6 | 0 / 0 / 3 | 9.86 |
| scroller-1 | 0.0005 | 0.0604 | 0.0686 | 8 / 49 | 0.50 | 1.50 | 5.6 | 0.49 | 0.5 | 11.3 | 10.5 | 0 / 0 / 3 | 21.66 |
| block_transition-1 | 0.0013 | 0.0488 | 0.0498 | 6 / 14 | 0.17 | 3.50 | 3.5 | 0.50 | 0.6 | 2.5 | 11.0 | 0 / 0 / 3 | 5.15 |
| prompt_typing-1 | 0.0014 | 0.0072 | 0.0118 | 6 / 11 | 0.83 | 1.80 | 2.0 | 0.99 | 0.8 | 1.0 | 1.0 | 0 / 1 / 3 | 3.12 |
| counter-1 | 0.0014 | 0.0097 | 0.0261 | 6 / 27 | 1.00 | 1.67 | 6.7 | 1.00 | 73.7 | 1.4 | 17.2 | 0 / 0 / 3 | 12.84 |
| stepped_zoom-1 | 0.0025 | 0.0128 | 0.0199 | 3 / 4 | 1.00 | 1.00 | 1.3 | 0.97 | 0.5 | 1.7 | 0.7 | 0 / 0 / 1 | 2.45 |
| **gold mk571** | 0.0066 | 0.0165 | 0.0187 | 5 / 13 | 0.80 | 1.60 | 5.2 | 0.92 | 2.0 | 8.0 | 79.2 | 2 / 0 / 5 | 4.89 |

Column definitions:
- **Matched:** truth elements with a one-to-one IR match at IoU ≥ 0.5, measured at their mid-visible frame.
- **Fragments:** IR elements with at least half their box inside the truth box at that frame.
- **Pieces:** the same, over the element's whole life (up to 40 sampled frames).
- **Text:** difflib ratio of the matched string.
- **ΔE:** text colour.
- **Visible err f:** the worse end of the matched element's visible range, in frames.
- **DL ratio:** characters of the kf-script view divided by characters of the reference page.

Structure items marked `ir: "P3"` are counted as unsupported, never as missed.

## What the baseline says

1. **Pixels are close; structure is not.** The kf-script page reproduces every synthetic source within L1 0.011–0.069. Yet:
   - the IR uses 2–15 pieces per truth element over its life;
   - its view is 2.5–22× longer than the page it came from;
   - not one P3 structure (split, stagger, mask, repeat, container, counter, steps, transition) is expressible.

   This is the spec's thesis measured: the numbers are mostly there, and the description is not.
2. **Temporal splitting** is the dominant failure. kinetic_title's title is 29 pieces, because the word stagger through a mask becomes short-lived sprites. counter's cards and numbers are 7–12 pieces each. scroller's lines are 2–10 pieces. mk571's mark is 6 sprites of 2–37 frames each. → P4 tasks 1 (stagger, counter and pattern induction) and 5 (render–compare refit, merging pieces).
3. **Masks and transitions:**
   - logo_sting's centre-out bg reveal becomes 11 pieces, and the worst frame (f16, L1 0.43) is the mask;
   - block_transition's four blocks are never matched (0 pieces at IoU ≥ 0.5);
   - scroller's inverse flash is its worst frame (f48, 0.26).

   → P3 tasks 1 (shots and transitions) and 5 (masks).
4. **Text colour on coloured surfaces is wrong.** White text on the accent bubble or card is recorded as the surface colour (`#3c6cff`, ΔE ≈ 98): chat_ui t1 and t3, counter l1, l2 and n1. Text on dark or neutral surfaces is right (ΔE ≤ 4.6). This is a measurement bug, separate from structure, and the cheapest fix the baseline found.
5. **Typing is lost.** prompt_typing's prompt is one text element with no glyph reveal, and the blinking caret was read as a trailing "l" (`Ship channel build launch.l`). This is the one `ir: "now"` structure missed on a synthetic page. The cursor is missed entirely (0 pieces).
6. **What already works:**
   - strings (text ratio 0.97–1.00 wherever a line is matched);
   - centres within 1–3 px;
   - drift (found on logo_sting and mk571);
   - mk571's mark pop, found on a 30-frame piece;
   - stepped_zoom's holds (2 pieces; the step lands as a split rather than a hold — P4 task 3).

## Read → measurement → render (CLAUDE.md §1)

The read was done from contact sheets of each source video (frame numbers burned in, every 6 frames) before looking at the IR. The first read found six template defects (title wider than the frame, frame border visible before its reveal, bubbles scrolling over the header, caret visible before the bar, a `tl.set` at t = 0 that a paused timeline skips, visible ranges ignoring off-stage elements). They were fixed and the read redone. Measurement is the truth file's boxes, probed in Chromium through `window.__bbox` every frame.

| Case | Read (source sheet) | Measurement (probed truth) | IR (diff) | Render vs source (L1 kf, worst frame) |
|---|---|---|---|---|
| kinetic_title | 3-word title rises word by word through a mask, subtitle fades up, words exit upward last-first | title f9–84, sub f27–75 | one title element in position (2.5 px) but 29 pieces over time and re-typeset; stagger, mask and exit unsupported | 0.0177, f16 0.049 (mid-stagger: the page shows a scaled whole title) |
| logo_sting | bg opens from the centre, frame pops from a dot, mark pops, frame stretches symmetrically as the mark slides left, name rises word by word, url drifts down | bg f6, frame f27, mark f40, name f78, url f90 | bg 11 pieces; frame matched, its visible range off by 45 f; mark is an orange text "N" (circle lost, pop missed); url drift found | 0.0283, f16 0.432 (the centre-out mask) |
| chat_ui | header, five bubbles alternating sides every 0.8 s, list steps up a row per bubble | bubbles f12–149 … f97–149 | bubbles and texts matched (10 of 12); header split in 3; white-on-blue text takes the bubble colour | 0.0110, f108 0.020 |
| scroller | 8 caps lines of two phrases scrolling up, inverse flash f40–47 | lines leave and enter at f42 … f54 | 4 of 8 lines matched, text ratio 0.49, 49 IR elements | 0.0686, f48 0.258 (the flash) |
| block_transition | headline A, 4 blocks rise in stagger and cover, background changes, blocks rise out, headline B | blocks f31–64 | blocks never matched; headline A in 2 pieces, B in 5 | 0.0498, f56 0.227 |
| prompt_typing | headline blurs in, bar fades in, prompt typed with a blinking caret, cursor glides to the button and presses, chip fades in | typed f24, cursor f92, chip f128 | prompt has no glyph reveal and reads the caret as "l"; cursor missed; button label smeared into its texture | 0.0118, f28 0.016 |
| counter | two cards pop in staggered, numbers count 0→200K and 0→60 slow–fast–slow | cards f6 / f9 | cards 9–12 pieces; final numbers only from f56 / f58 (visible error 50 f); white text recorded as card colour | 0.0261, f32 0.032 |
| stepped_zoom | brand word big f0–9, small f10–19, 1× from f20; two labels at f24 | word f0–89 | word matched, 2 pieces (the step splits it) | 0.0199, f0 0.053 |
| gold mk571 | P1 table (`p1.md`): bg centre-out, frame symmetric with settle, url drifts down, words rise | gold truth (read corrected by measurement) | bg unmatched (10 pieces); mark 6 pieces (pop found); frame matched, its visible range off by 115 f; name text 0.84 ("Your Business logo obol s") | IR 0.0187, gold page 0.0066 |

Read ↔ measurement disagreements on the synthetic pages: none after the template fixes. The truth is the page, so a remaining disagreement would be a read error. On mk571 the P1 corrections stand (3 of 8 events were wrong in the read).

## Tests

- Fast suite after P2: 2899 passed, 7 failed, 2 skipped (23 min). The 7 are the base commit's environment failures listed in `p1.md` (`litellm` is not installed; one 20 s timing budget). No new failures.
- Browser and touched tests, run sequentially so load cannot time out Playwright: 55 passed. They cover `test_kfscript_browser`, `_synth`, `_gold`, `_diff`, `test_roundtrip`, `test_ab_views`, `test_composite::test_compositor_matches_browser`, `test_glyph_reveal`, `test_perspective`, `test_renderer` and `test_composer`.

## Confirmation A/B on fresh clips (P1's switch condition)

`scripts/ab_views.py synth-gold` + `packets` on the eight round-trip analyses → `ab-synth/`. Questions:
- **Auto gold:** the same pre-registered generator as P1, built from `scene.json`.
- **Read gold:** for each truth element whose role is unique in its case, the IR ids inside it over its life. An id inside several truth boxes belongs to the smallest one.
- **Edit:** one colour edit per case.

80 questions, 16 packets, one fresh Sonnet subagent per packet — the same setup as P1 run 2. The packets were not seen or tuned before the run.

| | Brief | kf-script |
|---|---|---|
| auto (IR facts) | 83.6 | **91.8** |
| read (roles → ids) | 74.1 | 71.7 |
| comprehension (auto + read) | 81.8 | **88.0** |
| edit target (n = 5) | **100** | 80 |
| rule (kf edit ≥ brief and kf comp ≥ brief + 10) | | **not met** |

- **kf-script wins** on eases (4 of 4 disagreements: the brief has no exact ease), on the largest text and its colour, and on clips whose brief summarises away motion (logo_sting 94.9 vs 61.5, counter 100 vs 85.7, scroller 100 vs 85.7).
- **kf-script loses** on "when does X finish its last movement" (3 of 5 disagreements). The view prints every sub-threshold settle and hold key, so readers count noise as movement, as in P1. It also loses one role guess: in chat_ui, "the title" was read as the largest headline instead of the header's "Support", which cost the one edit miss.
- **Decision: the default view stays `brief`.** The P1 run 2 gain (+12) shrinks to +6 on fresh clips, and edit accuracy drops. Both kf-script losses trace to the IR's noise keys and unlabelled roles, which are P4's targets (hold fitting, read pass), not to the format. Rerun this A/B after P4.

## Not done

- Seeds 2–3 (the plan's optional extension). One seed per kind keeps the baseline at about 14 min. P3/P4 should add seeds when a kind's numbers move.
- The eval clips (ig1–ig3, envato1) were missing during P2. They were downloaded and gated later: `clips.md`.
