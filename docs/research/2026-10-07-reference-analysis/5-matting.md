# 5. Clean element textures (alpha + true foreground colour) with a known background plate

Sources read: arXiv HTML/abstract pages for BGMv2 (2012.07810), MatAnyone (2501.14677), RVM (2108.11515), Germer (2006.14970, tables via ar5iv), Lutz inverse compositing (2103.13423), Forte blur-fusion repo, pymatting repo, Smith & Blinn 1996 via secondary sources. I could not open the Levin 2008 or Smith-Blinn PDFs in full (marked below). Numbers are from the cited tables; anything else is marked "unverified".

## 1. Recommendation (ranked for Keepframe)

**Core idea.** The plate is known, so each pixel obeys I = αF + (1-α)B. One frame gives 3 equations for 4 unknowns. Neural matters are not needed. Add one constraint and the system closes. Three constraints are available in motion graphics, and they should be used in this order:

1. **Multi-frame triangulation (best, CPU, closed form).** Track the element (you already do). Warp N frames (say 6-12) into the element's own coordinates, and warp the plate the same way. The element's pixels are constant (or slowly changing) while B_t changes as the element slides over the plate. Per pixel and per channel, I_t = G + k*B_t with G = αF and k = 1-α, so a least-squares line fit of I against B over t gives k (shared across RGB, so fit 3 channels jointly) and G. Then α = 1-k and F = G/α. This is Smith & Blinn's triangulation matting (SIGGRAPH 1996) generalised from 2 to N backgrounds, so noise averages out. Runtime is a few NumPy ops per pixel per frame (milliseconds per element; unverified, but trivially cheap). It handles soft edges, glow, semi-transparent cards and motion blur only if the element's own appearance is constant across frames (not true under blur or fast motion; see failure modes). Weight each frame by plate-confidence and by var(B_t) at that pixel.
2. **Single-frame known-colour keying where the foreground is a flat colour (text, shapes).** Fix F (cluster the interior pixels of the glyph, not "top-10 % contrast vs global background") and solve α = clip(((I-B)·(F-B)) / |F-B|²). This is exact for solid-colour glyphs with anti-aliased edges, and also gives a clean estimate of drop-shadow alpha if you treat the shadow as a second layer with F = shadow colour. Do it in the same colour space the browser blends in (sRGB-encoded 8-bit, not linear).
3. **Alpha refinement + foreground colour recovery by pymatting (MIT).** Where 1 and 2 are ambiguous (the element colour equals the plate colour, or B has no variance across frames), use the difference-key alpha as the seed, build a trimap (certain-FG = |I-B| large, certain-BG = |I-B| ~ 0, unknown band = 3-6 px), and solve Levin closed-form matting only in the unknown band. Then estimate foreground colours with `estimate_foreground_ml` (Germer) or blur-fusion (Forte). Texture = (alpha, un-premultiplied F), stored as straight RGBA with F extended outward under α=0 pixels (this prevents halos under bilinear filtering and in After Effects).

**Fusion across frames.** For every element, score each frame by (a) mean |I-B| over the interior (more contrast with plate is better), (b) low motion blur (tracked speed), (c) no occlusion by other elements, (d) full visibility inside the canvas. Use the best frame for geometry, but compute α and F as the weighted median/least-squares over the top-K frames after warping. Do not average frames with different blur; the median of the top-K by sharpness is safer.

**Neural options are second line.** Only use them where the plate is wrong or the element touches real photos (people in photo cards).
- BGMv2 (Lin et al., CVPR 2021, arXiv 2012.07810) takes the clean background as input. This is the same setup as ours.
- MatAnyone (Yang et al., CVPR 2025, arXiv 2501.14677) takes a first-frame mask and gives temporally stable alpha.
- Both are person-matting models. They will fail on text and UI cards, so they are not the main path.

## 2. Per-paper notes

**Smith & Blinn, "Blue Screen Matting", SIGGRAPH 1996.** Triangulation (their "Solution 3"): two shots of the same foreground in front of two known backings, I0 = αF+(1-α)B0, I1 = αF+(1-α)B1, which solves α and F per pixel provided B0 and B1 differ everywhere. Works for arbitrary backings. Limit: both exposures must show the identical foreground (no motion, no blur change). Secondary sources only; full PDF not read. No code needed; it is two lines of algebra.

**Levin, Lischinski, Weiss, "A Closed-Form Solution to Natural Image Matting", TPAMI 2008.** Sparse linear system in the local colour-line model; needs a trimap or scribbles. Germer Table II shows it costs 26.3 s +/- 5.5 s on a Xeon Gold 6134 and 27.9 s on a 2019 MacBook i5 for the ~0.4 MP images used, and 7.8 GB RAM. At 1080p (2 MP) a full-frame solve is therefore minutes and GBs, which is too slow; restrict it to a narrow unknown band or to per-element crops (unverified extrapolation). Failure on flat graphics: the colour-line model degenerates on uniform foreground and background, so alpha in the unknown band becomes arbitrary when F is close to B.

**Germer et al., "Fast Multi-Level Foreground Estimation", ICPR 2020 (arXiv 2006.14970).** Given α, estimates F (and B) with a multi-level solver. Table I (ground-truth alpha): multi-level SAD 20.9e-3, MSE 1.44e3 (units as printed in the page I read), GRAD 8.89e-3; closed-form 21.1e-3, 1.34e3, 8.13e-3; IndexNet 28.8e-3 / 2.33e3; KNN 32.0e-3 / 3.25e3. So it matches closed-form quality. Table II: 2.04 s on Xeon Gold 6134 and 1.48 s on an i5 laptop, against 26-28 s for closed form. Table III: 1.18 GB vs 7.8 GB. Code: pymatting (MIT), CPU via Numba, optional CUDA/OpenCL. 1080p cost is probably 5-10 s on 4 cores (unverified); run per element crop, not per frame.

**Forte & Pitié, "Approximate Fast Foreground Colour Estimation", ICIP 2021 (Best Industry Paper).** "Blur-fusion": 11 lines of NumPy + OpenCV, alpha-weighted blurs of image and an initial F/B, comparable to the full multi-level method but faster. I did not find runtime/MSE numbers in the sources I could open (unverified). Repo: github.com/Photoroom/fast-foreground-estimation; check the licence before copying (not stated in what I read). Ideal as a fast default, since it is pure cv2 on 1080p in tens of ms.

**Lutz et al., "Foreground colour prediction through inverse compositing", WACV 2021 (2103.13423).** A small network; foreground SAD 28.32 vs closed-form 31.98. It does not use a known background and predicts poor background colours where B is barely visible. Not helpful here; our background is known.

**Sengupta et al., "Background Matting: The World is Your Green Screen", CVPR 2020 (2004.00626).** Needs a photo of the background; network predicts α and F, with adversarial training on real data. Limits: small background shift, humans only in training. Code released; licence not verified.

**Lin et al., "Real-Time High-Resolution Background Matting" (BGMv2), CVPR 2021 (2012.07810).** Base net at low resolution plus a refinement net on error patches. AIM test set SAD/MSE 12.86/12.01 vs BGM 16.07/21.00. 4K at 30 FPS and HD at 60 FPS on an RTX 2080 Ti (GPU only; no CPU speed given). Needs a pre-captured background; "limited to small motion"; struggles with substantial shadow and highly textured backgrounds. Code and data CC BY 4.0 (as stated on the project page). A person/portrait model, so its results on text, glows and UI cards are unverified and likely poor.

**Lin et al., "Robust High-Resolution Video Matting with Temporal Guidance" (RVM), WACV 2022 (2108.11515).** Recurrent ConvGRU + Deep Guided Filter; VideoMatte240K 512x288 MAD 6.08, MSE 1.47; HD 104 FPS on GTX 1080 Ti; 3.75 M parameters (14.5 MB, so CPU-runnable via onnxruntime, though no CPU timing given). No background or trimap input. Fails when people are in the background and favours simple backgrounds. Person-only; not a texture extractor.

**Yang et al., MatAnyone (CVPR 2025, 2501.14677).** Memory propagation, needs a first-frame mask. VideoMatte 1920x1080: MAD 4.24, MSE 0.33, dtSSD 1.19; YouTubeMatte 1080p MAD 1.99, MSE 0.71. Needs GPU for practical use (CPU timing unverified). The paper itself says video-matting data are small and synthetic data generalise poorly. Use only to matte people/photos inside cards. I did not review 2025-26 follow-ups (e.g. MatAnyone 2) in full; unverified.

## 3. Test that a texture contains no background

Use the renderer you already own as a ground-truth generator ("black/white double render"):

1. **Synthetic GT.** Build 30-50 cases in Keepframe's HTML renderer: titles (with blur, glow, drop shadow, 3D tilt), photo cards with rounded corners and 50 % opacity, gradient sprites. Render each case twice over pure black and pure white. Then triangulation gives exact α = 1 - (I_w - I_b)/(255) and F. Also render over the real plate with motion blur. Run the extractor on the plate render only. Report α SAD/MSE in the edge band and F ΔE against GT; target ΔE < 3 in the interior and < 6 in the 3-px edge band.
2. **Plate-swap halo test (also works on real clips).** Recomposite the extracted texture over (a) pure white, (b) pure black, (c) saturated magenta, (d) a hue-shifted plate. For real clips compare crops with the same element extracted against a different plate region or time. Halo score = mean over pixels in the 4-px ring outside the α>0.5 boundary of the chroma distance between the recomposite and the (c)/(d) ideal. The pass criterion is a halo ring ΔE < 2 and zero visible fringe at 4x zoom.
3. **Leak correlation.** In the edge band, compute the correlation between the F residual (F minus the nearest interior colour cluster) and the original plate B. A clean texture gives |r| < 0.1; a contaminated one gives r close to 1.
4. **Edit-after check.** Recolour the plate to a complementary colour, rerender the scene from the textures and compare with the same scene with the original plate recoloured through the renderer. Report L1 on element bounding boxes only.

## 4. Failure modes and what to avoid

- Triangulation needs varying B_t. A flat or very slowly changing plate (common in SaaS decks) leaves k undetermined; fall back to method 2 or 3 and flag low confidence (var(B_t) map).
- Plate errors (smeared moving content, per-frame exposure change) bias α directly. Never trust the temporal-median plate under or near big moving objects; use a masked, inpainted plate and treat |I-B| < threshold regions as "unknown" there.
- Motion blur: the element is no longer constant across frames. Prefer the sharpest frames and keep blur as an animated property instead of baking it in. Blur is a spatial smear of α, so estimate α on a still frame instead.
- Glows and shadows are not "over" composites. A screen/additive glow means I = B + glow; treat it as its own layer with a blend mode (or α=1, additive), not as semi-transparent F. Test by checking whether I-B is nonnegative everywhere (glow) or nonpositive (shadow).
- Video JPEG/H.264 chroma subsampling blurs colour edges at 2x scale. Matte on luma-sharp edges and upsample chroma with a guided filter; expect residual ΔE of 2-4 on thin text edges.
- Avoid: running RVM/MatAnyone/BGMv2 on text and UI (person priors, GPU); full-frame Levin solves (26 s per 0.4 MP, GBs of RAM); thresholded binary masks (current approach: bakes the background into edge pixels); estimating text colour against the global background colour.

## 5. Minimal build order
Difference key + known-colour solve (hour 1) -> multi-frame LS fit with variance map (day 1) -> pymatting band refinement + blur-fusion F extension (day 2) -> black/white double-render GT harness (day 2-3).
Licences: pymatting MIT; BGMv2/RVM/MatAnyone listed as CC BY 4.0 on their pages; Forte repo licence unverified.
