# fixes7: the tracker's "same content?" judgement, learned

## Why

The B13 rules took four revisions, each patching the counterexample the last one met:
- letters breaking up in a blur;
- a wipe circle growing across a gap;
- a sliding striped card.

On 2026-10-16 the user judged that fitting rules to references. They reverted B13 (`57b54d5`, B12 kept) and asked for the judgement to be learned instead. Their decisions:
- the scope is the tracker's "same content?" call;
- train with torch, run the model in numpy;
- part of the gold set may be used in training;
- find more data on the web when it runs short;
- SaaS launch videos count as data too.

The rules of `fixes5.md` still hold:
- one change per comparison;
- accept on the synthetic round trip plus gold;
- nothing tuned on the eval clips.

Every flag below was answered with data, features or labels, never a hand rule.

## The model

- **What it outputs.** `keepframe/analyze/assoc.py` gives P(two regions in two frames hold the same content).
- **Features (18 per pair).**
  - Geometry: shift over size, area ratio, box overlaps, centroid-aligned mask IoU (with and without scale), aspect change, gap, frame edges, size.
  - Appearance: Lab ΔE, a colour histogram distance, NCC of 24 px grey crops and of their gradients.
  - Context: how much nearer each region's nearest neighbour in the other frame is than the pair itself.
- **Network.** A 16→32→32→1 MLP (18 inputs now), trained with torch by `scripts/train_assoc.py`.
- **Inference.** Runs in numpy from `assoc.npz`, which ships as package data.
- **How the tracker uses it.**
  - Hungarian cost 1 − P, matched when P ≥ 0.5.
  - A gap join (≤ 8 frames) also needs P ≥ 0.5.
  - Without the weights, or on any error (`assoc_failed`), the rules run as before.
- **Training data** (`eval/assoc/`, never committed):

| Source | Clips | Truth |
|---|---|---|
| kf-script synthetic kinds, seeds 2–9 | 152 | element boxes; seeds 8–9 held out |
| `keepframe.ir.synth` generators, seeds 5000–5059 (tests use < 100) | 119 (+1 the generator cannot draw) | each element's own alpha; seed % 5 == 0 held out |
| LottieFiles free animations | 116 (+34 lottie-web cannot render) | per-owner alpha from isolation passes; a fifth held out |
| gold mk571 (Mixkit) | 3 windows | its reproduction page; trained on (the user's choice) |
| Envato SaaS launch previews | 117 windows of 40 videos | video-derived (ECC) labels; a fifth held out; the shipped model does not train on them (below) |

- **The label.** A pair is the same content when all three hold:
  - one dominant owner;
  - at most a quarter foreign;
  - b where the owner's motion takes a (within half of b's size), **or** both regions are the owner's main body (more than half of what shows of it).

## How it got here (each step a fix in data, features or labels)

| Step | Commit | What failed | Answer |
|---|---|---|---|
| 16 features | `e41cf3e` | Fast suite, seed-61 end-to-end: a lone mover 83 px in one frame, P 0.088, split in two. In the data a far identical shape is almost always another copy (Lottie: 4 of 3366 same), and a pair alone cannot tell them apart. | Two context features (`387eb3c`); P 0.80 |
| 18 features | `b67b155` | Three fast-suite tests on `ir.synth` scenes (90° turns in 24 frames, fades from 0, occlusion); all pass with the rules | The ir generators as training data (`75784e4`); an occluded owner's main body labelled the same content (`d4403d1`: flips 0.0–0.5 % of labels) |
| shipped | `8b23086` | `test_titles_guard` patched `solid_errors` everywhere to fail the title guard. The matcher now finds the scene's mover as one solid (5 elements as in the truth; the rules gave 6, the mover in 4 pieces), and `finish_solid_assets` hit the patch. | The test's patch now fails only the guard's measure. Fast suite: 2955 passed. |

## Held-out metrics (`docs/qa/assoc/train.json`)

| Held-out source | Shipped (no SaaS) AUC / acc | With SaaS AUC / acc |
|---|---|---|
| synthetic seeds 8–9 | **0.9961** / 0.973 | 0.9941 / 0.964 |
| ir scenes (seed % 5 == 0) | **0.9823** / 0.933 | 0.9752 / 0.911 |
| Lottie (a fifth) | **0.9942** / 0.966 | 0.9897 / 0.952 |
| SaaS (a fifth, against video labels) | 0.890 / 0.939 | 0.976 / 0.940 |

**Why the shipped model leaves SaaS out.** It loses on every truth-labelled source when SaaS is added.
- The SaaS labels agree with truth at 0.90 on the training synthetic and Lottie clips. For "same", their precision is only 0.64 and recall 0.73.
- The agreement curve is flat from ρ 0.05 to 0.65: the masks' mutual coverage decides, not the correlation.

**Leave one synthetic kind out** (`leave-one-kind-out.json`):
- most kinds score AUC ≥ 0.98 when unseen;
- the weak ones are block_transition 0.91, big_scroll 0.94, whip_pan 0.95, scroller 0.96.

## Round trip (`fixes7/m1.json`, `rt6-g1` → `rt7`, code `8b23086`)

**Environment.** Torch was installed in `.venv` to train, and it switches the pipeline's torch refine on. B12's baseline ran without it ("refine skipped"), and the refine caused OOMs on mk571 and envato1. Torch was therefore removed before the gold and real-clip runs.
- The round-trip cases use `refine=False`, so they were unaffected.
- Reinstall torch only to retrain.

| | Before → after | Note |
|---|---|---|
| Flags (parity ≤ 0.001, L1 kf ≤ +0.001, shot F1) | none | worst: hard_cut and label_grid +0.0002 |
| logo_sting | kf L1 **0.0282 → 0.0101** | the opening black full-frame layer now ends where the cream panel takes over (f6); see stray below |
| scroller, wipe, glow_cut, big_scroll | 0.0621 → 0.0605, 0.0066 → 0.0062, 0.0067 → 0.0065, 0.0745 → 0.0741 | glow_cut fragments 1.5 → 2.0 |
| mk571 (seen in training) | kf L1 0.0180 → 0.0094 | elements and positions unchanged |
| e24 typing | 23 glyphs kept | |
| **glow_merge (the target)** | **0.0274 → 0.0273: not met** | see below |

**glow_merge.** The truth makes the purple glow background. The analysis makes it two sprites over f0–43, whose tracks end when the letters reach it (f44). The specks the gap merge joined to them at B12 now start their own tracks.
- So the association is no longer what is wrong.
- What remains is the glow read as foreground, and dropped from f44 on. That belongs to the plate's foreground/background call, which is out of this change's scope.

**Stray area.** Stray area grows on logo_sting and mk571 (0 → 1.00), and a little on hard_cut, wipe, glow_title and glow_letters.
- On logo_sting and mk571, the opening full-frame layer is now cut where a panel takes over. Its first piece comes before any truth box, so the diff counts a full-frame stray (as with B13 in `fixes6.md`).

**Cost.**
- The first version's features took about 4 ms a pair, about 90 % of it the centroid-aligned mask IoU drawn on a canvas twice the masks' size.
- Tracking took about 5 s per round-trip case (rules: 0.1 s) and 149 s on mk571. On the real clips it took 3859 s over all 34 (rules: 35 s), 1237 s of it on envato1 at 2560×1440.
- The IoU now counts only where the two masks overlap, with centroids from row and column counts. The numbers are identical:
  - a randomized equality test against the canvas version (`tests/test_assoc.py`);
  - a logo_sting analysis whose scene and textures match byte for byte.
- It is 5–9× faster (0.9 vs 4.4 ms on a 300×200 pair, 15 vs 137 ms on a 2000×1000 pair).
- The round trip and the clips above stand as measured.

**Real clips** (`clips.md` §9, report only):
- The Mixkit set improves on average (kf L1 0.0284 → 0.0269; 6 better, 2 worse).
- The held-out SaaS previews are even (0.0415 → 0.0420).
- ig1 improves (0.0332 → 0.0288).
- **envato1 gets worse (0.0681 → 0.0767; f20–24 0.06–0.08 → 0.14).** The bottom glow takes in blurred letters a little each frame, every step looks like the same content, and the matcher chains f0–82 into one sprite.
- A pair-wise judgement cannot see drift that builds up. The upgrade path is a track-level comparison (each new region against the track's earlier regions), trained on the same truth.

**Decision: accepted** on the round trip (no flags, e24 kept), with the target unmet and explained. The real clips are in `clips.md` §9.

## Drift: M2 (a window rule, reverted) and M3 (track-level features, shipped)

**The problem.** M1 judges each step on its own. envato1's bottom glow takes in blurred letters a little each frame, so every step looks like the same content and the track chains f0–82. The user's direction (2026-10-17) was the upgrade path above: compare each new region with the track's earlier regions, trained on the same truth.

**M2 (`7f70a4f`, reverted in `f7ef77c`).** A candidate continued a track only if the pair matcher called it the same content as both:
- the track's last region;
- the track's oldest region within 8 frames.

Results (`fixes7/m2-window.json`, round trip against M1):
- No flags. envato1 went back to 0.0684 (B12 0.0681; M1 0.0767).
- But the fast suite failed seed 62 end-to-end, which ends with 6 elements instead of 4. A green fading in from opacity 0 changes colour over 8 frames more than a matcher trained on single pairs forgives.
- Taking the minimum of two pair scores is a rule, not something learned, so it was reverted.

**M3 (`a315863`, weights `ff38350`).** The track-level comparison is learned.
- **Features: 38.**
  - The 18 pair features of (last region, candidate).
  - The 18 pair features of (anchor, candidate). The anchor is the track's oldest region within 8 frames of the candidate.
  - What the candidate took in from the last region's frame's other regions.
  - What of the last region the candidate's frame's other regions now hold.
- **Label.** The candidate continues the last region, **and** it holds the track's content at the anchor. That is the truth label of both pairs; in training, the anchor is the truth owner's region.
- **Network.** 64 wide; trained on synthetic, ir scenes, Lottie and gold, as M1 was (`docs/qa/assoc/train.json`).
- **Held-out accuracy** (on its own, anchored label): synthetic 0.982, ir scenes 0.970, Lottie 0.981. AUC 0.998, 0.991 and 0.997.
- **Leave one kind out:** block_transition 0.86 and whip_pan 0.95 are the weak kinds.
- **Round trip** (`fixes7/m3.json`, M1 → M3): no flags, and e24 keeps its 23 glyphs.
  - Improved: logo_sting 0.0101 → 0.0081, block_transition 0.0217 → 0.0191, wipe 0.0062 → 0.0039, mk571 0.0094 → 0.0075.
  - glow_merge stays at 0.0273 (the plate's call, above).
  - Stray area grows on block_transition (2.0 → 3.04) and wipe (0.74 → 1.24).
- **Real clips:** `clips.md` §10.
- **Speed.** The share a candidate took in from its neighbours is read from a per-frame coverage map with the same numbers: a randomized equality test, and an identical analysis. That is about 5× faster on busy frames.
- **Fast suite.** `test_multiscene_analysis_shares_generation_cap` fails. Its scale-0.5 sphere stays one 3D candidate only while its pieces' median track is shorter than half its life, and M3 lands exactly on that boundary (6 of 12 frames).
  - The user chose to learn that decision too, not to move the boundary (below).

## The 3D candidate decision, learned (fixes7 S)

**Why.** `find_solids` kept a candidate when it was:
- broken into tracks whose median length is under half its life; or
- a stable box whose look changes (1 − NCC ≥ 0.15).

M3 put a test sphere exactly on the first boundary. The user (2026-10-17) chose to learn the decision, not to move the boundary. Their goal is that an AI can understand a scene and recreate it, so:
- a turning 3D object read as twenty fragments is a miss;
- a 2D card marked `pending_asset="3d"` misleads.

**The judge.**
- **Hard filters (unchanged).** A candidate must still pass the life floor, the area cap and the title veto.
- **Features (18 per candidate,** `solids._features`):
  - how long it lives;
  - how the region tracker breaks it up, and how much of it long tracks cover;
  - how its look changes in its box:
    - per step, raw;
    - after the best affine warp on the candidate's own pixels (ECC from 0°/±12°);
    - chained over 8 frames (drift);
  - its contrast;
  - how its outline, box and place change;
  - its size and fill;
  - whether it touches the frame edge.
- **Model.** A numpy MLP (`solids.npz`, the matcher's `Matcher` class) decides at P ≥ 0.5.
- **Fallback.** Without weights, with unfit weights, or on any error (`solid_judge_failed`), the rule decides as before.
- **Tests.** Direct `find_solids` unit tests pass no pipeline tracks, so they keep testing tracking, the area cap and the title veto under the rule. The analyses use the judge.

**Data.** Truth is by construction (`scripts/train_assoc.py solid_clip`; never committed). A candidate is a solid when one texture cannot show it.
- **Positives:**
  - textured spheres turning about an axis off the view axis;
  - tumbling boxes;
  - noise blobs whose look changes in place.
- **Negatives** (one texture moved, scaled, turned in the plane or faded):
  - cards, words and discs;
  - patterns spinning in place.
- **Clips:**
  - 1600 clips (seeds 6000–7599), half through an mp4v encode;
  - 600 `keepframe.ir.synth` scenes (seeds 7000–7299, the tests' generators with titles, plates and movers), every element 2D.
- **Labels.** The candidates come from the pipeline itself, stopped after its solids stage, so the regions and tracks are M3's. A candidate is labelled solid when half its pixels or more lie on a positive.
- **Hold-out.** Clips with seed % 5 == 0 are held out (439).
- **Weighting.** Training weights and the 5-fold cross-validation that picks the size give a third each to:
  - solids;
  - the clips' other objects;
  - the ir scenes' 2D elements.

**Held out, against truth** (`docs/qa/assoc/solids.json`, h32 e500):

| | Learned | Rule |
|---|---|---|
| AUC | 0.976 | — |
| accuracy | **0.938** | 0.888 |
| solids found (122) | **0.828** | 0.566 |
| the clips' other objects kept out (1187) | **0.934** | 0.912 |
| the ir scenes' 2D elements kept out (347) | **0.989** | 0.919 |

**How it got here** (each step answered with a feature or data, never a threshold):

| Step | What failed (held out, or a read of the sheets) | Answer |
|---|---|---|
| 15 features | False positives were rigid cards: the warp was scored on the box crop, ground included | Score the affine residual on the candidate's pixels |
| Masked residual | Flat objects (codec noise reads as change); overlapping 2D sprites merged into one candidate | Contrast; coverage by long tracks (17 features) |
| 17 features | A striped disc turning in the plane is a 3D candidate: its stripes shatter like a turning surface, ECC from identity fails past ~10° a frame, and spinning patterns were rare | Multi-start ECC; spinners as a quarter of the 2D objects |
| Balanced on solids vs everything else | 12% false positives on the ir scenes (rule 6.5%), static candidates among them | A third each to solids, clip objects and ir elements; 400 more ir scenes |
| Same | 13 of 20 isolated spinners called solid: over one frame a slow 3D turn is as affine as a turn in the plane | Drift of the chained step warps over 8 frames (spheres 0.77–1.53, boxes 0.30–0.97, spinners 0.02–0.22) |
| 18 features | Spinners moved like every other object, so an in-place spinner with a still outline was almost never a negative | Spinners in place 70% of the time, as their docstring says (653 clips regenerated) |

**Open.** Isolated patterns spinning in place, alone in the middle of the frame, are still called solid in 10 of 20 draws. The rule calls nearly all of them solid.
- The test disc passes.
- Picking a model by this diagnostic would fit it, so the choice stays with the cross-validation.

**Tests.** The fast suite passes (2894, plus 136 in the solid, mover, title and analysis-golden files). `test_multiscene_analysis_shares_generation_cap` passes, and a new test keeps the in-plane disc out.

**Round trip** (`fixes7/s-judge.json`, `rt9` M3 → `rt10` at `2d80588`; torch not installed):
- No flags. All 19 cases and mk571 are within 0.0003 kf L1 of M3; e24 keeps its 23 glyphs.
- None of the round-trip kinds holds a 3D object or a 3D candidate, before or after, so the judge changes nothing there.
- A first `rt10` timed out rendering glow_merge's reference page. That page loads gsap and fonts from the `wt-g1` worktree, which a disk cleanup had removed. The worktree was restored and the run repeated.

**Real clips** (`clips.md` §11, report only):
- M3's 6 candidates are all removed, and no new one is added:
  - footage patches and a title letter on mk793;
  - a video-background patch on mk547;
  - a title stroke on ig2;
  - a logo glow on mk798.
- My read of the frames calls none of them a 3D object.
- kf L1: mk547 0.0292 → 0.0285, mk793 0.0246 → 0.0253, mk798 0.0140 → 0.0139. The other 31 clips are identical.

**Decision: accepted.**
- Held out, it beats the rule on every truth-labelled group.
- The round trip has no flags, and e24 keeps its glyphs.
- On real footage, all six of the rule's candidates were false and all six are gone.
