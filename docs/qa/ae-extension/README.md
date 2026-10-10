# After Effects extension live-check results

Guide: [Install and live check](live-check.md).

- Date: 2026-10-06
- AE version: After Effects 2026 (26.5x89), Korean UI
- Windows version: Windows (win32 10.0.26300)
- Extension version: 1.0.238 (build 238-d01d1a5) for items 1–11; 1.20261006.130736265 (build 1a07860) for items 12–17 (live check 2, 2026-10-07)
- Method: the controller sent jobs through the web routes; frames were exported from the user's AE through the Higgsfield bridge and compared with Keepframe's own render of the same frames.

## Results

Record each scene as pass, fail, or not run. Leave unrun results blank.

| # | Check | How | Expected | Result |
| --- | --- | --- | --- | --- |
| 1 | Editable comp and layers | Send all three scenes. Inspect the Project panel, timeline, and layer properties. | A comp and Keepframe layers appear under `Keepframe/<project>`. Layers and keys are editable. || Pass — four comps (reveal, plate, model, ig2demo with 55 layers) created under `Keepframe/<project>`. |
| 2 | Resend is a no-op | Send the same version again. Compare layers, keys, the panel's job counts, and the next Undo action. | Nothing changes. Created, updated, and deleted counts are zero. No new undo step appears. || Pass — resending ig2demo v3 reported 55 unchanged, 0 created/updated/deleted. (Undo history not inspected.) |
| 3 | Reveal direction | Scrub the reveal text scene from hidden to visible. Inspect the Keepframe Reveal effect. | It wipes left to right. Linear Wipe angle is 270 degrees. Completion falls from 100 to 0. Feather is zero. || Pass — frames at 0.25/0.6/1.8 s wipe left to right and match Keepframe ([image](live1-reveal-ae-vs-keepframe.png)). |
| 4 | Image background | Inspect the image background scene's timeline and composition. | The background is at the bottom of the Keepframe layer stack. The intended image is visible behind the scene. || Pass — plate background at the bottom; frame matches Keepframe ([image](live1-plate-ae-vs-keepframe.png)). |
| 5 | GLB rotation axes | Scrub the spinning GLB. Inspect X, Y, and Z rotation separately. | It is a model layer. Rotation axes and signs match the scene. || Pass for Y rotation (the only animated axis in the scene): direction and timing match Keepframe. AE shows perspective foreshortening (Keepframe uses an orthographic camera). |
| 6 | GLB fit | Compare the model with its scene box at the initial frame. Inspect anchor, position, and scale. | The model fits its box. Z position is zero. Z scale follows X scale. || Pass after fix f45140b — anchored on the model bounds centre and fitted to 90% like the composer ([image](live1-model-ae-vs-keepframe.png)). |
| 7 | Advanced 3D renderer | Inspect the GLB comp's renderer in Composition Settings. Ask the controller to inspect its scripting match name. | The renderer is Advanced 3D (`ADBE Calder`). || Pass (indirect) — the model sync selected `ADBE Calder` without error; Composition Settings not inspected. |
| 8 | Text and font note | Inspect font, size, fill colour, and anchor. Scrub the Korean title. Open the web job's warnings. | Text uses the requested settings and measured anchor. Korean text is readable. A missing requested family produces an Arial substitution warning. || Pass after fixes f45140b/d01d1a5 — Hangul falls back to Malgun Gothic Bold, text sits on Keepframe's box model; ig2demo frame mean L1 0.0029 vs Keepframe ([image](live1-ig2demo-text-ae-vs-keepframe.png)). |
| 9 | Hand edit and overwrite | Change a Keepframe layer's managed position in AE. Resend. Click **Overwrite them** (덮어쓰기), then **Yes** (예) in the web card. | The first send warns about hand edits and applies nothing. The confirmed overwrite restores the scene values. || Pass — opacity changed to 50% in AE → send reported `applied:false, hand_edited:[kf:text]` and AE kept 50%; overwrite (`force:true`) restored 100% and rewrote only `kf:text` (2 unchanged). |
| 10 | User effect survives | Add a user effect to a Keepframe layer. Send a scene update that retains its layer kind. | The effect and its settings survive the update. || Pass — a Gaussian Blur added in AE to the reveal text layer survived an update that moved the text 20 px (v2: `kf:text` and its companion updated, blur still after Keepframe Reveal). |
| 11 | User layer stays untouched | Insert an untagged user layer between Keepframe layers. Update and reorder the Keepframe layers. | The user layer is never moved or changed by sync. New or deleted layers may change its numeric index. || Pass — a user solid ("user overlay", 30% opacity) above the Keepframe layers stayed on top and unchanged while v4 swapped the order of `e9`/`e5` (55 layers unchanged in content, only the two moved). |
| 12 | Partial or interrupted sync | Have the controller cause a mid-write failure. Inspect the panel, web job, and partial layer. Resend, then confirm overwrite if offered. | Failure text is visible. An incomplete ownership tag leads to a hand-edit warning on normal retry. Forced retry reuses that layer. Undo is available for partial changes; sync is not transactional. || Pass (live check 2) — with `e27 · title` locked in AE, v1→v4 replaced the background, then failed on `e27`: the web job and panel read “After Effects 오류: "e27 · title" 레이어가 잠겨 있어 "inPoint" 특성을 설정할 수 없습니다. (line 477)”. After unlocking, a normal resend refused with `hand_edited: [kf:e27]` and left AE untouched; the card's **Overwrite** (덮어쓰기) sent `force: true` and updated only `e27` (54 unchanged); the next resend was a no-op (55 unchanged). |
| 13 | Heartbeat during long sync | Run a sync longer than 90 seconds. Watch web progress and have the controller inspect device `last_seen` while AE is busy. | Heartbeats continue about every 15 seconds. The job remains running until completion and does not fail as disconnected. || Pass (live check 2) — a synthetic 201-layer, ~120k-key scene kept its job `running` for 600 s: 41 progress posts, at most 15 s apart, never swept as disconnected. Finding: at 10 min the panel's host-call limit failed the job (“After Effects did not respond within 10 min (a dialog may be open in AE)”) while AE kept executing the sync; the AE project had to be force-quit. Note: Composition Settings (Ctrl+K) left open does not block the sync in AE 26.5. |
| 14 | Network drop recovery | Drop the network during polling and during result posting. Restore it. Compare the panel status and web job. | The panel reports the failure and retries with backoff. A completed result is retried. After 60 seconds without contact, a running job can fail as `AE disconnected`; resend explicitly. A lost result acknowledgement can produce HTTP 409. || Pass (live check 2) — server stopped for 35 s while the panel was idle; the panel reconnected 25 s after the restart (backoff) and the next sync worked. Dropping the network during result posting was not exercised. |
| 15 | Revoke pairing | In the web device row, click **Disconnect** (연결 해제), then **Yes** (예). Repeat during a long sync. | The next authenticated request returns 401. The panel stops and asks for a new code. Already executing native AE work cannot be canceled or undone by the panel. || Pass (live check 2) — `DELETE /api/ae/devices/<id>` (the card's Disconnect) → the panel's next poll got 401 within 10 s; the panel stopped with “페어링 안 됨: Keepframe 웹 페이지의 새 코드를 입력하세요”. Revoking during a long sync was not exercised. Re-pairing with a new code replaced the old device (one device listed). |
| 16 | Version mismatch (426) | Have the controller simulate a protocol-major mismatch, including during a long host call. Open the panel's **Download extension** (확장 다운로드) link. | The panel stops, asks for an extension update, and offers `/ae/keepframe.zxp`. Already executing native AE work may still finish. || Pass (live check 2) — server restarted with protocol major 2: the panel's `/next` got 426, it stopped polling (no request for 60 s) and showed “Keepframe 확장을 업데이트하세요: http://…:8765/ae/keepframe.zxp”. Reopening the panel resumed it after the server went back to protocol 1. During a long host call was not exercised. |
| 17 | Copy log without secrets | Click **Copy log** (로그 복사) in CEP. Paste into a text file after pairing, errors, and recovery. | Copy works. The log contains neither the pairing code nor the device token. || Pass (live check 2) — after re-pairing and one sync, Copy log held four lines (not paired, paired with host, syncing, synced with counts) and no pairing code or token. The log is in memory only, so reopening the panel clears it. |

## Findings

Fixed during live check 1 (each with a fake-AE behaviour and tests):

1. Copy buttons failed on plain http (`navigator.clipboard` exists only in secure contexts) — 6ecdc5a.
2. The card's install command (cmd, bare file name) failed from an admin shell with UPIA status -160 — now PowerShell with the full Downloads path — b508520.
3. Browser GETs on plain http to a Tailscale name carry neither Origin nor Sec-Fetch-Site, so `/api/ae/state` and `/api/ae/devices` returned 403 — GETs now check the Host allowlist only — b508520.
4. A panel that dropped a claimed job left it `running` forever while it kept polling — a `/next` from a device with a running job now fails that job — e383163.
5. AE transforms are 3-D on every AV layer (Scale ThreeD, Position/Anchor ThreeD_SPATIAL); the sync read a third ease pair the spec never had (`undefined is not an object`) — 6f2ad04.
6. A rebuilt 1.0.0 zxp did not replace the installed one; builds now carry `1.0.<commits>` and a build id the panel checks against the AE script — 4231384.
7. Z Position, X/Y Rotation and Orientation are hidden on 2D layers and cannot be set — d8bba9a.
8. Look fixes from frame comparisons: fontless text drawn from its glyph image (plus a disabled editable text layer, user decision), Arial Bold / Malgun Gothic Bold fallbacks, Keepframe's text box model, model centring and 90% fit — f45140b, d01d1a5.

Found in live check 2 (open):

9. Very large scenes can outlast the panel's 10-minute host-call limit: keys are written one call at a time (value, ease, interpolation), about 5 ms per key at 600 keys per property in AE 26.5, so ~120k keys took over 10 minutes. The job then fails with a message that blames an open dialog although AE is only slow, and AE keeps running the sync. Typical scenes (ig2demo: 55 layers, 949 keys) sync in about 2 s.
10. Precomposing or deleting a Keepframe layer in AE makes the next send create it again in the main comp, with no warning (planned for slice 4). Until then, precompose Keepframe layers only after the last send.

Known differences:

- Known renderer difference (AE 26.5): AE draws the dark back face of a single-sided triangle; Keepframe's Three.js renderer culls that face. This is a renderer difference, not a placement bug.
- AE uses a perspective comp camera for 3D model layers; Keepframe's composer uses an orthographic camera, so rotating models show slight foreshortening in AE.

## Live check 3 (slice 2)

- Date: 2026-10-07
- AE version: After Effects 2026 (26.5x89), Korean UI
- Extension version and build: 1.20261007.45957109 (12d0e44) for the first pass; 1.20261007.95826219 (9a70dfd) after the fixes

1. Follow [Slice 2: verify](live-check.md#slice-2-verify).
2. Record mean / max / pass and render + upload + compare seconds per scene.
   Record the large sync time separately. Leave unrun results blank.
3. The web report shows percentages. Use normalized values in Calibration
   (for example, 2% is 0.02).

| # | Check | How | Expected | Result |
| --- | --- | --- | --- | --- |
| 1 | Verify reveal text | Click **Verify against AE** (AE와 비교) after sending this scene. Record mean / max / pass and render + upload + compare seconds. | The report compares the same sampled frame numbers. It shows the mean, worst frame error, and pass or fail. || Pass — v2: mean 0.0016, max 0.0021 (f32); 10 s end to end. |
| 2 | Verify image background | Click **Verify against AE** (AE와 비교) after sending this scene. Record mean / max / pass and render + upload + compare seconds. | The report compares the same sampled frame numbers. It shows the mean, worst frame error, and pass or fail. || Pass — v1: mean 0.0006, max 0.0015 (f16); 15 s (render + upload 9 s, compare 6 s). |
| 3 | Verify spinning GLB | Click **Verify against AE** (AE와 비교) after sending this scene. Record mean / max / pass and render + upload + compare seconds. | The report compares the same sampled frame numbers. It shows the mean, worst frame error, and pass or fail. || Pass — v1: mean 0.0115, max 0.0188 (f56); 10 s. Same placement and shape; AE lights the model lighter than Keepframe's renderer (known renderer difference, see below). |
| 4 | Verify ig2demo | Click **Verify against AE** (AE와 비교) after sending `ig2demo`. Record mean / max / pass and render + upload + compare seconds. | The report compares the same sampled frame numbers. It shows the mean, worst frame error, and pass or fail. || Pass — v4: mean 0.0012, max 0.0091 (f79); 15 s (render + upload 9 s, compare 6 s). Re-run on the fixed build: same numbers, 18 s. |
| 5 | Worst-frame images | Open **Details** (자세히). Open each worst-frame image in a new tab. | Up to three worst frames show **After Effects**, **Keepframe**, and **Difference** (차이). All images load. || Pass — the card shows one result line; **Details** holds three rows of After Effects / Keepframe / 차이 images, all loaded (1920 px wide), no console errors. |
| 6 | Substituted-font notes and region numbers | Verify text with a requested font missing from AE. Under **Details** (자세히), record the replacement font, text-region ID, and worst region difference. | Notes name the substituted font. Masked text entries (마스킹한 텍스트) show each region's worst percentage. These regions are excluded from the pass score. || Pass with finding — ig2demo has 15 substituted-font text layers (Linux font guesses such as Liberation Sans, DejaVu Sans, WenQuanYi Zen Hei shown in Arial / Malgun Gothic); their regions differ by 2.5–27 %. After the fix they appear as one localized list (`e38 · text: Arial로 표시, 글자 영역 차이 27.3%`). |
| 7 | Large sync time | Have the controller prepare a changed version or fresh test comp for the ~120k-key `ae-live-long` scene. Time **Send to AE** (AE로 보내기) until sync finishes. | Target: under 2 minutes. Record actual seconds and outcome. Slice-1 live check 2 took over 10 minutes. || Not met — measured on a 20-element × 600-frame scene (41 layers, ~24k keys) instead of the 120k-key scene: 101 s create / 112 s unchanged resend before the fix, 79 s / 80 s after. The ~120k-key run was not repeated (it would take minutes). Normal scenes stay fast (ig2demo, 949 keys: about 2 s). Timing instrumentation comes next; optimization moves to the next slice (user decision). |
| 8 | Key interpolation through the Higgsfield bridge | Have the controller call `ae_get_keyframes` on animated properties with an eased key and a linear key. Record in/out interpolation, influence, and speed. | Eased sides report `BEZIER` with the intended ease values. A linear key reports `LINEAR` on both sides. || Partial — `ae_get_keyframes` returns only times and values (and only for unseparated Position, Scale, Rotation, Opacity): e36 Opacity's 11 keys and e12 Opacity's 3 keys hold the written times and values. Interpolation is not exposed; ig2demo's frame match (mean 0.0012) confirms eased motion indirectly. |
| 9 | `saveFrameToPng` in Korean AE 26.5 | Run Verify in the user's Korean AE 26.5. Have the controller watch the temporary files and inspect the PNGs during export. Compare the render queue and undo history before and after Verify. | Files appear and become complete PNGs. The job's temp folder is removed after upload. Verify adds no render-queue items or undo steps. || Pass — frames appear and upload as complete PNGs in Korean AE 26.5 (Verify succeeded four times). The temp folder, render queue and undo history were not inspected directly. |
| 10 | Windows temp-folder agreement | Have the controller compare AE's `Folder.temp.fsName` with the panel's `os.tmpdir()`. Inspect the returned frame paths. | The `keepframe-<job id>` folders resolve to the same location. A mismatch fails with `Invalid frame from AE`. || Pass — no `Invalid frame from AE`; AE's `Folder.temp` and the panel's temp folder agree on the user's Windows machine. |
| 11 | AE dialog during Verify | Open a dialog in Korean AE during Verify. Record its name, how long it stays open, progress, and outcome. Dismiss it and retry once AE responds if needed. | If the dialog blocks the host call for 10 minutes, it times out. The reason says AE may still be working on a large scene or waiting for a dialog. || Not run in this pass. (Live check 2: Composition Settings left open does not block CEP host calls in AE 26.5.) |
| 12 | Verify without connected AE | Click **Disconnect** (연결 해제), then **Yes** (예) in the web card. Wait for **Not connected** (연결 안 됨). Inspect Verify. | **Verify against AE** (AE와 비교) is disabled. The reason reads “AE is not connected — open the Keepframe panel in After Effects” (AE가 연결되지 않았습니다 — After Effects에서 Keepframe 패널을 여세요). || Not run live; covered by `tests/test_ae_card_browser.py`. |

### Findings

1. Large syncs are slow in real AE: reading each key back for hand-edit detection dominated (each layer's fingerprint was read twice per sync and ease was read for linear keys). Fixed in 9a70dfd (one pass, no ease reads on all-LINEAR keys, old fingerprints accepted once and re-stamped): 112 s → 80 s for ~24k keys. The rest of the time is not yet attributed (ExtendScript JSON parsing and hashing are suspects); kfSync will report per-stage timings, and optimization is the first task of the next slice.
2. **Details** printed every past job's full warning list (30 English internal strings each, about 2,400 px) and the verify report listed the substituted-font facts twice. Fixed in 9a70dfd: one line per past job, the latest job's warnings behind their own count, one localized font list (card height 4,428 px → 1,649 px with the report open).
3. After the first fixes the AE chip still showed `AE 26.5x89` and an empty `경고 0개` summary remained; both fixed in b331ab4 (see 6).
4. Known renderer difference: AE's default lighting renders the GLB material lighter than Keepframe's renderer (placement and shape match).
5. Final build 1.20261007.110312957 (b331ab4), per-stage timings for the 41-layer, ~24k-key scene: create 66.7 s = validate 60.6 s + hash 4.2 s + write 1.0 s + read 0.9 s; unchanged resend 64.6 s = validate 60.8 s + hash 2.9 s + read 0.8 s. Parsing and validating the 0.9 MB spec in ExtendScript (no built-in JSON) takes 91 % of the time; AE reads and writes take about 2 s. ig2demo (949 keys): 1.4 s in total. The next slice speeds up spec parsing.
6. Final build UI checks: the chip reads `AE 26.5 연결됨`; **Verify against AE** is disabled while a verify runs (reason `비교가 끝나면 다시 비교할 수 있습니다`) and two quick POSTs queue one render; no warnings disclosure when the latest job has none; **Details** starts with the verify outcome and its version, and shows the latest sync's timings.

### Calibration

| Scene | Mean | Max | Pass |
| --- | --- | --- | --- |
| ig2demo v4 | 0.0012 | 0.0091 | yes |
| reveal v2 | 0.0016 | 0.0021 | yes |
| plate v1 | 0.0006 | 0.0015 | yes |
| model v1 | 0.0115 | 0.0188 | yes |

Rule: 2× the worst value on scenes that look right, rounded up to 0.005. Worst mean 0.0115 → `VERIFY_MEAN_MAX = 0.025`; worst frame 0.0188 → `VERIFY_FRAME_MAX = 0.04` (set in 9a70dfd). Substituted-font regions are excluded from both numbers.

## Live check 4 (Stage A Task 14: gradients, footage, styled text)

To run with the user in Task 15. Install a fresh build (`scripts/build_zxp.sh`; source version 1.1.0, stamped
`1.<YYYYMMDD>.<ms>`, which installs over the older stamped builds). The panel sends `X-Keepframe-Spec-Level: 3`
(a level-2 build from before the final fix wave gets 426 for a speed-edited scene with video, whose footage carries
a time stretch).
A panel from before Task 14 sends none: legacy scenes still sync on it, but a scene with video, a 2-stop gradient
or styled text gets HTTP 426 (the job fails with “update the Keepframe extension: …” and the panel stops and
offers the download). Leave unrun results blank.

| # | Check | How | Expected | Result |
| --- | --- | --- | --- | --- |
| 1 | Linear gradient background | Send a scene whose background is a 2-stop linear gradient (e.g. 135°). Inspect the background layer and **Verify against AE** (AE와 비교). | A solid with one **Keepframe Gradient** (Gradient Ramp): Ramp Shape Linear, Start/End of Ramp on the CSS gradient line (135° on 1920×1080: (210, −210) → (1710, 1290)), start/end colours as the stops. No warning. Verify passes. | |
| 2 | Radial and animated gradients | Send a radial 2-stop gradient and an animated (busy-plate) gradient. Scrub. | Ramp Shape Radial, Start of Ramp at the centre, End one radius to the right. Animated: Start/End/colour keys at the gradient keys' frames, linear between them. Verify passes. | |
| 3 | Gradient with more than 2 stops | Send a 3-stop gradient background. | The poster image is the background; the job warns `gradient background has more than 2 stops; exported as its poster image`. | |
| 4 | Video plate | Send a scene with a video background. Scrub frames 0, middle and last; open Interpret Footage. Verify. | A footage layer `background.mp4` (H.264, BT.709) at the bottom, frame-exact with Keepframe, no colour shift. Verify passes. | |
| 5 | Video sprite | Send a scene with a video sprite. Inspect the layer bar, Interpret Footage, and edges over the background. Verify. | Footage `<id>.mov` (ProRes 4444): the layer starts (startTime and In) at the sprite's first visible frame and plays its clip from frame 0 there; alpha **Straight**, no dark fringe. Verify passes. | |
| 6 | Text tracking and stroke | Send styled text with tracking and a stroke. Open the Character panel. | Tracking = tracking_em × 1000. Stroke on, width 2w, **Fill Over Stroke**, the analysed stroke colour. The PostScript name of an installed uploaded font is the text's font (no substitution warning). | |
| 7 | Shadow and glow | Send styled text with a shadow and a glow. Inspect **Keepframe Shadow 1/2** (Drop Shadow). | Direction atan2(dx, −dy), Distance √(dx²+dy²), Softness = blur; the panel's Opacity reads the analysed opacity in % (the script writes 0–255 — check this mapping). A glow has Distance 0. Shadows reach past the text box. | |
| 8 | Gradient fill | Send text with a gradient fill. Inspect the effect stack. | **Keepframe Fill** (Gradient Ramp) and **Keepframe Fill Matte** (Set Matte: Take Matte From Layer = this layer, Use For Matte = Alpha Channel) are the first two effects; the glyphs show the gradient over the text box, nothing outside them. With a stroke, the job warns that the stroke takes the gradient. | |
| 9 | Shear | Send italic-like text (shear) and compare glyph slant and baseline with Keepframe. | **Keepframe Skew** leans the glyphs the same way as Keepframe; the first baseline stays in place (Position = Anchor + baseline shift). | |
| 10 | Fade | Send text with a fade. | The job warns `text fade not exported`; the text is drawn without the fade. | |
| 11 | Resend, stop-colour change, style removed | Resend unchanged; then change one gradient stop colour in Keepframe and send; then remove a text's style and send. | The resend is a no-op (all unchanged, no undo step). After the colour change only the background updates; the Keepframe Gradient effect stays in place with only End/Start Color changed. Removing the style turns the stroke off and the tracking to 0. | |
| 12 | Hand edits on the new effects | In AE change a Drop Shadow distance, a text's tracking, the Set Matte channel, or slide a footage layer. Send, then **Overwrite them** (덮어쓰기). | The send refuses with a hand-edit warning for that layer; overwrite restores the Keepframe values. Moving the text layer in the stack is not a hand edit. | |
| 13 | Old panel and video preparation | Before installing, send a gradient scene from the old panel; then install and send a scene with a long video plate. | Old panel: the update message with the download link; the web job says “update the Keepframe extension: …”. New panel: “Preparing video for …” (영상 준비 중) while the footage is made in the background, then the sync. | |
