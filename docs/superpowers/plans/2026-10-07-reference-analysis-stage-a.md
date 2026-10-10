# Reference Analysis — Stage A (clean layers) Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL superpowers:subagent-driven-development. **Implementers = Claude subagents** (user decision 2026-10-07, not Codex); a task review per task, a whole-branch review at the end. At Task 0 this plan is copied to `docs/superpowers/plans/2026-10-07-reference-analysis-stage-a.md`. Steps use `- [ ]`.

## Context

The ig2demo SaaS-launch demo looked wrong after two ordinary edits, and every cause traced back to analysis: element textures are binary-masked crops carrying background pixels (the title carried the pink globe and an emoji); text colour was estimated against the global background (`#fb9d9d` instead of dark red); the median plate absorbed a smear of the moving title and baked in the rotating globe; "make the background navy" replaced that image with a flat colour. `render_l1` could not see any of it.

Spec (approved): `docs/superpowers/specs/2026-10-07-reference-analysis-design.md` (e9f491e); research `docs/research/2026-10-07-reference-analysis/`. This plan is **Stage A — clean layers**: plate v2, movers as own layers, matted textures, text style (colour, gradient, effects, weight, tracking, font render-and-compare, Hangul fallback), per-project font uploads, edit safeguards, and `keepframe eval-edits` with synthetic ground truth. Stages B (text as text) and C (AI-legible description) follow in their own plans.

## Decisions (user 2026-10-07 + rulings)

| # | Decision |
|---|---|
| D1 | **All bundled OFL fonts are committed** (user): 60–80 families as WOFF2 in `keepframe/fonts/files/` with `LICENSES/` and a manifest (family, category, axes, scripts, sha256, source commit). Tests run offline on the full set. |
| D2 | **Add `fonttools[woff]>=4.50`** (user): upload table validation, WOFF2, PostScript names, cmap coverage, subsetting for HTML embedding. |
| D3 | **Video plates and video sprites ship in Stage A as Task 13** (user), late and cuttable: VP9 WebM (+alpha for sprites) + poster PNG for HTML/preview; AE gets derived H.264 `.mp4` (plate) / ProRes 4444 `.mov` (sprite). If cut: unstable movers and textured-motion plates stay stills with a visible message. |
| D4 | Static plates fitted by a ≤ 4-stop linear/radial gradient with p95 ΔE ≤ 1.5 become `gradient` (editable); one existing test expectation changes. |
| D5 | **Tint = move to the target's lightness and hue while keeping the light/dark structure** (user): in CIELAB, `L' = L_T + k·(L − L̄)`, `a' = a_T + (a − ā)`, `b' = b_T + (b − b̄)`, `k = min(1, L_T / max(L̄ − L_min, ε), (100 − L_T) / max(L_max − L̄, ε))`, gamut-clipped. White plate + navy target → dark navy with the original variations. |
| D6 | AE text styles via effects/TextDocument (stroke + tracking in TextDocument, Drop Shadow for shadow/glow, Ramp + Set Matte for gradient fill); live AE check at the end. |
| D7 | Re-measure the baseline on master in this venv (torch CPU present) — the reference for time ≤ 1.5× and `render_l1` ≤ baseline + 0.005. |
| D8 | Never-visible holes: polynomial ring fit, else `cv2.inpaint` (Telea); filled pixels recorded as synthetic. No LaMa in Stage A. |
| D9 | Font upload UI: "Fonts" disclosure on the agent page (list + upload); no delete in Stage A. |

## Global constraints

- Every new analysis feature fails soft: on error, fall back to today's behaviour, add a report message, lower confidence — analysis never fails because of it.
- No VLM in Stage A (eval reports `vlm_calls: 0`).
- Browser routes keep existing checks (GET `_host_allowed`, POST `_same_origin`, ids via `_safe_render_project`). Do not copy the uncapped `/api/projects` multipart handler.
- Assets stay **flat files** under `scenes/<sid>/assets/` (edit candidates promote top-level files only; `edit/agent.py` 120-128, 224-227). `GET /assets/<name>` stays PNG-only; font bytes are never served by a route.
- Colour thresholds use calibrated CIELAB ΔE76 (new `keepframe/ir/colour.py`), not `background.rgb_to_lab` (8-bit OpenCV Lab).
- ExtendScript stays ES3; bump the extension version when the AE spec gains kinds/fields.
- UI copy ko + en (`i18n.js`); bump cache-buster on changed pages.
- Each new phase logs its duration (`log.info("plate pass2 %.2fs", …)`).
- Tests: full `.venv/bin/python -m pytest -p no:cacheprovider -q -m 'not browser and not gpu and not ocr'`; browser `-m browser <files>`; Node `node --test 'tests/ae_fake/*.test.js' 'extension/test/*.test.js'`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; never commit `eval/`, `graphify-out/`, `uv.lock`; `graphify update .` after merge.

## Review focus (failure modes the tests must pin)

1. Plate still holds a smear / baked element (title held ≥ 50 % of the shot; always-covered logo) → Task 3 `test_masked_median_ignores_title_present_in_70pct_of_frames`, `test_ig2_like_smear_removed`, `test_always_covered_logo_hole_poly_on_gradient`.
2. Textures still carry background / halo after recolour → Task 6 `test_texture_has_no_background_pixels`, `test_halo_ring_after_recolour`, `test_foreground_extended_under_zero_alpha`.
3. New `plate` stage breaks cache / rerun / corrections → Task 3 `test_plate_stage_cached_and_rerun_from_sprites_reuses_it`, `test_legacy_project_without_plate_json_rebuilds_on_rerun_from_sprites`, `test_corrections_bbox_prompt_uses_pass1_plate`.
4. Background edit on a picture loses content or mutates stored files → Task 12 `test_background_edit_on_picture_needs_choice`, `test_cancel_creates_no_version_or_assets`, `test_tint_writes_new_asset_and_keeps_original`.
5. Font upload abuse (oversize, non-font, traversal, cross-origin, font bytes served) → Task 11 `test_oversize_font_rejected_before_body_read`, `test_invalid_font_tables_rejected`, `test_cross_origin_upload_forbidden`, `test_font_bytes_never_served`.
Also: HTML vs numpy vs AE parity for styled text (Task 8 browser tests); compressed static plates not classified as video (Task 4 `test_compression_noise_still_static`); time budget (Tasks 3/6/10 perf tests, Task 15 gate).

## Facts the plan depends on (checked in code)

- Pass-1 plate (`background.py:32`, median + W/32 low-pass) feeds text, regions and `review/corrections.py:113-118`; keep it as `stages/plate_pass1.png`; the v2 plate is built after tracking.
- Text renders two ways: HTML `<span>` with system fonts ignoring the texture (`composer.py:101-106`) vs numpy compositor/preview/report using the texture (`server.py:423-441`). Styled text therefore needs a Pillow rasteriser and matching CSS, proven equal in browser tests.
- Pillow 12.3 has FreeType 2.14 with brotli/raqm/harfbuzz (WOFF2 loads); fontTools not installed (D2 adds it).
- AE cannot import WebM; Playwright Chromium cannot decode H.264 → D3 formats. ffmpeg with libvpx-vp9, prores_ks, libx264 is installed here and in the Dockerfile.
- `renderer.render` awaits a Promise returned by `window.__seek(f)`, so an async seek can wait for `<video>`.
- Edit choices: `intent.plan` creates `Conflict`s; `edit/agent.py` returns `needs_choice`; browser `appendChoices` (`agent.js:492`) / `edit-form.js:19`; no cancel path yet.
- Existing `test_dominant_gradient_uses_plate_on_analysis_and_rerun` expects `image` → becomes `gradient` (D4).

## Task overview

| Task | Deliverable | Depends | AE | Browser |
|---|---|---|---|---|
| 0 | Worktree, baseline, gold titles, fonts download (controller) | — | | |
| 1 | Calibrated colour, IR additions, all background readers | 0 | spec | yes |
| 2 | Synthetic ground truth + `keepframe eval-edits` ("before" numbers) | 1 | | CLI |
| 3 | Plate v2 pass 2 + hole fill + `plate` stage | 1 | | |
| 4 | Plate classification (gradient static/animated, video candidates) | 3 | | |
| 5 | Movers | 3, 4 | | |
| 6 | Textures v2 (α + F, frame scoring, fusion) | 3, 5 | | |
| 7 | Font registry + all bundled OFL fonts | 1 | | |
| 8 | Styled text rasteriser + composer CSS + embedded fonts | 7 | | yes |
| 9 | Text style analysis | 6, 8 | | |
| 10 | Font render-and-compare + Hangul fallback | 8, 9 | | |
| 11 | Per-project font uploads | 7, 8, 10 | spec `_font` | yes |
| 12 | Edit safeguards (tint/replace/cancel; styled text edits) | 4, 8, 11 | | yes |
| 13 | Video plates + video sprites (cuttable) | 4, 5, 6, 12 | | yes |
| 14 | AE export: gradient bg, footage, text styles, PostScript | 1, 8, 11, 13 | **extension + spec** | Node fake AE |
| 15 | Final eval-edits, time gate, sheets, live AE check (controller) | all | live | yes |

---

## Task 0: workspace, baseline, gold titles, fonts (controller)

- [ ] `git worktree add .worktrees/reference-a -b feature/reference-a` from master; venv `pip install -e '.[ocr,gpu,llm,dev]'` + Playwright Chromium. Copy this plan into `docs/superpowers/plans/2026-10-07-reference-analysis-stage-a.md`; SDD ledger with D1–D9 as rulings.
- [ ] Baseline (D7) on master, idle machine: `keepframe gate-m2-real --clips eval/clips --out eval/out/ra-baseline --max-frames 150 --render-check` → `docs/qa/reference-analysis/baseline.json` `{clip: {seconds, mean_l1, render_l1, elements, kinds}}` + `{torch, cores}`; also `gate-m2 --n 20`, `gate-m1`.
- [ ] Gold titles `docs/qa/reference-analysis/gold/<clip>.json`: `{"titles":[{"text","color","role","frame","box":[x0,y0,x1,y1]}]}` — envato1 "Build SaaS Promo"/"Build"; ig1 "Every day, ideas are born"/"inside your walls."; ig2 "Weekend"/"A Weekend Away"; ig3 "You just speak"/"Your idea". Colour = median of ≥ 30 hand-picked glyph pixels on the settled frame (coordinates in `gold/README.md`); contact sheet confirmed by the user before Task 2.
- [ ] Download the D1 font set (pinned google/fonts commit + Pretendard/Noto KR releases, sha256 recorded) for Task 7; the implementer commits them.

## Task 1: calibrated colour, IR additions, background readers

**Deliverable:** `gradient` backgrounds (static/animated) render the same in numpy and HTML; `video` backgrounds degrade to their poster everywhere they cannot play (Lottie refuses with a message; AE poster + warning until Task 14); old scene JSON loads unchanged.
**Files:** create `keepframe/ir/colour.py`, `keepframe/ir/gradient.py`; modify `ir/schema.py`, `analyze/composite.py` (76-84), `compose/composer.py` (117-139) + `template.html`, `render/lottie.py` (27), `render/plan.py` (593), `analyze/solid_assets.py` (`_plate` 90, temp scene 365-367), `ae/spec.py` (`_background` 327, `spec_asset_paths` 253), `session/brief.py` (75), `edit/agent.py` (113).
**Interfaces:**
```python
# ir/colour.py
def srgb_to_lab(rgb: np.ndarray) -> np.ndarray      # uint8 (...,3) -> float32 CIELAB D65, L 0..100
def lab_to_srgb(lab: np.ndarray) -> np.ndarray      # -> uint8, gamut-clipped
def delta_e(a, b) -> np.ndarray                     # ΔE76
def hex_to_rgb8(s: str) -> tuple[int,int,int]; def rgb8_to_hex(rgb) -> str
# ir/schema.py (additive, defaults; schema stays "keepframe.scene/1")
class GradientStop: offset: float; color: str
class Gradient: kind: Literal["linear","radial"]="linear"; stops: list[GradientStop]  # 2..8 ascending
                angle: float=180.0; center: tuple[float,float]=(0.5,0.5); radius: float=1.0
class GradientKey: t: int; gradient: Gradient
Background: kind Literal["color","image","gradient","video"]; value; confidence;
            gradient: Optional[Gradient]=None; gradient_keys: list[GradientKey]=[]; poster: Optional[str]=None; synthetic: Optional[str]=None
class TextureMeta: method: Literal["triangulation","two_colour","keyed","binary"]; frames: list[int]=[]; confidence: float=1.0; padding: int=0
class TextEffect: kind: Literal["stroke","shadow","glow"]; color: str; opacity=1.0; width=0.0; dx=0.0; dy=0.0; blur=0.0
class AlphaStop: offset: float; alpha: float
class Fade: angle: float=180.0; stops: list[AlphaStop]  # 2..4
class TextStyle: fill: Optional[Gradient]=None; fade: Optional[Fade]=None; effects: list[TextEffect] (≤4)
                 tracking_em=0.0; shear_deg=0.0; dx=0.0; dy=0.0; stroke_ratio: Optional[float]=None; confidence=1.0
FontGuess += scores: list[float]=[]; confidence=1.0; source: Literal["bundled","uploaded","system","generic"]="generic"
             file: Optional[str]=None; postscript: Optional[str]=None  # ^[A-Za-z0-9._-]{1,63}$, invalid -> None + warning
             fallback: Optional[str]=None; fallback_weight: Optional[int]=None; fallback_scale: float=1.0
Canonical += style: Optional[TextStyle]=None; texture_meta: Optional[TextureMeta]=None; video: Optional[str]=None
# ir/gradient.py
def gradient_t(g, w, h) -> np.ndarray; def render_gradient(g, w, h) -> np.ndarray  # uint8 sRGB-encoded interpolation (CSS)
def gradient_at(bg: Background, f: int) -> Gradient; def gradient_css(g, w, h) -> str
```
Geometry: linear `d=(sin θ, −cos θ)`, `L=|W sin θ|+|H cos θ|`, `t=((x+.5−W/2)dx+(y+.5−H/2)dy)/L+0.5`; radial `t=|p−c|/(radius·hypot(W,H)/2)`.
Readers: numpy → `render_gradient` (cached per key/frame) / poster for video; composer → CSS in `{{BG}}`, animated via template `bgAt(f)` mirroring `gradient_at`; Lottie → static gradient poster, refuses animated gradient and video `PlanConflict("Lottie does not animate a {kind} background; export HTML or After Effects")`; solid_assets → poster; AE spec → poster image + warning; plan pins poster/synthetic/value; brief `background gradient linear 135° #ffffff→#f6c1d0`.
**Tests:** `test_old_scene_json_loads_and_roundtrips`; `test_gradient_background_validation`; `test_srgb_to_lab_reference_values` (#ff0000 → (53.24, 80.09, 67.20) ±0.05; ΔE(#a8001c, #fb9d9d) > 30); `test_render_gradient_css_geometry`; `test_composite_draws_gradient_and_animated_keys`; [browser] `test_css_gradient_matches_numpy` (max ≤ 3 levels, mean ≤ 0.8), `test_animated_gradient_frames`; `test_lottie_poster_for_static_gradient`, `test_lottie_refuses_animated_gradient_and_video`, `test_plan_pins_poster_video_synthetic`, `test_ae_spec_gradient_and_video_use_poster_with_warning`, `test_solid_assets_plate_uses_poster`, `test_brief_gradient_line`.

## Task 2: synthetic ground truth + `keepframe eval-edits`

**Deliverable:** `keepframe eval-edits` writes `metrics.json`, sheets and a summary; run once now for the "before" numbers in `docs/qa/reference-analysis/README.md`.
**Files:** modify `ir/synth.py`, `cli.py`; create `keepframe/qa/{__init__,metrics,edits,sheets}.py`, `docs/qa/reference-analysis/README.md`.
**Interfaces:**
```python
def make_reference_scene(root, seed, *, plate: Literal["flat","gradient","animated","image"]="gradient", n_sprites=3, n_titles=1,
                         mover=False, logo=True, fonts=None, frames=60, size=(640,360), fps=30.0) -> Scene
def render_reference(scene, root, out, *, renderer: Literal["numpy","browser"]="numpy") -> Path   # mp4 yuv444p crf 8
def ground_truth(scene, root, frames, *, renderer) -> dict[str, dict[int, tuple[np.ndarray, np.ndarray]]]
#   black/white double render per element: α = 1 − mean_c(I_w − I_b)/255; F = I_b/α where α > 0.02
def true_plate(scene, root, f, *, renderer) -> np.ndarray
# qa/metrics.py
def plate_residue(plate, mask, ring=(6,16)) -> dict      # ring deg-2 fit: mean/p95 ΔE in mask, hf_ratio, residue_fraction
def bg_leak_fraction(rgba, local_plate) -> float          # α>0.5 pixels with ΔE(F,B) < 5
def leak_correlation(rgba, plate) -> float
def halo_ring(recomposite, ideal, alpha_scene, width=4) -> float
def outside_glyph_delta(src, rebuilt, edited, old_a, new_a, box) -> float
def smear_score(edited, old_a, new_a) -> float
def glyph_colour_delta(render, alpha, expected_hex) -> float
def alpha_errors(alpha, alpha_gt, band=3) -> dict          # sad, mse
def title_integrity(scene, titles) -> list[dict]           # text, whole, fragments (NFKC casefold, ratio ≥ 0.9)
# qa/edits.py
def edit_checks(root, scene_id, *, title, hide, frames, renderer) -> dict
def eval_synthetic(out, n=12, *, renderer) -> dict
def eval_clip(clip, gold, out, *, max_frames=150, baseline=None, reuse=False, renderer="browser") -> dict
def eval_edits(clips_dir, gold_dir, out, *, max_frames=150, baseline=None, synthetic=12, reuse=False, renderer="browser", strict=False, only=None) -> dict
# qa/sheets.py
def comparison_sheet(rows, labels, out, cell_w=480) -> Path
```
Checks on a temp copy: title → "Fall Drop Sale" (overflow `expand_box`); hide gold title or largest sprite; background `replace` #1a2a6c (+ `tint` once Task 12 lands). Three frames per clip (settled title, mid, last). Gates in `metrics.json`: synthetic — title `outside_glyph_delta` ≤ 1.0, `smear_score` ≤ 3, glyph ΔE < 5; hide plate-vs-ring ΔE ≤ 2, hf_ratio ∈ [0.5, 2], residue ≤ 0.02; recolour `halo_ring` < 2; alpha SAD (edge band) ≤ 0.03, F ΔE < 3 interior / < 6 edge; font top-3 ≥ 0.90. Clips — title integrity (reported), colour vs gold ΔE < 10, `render_l1` ≤ baseline + 0.005, seconds ≤ 1.5 × baseline, leak metrics reported, `vlm_calls: 0`.
CLI: `keepframe eval-edits --clips DIR --gold DIR --out DIR [--max-frames 150] [--baseline FILE] [--synthetic N] [--reuse] [--clip NAME…] [--renderer browser|numpy] [--strict]`.
**Tests:** `test_double_render_recovers_exact_alpha_and_foreground` (α ≤ 1/255, F ΔE ≤ 0.5 where α > 0.1); `test_reference_scene_holds_title_60pct_and_static_logo`; `test_plate_residue_clean_vs_smear` (< 0.5 vs > 8); `test_bg_leak_fraction_flags_binary_crop` (> 0.2 vs < 0.01); `test_halo_ring_flags_white_cutouts` (> 10 vs < 1); `test_title_integrity_whole_vs_fragments`; `test_eval_edits_cli_writes_metrics_and_sheet` (numpy renderer, fake OCR).

## Task 3: plate v2 pass 2 + hole fill + `plate` stage

**Files:** create `analyze/plate.py`; modify `analyze/background.py` (`PASS1_PATH = "stages/plate_pass1.png"`), `progress.py` (STAGES gains "plate" after "tracking"), `analyze/pipeline.py` (pass 1 to PASS1_PATH, `_stage_plate`, `_scene_background(model, bconf)` replacing 426-428/631-633, rerun 585-625), `review/corrections.py` (113-118 read pass 1, legacy fallback), `web/static/js/analyze.js` (PIPELINE, `STEP_STAGES.bg=["background","plate"]`).
**Interfaces:**
```python
def sample_frames(n: int, s: int = 48) -> list[int]
def occupancy(shape, rbf, boxes, reveal_boxes, frames, dilate_px=8) -> np.ndarray
def masked_median(frames, sample, occ, band=64) -> tuple[np.ndarray, np.ndarray]   # plate, counts
def find_holes(counts, frames, rbf, boxes, reveal_boxes) -> tuple[np.ndarray, np.ndarray]
def fill_holes(plate, holes, uncovered, *, poly_max_rms=2.0) -> tuple[np.ndarray, np.ndarray, list[dict]]
@dataclass
class PlateModel: kind; image; rgb; synthetic; confidence=1.0; stats={}; gradient=None; gradient_keys=[]; frames=None
    def at(self, f) -> np.ndarray; def crop(self, f, box) -> np.ndarray
def build_plate(frames, rbf, boxes, text_tracks, shape_tracks, *, bg_rgb, bconf, bg_override, pass1) -> PlateModel
def save_plate(sd, model) -> None; def load_plate(sd) -> PlateModel | None; def background_for(model, sd) -> Background
```
Algorithm: S = min(N, 48) samples; occupancy = region masks + OCR boxes (+2 px) + reveal exclusion boxes, dilated 8 px (raw plate difference deliberately excluded); masked median in 64-row bands with a uint16 sentinel 256 and `take_along_axis`; `count==0` candidates confirmed against all N frames inside their bboxes; per hole: ring `dilate(r)\dilate(2)`, r = clip(0.25√area, 8, 48), deg-2 polynomial least squares per channel → if ring RMS ΔE ≤ 2.0 use it (`poly`), else `cv2.inpaint(TELEA, 5)` on bbox ± 32 (`inpaint`); rings < 30 px → inpaint. Classify here only `color` (override or p95 ΔE vs mean < 2.0) / `image`; never low-passed. Downstream `plate_img = model.image if kind != "color" else None`. Outputs `assets/background.png` (same name), `assets/background_synthetic.png` (when holes), `stages/plate.json {version:2, kind, rgb, confidence, stats}`; confidence = bconf × (1 − 0.5·synthetic_fraction). Rerun: rebuild when boundary ≤ plate, else `load_plate`; missing `plate.json` (legacy) → rebuild.
**Tests:** `test_masked_median_ignores_title_present_in_70pct_of_frames` (pass-1 ΔE > 15 under title; v2 p95 < 1.0); `test_ig2_like_smear_removed` (p95 < 1.5); `test_always_covered_logo_hole_poly_on_gradient` (p95 < 1.0, method poly, synthetic IoU ≥ 0.95); `test_textured_hole_uses_inpaint_and_lowers_confidence`; `test_hole_check_uses_all_frames_not_only_samples`; `test_stage_order_has_plate_between_tracking_and_solids`; `test_plate_stage_cached_and_rerun_from_sprites_reuses_it`; `test_rerun_from_regions_rebuilds_plate`; `test_legacy_project_without_plate_json_rebuilds_on_rerun_from_sprites`; `test_corrections_bbox_prompt_uses_pass1_plate`; flat-colour tests stay green; `test_plate_v2_speed` (48×1080×1920, 10 boxes, < 8 s).

## Task 4: plate classification

**Files:** create `analyze/gradient_fit.py`; modify `plate.py` (`classify`), `pipeline.py` (messages); update `test_dominant_gradient_uses_plate_on_analysis_and_rerun` (D4).
**Interfaces:** `fit_linear(plate_small, valid, max_stops=4)`, `fit_radial(..., max_stops=3)`, `fit_gradient(plate, valid, *, work=160) -> (Gradient|None, p95)`, `temporal_stats(frames, sample, occ, median) -> dict` (p95_dE, rank2_ratio, instability map u for Task 5), `classify(model, stats, frames, sample, occ) -> PlateModel`.
Algorithm: fit at ≤ 160 px; linear θ 0..355° step 5 (64 t-bins, median Lab, Douglas–Peucker ΔE 1.0 → ≤ 4 stops, score p95), refine ±2.5° step 0.5; radial centre 5×5 grid. Static (temporal V p95 < 1.5, V = RMS ΔE vs masked median at 1/4 res after 3×3 blur, pixels with ≥ 3 uncovered samples): `color` if flat; `gradient` if fit p95 ≤ 1.5 and stop range ΔE ≥ 3; else `image`. Animated: per-sample low-res plates → SVD of mean-subtracted Lab; rank2_ratio ≥ 0.95 and per-sample fit p95 ≤ 3 for ≥ 90 % → `gradient` with keys (drop keys whose removal keeps error ≤ 1 ΔE); else video candidate → until Task 13 kind `image` + `stats.video_candidate` + message "background animates (p95 ΔE x.x); kept as a still image". Poster = rendered gradient or median.
**Tests:** `test_flat_is_color`; `test_linear_two_stop_is_gradient` (angle ±1°, stops ΔE < 1.5); `test_dominant_gradient_is_three_stop_gradient`; `test_radial_gradient` (centre ±0.03); `test_photo_texture_is_image`; `test_compression_noise_still_static` (σ 1.5 + H.264 crf 23 round trip); `test_drifting_gradient_is_animated_gradient` (≥ 2 keys, per-frame p95 < 3); `test_textured_motion_is_video_candidate_kept_as_image_with_message`; `test_gradient_reaches_scene_and_composites` (bg L1 < 0.01).

## Task 5: movers

**Files:** create `analyze/movers.py`; modify `plate.py` (movers in the plate stage, `stages/movers.pkl`), `pipeline.py` (claimed tracks removed before solids/sprites; `props["m{i}"]`), `review/overlay.py` (mover masks).
**Interfaces:** `@dataclass Mover(id, frames: dict[int, (bbox, mask)], claimed: list[int], residual: float, stable: bool)`; `established_mask(shape, text_tracks, obj_tracks, n)`; `instability(frames, sample, established, scale=0.25)` (fraction of samples with ΔE > 6 vs median); `find_movers(frames, sample, u, obj_tracks, shape_tracks, *, min_area=0.04, min_unstable=0.40, max_area=0.60) -> list[Mover]`; `mover_props(m, frames, plate, n_frames) -> dict` (raw, canon, cf, kind "sprite", z 0, first, last, mover True, stable).
Algorithm: u ≥ 0.40 at 1/4 res, close 5 px, components with area ≥ 4 % and bbox ≤ 60 %, stable ring (median u < 0.1 over 8 px ring), ≥ 30 % unstable in ≥ 40 % of frames. Claims object/shape tracks ≥ 80 % inside the dilated component in ≥ 60 % of their frames (never text). Per-frame mask `dilate((ΔE(I_f, plate) > 8) ∩ bbox ∪ claimed, 2)`; translation from centroid; stable if median per-frame crop ΔE vs temporal-median crop < 3. Plate: add mover masks to occupancy, recompute median in bbox ± 24, refill holes. Stable → median-crop texture (Task 6 mattes it); unstable → still texture + message "`{eid}` animates in place; kept as a still sprite" (until Task 13). z = 0.
**Tests:** `test_rotating_globe_on_gradient_becomes_mover_not_plate` (one mover, bbox ±4 px, plate under globe p95 < 3, unstable, message); `test_globe_fragments_are_claimed`; `test_sliding_card_is_not_a_mover`; `test_small_or_brief_instability_is_not_mover`; `test_full_frame_animation_is_plate_not_mover`; `test_spinning_sphere_on_flat_bg_remains_solid`; `test_overlay_shows_mover_masks`; `test_mover_ids_stable_across_rerun`.

## Task 6: textures v2

**Files:** create `analyze/matting.py`; modify `pipeline._stage_sprites` (phase after refine, before solids; same thread pool), `_elements_from_props` (TextureMeta, padded canonical size); update tests asserting `canonical.width == bbox width`.
**Interfaces:** `@dataclass Sample(frame, I, B, valid, own, others, score)`; `element_affine(raw_row, tex_shape, canon_wh, anchor=(0.5,0.5))` (= `composite.texture_to_scene_affine`); `gather_samples(raw, canon_binary, frames, plate, others_at, pad=3)`; `score_samples(samples, raw, top_k=8)`; `triangulate(samples) -> (alpha, F, determined)`; `two_colour(samples, fill_rgb) -> list[alpha]`; `keyed(samples)`; `fuse(alphas, Fs, weights)`; `extend_foreground(F, alpha, thr=0.05)`; `texture_v2(raw, canon_binary, frames, plate, *, others_at, pad=3, top_k=8, kind_hint=None) -> (rgba, TextureMeta)`.
Algorithm: warp `I_f` and `plate.at(f)` into texture space (`warpAffine` + `WARP_INVERSE_MAP`) with the refined raw transform; pad 3 px each side (canonical grows by 2p, centre/anchor fixed); other elements' pixels → α 0. Filters visibility ≥ 0.98, occlusion ≤ 0.02, opacity ≥ 0.95, reveal = 1 (relax in order, cap confidence 0.4). Score = contrast_norm × sharpness_norm × 1/(1+(speed/6)²); top K = 8. Triangulation when ≥ 6 samples and Σw‖B_t − B̄‖² ≥ 12²: `k = Σw(B−B̄)(I−Ī)/Σw(B−B̄)²; G = Ī − kB̄; α = clip(1−k); F = G/α`. Two-colour when interior p90 ΔE ≤ 6 and edge ΔE(F,B) ≥ 15 at ≥ 80 % of edge pixels: interior = mask eroded by max(1, 0.35·stroke); `α = clip(((I−B)·(F−B))/‖F−B‖²)`. Keyed otherwise (projection on F_near − B in the 3 px band; ambiguous where ΔE(F_near,B) < 15 → binary edge). Weighted median fusion; extend F under α < 0.05; confidence = clip(1 − meanΔE/10) × method factor (1.0/0.95/0.8/0.3). Exception → binary texture + message.
**Tests:** `test_two_colour_recovers_antialiased_glyph_alpha` (edge SAD < 0.03, F ΔE < 3 / < 6); `test_triangulation_recovers_soft_glow_over_varying_plate` (α MAE < 0.02); `test_texture_has_no_background_pixels` (old leak > 0.2, v2 ≤ 0.01, colour ΔE < 5 to #a8001c); `test_halo_ring_after_recolour` (< 2); `test_foreground_extended_under_zero_alpha` (< 3); `test_frame_scoring_skips_blurred_partial_occluded_and_faded_frames`; `test_texture_padding_keeps_transform`; `test_failure_falls_back_to_binary_with_message`; `test_textures_v2_speed` (40 × 200×100 × 150 frames < 20 s). `gate-m2 --n 20` still passes (≥ 16/20, temporal ≥ 0.7).

## Task 7: font registry + all bundled OFL fonts

**Files:** create `keepframe/fonts/{__init__,registry}.py`, `fonts/manifest.json`, `fonts/files/*.woff2` (all 60–80 families, D1), `fonts/LICENSES/*.txt`; modify `pyproject.toml` (package-data `fonts/**`; `fonttools[woff]>=4.50`), `Dockerfile` (no fetch needed), `cli.py` (`keepframe fonts list`).
Manifest entry: `{"family","category","files":[{"file","sha256","axes":{"wght":[100,900]},"italic"}],"scripts":["latin"|"hangul"],"license":"OFL-1.1","license_file","source"}`; categories `geometric_sans, neo_grotesque, humanist_sans, rounded_sans, condensed_sans, display, serif, slab, script, handwriting, mono`.
**Interfaces:** `@dataclass(frozen) FontFace(family, source, path, index=0, weight_range=(400,400), italic=False, category="neo_grotesque", postscript=None, sha256="", latin=True, hangul=False)`; `class FontRegistry: for_project(root) (uploaded > bundled > system), families(*, script=None), face(family, weight), covers(family, text) (fontTools cmap), hangul_fallback(family, weight) -> (family, weight)`; `safe_alias(name) -> str`.
Hangul map: geometric/neo-grotesque/condensed/display/script → Pretendard; humanist/rounded → Noto Sans KR; serif/slab → Noto Serif KR; handwriting → Nanum Pen Script; mono → Pretendard; weight rounded to 100.
**Tests:** `test_manifest_files_exist_hash_and_licence`; `test_bundled_set_coverage` (≥ 60 families, every category present, Pretendard/Noto KR cover U+AC00); `test_woff2_variable_weight_in_pillow` (Inter wght 800 vs 300 coverage ratio ≥ 1.4 — run first as a spike); `test_registry_precedence_uploaded_bundled_system`; `test_hangul_fallback_map`; `test_safe_alias`.

## Task 8: styled text rasteriser + composer CSS (browser)

**Files:** create `keepframe/fonts/raster.py`, `keepframe/fonts/css.py`; modify `edit/textraster.py` (delegate to the registry when a bundled/uploaded face exists), `edit/apply.py` (`write_text_texture` → `render_styled`), `compose/composer.py` (text branch 101-106, `{{FONTS}}`), `template.html` (`.el span`), `render/plan.py` (pin `FontGuess.file`).
**Interfaces:** `glyph_alpha(lines, size_px, faces, *, weight, tracking_em=0, shear_deg=0, box=None, dx=0, dy=0, fallback_scale=1.0) -> H×W float32`; `compose_text(alpha, fill, effects) -> RGBA float`; `render_styled(lines, font, color, style, *, registry, scene_dir=None, box=None) -> RGBA uint8` (un-premultiplied); `font_face_css(scene, scene_dir, registry) -> str`; `text_css(font, color, style, box_wh) -> str`.
Layout (matches CSS): line box = canonical height / lines; baseline = (line_h − (asc+desc))/2 + asc (hhea); `x_i = getlength(prefix_i) + i·t` (+ trailing t); shear about the baseline; runs split by cmap between primary and fallback, fallback at size × `fallback_scale` (= `@font-face size-adjust`). Effects: shadow/glow `op·C·GaussianBlur(shift(α,dx,dy), σ=blur/2)`; stroke ring `dilate(α,w) − α` under fill; order shadows → stroke → fill; gradient fill via `render_gradient`; fade multiplies α. CSS: family list, weight, size, line-height, `letter-spacing: Tem`; gradient `background-image + -webkit-background-clip:text + -webkit-text-fill-color:transparent`; `-webkit-text-stroke: 2w` + `paint-order: stroke fill`; `text-shadow` (blur = 2σ); fade `-webkit-mask-image`; shear `skewX(−θ)`; offset `left/top`. `@font-face` per used face (variable `font-weight: a b`, fallback `size-adjust`), subset with `fontTools.subset` to used codepoints + Basic Latin, cached in `~/.cache/keepframe/font-subsets/`.
**Tests:** `test_tracking_adds_n_times_t`; `test_shear_offsets_top_row` (±0.5 px); `test_weight_axis_changes_coverage`; `test_effects_forward_model`; `test_fonts_css_only_used_families_safe_names`; `test_legacy_text_without_style_html_unchanged`; `test_plan_pins_uploaded_font`; [browser] `test_css_matches_raster_{plain,tracking,weight,shear}` (mask IoU ≥ 0.90, bbox Δ ≤ 1.5 px), `test_css_matches_raster_gradient` (p95 ΔE ≤ 4), `test_css_matches_raster_effects` (box L1 ≤ 0.03), `test_hangul_fallback_runs_match` (IoU ≥ 0.85), `test_edited_text_preview_matches_html` (L1 ≤ 0.03).

## Task 9: text style analysis

**Files:** create `analyze/textstyle.py`; modify `pipeline._stage_sprites` (style phase after textures v2, text only), `_elements_from_props` (`Canonical.style`, `color`), `text.py` (old core-mask colour only as fallback), `rerun` (keep `style` for manual text, 638-645).
**Interfaces:** `stroke_width(alpha)` (2 × median DT on ridge pixels); `cap_height(alpha)`; `fill_colour(rgba, stroke_w) -> (hex, lab, spread)`; `fit_fill_gradient(F, interior) -> Gradient|None` (Lab ~ a + Bx·x + By·y); `fit_fade(alpha, interior) -> Fade|None`; `fit_effects(I, B, alpha, fill_rgb, cap) -> list[TextEffect]`; `glyph_centres(alpha, text) -> list[float]|None`; `analyse_text_style(rgba, meta, frames, plate, raw, text) -> (TextStyle, hex, dict)`.
Algorithm: fill = median Lab of F over α > 0.5 eroded by 0.35·stroke (matted texture → local plate); strokes < 3 px → 90th contrast percentile. Gradient only if SSE drops > 40 % and stops differ ΔE ≥ 6. Fade if interior α range > 0.3 and residual drop > 40 %. Effects on a wider crop (pad = max(4, 0.5·cap)), residual `R = I − (αF + (1−α)B)` outside α > 0.05: shadow grid dx,dy ∈ [−0.3cap, 0.3cap] (coarse 2 px then 1 px), σ ∈ {0,1,2,4,8}, colour/opacity by least squares, darkening; glow zero offset σ ∈ {2,4,8,16}, lightening; stroke w ∈ {1,2,3,4,6,8} (≤ 0.15cap), ring spread ΔE < 8. Accept if residual energy falls ≥ 30 %; ≤ 1 per kind.
**Tests:** `test_fill_colour_uses_local_plate` (ig2 case ΔE < 5 to #a8001c; old > 20); `test_two_stop_gradient_detected`; `test_flat_noisy_text_has_no_gradient`; `test_fade_detected`; `test_shadow_recovered` (offset ±1, blur ±2, colour ΔE < 8); `test_glow_recovered`; `test_stroke_width_within_1px`; `test_no_false_effects_on_20_clean_titles`; `test_stroke_ratio_tracks_weight` (±10 % at 300/400/700/900); `test_glyph_centres_count_matches`.

## Task 10: font render-and-compare + Hangul fallback

**Files:** create `keepframe/fonts/match.py`; modify `analyze/fonts.py` (`font_candidates`/`font_family_guess` become thin wrappers), pipeline style phase (sets `FontGuess`, `style.tracking_em/shear_deg/dx/dy`), `ir/synth.py` (`make_font_sample(seed)` over bundled fonts), `web/server.py` `_slim_scene` (scores); update `tests/test_font_candidates.py`.
**Interfaces:** `@dataclass FontFit(family, weight, size_px, tracking_em, shear_deg, score, dx, dy)`; `prefilter(alpha, text, registry, k=8)`; `fit_family(alpha, text, family, registry, *, stroke_ratio=None, centres=None) -> FontFit`; `match_font(alpha, text, registry, *, k=3, stroke_ratio=None, centres=None) -> (list[FontFit], confidence)`; `fit_hangul_fallback(best, alpha, text, registry) -> (family, weight, scale)`.
Algorithm: soft IoU of observed α vs rendered coverage, tight bbox, 64 px high, strings > 24 chars capped (longest words); prefilter by |log width ratio at matched cap height| + |stroke-ratio difference| to k = 8; size from cap height ("H"); weight golden-section on wght (variable) seeded by stroke-ratio calibration, static families try each weight; tracking by least squares on glyph centres else 1-D search [−0.06, 0.12] em step 0.01; shear {−12,…,12}° then ±2°, stored only if |shear| ≥ 4°; two rounds of coordinate descent; render cache per (family, weight, text, size bucket). Confidence 1.0 if top ≥ 0.80 and margin ≥ 0.02 else 0.5. Hangul text with a Latin best face lacking coverage → `registry.hangul_fallback`, refit size/weight on Hangul run columns (default scale 0.92).
**Tests:** `test_font_top3_on_synthetic_set` (≥ 0.90 over 30 samples; eval runs ≥ 60); `test_weight_size_tracking_accuracy` (weight ±100, size 3 %, tracking 0.02 em, each ≥ 80 %); `test_uploaded_font_joins_and_wins`; `test_low_confidence_on_tie`; `test_hangul_fallback_inter_bold_to_pretendard_700` (scale 0.88–0.96); `test_serif_maps_to_noto_serif_kr`; `test_match_runtime` (≤ 1.0 s median per 12-char text).

## Task 11: per-project font uploads (browser)

**Files:** create `keepframe/fonts/upload.py`; modify `web/server.py` (`POST`/`GET /api/projects/<pid>/fonts`; move `ae/api.py:_length` to a shared helper), `web/static/agent.html` + `js/agent.js` + `js/i18n.js` (`fonts.*` ko/en) + cache-buster, `analyze/pipeline.py` (registry from project root; chosen uploaded family copied by `scene_font_asset` to `assets/font-<sha16>.<ext>`, sets `FontGuess.file/source/postscript`), `edit/agent.py` + `apply.py` (`fonts: FontRegistry`), `ae/spec.py` `_font` (190-222: exact `guess.postscript` match first, rest unchanged).
**Interfaces:** `MAX_FONT_BYTES = 20 MiB`; `class FontRejected(ValueError): code in {too_large, bad_type, bad_tables, unsupported}`; `inspect_font(path, filename) -> dict` (family, original_family, style, weight_range, postscript, category, ext, sha256, bytes, latin, hangul); `store_font(project_root, tmp, filename) -> (dict, created)`; `list_fonts(project_root)`; `scene_font_asset(scene_dir, entry, project_root) -> str`.
Route: `_same_origin` → `_safe_render_project` → `name` matches `^[^/\\]{1,128}\.(ttf|otf|woff2)$` → Content-Length required ≤ 20 MiB else 413 before reading → stream 1 MiB chunks to `fonts/.upload-*.part` → magic bytes match extension (`\0\1\0\0`/`true`, `OTTO`, `wOF2`; reject .ttc) → WOFF2 `totalSfntSize` ≤ 64 MiB before decompress → `TTFont(lazy=False)` with cmap, head, hhea, hmtx, maxp, name and glyf+loca|CFF|CFF2; numGlyphs ≤ 65535; Latin or Hangul coverage → Pillow renders "Aa" (and "가") → `os.replace` to `fonts/<sha256>.<ext>`, atomic locked `index.json` → 201 new / 200 duplicate / `{"error": code}` 400|413. GET returns metadata only; no route serves font bytes. UI: "Fonts" disclosure (list + upload `.ttf,.otf,.woff2`, errors ko/en, note "used from the next analysis or text edit").
**Tests:** `test_oversize_font_rejected_before_body_read` (0 body bytes read); `test_cross_origin_upload_forbidden`; `test_bad_name_and_traversal_rejected`; `test_bad_magic_rejected`; `test_invalid_font_tables_rejected`; `test_duplicate_upload_is_idempotent`; `test_font_bytes_never_served`; `test_uploaded_font_used_in_analysis_and_copied_to_scene_assets`; `test_text_edit_uses_uploaded_font`; `test_ae_postscript_match_wins`; `test_ae_missing_postscript_keeps_substitution_warning`; [browser] `test_font_upload_ui_lists_and_reports_errors`.

## Task 12: edit safeguards (browser)

**Files:** create `edit/tint.py`; modify `edit/intent.py` `plan()` (`Conflict(id="background_kind", element="background", choices=["tint","replace","cancel"], reason=…)` when kind ≠ color; `font_missing` checks the registry before fontconfig), `edit/apply.py` (background branch 134-136; `_apply_text`/`_apply_color` via `render_styled` + registry + fallback), `edit/agent.py` (`cancel` → `EditResult(status="cancelled")`, nothing applied), `web/static/js/agent.js` + `js/review/edit-form.js` (cancelled status), `i18n.js` (`review.editChoice.tint/replace/cancel`, `agent.cancelled`, ko/en).
**Interfaces:** `tint_lab(lab, target_lab, stats) -> lab` implementing D5 (`L' = L_T + k(L − L̄)`, `a' = a_T + (a − ā)`, `b' = b_T + (b − b̄)`, k as in D5, gamut-clipped); `tint_background(bg, scene_dir, target_hex) -> Background` — image → new `assets/background.tint<n>.png` (+ poster); gradient → every stop/key mapped through the same formula; video → per-frame re-encode (Task 13); never overwrites an existing asset; keeps `synthetic`.
Text edits: font order uploaded > bundled > Hangul fallback > system > Hershey; keep analysed `style`; never touch tracks (incl. rx, ry, reveal); colour edit on a gradient fill sets `style.fill = None`, keeps effects.
**Tests:** `test_background_edit_on_picture_needs_choice` (image/gradient/video conflict; color none); `test_cancel_creates_no_version_or_assets`; `test_replace_sets_color`; `test_tint_writes_new_asset_and_keeps_original` (original bytes unchanged; mean L within ±3 of L_T; mean (a,b) within ΔE 3 of target; per-pixel L ordering preserved — Spearman ρ ≥ 0.98 vs original L); `test_tint_white_plate_navy_target_is_dark` (mean L ≤ L_T + 3 for #1a2a6c); `test_tint_gradient_maps_stops`; `test_title_edit_keeps_analysed_colour_effects_and_tracks` (glyph ΔE < 5, shadow at expected offset, tracks equal); `test_font_edit_to_uploaded_family`; `test_hangul_text_on_latin_font_uses_fallback`; `test_synthetic_title_edit_has_no_smear` (`outside_glyph_delta` ≤ 1.0, `smear_score` ≤ 3); [browser] `test_agent_shows_three_choices_and_cancel_does_not_apply`.

## Task 13: video plates + video sprites (cuttable; browser)

**Files:** create `analyze/videoasset.py`; modify `plate.py` (video candidates → kind `video`, per-frame plate memmap `stages/plate_frames.npy`, `assets/background.webm` + poster), `movers.py` (unstable movers → `assets/<eid>.video.webm` RGBA + `Canonical.video`, median crop as poster texture), `composite.py` (per-frame decode), `composer.py` + `template.html` (`<video>`, async `__seek`), `lottie.py` (refuse video sprites), `plan.py` (pin refs), `tint.py` (video tint).
**Interfaces:** `ffmpeg_vp9_ok() -> bool`; `encode_webm(frames, fps, out, *, alpha, crf=30) -> Path` (libvpx-vp9, yuva420p|yuv420p, `-b:v 0 -crf`, `-deadline good -cpu-used 4 -row-mt 1`); `class VideoReader(path, size, alpha): frame(i) -> np.ndarray` (rawvideo pipe, sequential, restart on backward seek, LRU 8); `fill_video_plate(frames, occ_all, hole_fill) -> np.memmap` (nearest uncovered frame, forward/backward passes in row bands).
Details: video time (f − start + 0.5)/fps; template `__seek` async awaiting `seeked` then `requestVideoFrameCallback`; `__ready` waits for `loadeddata`; size cap 200 MB → re-encode crf 36 → still + message; no ffmpeg/libvpx → Task 4/5 behaviour + message.
**Tests:** `test_webm_rgba_roundtrip` (α MAE ≤ 0.03, interior p95 ΔE ≤ 4); `test_reader_sequential_and_backward_seek`; `test_textured_motion_background_is_video_plate` (p95 ≤ 3 under a moving element); `test_rotating_globe_is_video_sprite` (globe-bbox L1 ≤ 0.6 × still-sprite L1); [browser] `test_video_frames_exact_in_chromium` (colour-bar frames 0, 1, 17, N−1), `test_render_deterministic_with_video` (+ `gate-m1`); `test_lottie_refuses_video_sprite`; `test_no_ffmpeg_falls_back_with_message`; `test_tint_video_reencodes_and_keeps_original`.

## Task 14: AE export (extension + spec)

**Files:** modify `ae/spec.py` (gradient: linear/radial with 2 stops → solid + `effects.gradient` = ADBE Ramp (start/end from the CSS gradient line, colours `[r,g,b,1]`, shape 1 linear / 2 radial, animated keys); > 2 stops → poster + warning; video → `"footage"` layers with `start_time = visible[0]/fps`; text source + `tracking = tracking_em·1000` + `stroke {color, width: 2w, over_fill: false}`; `effects.shadows` = Drop Shadow (direction atan2(dx, −dy), distance hypot, softness = blur; glow = distance 0); `effects.fill` = Ramp + Set Matte (self alpha); fade → warning "text fade not exported"; shear folded into skew); create `ae/footage.py` (plate → H.264 `.ae.mp4`, sprite → ProRes 4444 `yuva444p10le` `.ae.mov`, lazy, cached by source sha, flat assets); `extension/host/keepframe.jsx` (kind regex + `footage`; `owned()` adds "Keepframe Gradient", "Keepframe Shadow N", "Keepframe Fill", "Keepframe Fill Matte"; `writeEffects`, `writeLayer` (TextDocument stroke/tracking), `createLayer` (footage `startTime`), `managed`, `fingerprint`, `layerHash`, `kind()`); `tests/ae_fake/ae.js` (ADBE Ramp, ADBE Drop Shadow, ADBE Set Matte3, footage duration, TextDocument stroke/tracking); extension version bump; `docs/qa/ae-extension/README.md` live-check steps.
**Tests:** `test_gradient_background_ramp_points_from_css_angle` (135deg on 1920×1080 within 0.5 px); `test_multistop_gradient_exports_poster_with_warning`; `test_video_layers_use_derived_footage_and_start_time`; `test_text_stroke_tracking_shadow_glow_fill_spec`; `test_postscript_preferred`; host sync create → resync no-op, stop-colour change updates only that effect, footage startTime, ES3 regex; `test_spec_unchanged_for_legacy_scene` (golden JSON byte-identical).

## Task 15: final evaluation and approval (controller)

- [ ] Full suite, browser tests, Node tests, `gate-m1`, `gate-m2` (≥ 16/20, temporal ≥ 0.7).
- [ ] `keepframe eval-edits --clips eval/clips --gold docs/qa/reference-analysis/gold --out eval/out/ra-final --synthetic 60 --baseline docs/qa/reference-analysis/baseline.json --strict` on an idle machine.
- [ ] Gates: all synthetic gates; per clip `render_l1` ≤ baseline + 0.005, seconds ≤ 1.5 × baseline, title colour ΔE < 10; plate residue and `bg_leak_fraction` better than Task 2's "before" numbers.
- [ ] Live AE check (build zxp; user reinstalls): gradient Ramp, Drop Shadow/glow look, Ramp + Set Matte fill, ProRes alpha footage, PostScript match with an uploaded font installed on the AE PC.
- [ ] Review sheets (source | rebuilt | title edited | element hidden | background tinted/replaced) with the user; record in `docs/qa/reference-analysis/README.md`; whole-branch review (opus) → one fix wave → merge (user's choice) → `graphify update .`.

## Time budget (reference = Task 0 baseline × 1.5)

| Added phase | envato1 (2560×1440, 55 el.) | ig1 (126 el., 44 texts) | Kept cheap by |
|---|---|---|---|
| plate pass 2 + holes (T3) | ≤ 10 s | ≤ 6 s | uint16 sentinel sort in row bands; S ≤ 48; holes confirmed only in their bboxes |
| classification / movers (T4/T5) | ≤ 5 s / ≤ 3 s | same | 160 px fits; 1/4 resolution |
| textures v2 (T6) | ≤ 10 s | ≤ 20 s | crops only; K = 8; thread pool |
| style + fonts (T9/T10) | ≤ 10 s | ≤ 25 s | prefilter 8 families; render cache; 24-char cap |
| video encode (T13) | ≤ 60 s only when video | — | `-cpu-used 4 -row-mt 1`; crf cap |

Cut order if a time gate fails: prefilter k 8 → 5, `top_k` 8 → 5, then the shear search.

## Verification (end to end)

1. Unit + browser + Node suites green on the branch and again on the merged result.
2. `keepframe eval-edits` before (Task 2) vs after (Task 15) table committed in `docs/qa/reference-analysis/README.md`: plate residue, leak/halo, title colour ΔE, `render_l1`, seconds.
3. ig2demo re-analysed: title texture without the globe/emoji, title colour ≈ #a8001c, globe as its own layer (video sprite), plate without the red smear; "make the background navy" asks tint/replace/cancel and tint keeps the globe and gradient structure in dark navy; title edit "가을 여행" renders dark red in the matched font (or Hangul fallback) with no band.
4. Live AE check of Task 14 items in the user's AE 26.5.
