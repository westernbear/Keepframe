# Reference Analysis Quality — Design

Date: 2026-10-07 · Status: sections approved in conversation, awaiting written-spec review
Research: `docs/research/2026-10-07-reference-analysis/` (six notes + README with numbers checked against the full text)

## Why

The demo project (ig2demo, an Airbnb-style SaaS launch reel) looked wrong after two ordinary edits, and the causes were all in the analysis:

- A text element's texture was a binary-masked crop that carried the pink globe and an emoji behind the title. Its estimated colour came out pale pink (`#fb9d9d`) instead of the title's dark red, and when the text changed, the new clean glyphs revealed a red smear that the median background plate had absorbed.
- The rotating globe and the white/pink gradient were baked into one background image. "Make the background navy" replaced that image with a flat colour, so the globe vanished and the background pixels baked into element textures showed as white cut-outs.
- The intro's huge perspective, gradient-faded 3D title was reconstructed as fragments and letter pieces.

`render_l1` (pixel L1 to the source) did not flag any of this: contaminated textures match the source pixels by construction. The user asked for analysis an AI can truly understand — especially SaaS / product-launch references (`eval/clips` ig1–ig3, envato1) — so that rebuilding and editing keep the original look.

## Decisions (user, 2026-10-07)

| Topic | Decision |
| --- | --- |
| Order | Analysis quality first: stage A (clean layers), then B (big/kinetic text as text), then C (AI-legible scene description) |
| Goals | All four: big/kinetic text as one text element; clean layer separation; clean background plate; accurate colour, weight and font |
| Approach | Local CV for measurement + a VLM (ChatGPT-class, existing integration) for reading and naming; remote GPU models only as a later fallback |
| Background-colour edit on an image/video background | Warn, then let the user choose: tint (keep the picture, shift its colours) / replace with a flat colour / cancel |
| Brand fonts | Users may upload font files per project; they join the font candidates and are used for rendering |
| Success | Edit-after checks first, then title integrity, `render_l1` no worse, visual comparison sheets |
| Implementers | Claude subagents (not Codex) |

## Goals and non-goals

Goals
- Every element texture is clean: alpha plus true foreground colour, no background pixels.
- The background plate has no smears; an animated background stays animated; big moving objects are their own layers.
- Titles — including huge, perspective, gradient-faded or staggered ones — are one text element with the right string, colour, weight, font candidate and motion.
- The scene carries a semantic layer an agent can read: roles, motions in a closed vocabulary, beats, style tokens.
- Edits keep the original look; a verify-and-repair loop checks them.

Non-goals (this round)
- Live-action footage, camera reconstruction, motion-blur re-synthesis.
- Two-way editing of motion vocabulary (motion labels are derived and read-only; edits go through existing operations).
- GPU-only models in the default path (Omnimatte family, diffusion/video inpainting, neural text editors, video-to-Lottie models).
- Identifying a brand font by name from pixels (only matching among bundled and uploaded fonts).

## Architecture

The analysis stages stay (background → text → regions → tracking → solids → sprites → keyframes → semantics → constraints); the changes plug into them:

```
frames ──► plate v2 (occupancy-masked median, two passes; static | gradient | video) ──┐
      ├──► movers: large unstable regions → own layers (sprite or video sprite)        │
      ├──► text: OCR (PP-OCRv5 models) + VLM keyframe reading → merged title elements ─┤
      ├──► regions/tracking (unchanged core)                                          │
      ├──► textures v2: matting against the per-frame plate (α + F)                    │
      ├──► text style: local two-colour fill, gradient, effects, weight, tracking,     │
      │    font render-and-compare (bundled OFL + uploaded)                            │
      └──► semantics v2: roles (VLM), motions (CV), beats, style tokens, brief ◄───────┘
```

Rules: CV measures (geometry, tracks, timing, easing, colour, cuts); the VLM only reads strings and names roles/style classes from a few keyframes with closed-vocabulary JSON answers (research: VLM text spotting ≈ 0 on OCRBench v2; VLM temporal similarity 0.21–0.30 on Animation2Code). VLM failure or absence never fails analysis — it falls back to today's behaviour and flags low confidence.

## Stage A — clean layers

### Plate v2 (`keepframe/analyze/background.py`)
- Pass 1: today's median plate → element masks (regions, OCR boxes, tracks) dilated 8 px. Pass 2: per-pixel median over only the frames where the pixel is not covered; pixels covered in every frame are holes.
- Holes: fit a 2D low-order polynomial to the surrounding ring (exact on gradients); if the residual stays high, fill with OpenCV inpainting on a crop; record filled pixels as `synthetic` in the plate metadata.
- Classify by the masked temporal variance map: 95th-percentile ΔE < 1.5 → `image`; otherwise if a rank-2 fit over time explains ≥ 95 % of the variance and the spatial structure is a linear/radial gradient → `gradient`; otherwise → `video` (per-frame plate written as an asset, frames filled from the nearest frames where the pixel is uncovered, then holes as above).
- `Background.kind` gains `gradient` (stops + angle or centre, editable colours) and `video` (asset path). Composer: CSS gradient / `<video>`; AE spec: solid with a gradient effect (Gradient Ramp for linear, otherwise an image) / footage layer.

### Movers
- A large region (≥ 4 % of the frame) whose pixels stay unstable for ≥ 40 % of the shot becomes its own element rather than plate. If its appearance is stable after warping into its own coordinates (sprite-space median residual ΔE < 3), it is a normal sprite; otherwise it is a **video sprite** (`canonical.video`: cropped RGBA frame sequence, AE footage with alpha).

### Textures v2 (`sprites.py`, `refine.py`)
- Solve I = αF + (1−α)B per element against the per-frame plate:
  - multi-frame triangulation when the plate behind the element varies (≥ 6 frames warped into element space, per-pixel least squares);
  - two-colour model for flat-fill glyphs and shapes (F from the eroded interior, α from the projection);
  - narrow-band refinement only where both are ambiguous; foreground colour extended under α = 0 to avoid halos.
- Frames are scored (contrast to plate, sharpness, full visibility, no occlusion); the top-K sharp frames are fused by weighted median. Output textures are un-premultiplied RGBA PNGs plus `texture_meta` (frames used, confidence).

### Text style (`text.py`, new `fonts.py` logic)
- Fill colour: median Lab of the glyph interior (eroded by 0.35 × stroke width) using the **local** plate, never the global background; a two-stop linear gradient only when it cuts the residual by > 40 %; fades become an alpha gradient or the opacity track.
- Effects: stroke / shadow / glow fitted from the residual after fill + plate; stored as `effects` (CSS `text-shadow` / `-webkit-text-stroke`; AE layer styles).
- Weight from stroke width / cap height (distance transform), tracking from glyph centres.
- Font: render-and-compare over a bundled OFL set (≈ 40–80 families, licences shipped) plus fonts uploaded to the project; optimise size, weight, tracking, shear on the sharpest fully revealed frame; keep the top 3 with scores and a confidence flag; the VLM may only pick among rendered candidates.
- Hangul/CJK fallback: map the chosen Latin font's category and weight bucket to Pretendard / Noto Sans KR / Noto Serif KR (a handwriting face only for handwriting looks), then re-fit size and weight.

### Font uploads
- `POST /api/projects/<id>/fonts` (same-origin, admin/project permissions as other uploads) accepts `.ttf/.otf/.woff2` ≤ 20 MB, validates the font tables, stores it under the project (`fonts/`), never serves it outside the project's pages. Uploaded fonts join the candidates on the next analysis or text edit. AE export keeps using the device font list: if the paired AE has the same PostScript name it is used, otherwise today's substitution warning applies.

### Edit safeguards (`keepframe/edit/apply.py`, agent flow)
- `background` edit on an `image`/`gradient`/`video` background requires a choice: `tint` (Lab: keep L, move a/b toward the target colour; gradients recolour their stops; video plates are tinted per frame), `replace` (today's behaviour) or cancel. The agent asks before applying, with the warning text.
- `text`/`font` edits render with the analysed colour/gradient, weight, effects, tracking and the resolved font (uploaded > bundled > Hangul fallback), keep the element's rx/ry and reveal tracks, and write the new texture over the clean plate (no smear underneath any more).

## Stage B — big and kinetic text as text

1. **Reading (VLM):** per shot, 3–5 keyframes (settled, fully revealed; chosen by CV motion events) with burned-in frame numbers; ask for closed-vocabulary JSON per visible text: string (or `unreadable`), line breaks, case, font class, weight bucket, fill kind, effects, extrusion/perspective yes/no. The prompt says text may be cut off; strings are cached per scene.
2. **Merging:** OCR boxes and sprite fragments become one text element when colour/gradient, plane (co-planar quads, similar angle and cap height), onset and motion agree **and** each fragment's text fuzzy-matches a substring of a VLM string. Merged fragments leave the sprite list. Staggered letters become per-glyph delays inside the element (`reveal_mode: "glyphs"`).
3. **Geometry:** the title mask's minimum-area quad gives a homography; the rectified patch is re-read by PP-OCR to confirm the string; ECC (homography mode) tracks the whole plane; H decomposes into x, y, sx, sy, rot, skx, rx, ry, smoothed and simplified into keyframes.
4. **Appearance:** Keepframe renders the text (string, font, fill, weight, effects) instead of a cropped texture; motion blur is not reproduced.
5. **Rendering:** `rx`/`ry` become valid for 2D elements. Composer applies CSS `perspective` equal to AE's default comp camera zoom for the scene width, so HTML and AE agree; AE export enables 3D on such layers and writes X/Y rotation.
6. **Guard:** keep the text element only when its local L1 over visible frames ≤ the fragments' L1 + 0.005; otherwise keep fragments but put them in one group so edits treat them as one.

## Stage C — AI-legible scene description

- **Motions (CV, read-only):** per element, from tracks: entrance `fade_in`, `slide_in(dir)`, `scale_pop`, `mask_reveal(dir)`, `typewriter`/`stagger`, `tilt3d`, `blur_in`; hold `float`, `rotate`, `pulse`, `parallax`; exits mirrored; a group scaling together → `camera_push`. Each has frame range, distance/direction, an ease name fitted from the curve (linear, ease-in/out cubic, overshoot/spring) and confidence. Stored as `Element.motions` (derived; recomputed after edits).
- **Roles (VLM):** `headline, subheadline, body, ui_card, button, input, toggle, cursor, logo, icon, photo, shape, glow, background` plus today's values for compatibility; UI children grouped by VLM + geometry with the existing UI model.
- **Beats and shots:** motion onsets/ends clustered into `Scene.beats`; linked to the project's shot segmentation.
- **Style tokens:** `Scene.style = {palette, type_ramp, effects}` from plate and element colours, fonts and sizes, effect presence.
- **Agent brief:** the session tools' scene brief gains one line per element, e.g. `e27 headline "A Weekend Away" #a8001c bold · slide_in↑ 0.4s ease-out @beat2`, plus the beats and style tokens.
- **Verify and repair:** after an edit, motion predicates and keep constraints are checked; failures go back to the agent as predicate-level messages; at most 3 repair iterations (MoVer reached 93.6 % only with up to 50, so the cap is deliberate).

## Evaluation and testing

- **Synthetic ground truth** (extend `keepframe/ir/synth.py`): scenes rendered over black and over white give exact α and F; known plates (static, gradient, animated); known fonts/colours/gradients/effects for text.
- **Real clips:** the four `eval/clips` with gold extended by titles (string, colour, role) per clip.
- **`keepframe eval-edits`** (new CLI) runs per clip and writes metrics + comparison sheets (source | rebuilt | edited):
  - edit-after checks: change a title (pixels outside the glyphs unchanged vs source; new glyph colour vs analysed colour ΔE < 5), hide an element (plate under it vs ring ΔE and high-frequency energy; no residue of the element's colour), recolour/tint the background (4-px ring outside α > 0.5 ΔE < 2 vs an ideal recomposite);
  - title integrity: each gold title is one element whose string matches;
  - text colour vs gold ΔE < 10; font top-3 hit ≥ 90 % on the synthetic set;
  - `render_l1` no worse than today + 0.005; analysis time ≤ 1.5 × today; VLM calls ≤ 3 per scene.
- Unit tests next to each component; browser-marked tests for composer perspective/gradient/video; AE spec tests in the fake AE (gradient, footage, 3D rotation on 2D layers).

## Delivery

Three plans in order — A (plate v2, movers, textures v2, text style, font uploads, edit safeguards, eval-edits harness), B (VLM reading, merging, plane tracking, perspective rendering, guard), C (motions, roles, beats, style, brief, verify-and-repair). Each plan is implemented by Claude subagents with per-task review, ends with `keepframe eval-edits` on the four clips and a visual sheet the user approves, and is merged before the next starts.

## Risks and open items

- VLM availability/cost: optional and cached; analysis works without it at lower text quality.
- Perspective agreement between CSS and AE's camera must be checked in the live AE (slice-2 setup) once B lands.
- Motion-blurred frames mislead matting and font fitting; frame scoring excludes them, but heavy-blur titles may stay low confidence.
- Bundled font set size vs repository size; uploaded fonts are per project and private.
- `video` backgrounds/sprites increase project size; encode at the source resolution with a quality cap.
