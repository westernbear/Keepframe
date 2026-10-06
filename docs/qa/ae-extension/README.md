# After Effects extension live-check results

Guide: [Install and live check](live-check.md).

- Date: 2026-10-06
- AE version: After Effects 2026 (26.5x89), Korean UI
- Windows version: Windows (win32 10.0.26300)
- Extension version: 1.0.238 (build 238-d01d1a5)
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
| 12 | Partial or interrupted sync | Have the controller cause a mid-write failure. Inspect the panel, web job, and partial layer. Resend, then confirm overwrite if offered. | Failure text is visible. An incomplete ownership tag leads to a hand-edit warning on normal retry. Forced retry reuses that layer. Undo is available for partial changes; sync is not transactional. | |
| 13 | Heartbeat during long sync | Run a sync longer than 90 seconds. Watch web progress and have the controller inspect device `last_seen` while AE is busy. | Heartbeats continue about every 15 seconds. The job remains running until completion and does not fail as disconnected. | |
| 14 | Network drop recovery | Drop the network during polling and during result posting. Restore it. Compare the panel status and web job. | The panel reports the failure and retries with backoff. A completed result is retried. After 60 seconds without contact, a running job can fail as `AE disconnected`; resend explicitly. A lost result acknowledgement can produce HTTP 409. | |
| 15 | Revoke pairing | In the web device row, click **Disconnect** (연결 해제), then **Yes** (예). Repeat during a long sync. | The next authenticated request returns 401. The panel stops and asks for a new code. Already executing native AE work cannot be canceled or undone by the panel. | |
| 16 | Version mismatch (426) | Have the controller simulate a protocol-major mismatch, including during a long host call. Open the panel's **Download extension** (확장 다운로드) link. | The panel stops, asks for an extension update, and offers `/ae/keepframe.zxp`. Already executing native AE work may still finish. | |
| 17 | Copy log without secrets | Click **Copy log** (로그 복사) in CEP. Paste into a text file after pairing, errors, and recovery. | Copy works. The log contains neither the pairing code nor the device token. | |

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

Known differences:

- Known renderer difference (AE 26.5): AE draws the dark back face of a single-sided triangle; Keepframe's Three.js renderer culls that face. This is a renderer difference, not a placement bug.
- AE uses a perspective comp camera for 3D model layers; Keepframe's composer uses an orthographic camera, so rotating models show slight foreshortening in AE.
