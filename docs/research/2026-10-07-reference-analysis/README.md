# Reference analysis research (2026-10-07)

Question: how should Keepframe analyse SaaS / product-launch motion references (ig1–ig3, envato1 — see `reference-clips.png`) so that an AI agent understands them and edits keep the original look? Constraints: CPU only (4 cores, 15 GB), optional ChatGPT-class VLM calls, remote GPU only through hosted APIs.

Six notes, one per topic: `1-layers.md` (video → layers), `2-text.md` (text in motion video), `3-text-style.md` (font, colour, weight), `4-plates.md` (clean background plates), `5-matting.md` (matting with a known plate), `6-ai-legible.md` (AI-legible analysis). Some notes were written from fetched summaries rather than line-by-line reads; each marks unverified items.

## Numbers checked against the full text

| Claim | Source (checked) |
| --- | --- |
| Frontier VLMs de-render motion graphics well in appearance but poorly in timing: appearance / temporal similarity GPT-5.4 0.84 / 0.29, Gemini 3 Flash 0.80 / 0.30, Claude Sonnet 4.6 0.82 / 0.29, LLaMA 4 Scout 0.62 / 0.21, Qwen3-VL-8B 0.70 / 0.24 (image-frame input) | Animation2Code, arXiv 2606.28593, main results table |
| An LLM pipeline with a motion verifier produces a correct motion-graphics animation for 58.8 % of prompts without iteration and 93.6 % with up to 50 correction iterations | MoVer, arXiv 2502.13372, abstract and §results |
| VLMs read text but cannot localise it: OCRBench v2 (English) text recognition / text spotting — GPT-4o 61.2 / 0, Claude 3.5 Sonnet 62.2 / 1.3, Gemini-Pro 61.2 / 13.5 | OCRBench v2, arXiv 2501.00321, English results table |
| Foreground colour estimation: multi-level method 2.04 s vs closed-form 26.3 s (HPC) at equal quality (alpha SAD 20.9e-3 vs 21.1e-3) | Germer et al., arXiv 2006.14970, timing and accuracy tables |

## Conclusions used by the design

1. **CV measures, the VLM names.** Geometry, tracks, timing, easing, colours and cuts come from CV; the VLM reads strings, roles and style classes from a few keyframes with closed-vocabulary JSON answers, and never supplies boxes, seconds, directions, colours or free-form font names.
2. **Plates without smears:** per-pixel median over frames where the pixel is not covered by a (dilated) element mask, run twice; classify the plate as static image / smooth low-rank gradient (emit an editable gradient) / textured motion (video plate). Large movers (a rotating globe) become their own layer, not plate. Always-occluded holes: ring-fit polynomial first, CPU inpainting only for the rest; record filled areas as synthetic.
3. **Clean textures:** solve I = αF + (1−α)B against the known plate — multi-frame triangulation where the plate varies behind the element, the two-colour model for flat glyphs, narrow-band refinement only where ambiguous; never binary-masked crops.
4. **Text as text:** VLM reads the string and style class; CV fits the text plane (quad → homography), tracks it with ECC, decomposes into x/y/scale/rot/skew/rx/ry; fragments merge when colour, plane, cap height, onset/motion and substring all agree. Fonts by render-and-compare over a bundled OFL set (top-3 with confidence); colour from the local two-colour model; weight from stroke width / cap height; Hangul fallback by category and weight bucket.
5. **AI-legible output:** keep measured tracks as the source of truth and add a semantic layer — shots, roles, closed-vocabulary motion predicates derived from tracks, beats, style tokens — plus a verify-and-repair loop with an iteration cap.
6. **Measure what pixel L1 cannot see:** edit-after checks, plate-cleanliness (ΔE / high-frequency energy under removed elements vs a ring), recolour halo tests, synthetic ground truth rendered over black and white.
7. **Avoid as the main path:** Omnimatte/neural-atlas family, diffusion video inpainting, ProPainter/E2FGVI (GPU, non-commercial), neural text editors (raster output), video-to-Lottie models (icon-scale), VLM-only de-rendering.
