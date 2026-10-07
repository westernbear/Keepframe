# 1. Layer decomposition of motion-graphics video (research for Keepframe)

Verified = read in arXiv HTML/PDF text this session. "(unverified)" = from memory/abstract only.

## 1. Recommendations (ranked for THIS product)

1. **Do not adopt any Omnimatte-family model as the core.** They solve a different problem: real footage, soft effects (shadows/reflections), 80-frame windows at 384x672 to 480x832, minutes to an hour per clip on A100/A6000/TPU. Nothing runs on CPU. Motion graphics has no shadows to associate, and flat/gradient/blurred content is exactly where their inpainting hallucinates. Keep only the *ideas*: per-layer RGBA + a "clean background" pass + a layer-recomposition loss.
2. **Build a classical, CPU "layer = RGBA sprite track + background plate" pipeline** (the real answer for static-camera 2.5D motion design):
   - Plate = per-pixel *mode/low-percentile-of-motion*, not median. For a big mover (title sliding, globe), compute the plate only over pixels/frames where the pixel is judged "static" (frame diff < t for that pixel in >=k consecutive frames); everything else is a hole. Fill holes by temporal copy from frames where the mover is elsewhere; remaining holes by LaMa/OpenCV inpaint (LaMa ~27 MB, CPU-feasible at ~1-3 s per 512 px crop, 1080p slow, run on crop only; (unverified timing)).
   - Moving element = connected region of (frame - plate) tracked over time (already in pipeline). Alpha/colour *un-premultiplication*: estimate RGBA by solving frame = a*F + (1-a)*plate with the plate known in that frame. This is LayerD's "inverse blending" step (verified), and fixes "textures carry background pixels" without any network. Use a *known-plate* matting formula (Smith-Blinn triangulation when two backgrounds are available, otherwise a=|frame-plate|/|F-plate| with F from the sprite's high-confidence core).
   - Animated background (rotating globe, moving gradient, particles): treat as a **video plate**, not an element. Rule: if a region is >~25 % of the frame, or its appearance changes without rigid motion (non-affine optical-flow residual), emit it as a looped/trimmed *background video clip* (crop + mask) rather than sprites. A globe sprite *inside* a bigger static background is better as its own "video sprite" layer (cropped mp4/webm with alpha, or an image-sequence), re-rendered via `<video>`/canvas. Only decompose to parametric motion when a rigid affine fit explains > 95 % of pixels.
3. **Use a segmentation/matting model on a remote GPU (HF Space/API) only for hard cases**: SAM 2 for object masks on keyframes (prompted by the classical blobs) and Qwen-Image-Layered for *single keyframe* decomposition of a complicated still (see below). Per-keyframe, not per-frame.
4. **Represent output as HTML/CSS layers + AE layer list** (already the target). Lottie is not a good target for photo/gradient/blur content (see Avoid). Use Lottie only for pure-vector icons.
5. **Use LLM/VLM for layer *grouping and naming*, not pixels**: give the VLM the plate, element crops and tracks, ask it to merge fragments (letters -> one text), flag "this is an animated background". MG-Gen shows this layer-as-`<img>`-with-caption pattern works for stills; Animation2Code shows the VLM cannot be trusted for motion timing (temporal sim 0.21-0.31), so motion must come from tracking, not from the VLM.

## 2. Per-paper notes

### Omnimatte (Lu et al., CVPR 2021, arXiv 2105.06993)
- Separates: per-object RGBA layer incl. associated shadows/reflections. Self-supervised per-video 2D U-Net; needs rough masks (Mask R-CNN), RAFT flow, homography background.
- Numbers: paper main text gives none for runtime/hardware (verified: not stated). Per-video optimisation; GPU.
- Limits (verified): cannot separate things that stay stationary relative to background; bad when homography isn't a good background model; random-init variance.
- Code: Google research code, Apache-style (unverified). CPU: no.

### Omnimatte3D (2023) / OmnimatteRF (Lin et al., 2023, arXiv 2309.07749)
- OmnimatteRF = 2D RGBA foreground net + static TensoRF 3D background (handles parallax). Verified: single RTX 3090, up to 6 h full training, ~30 min bg retrain, <=15k iterations. Beats Omnimatte, D2NeRF, LNA on Kubric/Blender/DAVIS (PSNR/SSIM/LPIPS; exact numbers not extracted). Generative Omnimatte table: OmnimatteRF Kubric 40.91 dB / LPIPS 0.028 and ~3 h per video.
- Limits (verified): fails if bg is shadowed nearly always; limited resolution (UNet); static-scene assumption. Our static camera makes the 3D part pointless. CPU: no.

### Generative Omnimatte (Lee et al., CVPR 2025, arXiv 2411.16683)
- Separates: RGBA layers of masked objects + their effects, completes occlusions, handles dynamic backgrounds. Casper = video diffusion (Lumiere) fine-tuned with a **trimask** (remove / keep / uncertain) conditioning, then test-time optimisation to RGBA.
- Verified numbers: Kubric PSNR 44.07 / LPIPS 0.010 (OmnimatteRF 40.91 / 0.028, ObjectDrop 34.22 / 0.083). Original runtime ~12 min Casper + 15 min SSR upsample + ~7 min/layer = 35-49 min for 3 objects on TPUs.
- Public reimplementation (verified, github gen-omnimatte-public, Apache-2.0, CogVideoX-5B licence applies): CogVideoX-Fun 384x672, 85-frame windows (multidiffusion for 197), A100 1-2 min, A6000 4-5 min, then ~8 min omnimatte optimisation; Wan2.1 1.3B 480x832 ~10 min; 14B ~55 min. Authors say public weights are worse than the paper's.
- Limits (verified): no physical deformations, similar objects need instance masks, may attach unrelated bg effects.
- Fit: wrong domain (resolution <=480p, ~3-8 s clips only, minutes per clip, needs 48 GB GPU, not an HF ZeroGPU job). Could be a *rare* remote fallback to remove a big mover from a plate. Not recommended.

### OmnimatteZero (Samuel et al., SIGGRAPH Asia 2025, arXiv 2503.18033)
- Training-free; LTX-Video v0.9.1 / Wan2.1, 30 steps, temporal+spatial attention guidance, TAP tracking for object/effect extraction.
- Verified: 0.04 s/frame on A100 (24 fps) vs ~9 s/frame for Gen-Omnimatte; PSNR 35.11 (Movie) / 44.97 (Kubric), LPIPS 0.014 / 0.010, SSIM 0.992 (movie).
- Limits (verified): VAE round-trip changes pixels (bad for "keep original look"), TAP fails under heavy occlusion/low res, occlusion handling ~60 % in hard cases.
- Fit: the fastest GPU option (LTX is small, fits a 24 GB GPU); possible HF-Space remote job for background clean-plate inpainting of a mover. VAE drift and 8-px latent grid are a problem for crisp text/UI edges. Optional, later.

### EasyOmnimatte (arXiv 2512.21865, Dec 2025) and DBL-Diffusion (arXiv 2607.25802)
- EasyOmnimatte: Wan2.1 inpainting DiT + dual LoRA experts (effect / quality), end-to-end RGBA + background; verified "<10 s" per clip vs minutes for Gen-Omnimatte; trained on synthetic compositing, 8k its on 2 H100. Fails where base inpainter can't see effects. Code status unverified.
- DBL-Diffusion: VACE-1.3B dual branch RGB + RGBA; 81 frames at 480x832, 50 steps, ~1 h/case on A100 80 GB (verified). Too slow.

### Layered Neural Atlases (Kasten et al., SIGGRAPH Asia 2021) and Hashing-NVD (Chan et al., ICCV 2023, arXiv 2309.14022)
- LNA: per-video MLPs map each pixel to atlas (u,v) per layer + alpha; edit the atlas, it propagates. >10 h for 100 frames of 480p (reported in the Hashing-NVD paper, verified via search summary).
- Hashing-NVD: hash-grid atlases, ~40 min on one 3090 Ti for 100 frames 1080p (~25 s/frame), plus multiplicative residuals for lighting. Successors: CoDeF, RNA (ACCV 2024), HyperNVD (hypernetworks). All GPU, per-video training, rely on smooth, non-rigid-but-continuous motion and texture; fail on cuts, text appearing/disappearing, topology change (typical in motion design). Not for Keepframe.

### LayerD (Suzuki et al., ICCV 2025, arXiv 2509.25134) — most relevant for stills
- Separates a flat graphic design into RGBA layers (text, shapes, images) by iteratively matting the unoccluded top layer (BiRefNet Swin-L fine-tuned on Crello, 19,478 train designs / 48,725 pairs), completing the background with LaMa, recovering colour by inverse blending, plus **palette-based refinement** exploiting uniform-colour layers.
- Verified: beats YOLO and VLM baselines on RGB L1 and alpha IoU; user study 3.74/5, 71.4 % ranked first; fails on small objects, ambiguous granularity, needs shorter side >= 512.
- Code (CyberAgentAILab/LayerD, GitHub) public; licence unverified. BiRefNet Swin-L is ~220M params: CPU ~tens of seconds per image (unverified). Fit: run on 3-5 *key* frames (start-of-hold states) to get clean RGBA sprites, then track them. The palette refinement idea (snap flat layer to few colours) directly helps "text colour on coloured object" and "texture carries background pixels".

### Qwen-Image-Layered (arXiv 2512.15603, Dec 2025)
- Image -> N RGBA layers (variable, max 20 in training) via RGBA-VAE + VLD-MMDiT on Qwen-Image; trained on PSD-derived data. Apache-2.0 weights (verified on HF: Qwen/Qwen-Image-Layered, diffusers pipeline, 4-bit/GGUF/FP8 community builds; official HF Space Qwen/Qwen-Image-Layered exists, 538 likes).
- Fit: **best remote-GPU option for a keyframe**: give it a 1080p keyframe, get up to ~8-10 RGBA layers (text separate from card from background). Per-image, ~20B-class model: needs a GPU Space; a Space call per keyframe is feasible, per-frame is not. Failure modes: layers do not match source pixels exactly (generative regeneration, risk of changed text/colours -> must verify by recompositing and reject if L1 high), layer count is not controllable per element, no temporal consistency. Use as a *proposer of layer partition*, then re-assign our pixel-exact sprites to its layers.
- LaDe (arXiv 2603.17965): 11B, 256 H100 training, image->layers PSNR 32.65 (2-layer), no public code (verified: none mentioned). Skip.
- LiWi (arXiv 2605.14552): photos -> fg + bg + shadow layers; code "to be released"; skip.

### Animation / code side
- **Animation2Code** (arXiv 2606.28593, 2026): 1,069 CodePen animations (1024x768, 30 fps, 2-8 s). Zero-shot VLMs: execution 80-100 %, appearance sim 0.62-0.84 (GPT-5.4 best), **temporal sim 0.21-0.31**; Qwen3-VL-8B 41.5 % static output vs GPT 13.1 %; SFT made it worse (appearance 0.43-0.46, temporal 0.08-0.09); dual-critic refinement reached 0.73 / 0.28. Motion types: rotation 58.9 %, translation 52.3 %, scaling 35 %, opacity 25.9 %. Lesson: VLMs reconstruct look, not timing; tracked keyframes must supply motion; metrics (DreamSim+DTW, CoTracker3 tracklets) are reusable as our edit-after checks.
- **MG-Gen** (CyberAgent, arXiv 2504.02361, code github CyberAgentAILab/MG-GEN): single raster -> layers -> HTML `<img>` per layer with captions -> LLM writes JS animation. Pipeline: DocumentAI OCR + Hi-SAM text-stroke masks (words as alpha), YOLOv11 (Crello, 18,864 imgs) for objects, SAM2 for non-rect, LaMa for background. Human eval only (20 images, 10 raters). Limits: tiny text, overlapping layers, big inpaints. Same representation as ours; Hi-SAM text-stroke masks are the right tool for text sprites without background.
- **MoVer** (SIGGRAPH 2025 / TOG, github jama1017/MoVer): first-order-logic motion verification for SVG/anim. Use as an assertion language for the edit-after tests ("title moves right, ends at x"). Not a decomposer.
- **LogoMotion** (arXiv 2405.07065): extracts layers from a PDF/logo, VLM writes code, visually-grounded refine loop. (Details unverified.) Same lesson: render-compare-refine loop.
- **OmniLottie** (CVPR 2026, arXiv 2603.02138, Qwen2.5-VL, MMLottie-2M, github OpenVGLab/OmniLottie): text/image/**video->Lottie**. Verified: real-set success 88.1 % video-to-Lottie, FVD 227.11; 33-110 s/sample; 60-frame normalisation; avg 8.6 layers (max 324); invalid output 2.7-9.3 %; data CC BY-NC-ND / non-commercial. **LottieGPT** (arXiv 2604.11792): Qwen2.5-VL-7B, 660K animations, valid render 96.96 %, trained 8xH20 a week per stage; fails on complex multi-layer compositions; code unclear. Both generate *icon-like vector* animations (LottieFiles/Icons8 data); they cannot represent photos, blur, gradients, UI screenshots, perspective text. Non-commercial licence blocks productisation.

## 3. Concrete plan for the two named features

**"Big moving objects become their own layer"**
1. Build plate hypotheses per pixel with a temporal-stability mask (static if |I_t - I_t±k| small for a run >= 0.4 s). Large components of the unstable mask (> ~3 % of frame area or longer than a text box) -> candidate "mover".
2. Track the mover (flow/affine; CoTracker3 on CPU is heavy, use OpenCV LK/ECC on its mask). Fit rigid/affine motion per segment; keep per-frame residual.
3. Estimate sprite RGBA from frames: for each sprite pixel, take the *median over frames after warping to the sprite frame* (a per-sprite temporal median in sprite space - it is the correct place to take a median, and removes the plate and motion blur partially). This reuses the median trick but in object coordinates.
4. Fill the plate hole where the mover sat from frames where it was elsewhere; inpaint only unseen leftovers.
5. If the sprite itself changes inside (globe rotating): residual after rigid fit large -> keep as a **video sprite** (alpha mask + cropped frame sequence), not fragments.
6. Verification: recomposite plate + sprites, report L1; and edit-after test: delete the sprite and check the plate has no ghost (min over sprite area of ghost score vs surrounding).

**"Animated background as a video plate"**: classify the plate type = static / gradient-drift (fit linear/radial gradient params per frame -> parametric CSS gradient animation, editable colours) / video (loop). Gradient drift is cheap to detect (low-rank PCA of the background across frames: if rank <= 3 and spatially smooth, fit it). For "recolour the background" the parametric form is required; the video-plate form allows only hue-shift/LUT. Offer hue-rotate or gradient-map on the video plate as the recolour edit.

## 4. What to avoid
- Neural atlases / NeRF backgrounds (LNA, Hashing-NVD, OmnimatteRF): hours of per-video GPU training, assumptions broken by cuts and appearing text.
- Casper/Wan/CogVideoX omnimatte for 1080p, 5-120 s: low-res windows (<=832 px), minutes-to-hour per clip, VAE drift destroys crisp UI/text; hallucinated plate.
- Video-to-Lottie (OmniLottie, LottieGPT) as the representation: icon-style vector only, non-commercial data licences, 60-frame normalisation, invalid-output rates, no raster/blur/3D.
- Asking a VLM for motion timing (Animation2Code: temporal sim ~0.3) or fine-tuning small VLMs on small code sets (it got worse).
- Per-frame generative layer decomposition (Qwen-Image-Layered): no temporal consistency; use only on keyframes with a recomposite check.
- Median plate for anything that moves slowly or lives >50 % of the clip.

## 5. Unverified / gaps
- LayerD, MG-Gen, LaMa exact CPU timings and licences; Omnimatte original licence; EasyOmnimatte code release; Omnimatte3D (not read); HyperNVD/CoDeF numbers; LogoMotion details; Qwen-Image-Layered runtime per image and Space GPU quota (check Space page).
