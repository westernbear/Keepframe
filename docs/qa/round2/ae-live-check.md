# Round 2: live After Effects check

The controller prepares these projects on the home server, then the user runs
them on their Windows AE machine. This kit does not connect to an AE host.
Use the same branch/package on both machines so the installed panel includes
the reveal and model-layer changes.

## 1. Prepare the three projects on the server

From this checkout, with the controller's served workspace:

```bash
.venv/bin/python scripts/ae_live_check.py \
  --clip /home/singlerr/ref_stdio/eval/clips/ig2.mp4 \
  --workspace /path/to/served/workspace
```

Omit `--workspace` to create a new temp directory; use the printed directory
when starting the server. Chromium must be installed for the native references
(`.venv/bin/python -m playwright install chromium` if needed).

The JSON output and `ae-live-check-<run>.json` manifest give each project ID,
review URL path, AE export URL path, comparison frames, FPS, and native PNG
paths relative to that project's directory. Each run adds three fresh projects
and leaves existing projects intact. Preparation can take several minutes.

| Check | Scene and frames (zero-based) |
| --- | --- |
| Reveal | One `kind="text"` element, texture-backed to keep the glyphs identical; `reveal` goes 0→1 over frames 0–30, then holds. 640×360, 30 FPS. Compare 0, 7, 15, 22, 30, 60. |
| Plate | The first 120 frames of ig2 are analysed with this branch's plate-background pipeline, without optional OCR, GPU refinement, or ECC. The script requires an image background. Compare 0, 29, 59, 89, 119 at the clip's FPS (ig2: 60). Use `--ig2-frames N` for a different range. |
| Model | One `kind="3d"` element using the test suite's triangle GLB, with `ry` 0→360 degrees over frames 0–60. 640×360, 30 FPS. Compare 0, 10, 20, 30, 40, 50, 60. A frame-zero static texture is also available for the substitution proposal. |

The model fixture is a triangle, so back-facing/edge-on views can disappear in
the native renderer. Use the front-facing samples and the actual Rotate Y
keyframes to assess the full turn. The printed native PNGs are the reference;
do not treat the static fallback as a model that rotates.

The full-resolution ig2 preparation took about 22 minutes on this server. Its
current analysis also produces foreground `easing` compatibility issues for
`e1` and `e12` (AE temporal influence must be at least 0.1%). Review the existing
compatibility proposals before approving that AE plan; the kit preserves the
analysed tracks and the plate background. The controller may need its configured
LLM provider for these proposals.

## 2. Make the server accessible

The recommended browser tunnel is:

```powershell
ssh -L 8765:127.0.0.1:8765 <home-server>
```

Keep that SSH window open and open `http://127.0.0.1:8765` in the Windows
browser. Log in with the controller-provided administrator credentials.

AE pairing also needs the connector relay, separate from the browser listener.
For a tunnel-only check, the controller can run both listeners on the server:

```bash
export KEEPFRAME_AE_RELAY_URL=http://127.0.0.1:8766
export KEEPFRAME_AE_RELAY_HOST=127.0.0.1
export KEEPFRAME_AE_RELAY_PORT=8766
# KEEPFRAME_AE_RELAY_TOKEN and the administrator credentials must already be
# supplied privately in the server environment by the controller.
.venv/bin/keepframe serve --workspace /path/to/served/workspace \
  --host 127.0.0.1 --port 8765
```

In that setup, replace the single tunnel command with:

```powershell
ssh -L 8765:127.0.0.1:8765 -L 8766:127.0.0.1:8766 <home-server>
```

If the server already has a configured HTTPS AE relay, use the relay URL shown
by **Pair** instead. For same-LAN browser access the controller may use
`keepframe serve --workspace /path/to/served/workspace --host 0.0.0.0 --port 8765`
and the user opens `http://<server-lan-address>:8765` with admin login. A
non-loopback connector relay needs HTTPS; alternatively keep the relay on the
SSH tunnel. The browser port alone does not provide AE pairing.

## 3. Install and pair on Windows

1. Install this branch's Keepframe package into the Windows Python environment.
   From its checkout, install the package and AE extra:

   ```powershell
   pip install 'keepframe[ae]'
   # For an unpublished branch, install this checkout instead:
   pip install -e '.[ae]'
   keepframe ae-install
   ```

   If multiple AE installations are detected, use
   `keepframe ae-install --ae-path 'C:\Program Files\Adobe\Adobe After Effects 2025'`
   with the actual installation directory. Ensure `ffmpeg` and `ffprobe` are
   available on PATH for the connector's render checks.

2. Restart AE. In **Edit > Preferences > Scripting & Expressions**, enable
   **Allow Scripts to Write Files and Access Network**. Open
   **Window > Keepframe Panel** and keep the panel open. Start with an open,
   disposable AE project for these checks.

3. In the browser, open the first printed review path, inspect the scene, and
   approve it to enter the agent/export page. The printed AE export path is
   `/agent?project=<project-id>&scene=s1&v=v1`. Select the After Effects backend
   and preview mode, then click **Pair**.

4. The controller supplies the relay deployment token privately. Set it in
   the connector's PowerShell environment without putting it in command history:

   ```powershell
   $relaySecret = Read-Host 'Relay deployment token' -AsSecureString
   $env:KEEPFRAME_AE_RELAY_TOKEN = [Net.NetworkCredential]::new('', $relaySecret).Password
   Remove-Variable relaySecret
   keepframe ae-connect --url http://127.0.0.1:8766
   ```

   Use the **Pair** relay URL if it differs. Enter that project's pairing code
   at the hidden prompt. Leave the connector running. Wait for the page to show
   the AE version, current capabilities, and ready/connected status.

5. Create the preview plan, review its compatibility/substitution details,
   approve the plan, and execute it. Use **Pause** to stop at the baseline
   checkpoint before inspecting the composition. Inspect the baseline composition
   and
   keyframes in AE, and compare the printed frames against the native PNGs.
   Pair each of the other projects separately; stop the previous connector
   with Ctrl+C before pairing the next project. Pairing is project-specific.

## 4. Compare the results

Frame numbers are scene-local and zero-based. Set AE's time display to frames,
or use time = frame / FPS. For synthetic checks the review page's Original
pane contains the native rendered references. For ig2, Original is the source
clip; compare AE to the saved native PNGs/current reconstruction as well.

To copy a project's exact native references to Windows, use the printed
workspace/project ID and the same SSH host (replace the placeholders):

```powershell
scp -r '<home-server>:<workspace>/<project-id>/scenes/s1/live-check' '.\ae-native-<project-id>'
```

The manifest's `native_frames` entries identify the corresponding PNG for
each scene frame. For ig2 these filenames use render-output order, so follow
that mapping instead of interpreting the filename suffix as the scene frame.

**Reveal:** Open the text footage layer's Linear Wipe effect. Completion should
go from 100% at frame 0 to 0% at frame 30, with angle **270 degrees** and feather
**0**. Confirm the reveal edge moves in the same direction as the native render
at frames 7, 15, and 22. The fully revealed frame alone cannot establish the
direction. Glyph position, layer opacity, and scale should stay fixed. Record
the direction explicitly; this is the live check for the wipe-angle sign.

**Plate:** Confirm the image plate is the bottom layer beneath the foreground,
fills the composition, and remains visible through frames 0–119. Compare the
background gradient/details to the native PNGs; a flat replacement or a plate
covering the foreground is a failure. Record the plate's layer index/name and
take a screenshot of the layer stack and composition together.

**Model:** On AE **≥ 24.1**, confirm a real model layer is present, the GLB is
imported, and the composition uses Advanced 3D. Rotate Y should be keyed from
**0 to 360 degrees**; frames 10/20/30/40/50 correspond to 60/120/180/240/300
degrees. Compare the visible samples and the return to frame-zero orientation
at frame 60. Capture the renderer setting, layer type, and rotation keys. The
fixture has no detailed materials, so this checks import and spin rather than
complex shading fidelity.

On older AE, or if model-layer capability is absent, expect a **static-texture
substitution prompt** that names the lost 3D/spin semantics. Record the prompt
before acknowledging it. If you choose the substitution, footage is expected,
and kept spin predicates must say **not verifiable: substituted**. They must
not silently pass. This kit keeps its spin predicates enabled, so that fallback
does not produce a passing checkpoint/final export. Do not disable those keeps
when collecting this evidence. A static view proves only the fallback path.

## 5. Report back to the controller

Copy this template into the controller's round-2 QA README/evidence record and
attach screenshots (including any prompt/error). The controller records the
live result and returns failures as fix tasks.

```text
Date / Windows version / AE full version / Keepframe branch or revision:
Check / project ID / scene / version / plan ID:
Compared frames and FPS:
Keyframe check: PASS / FAIL (values and times observed)
Frame comparison: PASS / FAIL (mismatch and first affected frame)
Reveal direction: native edge direction / AE edge direction / angle observed
Plate: bottom-layer index/name / composition coverage
Model: native layer + Advanced 3D / static substitution prompt + lost semantics
Keep verification: result and exact failure or "not verifiable" text
Screenshot filenames and optional AEP/preview artifact links:
Connector/panel error text, if any:
```

Use exported AEP/MP4/ZIP links if the session reaches final export; a failed
checkpoint's screenshots and verification details are sufficient to report a
problem. Keep credentials, pairing codes, and deployment tokens out of the
evidence. Until the user supplies these results, live AE rendering and the
270-degree wipe direction remain unconfirmed.
