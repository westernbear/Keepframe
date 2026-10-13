# What text should a motion-graphics reference become? (2026-10-13)

Question (user, 2026-10-13): the core analysis pipeline is being redesigned. A reference will be a motion-graphics video, and the key decision is the **format** that turns it into text an AI understands and edits. Check every intermediate representation against what the agent itself reads from the reference.

Answer: **kf-script** — a constrained, executable HTML/CSS + GSAP-timeline document with a semantic header, generated from the canonical scene JSON (which stays the store for the AE spec, Lottie export and versions). Evidence below: literature, five Mixkit references found with a headless browser, the agent's own frame-by-frame reads, CV measurements, a hand-written kf-script of one reference rendered back against the source, and the current pipeline's output on the same clips.

## 1. Where the project stands

| Area | State (master `bf66db4`) |
| --- | --- |
| M1/M2, admin, core flow, round 2, AE send + verify | merged |
| Reference analysis Stage A (clean plates, textures, text style, fonts) | merged |
| Stage B (big/kinetic text as text) | merged 2026-10-12; the visual fix wave was cut back to principled fixes because rules fitted to the 4 eval clips kept fixing one case and breaking another |
| Stage C (AI-legible description: motions, roles, beats, style, brief, verify-repair) | not started |

What the agent sees today is `scene_brief` (`keepframe/session/brief.py:76`): one row per element with id, kind/label, text, centre/size, visible range and motions derived on the fly from tracks (`verify/matrix.py:178`, 6 motion types). No shots, no keyframes, no eases, no font/style, no stagger, no masks, ≤ 40 elements. The scene JSON itself has tracks with preset cubic-bezier eases, text style and glyph reveals, but no shots, beats, stored motions, shape primitives or patterns.

## 2. Literature (✓ = number checked in the paper's text by us or by the 2026-10-07 note; others from fetched summaries)

| Format as the LLM's view | Evidence |
| --- | --- |
| Raw Lottie JSON | Frontier models rarely produce valid files: video→Lottie GPT-5 9.2 % / 7.4 %, Gemini 3.1 Pro 0 % / 0 % on the two subsets vs a fine-tuned tokenizer model 88.1 % / 80.7 %, at 37–41 k tokens and ~110 s per output, PSNR ≈ 16 (OmniLottie, arXiv 2603.02138, Table 1 ✓). Text→Lottie 3-shot valid GPT-5 10.9 %, Claude Sonnet 4.5 45.2 %, Gemini 2.5 Pro 30.0 %; zero-shot "cannot generate any valid" file (LottieGPT, 2604.11792, Table 2 and §10 ✓). Good export target, poor reading/editing view. |
| HTML/CSS/GSAP code | Frontier models write runnable animation code 99–100 % of the time (summary), appearance 0.80–0.84, but temporal similarity ≤ 0.31 when they must infer timing from pixels (Animation2Code, 2606.28593; appearance/temporal ✓). GPT-4o writing GSAP: 58.8 % correct first pass → 93.6 % with predicate-level verifier feedback (MoVer, 2502.13372 ✓). LogoMotion (2405.07065): HTML + anime.js, 68 % → 96 % after 4 visually grounded repairs. Keyframer (2402.06071): CSS keyframes on SVG ids, 6.7 % syntax errors. |
| SVG motion program | Motion Vectorization (2309.14642, TOG 2023): video → SVG with per-frame transforms + z-order, imports to AE; dense per frame, no text-as-text, not tested with LLMs. |
| Predicate DSL | MoVer's logic DSL (Allen intervals, directions, magnitudes) — GPT-4o writes it 95.1 % correctly; a *checking* language, not a description. |
| Compact custom notations | TOON/TRON-style notations save 18–27 % tokens at 9–14 points of accuracy ("Notation Matters", 2605.29676, abstract only). Unfamiliar syntax costs accuracy. |
| Manim / Remotion / ExtendScript | No evidence as an LLM reading/editing view for motion graphics; Manim is under-represented in pre-training (2604.18364). |

VLM limits that shape the pipeline (2025–2026): timing and direction still fail (Animation2Code temporal ≤ 0.31; fine-tuning made it worse, 0.24 → 0.08 ✓); per-timestamp boxes near zero on VideoZeroBench (1.0 %, unverified). VLMs name and critique; CV supplies numbers. Proposal generators worth a test: SAM 3/3.1 text-prompted video masks (hosted, unverified price ~$0.20 / 1k frames), LayerD / Qwen-Image-Layered for RGBA layers on keyframes. PP-OCRv5 is *not* a drop-in here: Stage B measured it and reverted (word-gap box splits, `docs/qa/reference-analysis/stage-b.md`).

No paper measures "keep X, change Y" editing accuracy for any format; the choice below rests on generation/verification evidence plus our own round-trip test (§4).

## 3. References found with a browser, read by the agent, then measured

Headless Chromium (Playwright; gstack install was blocked in this container) listed Mixkit's free After Effects templates. Five 1080p previews (59.94 fps) were read from contact sheets with burned-in frame numbers (`gold-reads.md`), then measured with simple CV (per-row colour extents, boxes, stroke widths, per-word ink).

| Clip | What it is | Agent read vs measurement |
| --- | --- | --- |
| mk571 Swipe Corporate Logo (8.0 s) | logo sting: centre-out teal reveal, frame pops, mark pops, frame stretches, name rises | **3 of ~8 events wrong**: reveal direction (read left→right, really centre-out), frame stretch (read rightward, really symmetric about the centre), url (read fade, really a 24 px drift down). Missed a late frame settle (1076 → 1012 px). |
| mk544 Bold Text Titles (3.7 s) | kinetic type: scrolling stacked lines, inverse flash, glyph flicker, block transition, stepped logo zoom | structure right; missed that "CREATIVES" slides up 170 px; read a stepped zoom (≥2× → 0.56× hold → 1×) as one tween; flash range off by 2 frames |
| mk543 Message Interface (7.0 s, 9:16) | chat UI: 9 alternating bubbles, list scrolls | structure + all 9 strings read; cadence read as ≈ 42 f, measured ≈ 47 f (0.78 s) |
| mk800 Kinetic Opener | "WELCOME" glyph stagger, zoom-blur into live action at f100 | live action after f100 is out of scope |
| mk713 Shape Motion Intro (15 s) | ~8 shots of shapes + type | shot list only |

Lesson: the agent's read is right about **structure and meaning** (shots, which texts, roles, what kind of choreography) and unreliable about **directions, magnitudes and timing** — the same split the literature reports for VLMs. Measurement is right about numbers and blind to meaning.

## 4. The format can carry a reference: mk571 written as kf-script and rendered back

`gold/mk571.html` is the agent's corrected read written by hand as kf-script: 5 elements, 10 timeline entries, 57 lines including the header and CSS. Rendered in Chromium and compared with the source every 4th frame (121 frames):

- mean RGB L1 **0.0066**, worst 0.062 at f48 (the reveal's snap, approximated by two preset eases); every other frame ≤ 0.035.
- For scale: AE verify passes at mean ≤ 0.025, frame ≤ 0.04.
- `gold/mk571_sheet.jpg`: source | kf-script render | difference.

The render-compare loop also caught two errors that reading alone would not: the frame settle the agent never noticed (L1 stuck at 0.014 after f256 until added) and a GSAP quirk (Chromium collapses a 4-value `inset()` so a clip-path tween jumps; 2-value form fixed it).

## 5. The current pipeline on the same references

`keepframe analyze --no-captions` (master code, CPU, no VLM) on the full clips; the agent-facing text is `scene_brief` (saved in `briefs/`). Each IR was checked against the agent's corrected read (`gold-reads.md`).

| Clip | Read (elements that matter) | Pipeline IR | Recon L1 | Analysis |
| --- | --- | --- | --- | --- |
| mk571 | 5: background reveal, frame, mark, name, url | 13 (9 sprites, 4 texts); frame split over 4 elements + a "口" OCR read of the small square; mark over 5 short sprites; name "Your Business logo obol s"; url ✓ | 0.0165 (gold kf-script 0.0066) | 873 s |
| mk544 | ~10 over 4 phases + a transition | 68 (35 sprites, 33 texts): all 6 strings found, plus 20 glyph fragments ("REA", "EATI", "3H", "m"…); no phases | 0.0672 | 645 s |
| mk543 | header, footer, 9 bubbles (template × 9), list scroll | 61 (26 sprites, 35 texts): header ✓, all bubble text present but as 17 separate line elements, 9 timestamps ✓ (some spaces lost); background classified `video` | 0.0179 | 992 s |
| mk800 | "WELCOME" glyph stagger, then live action | 142 (136 sprites): "WELCOME" ✓ as one text; live action became 136 sprites (the CLI has no live-action gate; the web upload does) | 0.0423 | 619 s |

Agreement with the read, item by item:

| What the read says | IR | Verdict |
| --- | --- | --- |
| mk571 teal opens from the centre | two full-frame black sprites drifting 26–70 px apart | structure plausible (curtains), magnitudes wrong |
| mk571 frame stretches 241 → 1076 px about its centre, stroke constant | a 858×256 sprite "scales ×1.17" after three other elements | ✗ — a texture cannot stretch without stretching its stroke, so the element breaks up; needs a shape primitive |
| mk571 url drifts down 24 px | "moves down 13px … out_back" | direction ✓ (the read said fade; CV was right) |
| mk571 name rises word by word through a mask | one text "moves right 147px" | ✗ — mask + stagger not representable |
| mk544 scroller, inverse flash, glyph flicker, block transition, stepped zoom | per-fragment "moves up-left 850px", "scales ×2.53", "rotates −28°/+84°" | ✗ — glyph-level choreography and transitions turn into fragments with invented transforms |
| mk543 list scrolls ≈166 px every ≈0.78 s | every line repeats "moves up 166px" 5–6 times | numbers ✓ (the read said every 42 f; measured 47 f), structure ✗ (no container, no bubble group, no repetition) |
| mk800 glyphs grow in left→right | "WELCOME" "scales ×17.63, moves right 367px" | ✗ — a stagger read as one transform |
| keep predicates | 208/211 … 6189/6260 locked | locked on fragments, so "keep" would preserve artefacts |

Pattern: CV numbers are often right (directions, scroll distance, cadence, colours, strings); what is missing is **structure** — shots/phases, shape primitives, per-word/glyph splits with stagger, masks, containers, repetition — so the IR spends dozens of elements and invented transforms to approximate what the read describes in a few lines. That is a representation gap first, a measurement gap second.

## 6. Decision: kf-script

Canonical store stays `scene.json`. The LLM-facing text becomes kf-script, emitted from it:

```html
<!-- kf-script v0 · <title>
size · fps · frames · times in seconds (frame = s × fps)
shots: sh1 0.00–2.10 "<shot intent>" | sh2 …  (transitions named: cut | wipe | blocks | zoom)
style: palette <role #hex …> | type <family weight size per role>
intent: <one or two sentences: what the piece does, in order> -->
<div id="frame" data-role="frame" data-src="cv" data-conf="0.93" class="c" style="…"></div>
<div id="name" data-role="brand_name"><span class="w"><span>Your</span></span> …</div>
<script>
tl.addLabel("sh1", 0)
  .from("#frame", { width: 0, height: 0, duration: 0.90, ease: "power3.inOut" }, 0.77)
  .from("#name .w > span", { yPercent: 100, stagger: { each: 0.13 }, ease: "power4.out" }, 2.92)
</script>
```

Why this and not the alternatives:

1. **Familiar**: the syntax frontier models write most reliably (≈ 99–100 % runnable); Lottie is 0–45 % valid without fine-tuning. Keepframe already composes HTML + GSAP.
2. **Executable, so it can be checked**: rendering the text and diffing it against the source measures how faithful the description is (§4). A description that cannot be rendered cannot be verified.
3. **Expressive for what the references contain**: per-word/glyph splits with `stagger {each, from: "start"|"random"}`, masks (`overflow` / `clip-path`), shape primitives (border boxes, SVG paths), multi-phase `keyframes`, holds and `steps(n)`, labels for shots and beats, nested timelines for repeated patterns (the chat's 9 bubbles = one template × 9).
4. **Compact**: keyframes with named eases, not frames (mk571: 57 lines for 482 frames).
5. **Editable without drift**: the agent edits by id (text, colour, timing, ease); edits are applied as patches to `scene.json`, never by regenerating the document. AE export keeps using the JSON.

Rules for the subset (so it parses back and exports to AE): absolute layout in px; animatable props limited to x, y, scale(X/Y), rotation(X/Y), opacity, width/height, borderWidth, clip-path inset, colour, blur; eases from GSAP presets (`none`, `power1–4`, `expo`, `sine`, `circ`, `back(n)`, `steps(n)`) and `CustomEase` only when the fit needs it; numbers come from CV and carry `data-src`/`data-conf`; meaning (roles, shot names, intent) comes from the read and is marked as such.

## 7. Core redesign this implies: fit a kf-script to the video

The pipeline's job becomes "find the shortest kf-script whose render matches the reference", one objective instead of per-case thresholds:

1. **Shots and transitions** as first-class objects (cuts exist in `analyze/shots.py`; add transition types).
2. **Proposals** per shot: OCR lines → words → glyphs, colour layers vs the plate, optionally hosted SAM 3 masks.
3. **Typing**: text (string, font fit), shape primitive (rect/ellipse/polygon, fill/stroke), or textured sprite — a primitive wins when its residual is as low (fewer parameters).
4. **Tracks → tweens**: per-layer transforms (ECC), segmented into tweens with preset eases, holds and steps (extends `keyframes.py` `reduce_curve`/`fit_ease`).
5. **Structure**: stagger (the same tween shifted across siblings), repetition (template + instances), containers (shared transform → parent), masks.
6. **Render–compare refit**: emit → render (existing Chromium renderer) → per-element residual → merge/split/retype; keep a change only if residual + description length drops.
7. **Read**: VLM/agent names roles, shots and intent from keyframe sheets with burned-in frame numbers, inside the CV structure.

Evaluation that cannot overfit 4 clips: a **synthetic round trip in the same format** (random kf-scripts from templates — kinetic title, logo sting, chat UI, scroller, block transition — rendered, analysed, and diffed structurally and in pixels), plus a few hand-made gold scripts of real references like `gold/mk571.html`.

## 8. How an intermediate representation is checked (user rule)

Every IR check compares three things and reports disagreements instead of resolving them silently:

| Check | Owner of truth | Example |
| --- | --- | --- |
| Agent's own read of the reference (contact sheets, burned-in frames) | structure, strings, roles, shot boundaries, choreography type | "9 chat bubbles alternating", "name rises word by word" |
| CV measurement | directions, magnitudes, timing, colours, sizes | "frame widens symmetrically 241 → 1076 px, f118–f214" |
| Render of the IR vs source | whether the IR as a whole reproduces the clip | mean L1, worst frame, residual sheet |

## 9. Next steps (smallest first)

1. `kfscript.emit(scene)` from today's IR as the agent's view, with render-compare; A/B it against `scene_brief` on a small edit set (no paper measures edit accuracy per format, so this is the check the decision still owes).
2. Synthetic kf-script generator + round-trip harness.
3. IR additions: shots/transitions, shape primitives, text splits + stagger, masks, holds/steps, patterns.
4. Render–compare refit loop and the read pass with a disagreement report.
5. Edits as kf-script patches; AE export of the new primitives.

Files: `gold-reads.md` (reads + measured corrections), `briefs/` (what the agent sees today, per clip), `gold/mk571.html` (+ `icon.png`), `gold/mk571_sheet.jpg`, `sheets/` (contact sheets), `tools/gold_compare.py` (render a kf-script page and diff it against a video), `sources.md` (clip URLs).
