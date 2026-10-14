# kf-script P3 · IR primitives — Implementation Plan (task level)

> **For agentic workers:** this plan is task level. Before starting, refine each task into Deliverable / Files / Interfaces / Gates / Tests against the code at that commit, in the P1 plan's format. One commit per task.

## Context

- Spec §Format ("P3 extends the vocabulary") and §Redesigned analysis.
- P2's truth level (b) lists the structure the IR cannot express: shots, shape sizes, holds/steps, splits, masks, counters, cursors, containers, patterns. P3 adds them to the IR and to every renderer and exporter, so P4 can fit them and P5 can edit them.

## Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | No new `Element.kind` literals; new primitives are optional sub-objects with `exclude_if`. | 23 kind-dispatch sites (AE spec 9, Lottie 7, edit/apply 4, composer 3) would break. |
| D2 | Every primitive lands in numpy (`analyze/composite.py`) and Chromium (template) together, with a parity test. | numpy is the analysis renderer; Chromium is the product renderer. |
| D3 | AE: supported or a warning; spec `SPEC_LEVEL` bump when the spec gains a field; ExtendScript stays ES3. Lottie: supported or refused with a message. | As Stage B. |
| D4 | kf-script parser over the P3 subset (stdlib `html.parser` + a tokenizer for `tl.fromTo/to/set` and helpers); emit → parse → emit idempotent. | P5 patches and P2 truth level (b) need it. |

## Tasks

| Task | Deliverable |
|---|---|
| 1 | `Scene.shots: [Shot{id, frames:(a,b), transition{type: cut|fade|slide|wipe|blocks|zoom|unknown, frames}, intent}]` (`exclude_if` empty); the pipeline fills it from `analyze/shots.py` boundaries inside the analysed range; kf-script labels per shot and a `shots:` header line. |
| 2 | Shape primitive `Canonical.shape{kind: rect|ellipse, fill, stroke, radius}` on kind `sprite`; new PROPS `w`, `h`, `stroke_w` (DEFAULTS, ERR, `_known_props`); numpy draws it; composer emits a div with border/radius; AE shape layer; Lottie rect/ellipse. |
| 3 | `Keyframe.hold: bool` (`exclude_if` False): the value holds until the next key, then jumps; `eval_track`, template, numpy, AE hold keyframes, Lottie `h: 1`. |
| 4 | `Canonical.split{unit: word|glyph, items, delays, ramp, from: start|end|random, props}` generalising `GlyphReveal` (which stays as the glyph/opacity case); retime and retext keep it; AE text animator or a warning. |
| 5 | Masks: `Canonical.mask{kind: inset}` with PROPS `mask_l/r/t/b` (generalising `reveal`); numpy column/row cuts; composer clip-path; AE mask path keyframes; Lottie mask. |
| 6 | Counter: `Canonical.counter{from, to, format, snap}` + a `value` track on a text element; numpy and composer render the formatted value per frame; AE source-text keyframes. |
| 7 | Cursor: role `cursor` on a sprite with a `press` track (scale dip); brief and kf-script name it. |
| 8 | Containers and patterns: `Element.parent` (transform composition in numpy, nested DOM, AE parenting, Lottie `parent`); `Group.pattern{template, instances[{id, dt, dx, dy}]}` emitted as a nested timeline. |
| 9 | kf-script helpers (`typeOn`, `counter`, `cursor`) in the template runtime; the emitter uses them; the parser maps them back. |
| 10 | Parser `kfscript.parse(doc) -> Scene` over the subset; `emit(parse(emit(s))) == emit(s)`; P2 truth level (b) items move from `P3` to `now` where the IR now expresses them; truth page vs IR render parity on the P2 templates. |

## Gates

- numpy vs Chromium per primitive: mean L1 < 0.02 (as `test_compositor_matches_browser`).
- Legacy scenes bit-identical (composer hashes, `test_spec_unchanged_for_legacy_scene`).
- P2 round trip: `kf_parity` holds; structure items now expressible switch from `unsupported` to scored.

## Verification

Fast suite; browser parity tests per primitive; fake-AE tests; `keepframe eval-roundtrip --reuse` against the P2 baseline.
