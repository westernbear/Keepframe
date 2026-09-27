# Higgsfield AI + Adobe After Effects: public reference for Keepframe

## Scope and confidence

This report uses Higgsfield's public product/help pages, its public Adobe installer manifest/package, and Adobe's CEP/After Effects scripting documentation. A statement marked **[Inference]** is an architectural deduction from those sources, not a disclosed Higgsfield implementation. Marketing claims such as “editable” are reported as claims; they are not treated as proof of a particular file format, command protocol, or recovery guarantee.

## Executive read

Higgsfield exposes two related surfaces:

1. A native Adobe panel for cloud AI generation/finishing. It places generated media into the active timeline and exposes Generate Video/Image, Reframe, Remove Background, Draw to Edit, Upscale, and Edit Video. ([Plugin page](https://higgsfield.ai/plugins/after-effects), [integration help](https://higgsfield.ai/creator-hub/help-center/integrations/external-integrations-higgsfield), [announcement](https://higgsfield.ai/blog/higgsfield-after-effects))
2. An agent-driven After Effects workflow. Higgsfield's MCP/AI Motion Designer connects an external agent to the installed plugin and claims direct edits to the open composition: layers, keyframes, effects, expressions, and scripts. ([AI Motion Designer](https://higgsfield.ai/ai-motion-designer), [After Effects announcement](https://higgsfield.ai/blog/higgsfield-after-effects), [AE MCP workflow](https://higgsfield.ai/mcp/use-after-effects?tab=claude))

This is a useful UX reference for Keepframe's “agent works in a live AE project” goal, but it is **not** evidence of Higgsfield providing Keepframe's required deterministic IR import, project-bound pairing, durable command journal, checkpoint verifier, immutable artifact slots, or final-plan approval. The public pages describe capabilities and prompts, not those contracts. ([MCP setup](https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent), [AE MCP workflow](https://higgsfield.ai/mcp/use-after-effects?tab=claude), [shot-composer workflow](https://higgsfield.ai/mcp/shot-composer?tab=claude))

## 1. Installation and runtime topology

### Verified

- The current plugin page says the macOS installer is a `.dmg`: drag `Higgsfield.app` to Applications, launch the installer, and it detects After Effects automatically without a helper app. Its Windows instructions now describe a native `.msi` installer that detects Adobe applications automatically. ([Current installation/FAQ](https://higgsfield.ai/plugins/after-effects))
- Higgsfield publishes a versioned Adobe update manifest. The manifest currently identifies release `1.0.57`, a macOS `.pkg` URL and Windows `.msi` URL, a `.zxp` URL, and SHA-256 values for each downloadable artifact. ([Installer manifest](https://hf-adobe-updates.higgsfield.ai/adobe/manifest.json))
- The public ZXP archive is named `ai.higgsfield.cep.zxp` and exposes `CSXS/`, `js/`, `jsx/`, `bridgehost/`, `node_modules/`, and `supercomputer-bridge.cjs`. ([Public ZXP archive](https://hf-adobe-updates.higgsfield.ai/adobe/ai.higgsfield.cep.zxp?v=1.0.57))
- The panel is opened from **Window → Extensions → Higgsfield** in After Effects. ([Plugin page](https://higgsfield.ai/plugins/after-effects), [integration help](https://higgsfield.ai/creator-hub/help-center/integrations/external-integrations-higgsfield))
- The older first-party help/blog instructions still describe Windows ZXP installation through ZXP Installer and say After Effects 2024 / 24.0 or newer. ([Older installation/help](https://higgsfield.ai/creator-hub/help-center/integrations/external-integrations-higgsfield), [July 2026 announcement](https://higgsfield.ai/blog/higgsfield-after-effects))

### [Inference]

The `CSXS` + `jsx` + `js` layout and the `cep` artifact names are consistent with a CEP HTML extension whose browser-like panel communicates with an After Effects ExtendScript host. Adobe's CEP guide documents the same separation: a required `CSXS/manifest.xml`, client HTML/JavaScript, host `.jsx`, and `CSInterface.evalScript()` for client-to-host calls. ([Adobe CEP guide](https://github.com/Adobe-CEP/Getting-Started-guides), [Adobe CEP resources](https://github.com/Adobe-CEP/CEP-Resources))

That does **not** prove that Higgsfield uses a particular local daemon, port, socket, WebSocket, polling loop, or exact `bridgehost` behavior. The public Higgsfield pages do not document those details. ([Plugin page](https://higgsfield.ai/plugins/after-effects), [MCP setup](https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent), [public ZXP archive](https://hf-adobe-updates.higgsfield.ai/adobe/ai.higgsfield.cep.zxp?v=1.0.57)) Keepframe should therefore copy the *seam* (panel ↔ host-side adapter), not reverse-engineer or depend on Higgsfield's package internals.

## 2. AE-side UI and user workflow

- The panel is a single in-app surface covering both After Effects and Premiere Pro. Higgsfield describes five core tools plus generation tools; generated clips, B-roll, transitions, and visuals are intended to land directly in the Adobe timeline. ([Plugin page](https://higgsfield.ai/plugins/after-effects), [announcement](https://higgsfield.ai/blog/higgsfield-after-effects))
- AI Motion Designer is started from ChatGPT with the Higgsfield plugin and `/use-after-effects`; the same workflow is advertised through Higgsfield MCP/other agents. ([AI Motion Designer](https://higgsfield.ai/ai-motion-designer), [motion-designer announcement](https://higgsfield.ai/blog/ai-motion-designer-after-effects-gpt))
- Higgsfield's workflow pages ask for a brief and visual assets, then describe a path from prompt to an editable composition, layer inspection, motion preview, and saved project. ([AE MCP workflow](https://higgsfield.ai/mcp/use-after-effects?tab=claude), [shot-composer workflow](https://higgsfield.ai/mcp/shot-composer?tab=claude))
- Several official examples explicitly ask to preserve editable layers, text, colors, keyframes, controls, or project structure. ([AI Motion Designer](https://higgsfield.ai/ai-motion-designer), [illustration workflow](https://higgsfield.ai/mcp/illustration-animation?tab=claude), [shot cleanup workflow](https://higgsfield.ai/mcp/shot-cleanup?tab=claude))

**Keepframe implication:** the panel should feel like a native AE tool and expose the current composition/capability state, but Keepframe's render card must remain the approval authority. Higgsfield's one-click/direct-edit UX is not a reason to let the model execute an unapproved render or bypass the coordinator. ([Plugin page](https://higgsfield.ai/plugins/after-effects), [announcement](https://higgsfield.ai/blog/higgsfield-after-effects))

## 3. Authentication and pairing

### Verified

- The regular Higgsfield MCP connects to an existing Higgsfield account, uses existing plan credits, and does not require an API key. Claude setup is a custom connector URL followed by a Higgsfield authorization window; ChatGPT uses the official plugin; Cursor uses its marketplace integration. ([MCP overview](https://higgsfield.ai/creator-hub/help-center/integrations/what-is-higgsfield-mcp), [connection help](https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent))
- The Adobe plugin FAQ says users sign in to the existing Higgsfield account from the panel and use the same credits. ([Plugin FAQ](https://higgsfield.ai/plugins/after-effects))
- The After Effects bridge is presented as a separate MCP URL, `https://bridge.higgsfield.ai/mcp`, and the public page says to install the Adobe plugin before adding that bridge in Claude. Supercomputer is said to detect the installed plugin automatically. ([Plugin page](https://higgsfield.ai/plugins/after-effects), [announcement](https://higgsfield.ai/blog/higgsfield-after-effects))
- Higgsfield's public help describes expired MCP authorization as a disconnect/reconnect problem and recommends reconnecting the connector. ([Connection troubleshooting](https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent))

### [Public-doc gap / inference]

The reviewed sources do not specify a project-bound pairing code, device token, one-active-device rule, controller/browser session, capability binding, or local token storage. ([MCP overview](https://higgsfield.ai/creator-hub/help-center/integrations/what-is-higgsfield-mcp), [connection help](https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent), [announcement](https://higgsfield.ai/blog/higgsfield-after-effects)) The account authorization flow should therefore **not** be copied as Keepframe's security model. Keepframe still needs the approved design's project-scoped controller cookie, short-lived high-entropy pairing code, hashed device token, replacement/unpair semantics, and separate connector-only relay.

## 4. Asset and project transfer

### Verified

- Higgsfield says generated video/image results are dropped directly into the Adobe timeline; the plugin can also operate on the active clip for Reframe, Remove Background, Draw to Edit, Upscale, and Edit Video. ([Plugin page](https://higgsfield.ai/plugins/after-effects), [announcement](https://higgsfield.ai/blog/higgsfield-after-effects))
- Higgsfield says web-platform generations can be brought into the project through the same panel, and the agent can import Higgsfield generations directly into the composition. ([Announcement](https://higgsfield.ai/blog/higgsfield-after-effects))
- The agent workflow asks for source assets/project files, says to preserve existing work, and says the workflow requires the desktop software to be connected. ([shot-composer workflow](https://higgsfield.ai/mcp/shot-composer?tab=claude), [shot-cleanup workflow](https://higgsfield.ai/mcp/shot-cleanup?tab=claude))
- Higgsfield's public examples say users can direct the agent at their own text, visuals, colors, footage, and references rather than a generic template. ([AI Motion Designer](https://higgsfield.ai/ai-motion-designer), [motion-designer announcement](https://higgsfield.ai/blog/ai-motion-designer-after-effects-gpt))

### [Public-doc gap]

The public material does not publish an asset manifest, content-addressed IDs, hash/length verification, upload/download protocol, media relink policy, AEP packaging layout, project versioning, or checkpoint retention rules. ([Plugin page](https://higgsfield.ai/plugins/after-effects), [announcement](https://higgsfield.ai/blog/higgsfield-after-effects), [shot-composer workflow](https://higgsfield.ai/mcp/shot-composer?tab=claude)) “Drops into the timeline” should be treated as a user-visible result, not a transferable asset contract. Keepframe must keep server-issued immutable asset IDs and plan-scoped copies, then make the connector import only those IDs.

## 5. Editability and command boundary

- Higgsfield claims the agent edits native AE content: layers, keyframes, effects, expressions, scripts, text, colors, timing, and animation. It explicitly says created elements remain normal editable After Effects elements. ([Announcement](https://higgsfield.ai/blog/higgsfield-after-effects), [motion-designer announcement](https://higgsfield.ai/blog/ai-motion-designer-after-effects-gpt))
- Adobe's After Effects object model exposes projects, compositions, footage, layers, masks, effects, properties, and render-queue items to scripting; animatable properties can be set/keyframed, and expressions can be set where supported. ([Adobe After Effects scripting overview](https://ae-scripting.docsforadobe.dev/introduction/overview/), [Adobe Property object](https://ae-scripting.docsforadobe.dev/property/property/))
- Adobe's CEP guide shows the conventional panel-to-host boundary: client JavaScript calls `evalScript()`, which invokes host-side ExtendScript. ([Adobe CEP guide](https://github.com/Adobe-CEP/Getting-Started-guides))

**[Inference]** Higgsfield's advertised “direct edits” likely cross an equivalent panel/host boundary, and the package's JSX/bridgehost names are consistent with that. No public Higgsfield source or schema proves whether commands are scripts, structured actions, or a mixture.

**Keepframe recommendation:** use Higgsfield's native-editability goal, but use Keepframe's closed, typed operation union and stable `layer_instance_id`/`source_element_id` targeting. Do not accept arbitrary ExtendScript, generated code, reflective property paths, or model-selected MCP method names. Adobe scripting is powerful enough to mutate files and network state, while Higgsfield's public natural-language interface does not disclose replay/idempotency or lock enforcement. ([Adobe scripting overview](https://ae-scripting.docsforadobe.dev/introduction/overview/), [Higgsfield AI Motion Designer](https://higgsfield.ai/ai-motion-designer))

## 6. Command transport and runtime topology

### Verified

- Higgsfield publishes two conceptually different MCP endpoints: the ordinary cloud generation connector (`https://mcp.higgsfield.ai/mcp`) and the Adobe bridge (`https://bridge.higgsfield.ai/mcp`). ([MCP overview](https://higgsfield.ai/creator-hub/help-center/integrations/what-is-higgsfield-mcp), [plugin page](https://higgsfield.ai/plugins/after-effects))
- The bridge is described as connecting an agent to the installed After Effects plugin so the agent can work inside compositions/layers; the workflow pages require the desktop Adobe software to be connected. ([Announcement](https://higgsfield.ai/blog/higgsfield-after-effects), [shot-composer workflow](https://higgsfield.ai/mcp/shot-composer?tab=claude))
- The public package includes `bridgehost`, `jsx`, and `supercomputer-bridge.cjs`. ([Public ZXP archive](https://hf-adobe-updates.higgsfield.ai/adobe/ai.higgsfield.cep.zxp?v=1.0.57))

### Not disclosed

The cited sources do not specify command names, JSON schemas, sequence/nonce semantics, heartbeats, leases, result caching, retry behavior, transport (HTTP streaming, WebSocket, polling, or local IPC), or what happens when AE closes mid-command. ([Announcement](https://higgsfield.ai/blog/higgsfield-after-effects), [MCP setup](https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent), [public ZXP archive](https://hf-adobe-updates.higgsfield.ai/adobe/ai.higgsfield.cep.zxp?v=1.0.57)) Those are exactly the failure/replay boundaries Keepframe must define itself.

**What is reusable:** a distinct agent-facing connector plus a local host adapter, with the agent operating on a live composition.

**What is not reusable:** Higgsfield's public bridge URL or account token as a Keepframe relay; an opaque/unversioned command surface; or an assumption that a browser can reach AE directly. Keepframe's connector-only listener, outbound HTTPS long-poll, local stdio MCP process, atomic file bridge, sequence/lease checks, and replay journal are deliberate additions rather than Higgsfield facts.

## 7. Generation, editing, and render loop

- Higgsfield says AI inference runs on its servers and the processed result is sent back into the Adobe timeline. The panel can regenerate from the same workspace without browser switching. ([Plugin FAQ](https://higgsfield.ai/plugins/after-effects), [announcement](https://higgsfield.ai/blog/higgsfield-after-effects))
- The agent workflow is described as prompt → composition edits → inspection/preview → saved editable project; VFX and cleanup examples also ask for a preview and a saved project. ([AE MCP workflow](https://higgsfield.ai/mcp/use-after-effects?tab=claude), [shot-composer workflow](https://higgsfield.ai/mcp/shot-composer?tab=claude), [shot-cleanup workflow](https://higgsfield.ai/mcp/shot-cleanup?tab=claude))
- Adobe's documented RenderQueue API can start, pause, stop, and observe rendering, including status/error callbacks. ([Adobe RenderQueue object](https://ae-scripting.docsforadobe.dev/renderqueue/renderqueue/))

The Higgsfield pages do not describe a deterministic baseline from an external IR, a checkpoint-0 artifact, frame sampling contract, predicate verification, final-plan approval, or immutable AEP/MP4 upload slot. ([AE MCP workflow](https://higgsfield.ai/mcp/use-after-effects?tab=claude), [shot-composer workflow](https://higgsfield.ai/mcp/shot-composer?tab=claude), [shot-cleanup workflow](https://higgsfield.ai/mcp/shot-cleanup?tab=claude)) Treat its “preview” and “editable project” language as product-level workflow claims, not as evidence of Keepframe's state machine.

**Keepframe recommendation:** retain the approved design's inspect → typed batch → save → render preview → verify → checkpoint loop. Use AE's Render Queue for local render control, then package through the planned fixed ffmpeg/AEP path; do not infer that Higgsfield's cloud generation loop solves final render correctness.

## 8. Error and offline behavior

- Higgsfield says an active internet connection is required while generating because inference is server-side, and the user must be signed in. ([Plugin FAQ](https://higgsfield.ai/plugins/after-effects), [announcement](https://higgsfield.ai/blog/higgsfield-after-effects))
- Its troubleshooting guidance says to fully quit Adobe before reinstalling, rerun the installer, run the Windows installer as administrator when needed, and check macOS security prompts. ([Plugin FAQ](https://higgsfield.ai/plugins/after-effects), [announcement](https://higgsfield.ai/blog/higgsfield-after-effects))
- Higgsfield's MCP help recommends reconnecting when authorization expires and retrying transient MCP HTTP exchange errors. ([Connection troubleshooting](https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent))

The public docs do not define offline editing, an offline queue, pause/resume state, safe in-flight cancellation, checkpoint rollback, or native fallback. ([Plugin FAQ](https://higgsfield.ai/plugins/after-effects), [connection troubleshooting](https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent), [AE MCP workflow](https://higgsfield.ai/mcp/use-after-effects?tab=claude)) Keepframe should preserve the last passing checkpoint and show an AE-specific paused reason for disconnect/timeout/render/upload failure; it must never silently switch to native.

## 9. Supported AE/platform versions

The current plugin page says After Effects **2025 / 25.0 and newer**, Windows 10/11 64-bit, and a macOS universal installer for Apple Silicon and Intel. ([Current plugin FAQ](https://higgsfield.ai/plugins/after-effects)) The integration help and July 2026 announcement instead say After Effects **2024 / 24.0 and newer** and describe Windows ZXP installation. ([Integration help](https://higgsfield.ai/creator-hub/help-center/integrations/external-integrations-higgsfield), [July 2026 announcement](https://higgsfield.ai/blog/higgsfield-after-effects))

This is a material source conflict, not a harmless wording difference. The current plugin page and versioned manifest are the best operational references for the present installer, but Keepframe should not hard-code either claim: the connector capability snapshot must report the actual AE version, OS/architecture, panel version, ffmpeg availability, fonts/effects, and schema digest. ([Current plugin page](https://higgsfield.ai/plugins/after-effects), [installer manifest](https://hf-adobe-updates.higgsfield.ai/adobe/manifest.json)) Higgsfield's public compatibility claims do not validate Keepframe's approved first-platform assumption of Windows + AE 2022+.

## 10. What Keepframe can and cannot copy

| Higgsfield pattern | Keepframe decision |
|---|---|
| Native panel discoverable from AE's Window menu | **Copy the UX seam.** Ship a small first-party panel and show connector/capability/command state in it. ([Plugin page](https://higgsfield.ai/plugins/after-effects), [Adobe CEP guide](https://github.com/Adobe-CEP/Getting-Started-guides)) |
| One installer, auto-detection, direct in-timeline results | **Copy selectively.** Keep `ae-install`, but retain explicit AE selection, Windows-only scope, and the required manual preference step. Do not make installation silently alter Adobe preferences. ([Plugin page](https://higgsfield.ai/plugins/after-effects)) |
| Existing-account sign-in and public MCP bridge | **Do not copy the trust model.** Keepframe needs project-bound pairing, controller cookie, device token, capability digest, revocation, and a connector-only relay. ([Connection help](https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent)) |
| Agent edits native text/layers/keyframes/effects | **Copy the product goal.** Implement a server-validated operation union over a deterministic IR→AE baseline; preserve editability and lock source IDs. ([Announcement](https://higgsfield.ai/blog/higgsfield-after-effects), [AE MCP workflow](https://higgsfield.ai/mcp/use-after-effects?tab=claude)) |
| Natural-language/direct script-like agent control | **Do not copy the command surface.** Never relay raw code or arbitrary property lookup; require closed schemas, bounds, sequence, expected checkpoint, and replay-safe results. ([Adobe scripting overview](https://ae-scripting.docsforadobe.dev/introduction/overview/)) |
| Cloud generation → asset inserted into timeline | **Copy as an optional asset operation only.** Keepframe's immutable server-issued assets must be imported by ID/hash; generated media cannot replace deterministic scene/project transfer. ([Announcement](https://higgsfield.ai/blog/higgsfield-after-effects)) |
| Prompt → inspect/preview → saved project | **Copy the loop shape, not the guarantees.** Add deterministic verification, checkpoint artifacts, user stop/continue, and final approval because Higgsfield does not publish those contracts. ([AE MCP workflow](https://higgsfield.ai/mcp/use-after-effects?tab=claude), [shot-cleanup workflow](https://higgsfield.ai/mcp/shot-cleanup?tab=claude)) |

## 11. Concrete recommendations mapped to approved Keepframe Tasks 3–9

- **Task 3 — coordinator:** implement the durable serialized state machine and command journal exactly as approved. Add capability/heartbeat and direct-application results, but do not model the session as one synchronous Higgsfield-style request. Persist command ID, nonce/sequence, expected state/checkpoint, payload/result digests, lease, and replay result before delivery.
- **Task 4 — security/pairing:** do not reuse Higgsfield account auth or expose `bridge.higgsfield.ai/mcp`. Keep the separate public relay and private browser listener, project-bound 128-bit pairing code, hashed controller/device secrets, same-origin checks, one active device, safe replacement, and explicit unpair.
- **Task 5 — connector/panel:** copy the panel↔local-host seam suggested by the CEP package, but implement the approved Windows connector + stdio MCP child + nonce-bearing atomic file bridge. Keep the panel data-only and fixed-function; do not allow the LLM to choose MCP tool names, paths, URLs, or raw AE code. Use the current Higgsfield installer drift as a reason to report exact capability/version, not to add macOS/Premiere scope.
- **Task 6 — baseline/mapping:** make native AE editability a hard requirement. Map the IR to text/footage/null/precomp/layer segments, transforms/opacity/keyframes/easing, and stable source/instance IDs. Any unsupported semantics must go through the approved explicit substitution acknowledgement; Higgsfield's “editable” claim is not a substitute for lock/predicate enforcement.
- **Task 7 — vision loop:** copy the user-facing inspect/refine/preview rhythm, but add Keepframe's checkpoint 0, multimodal frame sampling, typed batches, no-progress pause, exact keep verification, and last-passing reopen. Do not import a numeric iteration cap or assume cloud regeneration is equivalent to a verified AE candidate.
- **Task 8 — finalization:** use the AE Render Queue controls documented by Adobe for preview/final rendering, then fixed ffmpeg and immutable artifact slots. Package `project.aep`, collected media, and `dependencies.json`; Higgsfield does not document a portable package or dependency manifest to reuse. ([Adobe RenderQueue object](https://ae-scripting.docsforadobe.dev/renderqueue/renderqueue/), [shot-composer workflow](https://higgsfield.ai/mcp/shot-composer?tab=claude))
- **Task 9 — render card:** present the direct-edit convenience as a backend status, not as the approval gate. Keep the explicit native/After Effects selector, preview/final mode, connector/capability status, substitution acknowledgement, revision, checkpoints, stop/continue/manual sync, checkpoint selection, finalization, artifact links, and exact paused reason. AE failure must remain AE-specific and never auto-submit native.

## Source quality concerns

1. Higgsfield's pages are mutable marketing/help surfaces. They conflict on minimum AE version (2024 vs 2025) and Windows installation (ZXP vs current MSI), so versioned manifests and runtime capability checks should win over copied prose. ([Plugin page](https://higgsfield.ai/plugins/after-effects), [integration help](https://higgsfield.ai/creator-hub/help-center/integrations/external-integrations-higgsfield), [installer manifest](https://hf-adobe-updates.higgsfield.ai/adobe/manifest.json))
2. The public manifest provides artifact URLs, version, and hashes, but not release notes, protocol compatibility, signing/certificate details, or an API schema. ([Installer manifest](https://hf-adobe-updates.higgsfield.ai/adobe/manifest.json))
3. The bridge endpoint is authentication-gated and the public docs expose its URL, not its transport or command contract. The package directory names support only a cautious CEP/host-bridge inference. ([Bridge endpoint](https://bridge.higgsfield.ai/mcp), [public ZXP archive](https://hf-adobe-updates.higgsfield.ai/adobe/ai.higgsfield.cep.zxp?v=1.0.57), [Adobe CEP guide](https://github.com/Adobe-CEP/Getting-Started-guides))
4. The MCP/skill pages are workflow prompts and product claims; “editable project,” “inspect,” and “preview” do not specify serialized artifacts, validation, replay, or failure recovery. ([AE MCP workflow](https://higgsfield.ai/mcp/use-after-effects?tab=claude), [shot-composer workflow](https://higgsfield.ai/mcp/shot-composer?tab=claude))
5. Adobe CEP and scripting documentation establish what the host platform can do, not what Higgsfield actually implements. Keepframe should use those docs for platform seams and its own strict contracts for security, determinism, and recovery. ([Adobe CEP resources](https://github.com/Adobe-CEP/CEP-Resources), [Adobe After Effects scripting overview](https://ae-scripting.docsforadobe.dev/introduction/overview/))
