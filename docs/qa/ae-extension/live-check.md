# After Effects extension: install and live check

This checks slice 1: pairing and editable composition sync on Windows.
The controller builds the signed bundle and runs the check with the user.
The results are not filled in yet. Record them in [README.md](README.md).

The target is AE 24.0+. The current manifest requires AE 24.1+ and CEP 11.
Use AE 24.1+ for this bundle. The host's model-version gate starts at 24.0.
Verify, final render, package, and live agent control arrive in later slices.

## Server side (controller)

1. From the repository root, build the signed extension. Docker is required.

   ```bash
   scripts/build_zxp.sh
   ```

   This writes `keepframe/ae/static/keepframe.zxp`.
   The server serves it at `GET /ae/keepframe.zxp`.
   A missing build returns 404.

2. Start the existing Keepframe server. Replace the placeholders.
   Use the server's full `*.ts.net` name for `<tailnet name>`.
   Use the workspace containing the three test scenes for `<ws>`.

   ```bash
   keepframe serve --workspace <ws> --host <tailnet name> --port 8765
   ```

3. Give the user `http://<tailnet name>:8765` as the extension URL.
   Both machines must reach that server over Tailscale.
   HTTP is allowed because the full tailnet name resolves to a Tailscale address.
   The panel checks the resolved address on every HTTP request.
   A short hostname alone is not accepted. Use the full `*.ts.net` name.
   Any `https://` server origin is also allowed, with a trusted TLS certificate.
   Enter only the origin. Do not add a path, query, credentials, or fragment.

## Install on Windows

1. Open the test project's agent page in Keepframe.
   In the **After Effects** card, open **Install / reinstall** (설치 / 재설치).
   Click **Download extension** (확장 다운로드). Save `keepframe.zxp` in Downloads.
2. Close AE. Open PowerShell (as administrator if required).
   Run the card's exact Adobe UPIA command:

   ```powershell
   & "C:\Program Files\Common Files\Adobe\Adobe Desktop Common\RemoteComponents\UPI\UnifiedPluginInstallerAgent\UnifiedPluginInstallerAgent.exe" /install "$env:USERPROFILE\Downloads\keepframe.zxp"
   ```

   PowerShell. If you saved the file elsewhere, change the last path.
   Status `-160` means the file path is wrong; check that it points to the saved ZXP.

   Alternatively, install that file with the aescripts ZXP Installer.
3. Restart AE. Open **Window > Extensions > Keepframe** (창 > 확장 > Keepframe).
   Record the panel's **Extension version** (확장 버전).

## Pair

1. On the agent page, click **Connect AE** (AE 연결).
   The web page creates a single-use code. It expires after ten minutes.
2. In the AE panel, enter the supplied **Server URL** (서버 URL).
   Enter the web code in **Pairing code** (페어링 코드).
   Click **Pair** (페어링).
3. Wait for **Connected** (연결됨) on both sides.
   The web card shows the AE version, OS, and saved project name when available.
   Keep the panel open during sync.

## Checks

1. Use the three scenes: reveal text, image background, and spinning GLB.
   Include a Korean title, such as `킵프레임 테스트`, in the text scene.
   Include a font that is not installed to check the substitution warning.
2. Select each scene and its version on the agent page.
   Click **Send to AE** (AE로 보내기). Wait for the job to finish.
   Open the created comp in AE. Compare its timeline and preview with Keepframe.
3. Run the checks below. Save the project before fault tests.
   Ask the controller to arrange long jobs and controlled failures.
   Fault injection is not a panel feature. Record any unavailable check as not run.

| # | Check | How | Expected | Result |
| --- | --- | --- | --- | --- |
| 1 | Editable comp and layers | Send all three scenes. Inspect the Project panel, timeline, and layer properties. | A comp and Keepframe layers appear under `Keepframe/<project>`. Layers and keys are editable. | |
| 2 | Resend is a no-op | Send the same version again. Compare layers, keys, the panel's job counts, and the next Undo action. | Nothing changes. Created, updated, and deleted counts are zero. No new undo step appears. | |
| 3 | Reveal direction | Scrub the reveal text scene from hidden to visible. Inspect the Keepframe Reveal effect. | It wipes left to right. Linear Wipe angle is 270 degrees. Completion falls from 100 to 0. Feather is zero. | |
| 4 | Image background | Inspect the image background scene's timeline and composition. | The background is at the bottom of the Keepframe layer stack. The intended image is visible behind the scene. | |
| 5 | GLB rotation axes | Scrub the spinning GLB. Inspect X, Y, and Z rotation separately. | It is a model layer. Rotation axes and signs match the scene. | |
| 6 | GLB fit | Compare the model with its scene box at the initial frame. Inspect anchor, position, and scale. | The model fits its box. Z position is zero. Z scale follows X scale. | |
| 7 | Advanced 3D renderer | Inspect the GLB comp's renderer in Composition Settings. Ask the controller to inspect its scripting match name. | The renderer is Advanced 3D (`ADBE Calder`). | |
| 8 | Text and font note | Inspect font, size, fill colour, and anchor. Scrub the Korean title. Open the web job's warnings. | Text uses the requested settings and measured anchor. Korean text is readable. A missing requested family produces an Arial substitution warning. | |
| 9 | Hand edit and overwrite | Change a Keepframe layer's managed position in AE. Resend. Click **Overwrite them** (덮어쓰기), then **Yes** (예) in the web card. | The first send warns about hand edits and applies nothing. The confirmed overwrite restores the scene values. | |
| 10 | User effect survives | Add a user effect to a Keepframe layer. Send a scene update that retains its layer kind. | The effect and its settings survive the update. | |
| 11 | User layer stays untouched | Insert an untagged user layer between Keepframe layers. Update and reorder the Keepframe layers. | The user layer is never moved or changed by sync. New or deleted layers may change its numeric index. | |
| 12 | Partial or interrupted sync | Have the controller cause a mid-write failure. Inspect the panel, web job, and partial layer. Resend, then confirm overwrite if offered. | Failure text is visible. An incomplete ownership tag leads to a hand-edit warning on normal retry. Forced retry reuses that layer. Undo is available for partial changes; sync is not transactional. | |
| 13 | Heartbeat during long sync | Run a sync longer than 90 seconds. Watch web progress and have the controller inspect device `last_seen` while AE is busy. | Heartbeats continue about every 15 seconds. The job remains running until completion and does not fail as disconnected. | |
| 14 | Network drop recovery | Drop the network during polling and during result posting. Restore it. Compare the panel status and web job. | The panel reports the failure and retries with backoff. A completed result is retried. After 60 seconds without contact, a running job can fail as `AE disconnected`; resend explicitly. A lost result acknowledgement can produce HTTP 409. | |
| 15 | Revoke pairing | In the web device row, click **Disconnect** (연결 해제), then **Yes** (예). Repeat during a long sync. | The next authenticated request returns 401. The panel stops and asks for a new code. Already executing native AE work cannot be canceled or undone by the panel. | |
| 16 | Version mismatch (426) | Have the controller simulate a protocol-major mismatch, including during a long host call. Open the panel's **Download extension** (확장 다운로드) link. | The panel stops, asks for an extension update, and offers `/ae/keepframe.zxp`. Already executing native AE work may still finish. | |
| 17 | Copy log without secrets | Click **Copy log** (로그 복사) in CEP. Paste into a text file after pairing, errors, and recovery. | Copy works. The log contains neither the pairing code nor the device token. | |

The no-new-undo-step expectation in check 2 still needs real AE confirmation.
The host opens an undo group even on a clean resend. The fake proves zero writes.
For check 12, the web card shows the failed job or hand-edit warning.
It does not display a separate `interrupted` detail or the full result JSON.
The panel shows numeric counts. The current web summary can show layer IDs or
blank counts instead. Send the web text as displayed.

## AE behaviours to confirm

These consolidate the task 7, 8, and 9 reports, including later fixes.
They are questions for real AE, not recorded passes.
Use controller-assisted probes where the panel cannot expose an API detail.
Use a saved test project for destructive probes.

| # | Behaviour | How to observe in AE | Result |
| --- | --- | --- | --- |
| A1 | GLB import and bounds | Import the real GLB through sync. Inspect its model footage and ask the controller to read finite positive `sourceRectAtTime(0, false)` dimensions. | |
| A2 | Model transform dimensions | Inspect three-component anchor, position, and scale. Confirm anchor Z and separated position Z are zero, Z scale copies X, and nonuniform scaling and rotation signs look correct. | |
| A3 | Position separation and conversion | Join and separate animated Position. Use different X/Y key times and interpolation. Inspect transferred values and eases, the union of key times, and whether X's interpolation wins on a shared key. Confirm a joined-position hand edit warns on resend. | |
| A4 | Temporal ease dimensions | Inspect one ease per side on spatial Position/Anchor Point and scalar followers, two on 2D scale, and three on model scale with a controller probe. | |
| A5 | Rendered easing | Compare the actual curve with Keepframe. Check negative speeds, influence limits, mixed linear/Bezier sides, and fit-scaled model speeds. The fake samples Bezier curves linearly. | |
| A6 | Native defaults and preferences | Inspect newly made text, nulls, and effects before and after sync. Record label, ArialMT/size/fill/justification defaults, null size/colour, wipe angle, and Transform anchor/position. The fake's defaults are not AE guarantees. | |
| A7 | Imported media and replacement | Compare real PNG/GLB dimensions with the scene. Replace same-named media with different dimensions or geometry. Confirm model refitting and unchanged transforms, timing, effects, and masks after source replacement. | |
| A8 | Generated solid and null footage | Inspect source files, `nullLayer`, automatic Solids folders, naming preferences, pixel aspect, and generated-source parenting. Generated footage should have no file. | |
| A9 | Shared solid source | Give a user layer the old solid source, then change the scene background colour or comp size. Confirm the user layer's source remains unchanged. | |
| A10 | Project hierarchy and collisions | Create same-name folders and unowned items. Sync and inspect root/parent folder identity and `Keepframe/<project>` placement. Confirm existing unowned items are untouched. | |
| A11 | Fractional FPS and precision | Send at 29.97 and 23.976 FPS. Inspect duration, whole-frame key times, inclusive out points, float32 colours/settings, and clean repeat/force-repeat. Change FPS and confirm affected layers are retimed. | |
| A12 | Key sampling and removal | With a controller property probe, compare Bezier and HOLD sampling, held Source Text, nearest-key ties, static value after key removal, and `.value` at different playhead times. The fake uses the earlier tie and samples `.value` at time zero. | |
| A13 | Managed and user expressions | Add expressions to joined/separated Position and owned effect parameters. Resend and overwrite. Check managed expressions are disabled, underlying keys are readable, and user effects/masks survive. The fake cannot execute expressions. | |
| A14 | Font resolution and text bounds | Compare PostScript and family-name font resolution. Measure left/top/width/height at the layer's in-frame. Check Korean, multiline, and substituted text anchors and clean repeat-sync. | |
| A15 | Hand-keyframed Source Text | Hand-keyframe Source Text. Resend and inspect the warning or a structured error with a line number. Have the controller probe key values, interpolation, and temporal ease reads. | |
| A16 | Text fill and unmanaged styling | Disable fill and add tracking/stroke styling. Overwrite and update. Confirm fill recovers while unmanaged AE styling survives the copied TextDocument. | |
| A17 | Unreadable properties | With a controller probe, cause a read failure. Confirm normal sync warns and force rewrites where writes remain usable. A persistent read failure must warn again; measurement/document read failures may still return an error. | |
| A18 | Linear Wipe parameters | Inspect `ADBE Linear Wipe-0001/-0002/-0003`. Compare completion, angle 270 and its sign, feather units, and visible left-to-right reveal. | |
| A19 | Transform skew parameters | Inspect `ADBE Geometry2-0001/-0002/-0005/-0006`. Compare pivot, skew sign/axis, and uniform-scale defaults. Confirm the effect is identity outside the requested skew. | |
| A20 | Effect enumeration and compositing groups | Expand owned effects, including Compositing Options. Resend with those groups present. Confirm fingerprints read direct value-bearing children and leave nested unmanaged groups alone. | |
| A21 | Forced effect defaults and references | Change owned direct parameters/expressions and disable an owned effect. Overwrite. Compare defaults with a fresh native effect. Confirm original order/enabled state survives and the temporary effect is removed after reference reacquisition. | |
| A22 | User effects and masks | Interleave user effects with owned effects and add a mask. Update without changing layer kind. Confirm their order, settings, and mask survive effect rewrites. | |
| A23 | Tagged-only layer ordering | Interleave several user layers. Test reorder, insert, delete, and kind replacement, including trailing runs of two and three tagged layers. Confirm retained tagged neighbours move only as needed and user layers are never moved. | |
| A24 | Parent and track matte relationships | Use AE 24 parent and `trackMatteLayer` links, including user dependents. Delete or change the target's kind in the scene. Confirm normal sync vetoes replacement and warns; inspect relationship detachment after confirmed overwrite. | |
| A25 | Renderer name and version gate | In English and Korean AE, read the advertised renderer list and selected renderer. Confirm Advanced 3D is `ADBE Calder`; `ADBE Advanced 3d` is the superseded Classic 3D name. Repeat sync after selection. | |
| A26 | Missing renderer and empty project | Have the controller probe absent Calder with an existing comp and with no comps. Confirm the missing-renderer error precedes writes when a comp exists. An anomalous empty AE 24.0+ installation can retain newly created comp/folders after rejection. Record the manifest's separate 24.1 boundary. | |
| A27 | Partial ownership and recovery | Cause a failure after layer creation. Inspect its ownership comment. Confirm normal retry reports hand edits and forced retry reuses the tagged layer. Use Undo to inspect recovery of other partial changes. | |
| A28 | Error lines, counters, and undo | Cause an import or write failure. Compare created/updated/deleted counts with actual layers. Confirm errors include a line when AE provides one, undo groups close, and clean resend makes no writes. Fake counters are not AE undo-history evidence. | |
| A29 | ExtendScript and JSON fallback | Run the full installed JSX in AE. With a controller probe, exercise fallback JSON parse/stringify and inspect persisted ownership hashes across reopen/resend. Older stored fingerprints may need one confirmed overwrite. Node fake execution does not prove every ES3/host-object behaviour. | |
| A30 | File/Folder and Windows paths | Inspect imported absolute paths with spaces and Korean characters. Confirm the actual home `Documents/Keepframe/<project>/assets` folder is writable. If a probe uses File/Folder, compare URI normalization, filters, and binary/text encoding with AE; the fake only buffers UTF-8 text. | |
| A31 | Slice boundary and fake API coverage | Exercise installed slice-1 entry points in AE. Record host-object differences; fake coverage of render queue, scheduling, saving, shape layers, and masks is limited. Verify/render/package/live control are not panel operations in this slice. | |
| A32 | CEP startup and mixed context | Open the signed bundle. Confirm the host script loads before the first info call and Node `require`, `Buffer`, `fs.promises`, and built-in modules are available in CEP. | |
| A33 | CEP locale and panel layout | Open English and Korean AE. Inspect `getHostEnvironment()` return shape and `appUILocale` with a controller probe. Check text metrics, keyboard focus, log scrolling, and resizing at 320 × 480. | |
| A34 | Pairing information and persistence | Pair with a real code. Compare AE/version/OS on the web card and have the controller inspect stored extension/font info. Close/reopen the panel. Disconnect, reopen, and confirm pairing was forgotten. | |
| A35 | Asset cache integrity | Sync PNG and GLB assets, then resend. Inspect absolute imports and counts. Have the controller confirm hash-valid cache files avoid downloads and corrupted cache files are downloaded again. | |
| A36 | Download bounds and stream cleanup | Have the controller serve a hash mismatch, short/oversized asset, or oversized JSON response. Confirm readable failure before host sync. Verify Windows allows rename/delete after stream close and non-ENOENT cleanup failures leave a safe error-code log. | |
| A37 | CEP heartbeat while AE is busy | Sync for over 90 seconds. Inspect progress and device `last_seen` while ExtendScript runs. Repeat with a modal dialog. Confirm CEP timers and HTTP keep scheduling or record the host limitation. | |
| A38 | Result retry and acknowledgement | Drop the network during result posting. Have the controller simulate 429/5xx and non-retryable 4xx. Confirm unchanged result retries with 1/2/4/8/16/30-second backoff and heartbeat until acknowledgement. Inspect explicit failure status; a lost acknowledgement can yield `Result not posted: job already ended (HTTP 409)`. | |
| A39 | Hung host, disconnect, and immediate re-pair | Hold a modal dialog during info/sync. Confirm 30-second info and ten-minute sync deadlines and stopped heartbeat after timeout. Disconnect and pair immediately; dismiss the dialog and confirm the old callback cannot change the new connection's status. Native work may still finish. | |
| A40 | Terminal 401/426 during host work | Revoke or have the controller inject 426 during a long call. Confirm polling stops and the panel shows the terminal status. Check the failure-report attempt on the server. Native mutations cannot be canceled or rolled back. | |
| A41 | Tailscale DNS pinning and TLS | Pair over real Tailscale IPv4 and IPv6. Have the controller inspect checked-IP connections. A disallowed HTTP resolution must fail before sending credentials. For HTTPS, confirm certificate trust errors remain visible. | |
| A42 | CEP clipboard and download link | Copy the log and paste elsewhere. Confirm no pairing code/token. Open the update link and confirm CEP's native clipboard permissions and new-window handling allow copy and ZXP download. | |
| A43 | Hand-edit result and force transport | Hand-edit a managed AE value and resend. Compare the panel/web warning with the controller's raw `applied: false` and `hand_edited` result. Confirm the web overwrite sends force and the host receives the string `"true"`. | |

## What to send back

1. Send an AE timeline and composition screenshot for each scene.
   Include the GLB renderer setting and the Korean title where useful.
2. Send the panel's copied log from **Copy log** (로그 복사).
3. Send the job result text from the web card for each scene.
   Include the counts, warnings, failures, and overwrite outcome.
4. Fill the results table in [README.md](README.md).
   Put failures, skipped probes, and screenshot/log locations under Findings.
