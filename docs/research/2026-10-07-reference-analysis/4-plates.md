# 4. Clean background plates (foreground removal without smears)

Verified = read from paper/README this session. "Unverified" = my estimate or recall; benchmark on the target box before relying on it.

## 1. Recommendations (ranked for Keepframe)

**Core idea: do not inpaint first. Estimate the plate from pixels that are visible, using occlusion masks, and only inpaint what was never visible.** Motion-graphics backgrounds are flat or smooth gradients, so exact observation beats any generative fill.

1. **Masked temporal estimation (CPU, ~0 cost), the default.** Per pixel, take the median (or the mode of quantised Lab) over only the frames where the pixel is NOT in a foreground mask. Foreground mask = element boxes/segments (from the existing diff and OCR stage, dilated 6-10 px to cover glows, shadows and motion blur) plus the current "differs from plate" mask. Iterate twice: plate, then masks, then plate. Vectorised numpy on 24 to 120 downsampled frames costs about 0.2-1 s per 1080p plate (unverified). This replaces the plain median, which bakes in anything occupying more than 50 % of the samples.
2. **Static or video plate decision (see section 3).** Compute a per-pixel temporal variance on the unmasked samples. Low variance, so a single image. High variance, so a parametric or per-frame plate.
3. **Fill never-visible holes with classical or small-network inpainting, not video diffusion.** First choice is a flat or gradient fit (below). Fall back to LaMa on CPU for textured or photographic holes. Escalate to remote ProPainter only for animated plates with real occlusion.
4. **Measure cleanliness with residue metrics (section 4)** and use them as the gate for escalating.
5. **Avoid diffusion video inpainting (DiffuEraser, VideoPainter) for the default path.** It needs GPU, is slow, changes the pixels outside the hole through VAE round-trips, and invents texture on flat colour (see section 5).

## 2. Methods: numbers, licence, fit

**Classical background estimation**
- Median or mode with exclusion mask: CPU, trivial memory (T x H x W x 3 uint8; 24 frames at 1080p is about 150 MB). Fails only where a pixel is occluded in every sample. Handles gradients exactly if the gradient is static. Does not handle animated backgrounds, which need a per-frame or parametric model.
- Robust PCA or low-rank plus sparse (Candes et al. 2011, "Robust Principal Component Analysis?", JACM, from recall): the background is low rank across frames, the foreground sparse. It works for slow global changes (a pulsing glow, a moving gradient) because a few basis images capture it. On CPU, downsample to about 270p, run inexact-ALM, then upsample the temporal coefficients and apply them to a full-res basis. Cost is a few seconds to tens of seconds for 100 frames at 270p (unverified). Failure: large foreground (above ~30 % of the frame per frame) or foreground that is itself low rank (slow-moving big shapes) gets absorbed into the background. It also ignores the mask unless you use the masked or weighted variant (weighted PCA or EM-PCA). Use PCA on unmasked pixels only.
- Parametric fit for gradients and glows: fit linear or radial gradient plus Gaussian blobs to unmasked pixels (least squares on a low-order 2D polynomial or a coarse 16x9 grid with bicubic upsampling plus a small blur). This extrapolates cleanly into always-occluded areas. Cost is milliseconds. Fails on photos or textured backgrounds.

**Image inpainting**
- **Telea / Navier-Stokes (OpenCV `cv2.inpaint`)**: CPU, about 0.1-1 s for 1080p with small masks, smears linearly on large holes and cannot hold a gradient across a large hole. Use only for holes under ~10 px (anti-alias fringes, halo cleanup).
- **PatchMatch** (Barnes 2009; Content-Aware Fill lineage): CPU, seconds per 1080p large hole, good for repeated texture, produces visible repeats or streaks on smooth gradients. Often OK on flat colour. Skip.
- **LaMa** (Suvorov et al., "Resolution-robust Large Mask Inpainting with Fourier Convolutions", WACV 2022, arXiv 2109.07161; verified): FFC ResNet, 27M params (Big-LaMa 51M), trained at 256 crops but generalises to 512 and 1536 (paper), about 20 % slower than a regular conv net at 512. Apache-2.0 per repo (the arXiv text I fetched said CC BY 4.0 for the paper; code licence is Apache-2.0 from recall, verify). Runtime on 4-core CPU at 1080p: estimate 10-30 s for the full frame (unverified); crop to the hole plus 128 px context, resize to 512, then 1-3 s. GPU about 0.1-0.3 s (unverified). Memory about 2-4 GB at 1080p CPU. Limitation stated by authors: strong perspective distortion. On motion graphics: very good on flat and gradient fills (continues gradients), poor when the hole crosses a hard edge between two flat regions with no hint of where the edge goes. Not told about text, so mask leftovers (anti-aliased halos) stay if the mask is tight. **Dilate masks 6-10 px.** Packaged in IOPaint (Apache-2.0, `--device=cpu` supported, verified).
- **MI-GAN** (Sargsyan et al., ICCV 2023): 5.95M params, 296 ms for 256x256 on an iPhone 14 Pro Max (verified via search). ONNX export exists; a good CPU option for crops (about 0.2-0.5 s per 512 crop on 4 cores, unverified). Quality lower than LaMa on large holes; fine for flat fills. Licence MIT per repo (unverified).

**Video inpainting (all need a GPU in practice)**
- **E2FGVI** (Li et al., CVPR 2022, arXiv 2204.02663; verified): end-to-end flow-guided feature propagation plus focal transformer, 0.12 s/frame at 432x240 on Titan Xp, 682 GFLOPs, trained and tested at 432x240. Authors: "implausible content and many artifacts" under large motion or much missing detail. **CC BY-NC-SA 4.0, non-commercial**. Not usable for a product.
- **ProPainter** (Zhou et al., ICCV 2023, arXiv 2309.03897; verified): recurrent flow completion (0.005 s/frame vs 0.231), dual-domain (image and feature) propagation with reliability check, mask-guided sparse transformer. 0.083 s/frame on 480p-class setups (E2FGVI 0.085), 808 GFLOPs in the table I read (E2FGVI 986 there). PSNR 34.43 YouTube-VOS, 34.47 DAVIS. Repo memory table: 1280x720 50 frames 28 GB fp32 / 19 GB fp16, 80 frames OOM fp32 / 25 GB fp16; 720x480 11 / 7 GB; use `--fp16`, `--resize_ratio`, `--subvideo_length`. No documented CPU mode. 1080p is not in the table; extrapolating (about 2.25x pixels of 720p) means chunking and downsizing (unverified). **NTU S-Lab License 1.0, non-commercial**. Paper notes that gains come from motion-rich content; static scenes give flow propagation little to do. On motion graphics: flow is unreliable on flat colour (aperture problem) and on large glows, so propagation falls back to the transformer, which hallucinates blur. It works well for a moving opaque object over a textured or gradient background where flow of the background is near zero (static camera): it simply copies background pixels from other frames, which is what the masked median does anyway.
- **FGT** (Zhang et al., "Flow-Guided Transformer for Video Inpainting", ECCV 2022): ran out of 32 GB at 480p in the ProPainter comparison (verified in the ProPainter text). Skip.
- **DiffuEraser** (Li et al., 2025, arXiv 2501.10018, recalled; project page fetched; the arXiv HTML 404'd so paper numbers are unverified): SD 1.5 plus BrushNet plus AnimateDiff motion modules, ProPainter as prior. Repo table (250 frames on an L20 GPU): 1280x720 33 GB / 314 s, 960x540 20 GB / 175 s, 640x360 12 GB / 92 s. That is about 1.25 s/frame at 720p. Apache-2.0 but must comply with ProPainter's licence (non-commercial in practice). 1080p would exceed 48 GB without tiling (unverified).
- **VideoPainter** (Bian et al., 2025, arXiv 2503.05639; verified): dual-branch, a 2-layer context encoder (6 % of backbone) on CogVideoX-5B-I2V, trained at 480x720, VPData 390K clips. No runtime or memory in the paper. Limits quoted: base-model physics and motion limits, bad with low-quality masks. A 5B DiT is minutes per short clip on an A100 (unverified). Not suited to flat motion graphics at 1080p.
- Other 2024-2026 work (VIVID-10M, MiniMax-Remover, ROSE, etc.) not read; do not claim anything about them.

**Remote GPU (HF Space) cost** (estimates, unverified): a ZeroGPU Space gives an H200 slice for a short time window; ProPainter at 540p, 150 frames about 15-40 s compute plus 5-20 s queue/cold start plus video upload/download of 5-20 MB: about 30-90 s wall clock. DiffuEraser at 540p 150 frames about 110 s compute (from table scaling) plus overhead: 2-4 min. LaMa on a crop: about 1-3 s plus roughly 2 s round-trip overhead, which is only worth it if CPU LaMa takes over 10 s. Free ZeroGPU quotas are limited per account; budget at most a few calls per analysis.

## 3. Single image plate vs video plate

Decide per shot (after cut detection), from the unmasked variance map V (per pixel std over visible samples, Lab ΔE):
- **Image plate** if the 95th percentile of V over the frame is under ~1.5 ΔE (sensor-free synthetic video: a pure static background has V near 0; compression noise is about 1). Most cases: flat colour or a static gradient.
- **Parametric / low-rank plate video** if V is high but the variation is spatially smooth and low rank (a drifting gradient, a pulsing glow, a colour shift): RPCA or a small PCA basis with 2-4 coefficients per frame, stored as keyframed coefficients. Rebuildable in HTML as a gradient animation. That also matches the scene model better (the background becomes a keyframed gradient layer, not a video).
- **Per-frame plate video** only when content in the plate is really moving and texture-rich (rotating globe, video-like background). Better: pull the globe out as its own layer (goal 2) and make the remainder a static plate. Treat any region whose temporal std is high AND which has coherent motion as an element, not background.
- Always keep the plate separate from the element-bake: an element layer's texture must come from the source frame at alpha, never from the plate.

**Always-occluded areas** (no visible sample in any frame): (1) if surrounded by smooth colour, fit a 2D polynomial or coarse-grid fit to ring pixels (exact for gradients); (2) otherwise LaMa on a crop with 128 px context, mask dilated; (3) flag the hole in the plate metadata as "synthetic" so edit-time checks and the UI know that area is invented. If the occluder is an element that the user can hide, that is the common case. For text over a gradient, a gradient fit is better than any network.

## 4. Measuring plate cleanliness

- **Residue under removed elements (no ground truth needed):** for each element mask M (dilated by 4 px) compute plate vs. a smooth model: (a) mean ΔE between plate in M and the ring plate pixels (ring 6-16 px outside M) after removing the low-order gradient; (b) high-frequency energy ratio inside vs. outside the ring (Laplacian variance ratio, should be about 1 on flat backgrounds); (c) OCR the plate: run RapidOCR on the plate, expect zero detections (catches text ghosts); (d) re-diff: element masks recomputed against the plate should contain the element and nothing else; any region where "plate minus frame" is non-zero outside every element is baked content.
- **Synthetic test (best gate):** composite known sprites and text onto a plate in the test set (from Envato templates with the foreground layer off if available), run the pipeline, and report mean ΔE, 95th percentile ΔE and SSIM over the former element masks, plus the fraction of mask area ΔE > 3. Cheap to build and exactly the edit-after check (hide an element, compare).
- **Edit-after check:** remove each element, re-render, and compare with a source frame where the element is absent (when it appears/disappears in the clip, frames before and after give real ground truth for that region).
- **Temporal check** for video plates: flicker = mean |P_t - P_{t-1}| in unmasked regions vs. source flicker.

## 5. Failure modes on motion graphics

- Flow-based video inpainting: flat colours and glows give no flow (aperture problem); text fringes and motion blur leak into the mask boundary, so the plate shows halos if the mask is tight. Propagated pixels from semi-transparent glows double-expose.
- Diffusion inpainting: VAE round-trips shift colour/banding of smooth gradients, hallucinated texture or objects in flat holes, slow at 1080p, non-commercial dependence on ProPainter (DiffuEraser).
- Median with no exclusion: ghosts of anything present in over half the samples.
- RPCA without masks: slow-moving big elements absorbed into the background.
- Gradients: 8-bit banding appears after repeated resampling; keep plate in float or add 1-LSB dither at export.
- Transparency / blur layers (frosted-glass UI, depth-of-field): the plate under a translucent panel is changed pixels, not a hole; treat as an element with its own backdrop filter, and don't remove it from the plate unless it is truly a layer.

## 6. What to avoid
- E2FGVI/ProPainter/DiffuEraser in a shipped product (licences non-commercial; GPU only).
- VideoPainter/CogVideoX for plates (heavy, 480p-trained).
- Telea/PatchMatch for holes over ~20 px.
- Declaring a plate "clean" from L1 to the source: it passes by definition when the element is baked in; use section 4.

## 7. Plug-in plan
Existing element/OCR masks -> dilate -> masked median or mode over N frames (stage 1) -> variance map -> shot classification -> optional parametric/PCA fit -> hole fill (gradient fit, then CPU LaMa/MI-GAN crops, then remote ProPainter only for animated textured plates) -> residue report. Store plate image(s) or coefficients plus the synthetic-hole mask.
