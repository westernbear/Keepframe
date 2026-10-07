# 2-text: Text in motion graphics (detect, read, track, merge)

Verification legend: [V] = number read from the paper/page in this session. [U] = from memory/unverified.
Caveat: I did not view the contact sheet; I worked from the brief.

## 1. Ranked recommendations

**Core design: VLM = semantics, CV = geometry. Never trust a VLM box, never trust OCR strings on stylised text.**

1. **VLM reads strings and style from 3-5 keyframes per title (cheap, high value).** Choose frames where the title is fully visible and still: the settle frame of an entrance and the pre-exit frame. Send full frame plus a crop. Ask for JSON: `string`, `case`, `line breaks`, `font class` (sans/serif/script/mono), `weight` (100-900 estimate), `fill` (solid hex, or gradient with two stops and angle), `stroke`, `shadow/glow`, `extruded yes/no`, `text role` (title/UI/accent). Ask it to answer `unreadable` rather than guess. Run it twice with different frames and flag strings that disagree.
   - Why: on OCRBench v2 (arXiv 2501.00321, Table 4, English private set) [V], Gemini-1.5-Pro scores 59.1 on recognition, GPT-4o 58.6, Claude-3.5-Sonnet 52.9 and Qwen2.5-VL-7B 51.5. Text spotting (string plus box) is 6.6 / 0 / 2.5 / 3.1 for the same models [V]. So VLMs read well and localise very badly. InternVL3-14B gets 78.3% answer accuracy but 12.9% IoU on answer-region localisation [V]. The paper lists "rare text", "fine-grained spatial perception" and "overlapping/rotated text" as failure classes [V].
   - On video frames (arXiv 2502.06445, 1,477 frames) [V], GPT-4o scores 76.2% and Gemini-1.5-Pro has the lowest WER (0.2385), against RapidOCR 57.0% and EasyOCR 49.3%. Observed failures: Claude invented "Coconut Milk"; GPT-4o completed truncated words. Both matter for partially off-screen titles, so tell the model that text may be cut off and to transcribe only what is visible, or mark the cut. Content-policy refusals also happen.
   - Qwen2.5-VL / Qwen3-VL numbers on OCRBench v2 and CC-OCR for 2025-26 models: [U], I did not fetch them. Use them only as a free HF-hosted second opinion.

2. **CV recovers exact geometry; do not use detector boxes as the final result.** Use the VLM string as a prior and fit it to pixels.
   - Segment the title by a foreground mask: difference from a clean plate, or colour/gradient-consistent connected components seeded by the OCR boxes. Then take the minimum-area quadrilateral.
   - Rectify with the homography H of that quad. Re-OCR the rectified patch (PP-OCRv5 rec) to verify the string against the VLM. Disagreement means the VLM string wins, but the element gets a low-confidence flag for human review.
   - Fit text as a plane: baseline, cap-height, x-skew, rx/ry. For extruded 3D titles, model the front face plus an extrusion layer, or fall back to a flat sprite with 3D metadata. Do not try to read extrusion depth from OCR boxes.

3. **Track the plane, not the glyphs.** One element per title:
   - Initialise with H from the keyframe. Propagate with a feature-based ECC / KLT on the text mask (`cv2.findTransformECC`, MOTION_HOMOGRAPHY, or LK on corner points of the quad) using a pyramid. Flat-coloured glyph edges give few features, so use dense ECC on an edge or mask image rather than SIFT.
   - Re-detect on every Nth frame and re-anchor if ECC cost rises (a cut, a morph, a blur peak).
   - Decompose the per-frame H into translate / scale / rotate / skew / perspective (rx, ry). Smooth with Savitzky-Golay or a Kalman filter, then fit keyframes with RDP-style simplification.
   - Cheap and CPU-fast: ECC on a 540p crop is milliseconds [U].

4. **Merge fragments into ONE element (the actual bug in the current pipeline).** Greedy graph clustering of per-frame OCR boxes plus segmentation components:
   - Connect fragments if they are the same colour/gradient (Lab distance), have co-planar rotated quads (similar angle, similar cap-height, within about 1.5 x height of each other, baseline-consistent), and have the same temporal onset and motion (identical H up to a translation).
   - A fragment's string must be a substring/line of the VLM string (fuzzy match, e.g. RapidFuzz `partial_ratio` above 80).
   - Result: one track with the VLM string, the union mask, and a single H(t). Fragments whose residual after warping the union mask is large are letter-level animations (staggered per-letter reveals). Emit them as per-glyph offsets/delays inside the one element, not as separate elements.
   - Make "sprites that overlap a text quad" a negative: remove them from the sprite list.

5. **Colour/weight from the rectified patch, not the global background.** Take the mask interior, k-means (k=2-3) on mask pixels only, eroded 1-2 px to avoid antialiasing. If luminance varies along the title's axis, fit a linear gradient (two stops, angle). Cross-check the VLM hex against this; trust the CV values, use the VLM for class (gradient or not, stroke or not). Estimate weight as stroke-width / cap-height on the rectified mask (distance transform), then pick the nearest font weight in a rendered reference font. Font identification from the rectified patch with a retrieval model (e.g. render candidate fonts and match by SSIM) is [U]; I did not verify any font-ID paper.

6. **UI text in mockups.** Use PP-OCRv5/RapidOCR line boxes directly, unmerged. UI text is small and crisp, and OCR is good at it. Merge only into rows/cards via layout proximity. The VLM is used once per screen for roles (label/button/value).

## 2. Per-paper / tool notes

**OCRBench v2** (Fu et al., arXiv 2501.00321v2, 2025): 31 scenarios, 10k human-verified QAs, 38 LMMs evaluated; 36 of 38 score below 50/100 [V]. Overall English: Gemini-1.5-Pro 51.6, GPT-4o 47.6, Claude-3.5 47.5, Qwen2.5-VL-7B 41.8 [V]. The older leader-board for the strongest models may have moved; I did not fetch the 2026 table. Relevance: localisation and spotting are the weak spots.

**GoMatching** (Li et al., NeurIPS 2024, arXiv 2401.07080) [V]: freezes DeepSolo (image text spotter), adds a rescoring head and a Long-Short-term Matching (LST) tracker transformer. ICDAR15-video MOTA 72.0 / IDF1 80.1; DSText MOTA 22.8 / IDF1 46.1; BOVText MOTA 52.9 / IDF1 62.6. About 10.6 FPS at 1000 px on an RTX 3090. Failure: very small text and strong motion blur. GPU only, so it can't run on your CPU box in real time. Domain mismatch: these are real-world videos, not stylised titles. Not recommended; the ideas (frozen spotter + association by appearance and box) are what we replicate with a plane tracker. TransDETR (TPAMI 2023) is the same class and heavier [U numbers].

**PP-OCRv5** (arXiv 2603.24373; PaddleOCR 3.0 report arXiv 2507.05595) [V partly]: about 5M parameters; DB detector with PP-LCNetV3 backbone, SVTR_LCNet recogniser with GTC. Weighted in-house accuracy 80.1% vs 53.0% for PP-OCRv4 (note: a vendor benchmark). Handwriting 41.7% (zh) / 49.4% (en), so script accent words will be poor. The server rec model takes 31.2 ms on CPU vs 36.9 ms for v4 server [V from a search snippet, per-line, not per frame]. A newer PP-OCRv6 (arXiv 2606.13108) exists; I only saw the title, so [U]. Apache-2.0. RapidOCR runs the same PP-OCR ONNX models via onnxruntime, so it is the right CPU path, not a different algorithm; upgrading its model files to v5 is cheap.
   - Per 1080p frame on 4 CPU cores: roughly 0.3-1.5 s for det plus rec [U, my estimate]. Fine for keyframes and every-Nth-frame re-detection, not for every frame.

**DBNet / DBNet++ (TPAMI 2022) and other arbitrary-shape detectors** [U]: DBNet++ is what PP-OCR's detector descends from. Arbitrary-shape polygon detectors help with curved text but are trained on photographic scenes; flat, huge, gradient-faded glyphs often get split per glyph or missed. Not worth swapping in; segmentation by plate-difference is more reliable for our synthetic content.

**Rectification (ASTER, TPS-STN, 2018-19) / homography** [U]: modern recognisers (PP-OCRv5, VLMs) have largely dropped the explicit rectifier. We have a better one: a computed H from the tracked quad. Rectifying before reading is what makes perspective titles legible for PP-OCR.

**Video text spotting benchmarks (DSText, BOVText, ICDAR15-video)**: see GoMatching numbers. BOVText has Chinese+English captions and stylised video text; this is the closest benchmark to our genre but still real video. [V for numbers via GoMatching.]

## 3. What to avoid

- **VLM-produced bounding boxes as final geometry.** IoU on exact region is about 13% for a strong 14B model [V] and spotting F1 is around 0-7 for GPT-4o/Gemini/Claude [V]. Use them only as a rough "which region" hint for choosing the crop.
- **Training or running video text spotters (TransDETR/GoMatching) here.** GPU-only, domain mismatch, no benefit over a plane tracker for 1-5 titles per clip.
- **Running OCR on every frame and linking boxes.** It is what fragments titles. Detect on keyframes, track geometrically.
- **Trusting OCR strings for script/handwritten accents and heavily blurred titles.** Use the VLM there, and verify it against a clean frame.
- **Letting a VLM "complete" cut-off words.** Observed with GPT-4o in 2502.06445 [V]. Instruct explicitly.
- **Global-background text colour.** Always sample inside the text mask.

## 4. Open items I could not verify
- Qwen3-VL / GPT-5-class OCRBench v2 and CC-OCR numbers; bounding-box accuracy of Qwen3-VL (it uses a 0-1000 relative coordinate grid [U]).
- PP-OCRv6 claims; actual CPU latency per 1080p frame.
- Any published method for reading extruded/3D motion-blurred titles; I found none, so the VLM-plus-plane-fit approach above is my synthesis, not a cited result.
