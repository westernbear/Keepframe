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
