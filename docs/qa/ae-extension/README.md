# After Effects extension live-check results

Guide: [Install and live check](live-check.md).

- Date:
- AE version:
- Windows version:
- Extension version:

## Results

Record each scene as pass, fail, or not run. Leave unrun results blank.

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

## Findings

- Known renderer difference (AE 26.5): AE draws the dark back face of a single-sided triangle; Keepframe's Three.js renderer culls that face. This is a renderer difference, not a placement bug.
