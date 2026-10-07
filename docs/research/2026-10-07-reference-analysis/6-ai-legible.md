# 6. Making motion-graphics references AI-legible (VLM + CV + IR)

Evidence base: fetched via WebFetch summaries of arXiv HTML (Animation2Code, MoVer, OmniLottie, LogoMotion, MotionBench, Graphic-Design-Bench, Direction-Blindness, VisPhyWorld) plus search excerpts (FAVOR, TemporalBench, TransNetV2, OmniParser). Items marked (unverified) were not read in full.

## 1. Top recommendations (ranked for Keepframe)

1. **Do not ask the VLM for timing or motion numbers. Measure them with CV; ask the VLM only for semantics.** Evidence is consistent: VLMs reproduce appearance but fail on temporal dynamics (Animation2Code: appearance 0.62-0.84, temporal 0.21-0.31 for Gemini 3 Flash, Qwen3-VL, GPT-5.4, Claude Sonnet 4.6, Llama 4 Scout; 13-42% of "executable" outputs were static). Direction of motion is near chance in simple cases (Gemini 2.5 Flash 53.5%; others ~25-28% before tuning). FAVOR-Bench: Gemini-1.5-Pro 49.9%, Qwen2.5-VL-72B 48.1%, GPT-4o 42.1%. TemporalBench: GPT-4o 38.5% multi-binary accuracy vs 67.9% human. MotionBench: best models under 60%, counting repeated motions near random.
2. **Make the IR a typed scene graph with measured tracks, semantic labels, and a small set of named motion predicates (MoVer-style), and close the loop with a verifier.** MoVer: first-pass animation correctness 58.8%, up to 93.6% after up to 50 feedback rounds with predicate-level failure reports; minimal or no feedback is far worse. Logomotion: 68% error-free first pass, 96% after k=4 visually grounded repairs.
3. **Split the work into three roles**: CV = geometry/time/colour; VLM = naming, grouping, intent, style words, curve-family choice among candidates; LLM agent = write/edit IR, then verify by re-render and diff against the measured tracks.
4. **Use per-element crops and per-time-window keyframe sheets for the VLM, never the raw video.** Graphic-Design-Bench: component detection mAP@0.5 only 6.4%, font ID 23.7% over 167 typefaces, colour prediction sometimes dE>52. So never let the VLM supply boxes, colours or font identity from whole frames; give it cropped elements and ask classification or ranking questions (choose among N candidate fonts rendered side by side).
5. **Iterative render-compare repair beats one-shot**: Animation2Code refinement gave +4.4% appearance and +9.0% temporal in iteration 1 and little after. Fine-tuning a small VLM on video to code made it worse (temporal 0.24 -> 0.08), a warning against training on code priors without grounding.

## 2. What makes a video "AI-legible"

Layers, from cheap/deterministic to semantic:
- **L0 shots and beats** (CV): cut list, plus motion-energy beats (frame-diff energy peaks, audio onsets if present).
- **L1 scene graph per shot** (CV + VLM): nodes with id, role, bbox, z-order, parent group (card contains title, button, icon). Roles come from a closed vocabulary.
- **L2 measured tracks** (CV): per node x, y, scale, rotation, opacity, reveal-mask progress, sampled at video fps then fitted to keyframes + easing.
- **L3 motion predicates** (derived, deterministic): `slide_in(dir=left, dist=240px, t=0.40-0.72, ease=out_cubic)`, `pop(overshoot=1.08)`, `typewriter(cps=22)`, `mask_reveal(dir=up)`, `tilt3d(rx,ry)`, `camera_push(1.0->1.15)`, `stagger(step=0.06)`. The LLM edits predicates, which compile to tracks.
- **L4 style tokens**: palette (k-means in Lab, with roles bg/surface/accent/text), type (family guess + weight + tracking + case), effects (glow radius/colour, blur, shadow, gradient stops, grain), corner radius, spacing grid.
- **L5 intent notes**: free-text per shot ("hero line lands on the downbeat; UI card enters second to hold focus").

Reason it works: the LLM can read and edit discrete symbols (MoVer's atomic motion = agent, type, direction, magnitude, origin, duration, postcondition), while numbers come from measurement. Allen interval relations (before/meets/overlaps/during) between predicates capture "beats" and choreography better than absolute seconds alone.

## 3. VLM vs CV division of labour

| Task | Owner | Why / evidence |
|---|---|---|
| Shot cuts | TransNetV2 (CPU feasible; F1 77.9 ClipShots, 96.2 BBC; runtime on CPU unverified) or simple histogram + SSIM diff for hard cuts | Motion graphics have many full-frame transitions (wipes, dissolves) that look like content motion; confirm with VLM on candidate frames |
| Element boxes / text | RapidOCR + diff-vs-plate (existing) | VLM boxes are poor (6.4% mAP on design components) |
| Element role/label | VLM on crops (+ context frame with box drawn) | semantic task VLMs do well |
| UI structure inside mockups | Optional OmniParser v2 (YOLOv9 icon detect + Florence caption, MIT; GPU preferred, CPU latency unverified) or ScreenAI/Ferret-UI-style parsers (unverified details) | Gives grouping of cards/buttons/toggles; for stylised dark/glass UI expect misses; treat as proposals |
| Trajectories | point tracking (CoTracker3 as used by Animation2Code; CPU-slow) or box/template tracking + optical flow (RAFT/Farneback) for sprites | CoTracker3 is noisy on small, fast, low-texture synthetic elements per authors |
| Easing / timing | curve fitting on tracks (cubic-bezier fit, spring fit) | VLM can only say "ease-out, snappy" |
| Direction/magnitude | CV only | "directional motion blindness": direction info exists in encoders but is not bound to the answer |
| Camera push vs element zoom | CV: global affine fit over static-structure points | VLMs confuse camera and object motion (MotionBench camera category) |
| Style tokens | CV for palette/blur/glow numbers; VLM for names and mood | Colour from VLM unreliable |
| Narrative / intent / naming | VLM/LLM | |
| Verification | re-render + predicate checks + diff | MoVer-style |

Where VLMs fail, specifically: temporal precision (timestamps are text tokens that overfit language priors), repetition counting, left/right/up/down, camera vs object, small fast objects, font identity, exact colours, text rendering inside generated code (~25% illegible strings).

## 4. Prompt and schema designs

**Keyframe sheet prompt (per shot, per node set)**: give 6-12 frames sampled at CV-detected motion events (not uniform), each labelled with its exact timestamp burned in, plus a crop strip per candidate node. Ask for JSON only:
```
{"node_id":"n7","role":"headline|subhead|ui_card|button|toggle|cursor|logo|photo|icon|blob|glow|particle|background|shape",
 "text":"...", "parent":"n3|null", "style_words":["script","gradient-fill"],
 "motion_label_guess":"slide|scale|pop|typewriter|mask_reveal|tilt3d|parallax|fade",
 "confidence":0-1, "evidence_frames":[12,14]}
```
Then **constrain the guess by CV**: supply the measured track summary ("x -240->0 over 0.32 s, scale const") and have the VLM pick among compatible predicate names. This turns an open temporal question into classification, which VLMs handle.

**Beat/intent pass (LLM, text-only)**: input = shot list + predicate table with times; output = grouping of predicates into beats and a named choreography ("stagger left to right, 60 ms step"). Text-only avoids video misperception.

**Repair loop**: after the agent edits the IR, render, run predicate checks (position at t, monotonic direction, duration within tolerance), feed back a failure report naming predicate and numeric delta (MoVer's finding: detailed reports matter). Cap iterations at 3 for the cost curve (Animation2Code gains flatten after iteration 1-2; MoVer needed more only for hard prompts).

## 5. Recommended IR (read/edit by an LLM agent)

JSON (or YAML), one file, stable ids, human units (px at 1080p, seconds), four sections:
```
meta{fps,size,duration,palette_roles{bg,surface,accent,text}}
shots[{id,t0,t1,transition_in{type,dur},camera{track_ref|null}}]
nodes[{id,shot,role,label,parent,z,asset_ref|text{string,font_guess,weight,fill,tracking},
       style{radius,shadow,glow,blur,gradient,opacity_base},
       anchor{x,y} /*layout in parent coordinates*/,
       tracks{x,y,scale,rot,opacity,reveal,rx,ry:[{t,v,ease|bezier|spring}]},   // measured, source:"cv"
       motion[{pred:"slide_in",dir:"left",t0,t1,ease,params}],                  // derived, regenerates tracks
       provenance{source:"cv|vlm|agent",conf,evidence_frames}}]
beats[{t,kind:"cut|hit|word",nodes:[...]}]
checks[{id,pred:"at(n7,t=0.7).x≈960±8", status}]
```
Rules: tracks are the source of truth for rendering/AE export; `motion` predicates are a compressed editable view that compiles to tracks (agent edits "duration 0.5 -> 0.8" and tracks regenerate; manual track edits flip the predicate to `custom`). Text nodes carry string + style separately from motion so edit-after works (change the title, keep the animation). Keep a `plate` node and a `flags` list for nodes whose separation was uncertain (e.g. "possible plate smear").

## 6. Per-paper notes

- **Animation2Code (2026, arXiv 2606.28593)**: 1,069 CodePen videos (1024x768, 30 fps, 2-8 s). Metrics: appearance (DreamSim + DTW), temporal (CoTracker3 tracklet direction + speed, Chamfer). Table 2 numbers above; human agreement alpha 0.81/0.73. Limits: CoTracker3 not tuned for synthetic animation; CodePen only. Use: its metric design is directly reusable as our edit-after regression test (temporal similarity of re-render vs source).
- **MoVer (2025, arXiv 2502.13372)**: first-order-logic DSL over per-frame SVG, Allen interval algebra, rectangle algebra, GSAP target; 95.1% correct verifier programs, 58.8% -> 93.6% with feedback. Limits: complex paths, ambiguous language. SVG-only; we need a DOM/CSS-transform variant.
- **LogoMotion (2024, arXiv 2405.07065)**: layered PDF -> VLM layer captions + hierarchy -> pseudo-code concept -> anime.js; visually grounded repair; 96% runs resolved at k=4. Closest workflow to ours, but it generates motion from semantics rather than reconstructing measured motion.
- **OmniLottie (CVPR 2026, 2603.02138)**: Qwen2.5-VL backbone + Lottie tokenizer (5.3x shorter than JSON), MMLottie-2M; video-to-Lottie 88.1% success, FVD 227, PSNR 16.1, SSIM 0.82, 111 s/sample, 36.8k tokens; invalid sequences possible, limited context. Lottie is a useful export target, but PSNR 16 means not faithful reconstruction; GPU-bound. LottieGPT (2604.11792) similar (660K animations), not read.
- **MotionBench (2501.02955)**: 5,385 videos, 8,052 questions, six categories; <60% for all; higher fps and TE Fusion help. **FAVOR-Bench (2503.14935)**, **TemporalBench (2410.10818)**: numbers above. **Direction blindness (2605.22823)**: fix needs training (DeltaDirect), not prompting.
- **VisPhyWorld (2602.13294)**: models output text analysis + JSON spec + code; strong layout, weak dynamics; a backend with explicit physics helped. Lesson: give the model a backend where motion is parameterised by the right primitives (springs, easings).
- **Graphic-Design-Bench (2604.04192)**: 49 tasks incl. animation/temporal reasoning; numbers above.
- **OmniParser v1/v2 (2408.00203; MS repo)**: YOLO icon detector + caption model, MIT for v2 weights (v1 detector AGPL, avoid); 39.5% ScreenSpot-Pro with GPT-4o. Not designed for stylised tilted mockups (expect failures on perspective, glow). ScreenAI, Ferret-UI 2, UI-TARS: not read in full.
- **TransNetV2 (2008.04838)**: F1 77.9 ClipShots; CPU speed unverified (small 3D-CNN, likely feasible).

## 7. What to avoid

- Asking a VLM "at what second does X appear / how long does it take" or "which direction": near-chance or imprecise.
- Fine-tuning a VLM to emit code from video without grounding (Animation2Code: temporal 0.24 -> 0.08).
- Pure end-to-end video-to-Lottie (OmniLottie) as the main path: GPU, 110 s/clip, PSNR 16, no semantic edit handles.
- Using VLM-provided boxes, colours or font names directly.
- Judging success by appearance only: appearance saturates while temporal stays low; always include a temporal metric and edit-after checks.
- OmniParser v1 AGPL icon detector; treat all UI parsers as proposal generators.
- Uniform frame sampling for the VLM: sample at motion events with burned-in timestamps.
