# kf-script — AI-legible scene format and analysis redesign — Design

Date: 2026-10-13 · Status: approved in conversation (plan approval 2026-10-13)
Research: `docs/research/2026-10-13-ai-legible-format/` (literature, Mixkit and Envato SaaS references, reads, measurements, pipeline comparison)

## Why

- A motion-graphics reference has to become text an AI understands and edits. Research compared formats:
  - Raw Lottie: frontier models produce valid files 0–45 % of the time (OmniLottie Table 1, LottieGPT Table 2).
  - HTML/CSS/GSAP: runnable ≈ 99–100 %; models cannot infer timing from pixels (Animation2Code temporal ≤ 0.31), so numbers must be measured; predicate feedback lifts correctness 58.8 % → 93.6 % (MoVer).
  - A hand-written GSAP page of mk571 rendered back at mean L1 0.0066 (AE verify passes at 0.025).
- The current pipeline gets many numbers right but has no **structure**: shots, shape primitives, text splits with stagger, masks, containers, repetition, counters, typing, cursors. All seven references show it (research §5, §5b). The agent's brief also hides what the IR already holds (glyph reveals).
- Stage B's visual fix wave fixed one clip and broke another three rounds in a row: rules fitted to four clips. The redesign needs a measuring stick that cannot be overfitted.

## Decisions (user, 2026-10-13)

| Topic | Decision |
| --- | --- |
| Format | kf-script: executable HTML/CSS + GSAP timeline, emitted from `scene.json` |
| Canonical store | `scene.json` (AE spec, Lottie, versions); kf-script is a view, edits are patches |
| IR checks | three-way: the agent's own read of the reference, CV measurement, render vs source (CLAUDE.md §1) |
| Rules | `CLAUDE.md` + this spec |
| Execution now | P1 (view) and P2 (round trip); P3–P5 planned |
| Environment | all tests and measurements in the cloud container |

## Format (v0, P1)

A kf-script document is a complete HTML page that renders the scene and reads as a description of it.

```html
<!-- kf-script v0 · mk571
size 1920x1080 · fps 59.94 · frames 482 (8.04 s) · time in seconds (frame = s × fps)
shots: sh1 0.00–8.04
style: palette #5ac9a4 bg · #ffffff e11 e6 · #09728a e3 | type Roboto 700 77px e11 · Roboto 400 26px e6
intent: (unread)
conventions: .el is placed at its anchor; x, y anchor px; sx, sy scale; rot, skx deg; rx, ry deg (tilt);
             reveal = visible fraction left→right; glyph reveals listed per element -->
<div class="el" id="el-e11" data-role="title" data-src="cv" data-conf="0.92" …>…Your Business logo…</div>
<script>
const tl = gsap.timeline({paused: true});
CustomEase.create("out_cubic", "M0,0 C0.33,1 0.68,1 1,1");
tl.fromTo(S["e11"], {y: 612, opacity: 0}, {y: 540, opacity: 1, duration: 0.1335, ease: "out_cubic"}, 2.9196); // f175–f183
</script>
```

Rules:

1. **Header comment**: id, size, fps, frames, duration, time unit; `shots` (one whole-scene shot until P3, then the IR's shots with intents and named transitions); `style` palette (colours with the element ids using them) and type (family/weight/size with element ids); `intent` (from the read pass, P4; `(unread)` before); `conventions`.
2. **Markup**: the composer's markup, one `.el` per element, `id="el-<IR id>"`, plus `data-role` (label, else role), `data-src` (provenance), `data-conf`, escaped `data-caption`. Text is real text (`fonts.css.text_html`).
3. **Timeline**: `tl.fromTo(S["<id>"], from, to, start)` on per-element state objects with IR prop names (`x y sx sy rot skx rx ry opacity reveal`). The shared runtime (`compose/template.html`) applies them in Keepframe's transform order, so rx/ry need no special case. Segments sharing `(t0, t1, ease)` are merged into one tween. Times are seconds to 4 dp with a `// fA–fB` comment.
4. **Eases**: `PRESET_EASES` names registered with `CustomEase` only when used; fitted beziers `ease1…`; linear `"none"`.
5. **Runtime contract**: `window.__seek(f)`, `window.__ready`, `window.__bbox(id)` — the renderer's contract (`render/renderer.py`).
6. **Injection safety**: the script holds only numbers and ids serialised with `json.dumps`; observed text lives only in escaped markup.
7. **Not kf-script**: hand-authored reference pages (`gold/mk571.html`, P2 templates) are free GSAP pages with the same runtime contract. They are truth, not views.

P3 extends the vocabulary: props `w`, `h`, `stroke_w`, mask edges, counter value; `hold` keys; helpers `typeOn`, `counter`, `cursor`; shot labels; nested timelines for patterns.

## LLM view

- The kf-script page without vendor code, fonts or data URIs (asset names instead).
- Budget `VIEW_CHARS` (initially 24 000, set from P1 measurements). Elements ranked by `ir.importance.rank_elements`; the lowest are dropped until under budget, then `<!-- omitted N: ids… -->`.
- `KEEPFRAME_AGENT_VIEW=brief|kfscript` picks the agent's second system message (`session/agent._scene_summary`). The default stays `brief` until the A/B says otherwise (pre-registered rule in P1 Task 7).

## Redesigned analysis (P3–P4)

The pipeline's job becomes: **find the shortest kf-script whose render matches the reference.**

1. Shots and transitions as IR objects (`analyze/shots.py` boundaries inside a scene).
2. Proposals per shot: OCR lines → words → glyphs, colour layers vs the plate (optionally hosted SAM 3 masks).
3. Typing: text, shape primitive (rect/ellipse, fill/stroke), or textured sprite; a primitive wins when its residual is as low.
4. Tracks → tweens: transforms (ECC), segmented into tweens with preset eases, holds and steps.
5. Structure: stagger, repetition, containers, masks, counters, typing, cursors.
6. Render–compare refit: emit → render → per-element residual → merge/split/retype; keep a change only if residual + description length drops.
7. Read pass: VLM/agent names roles, shot intents and choreography from keyframe sheets, inside the CV structure; disagreements are reported.

## Evaluation

- **Round trip (P2)**: hand-authored templates written from the reads (kinetic title, logo sting, chat UI, scroller, block transition, prompt typing, counter, stepped zoom) with two-level truth (IR-expressible facts; structure labels marked `now` or `P3`) → render → analyse → emit → structural diff + pixel L1. Baselines are recorded; only `kf_parity` gates in P2.
- **Gold references**: `gold/mk571.html` and its truth; more as references are read.
- **A/B of agent views (P1)**: blind packets answered by fresh subagents now, by the production model when credentials exist.
- **IR checks**: CLAUDE.md §1 in every QA document.

## Phases

| Phase | Deliverable | Exit |
| --- | --- | --- |
| P1 view | emitter, parity, fidelity, LLM view, agent switch, A/B | parity ≤ 0.002/frame; fidelity and A/B recorded |
| P2 round trip | templates, render, diff, `eval-roundtrip`, mk571 gold | deterministic harness; baseline recorded |
| P3 primitives | shots, shapes, holds, splits, masks, counters, cursors, containers, patterns, parser | numpy = Chromium; AE/Lottie handled; emit → parse → emit idempotent |
| P4 fitting | structure stage, shape typing, hold fitting, shots, refit loop, read pass | P2 structure F1 and L1 beat baseline; no gold regression |
| P5 edits | kf-script patches, new-primitive edits, verify-repair, AE export, production A/B | patches apply with stable ids; verify-repair passes the A/B edit set |

## Risks

- GSAP seek semantics with explicit tweens (P1 parity first).
- View size on busy scenes (rank + budget).
- A/B validity: subagent models are stronger than production; small n.
- Disk and CPU in the container (prune caches; 540p cases).
- Paid-template previews: research use only, never committed.
