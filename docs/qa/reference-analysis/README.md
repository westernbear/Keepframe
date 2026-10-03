# Reference analysis verification · 2026-10-03

Implementation: one original preview with an SVG analysis layer; immutable observations and original-frame arrays referenced by each scene version. The existing recreation, render, reconstruction frame and diagnostic code paths remain available.

## Automated checks

The related regression run passed **91 tests** (4 browser/GPU/OCR-marked tests excluded). It covered store/schema, pipeline, corrections, edit/session behavior, web APIs, review/static UI contracts, workflow and navigation. Additional focused tests cover:

- Actual masks through movement, rotation and occlusion; holes and separate islands; empty frames.
- OCR boxes and actual confidence; manual text and box corrections.
- Frozen observations and original frames after reanalysis, inherited analysis on edits, and edits branched from historical versions.
- Legacy cache migration only to the latest version; unavailable historical layers.
- Original-only preview caching, versioned original URLs, and cancellation of a late playback promise after a seek/restart.

Commands:

```sh
.venv/bin/python -m pytest tests/test_analysis_overlay.py tests/test_playback_transport.py tests/test_pipeline.py tests/test_corrections.py tests/test_store.py tests/test_schema.py tests/test_edit.py tests/test_session_tools.py tests/test_web_review.py tests/test_web_server.py tests/test_web_static.py tests/test_web_workspace.py tests/test_web_edit.py tests/test_web_states.py tests/test_web_agent.py tests/test_web_jobs.py tests/test_maker_navigation.py tests/test_workflow_shell.py -m 'not browser and not gpu and not ocr' -q
git diff --check
graphify update .
```

## Browser checks

Used the gstack `browse` shared Chromium on a disposable workspace at `/tmp/keepframe-overlay-qa`, port 8876. This host requires `GSTACK_CHROMIUM_NO_SANDBOX=1` when launching browse.

| Scenario | Observed result |
| --- | --- |
| Select text region | SVG, object list and detail select `e3`; recognized and saved text shown separately; no fabricated demo OCR score |
| Select timeline object | List, timeline and overlay all select `e3` |
| Opacity | Initial SVG fill opacity `0.25`; keyboard adjustment to 1% produces `0.01` |
| Hide layer | Zero overlay paths; original remains visible |
| Draw correction | Separate edit mode captures box `349,160,415,205` at frame 0 |
| Apply correction | New version `v2`; detail size changes to `66 × 45`; image URL and overlay both use `v2` |
| Rapid seek | Delayed frame 1 response cannot replace frame 2 |
| Version race | Delayed `v1` state response cannot replace selected `v2` |
| Overlay outage | Old regions disappear; original advances past frame 5 |
| Historical layer absent | “No analysis layer for this version”; zero regions, original width 640; correction mode disabled |
| Review network | **0 `/frame/recon/` requests**, including playback and seeking |
| Approve | Navigates to `/agent?project=demo&scene=synth7&v=v2` |
| Layout/language | Checked 1440×900, 1280×720 and 390×844; Korean and English controls/details |

`race-check.js` can be run with `browse eval` on the disposable demo after creating `v2` with a box correction. It intentionally delays responses and simulates an overlay outage, restores `fetch` afterward, and asserts the outcomes. Run it with the UI set to Korean and frame 0. `draw-check.js` dispatches pointer events in the browse page to verify drag coordinate mapping; select `e1` and enable Edit region first. Neither script is intended for a production project.

Screenshots: [desktop](desktop.png), [mobile](mobile.png). Console checks found no application errors. `graphify update .` completed; its optional semantic community relabeling was not run.
