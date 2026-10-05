# Round 2 evaluation

Commands (clips in the main checkout's `eval/`, not committed):

```
keepframe gate-m2-real --clips eval/clips --out eval/out/<name> --max-frames 150 --render-check
python scripts/eval_prompts.py --project eval/out/<name>/<clip> --workspace eval/ws --gold docs/qa/round2/gold/<clip>.json
```

- `mean_l1`: the analysis reconstruction compared with the source frames (numpy composite).
- `render_l1`: the composed HTML rendered in Chromium, compared with the source on every 10th frame. This is the end-to-end number.

## Baseline (2026-10-04, commit ff9e6f3 = master analysis code)

| clip | elements | kinds | mean_l1 | render_l1 | low_conf | keep_on / constraints |
| --- | --- | --- | --- | --- | --- | --- |
| envato1 | 56 | sprite 47, text 9 | 0.0750 | 0.0868 | 43 | 4734 / 4744 |
| ig1 | 128 | sprite 84, text 44 | 0.0393 | 0.0425 | 86 | 55444 / 55484 |
| ig2 | 54 | sprite 39, text 15 | 0.0644 | 0.0738 | 47 | 5543 / 5549 |
| ig3 | 71 | sprite 61, text 10 | 0.0175 | 0.0219 | 38 | 6443 / 6443 |

`mean_l1` is identical to the round-1 final run, so analysis is deterministic.

Stage seconds (4 CPU cores, run alongside other work; indicative only, speed decisions use a separate quiet A/B run):

| clip | text (OCR) | regions | sprites | total |
| --- | --- | --- | --- | --- |
| envato1 | 477 | 460 | 406 | 1513 |
| ig1 | 810 | 207 | 157 | 1970 |
| ig2 | 572 | 336 | 326 | 1342 |
| ig3 | 459 | 44 | 24 | 633 |

### Prompt evaluation with gold (`gpt-6.1-sol`, preview only, scenes captioned)

The gold answers in `docs/qa/round2/gold/` (committed so they can be reviewed) are written from the frames, by content and position, so they stay valid when a scene is re-analysed.
- "ok" means the edit is a valid typed edit.
- "correct" means every target is the element a person would pick.
- Not graded: the logo prompt on clips with no logo, and the card prompt everywhere (there are several cards, or none). That leaves 25 gradable prompts.

| clip | ok / 8 | correct / gradable |
| --- | --- | --- |
| envato1 | 4 | 3 / 6 |
| ig1 | 5 | 2 / 6 |
| ig2 | 7 | 5 / 7 |
| ig3 | 3 | 2 / 6 |
| Total | 19 / 32 | 12 / 25 |

The background and whole-scene speed prompts are correct everywhere. Every miss comes from fragmentation:
- **"Second text":** the agent picks a fragment every time (envato1 `ul`, ig1 the title, ig2 a giant-word `e`, ig3 `U`).
- **Title prompts:** they fail on envato1, ig1 and ig3, where the headline is split into pieces, and the agent asks which piece to edit.
- **Logo prompt on ig2:** the Airbnb logo is not found, because it is not its own element.

## Final (2026-10-05, commit 6feae9e = merge head of feature/round2)

Measured on this machine (4 CPU cores, no GPU), with the 3D adapter configured
(TRELLIS, user's HF login) and a torch-free venv. Baseline is commit ff9e6f3.
Baseline timings ran alongside other work; final ran mostly alone, so speedups
are indicative.

| clip | analysis s base→final | mean_l1 base→final | render_l1 base→final | elements base→final | predicates base→final |
| --- | --- | --- | --- | --- | --- |
| envato1 | 1513→412 (3.7×) | 0.0750→0.0674 | 0.0868→0.0791 | 56→55 | 4744→983 |
| ig1 | 1970→366 (5.4×) | 0.0393→0.0402 | 0.0425→0.0455 | 128→126 | 55484→3769 |
| ig2 | 1342→412 (3.3×) | 0.0644→0.0646 | 0.0738→0.0712 | 54→50 | 5549→1033 |
| ig3 | 633→215 (2.9×) | 0.0175→0.0183 | 0.0219→0.0228 | 71→64 | 6443→678 |

An earlier run at c086117 (before the last two analysis fixes) gave similar
numbers; the head adds glyph-core text colour (envato1 title #140a35 → #e7dffd,
so edited titles stay visible) and the 60%-of-frame cap on 3D candidates.

### Synthetic gates

| Gate | Final result |
| --- | --- |
| `gate-m1` | 20/20 (constraints, hash, layer), at c086117 |
| `gate-m2` | Passed at 6feae9e: 18/20 frame_l1, tracking ok, temporal 0.798 |
| `gate-m3` | 8/8, at c086117 |

### Speed decisions

Adopted:

- Region labelling inside component boxes: bit-identical.
- ECC on ≤384 px crops.
- Report stage one-pass + ROI warps: ~22× on synthetic.
- OCR on 4 parallel single-thread engines: 2.6–3.5×, output identical frame by frame.

The OCR detection cap (`--ocr-max-side`) was rejected as a default speedup and
remains opt-in, default off. It changed text tracks (envato1 9→5, ig2 15→8) and
cost envato1 +0.0116 mean_l1.

### Plate and text reveal

Stroke/opacity now use the local plate colour (gradient clips −0.0006 mean_l1).
Refine composites over the plate are unit-tested, but unmeasured on real clips:
there was no GPU and CPU torch was too slow.

“Build SaaS Promo” (envato1) and “and it builts for you” (ig3) became one text
element each with a reveal track. The ig3 title “You just” fades in letter by
letter without box growth; it stays one element without reveal. Its giant
“just” zoom fragments remain.

### 3D generation and fidelity guard

At the merge head, the only 3D candidate is ig2's globe (e23): fragments .080,
still .102, model .291 → the guard kept the fragments. TRELLIS
(`trellis-community/TRELLIS` via `scripts/asset_adapter_hf.py`, ~50 s per GLB)
generates the GLB; the guard compares local L1 on every 5th visible frame
(lower is better).

Earlier run (c086117, before the full-frame cap) — 4 GLBs generated:

| clip / element | fragments L1 | still L1 | model L1 | Guard choice |
| --- | --- | --- | --- | --- |
| envato1 e1 | .157 | .182 | .185 | fragments |
| envato1 e33 | .087 | .185 | .148 | fragments |
| ig2 e1 (full-frame "Weekend" opening — now excluded by the cap) | .134 | .108 | .217 | still |
| ig2 e18 (globe) | .080 | .102 | .292 | fragments |

No generated model reproduced the source better than the alternatives.
Single-crop image→3D guesses the unseen side and texture.

Generation is automatic whenever `KEEPFRAME_ASSET_API_URL` is configured
(user decision). Object crops go to the public Hugging Face Space under the
user's account and use its ZeroGPU quota. Each analysis makes at most 2
generation requests, largest solids first; other candidates stay “3D 후보”
with a “3D 생성” button.

### Prompt evaluation: baseline→final (final scenes from c086117)

Gold in [gold/](gold/) is user-confirmed; 25 prompts are gradable.

| clip | correct baseline | correct final |
| --- | --- | --- |
| envato1 | 3 | 5 |
| ig1 | 2 | 2 |
| ig2 | 5 | 5 |
| ig3 | 2 | 2 |
| Total | 12 | 14 |

Valid typed edits (“ok”): 19/32 → 20/32. Remaining misses: “second text” picks
a fragment everywhere; ig1/ig3 title prompts ask which fragment.

### Gaps closed

- Agent `correct`/`set_keep` are preview-only, with full field display and browser confirmation.
- Font names are validated on every input path.
- Temporal verification has a per-element floor of 0.5 for elements with ≥10% of motion.
- Chat accepts SVG (sanitised, offline Chromium raster) and GLB.
- Pairwise predicates use only the 24 most salient elements (ig1 55k→3.8k).

### After Effects status

AE work is unfrozen. Image backgrounds map to a bottom footage layer, reveal
maps to Linear Wipe (angle 270° still needs live confirmation), and `kind="3d"`
maps to an AE model layer when AE ≥ 24.1 reports `model_layers`; otherwise a
substitution is proposed. AE verification extracts reveal/spin motions with
matching ids. The panel's ES3 regex character-class parse issue is fixed.

The kit and [live AE checklist](ae-live-check.md) are ready. Access uses
Tailscale: UI on the tailnet IP, relay through `tailscale serve` HTTPS.
**The user has not completed the live AE check; it remains open.** A
Higgsfield-style CEP extension (one install, no Python connector) is planned
after that check, by user decision.

### Known limits

- OCR still takes ~45-70% of analysis on text-heavy clips.
- Zoomed/kinetic text fragments and title/second-text prompt ambiguity remain.
- TRELLIS models lose to fragments or stills on these clips.
- Refine-over-plate is unmeasured on real clips.
- AE live verification is pending.
- Demo thumbnail 404 is pre-existing.
