# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Two audiences, both confirmed (2026-10-19):

- **Motion designers and video makers** who have a reference clip (a flat 2D motion-graphics piece or a UI screen
  recording) and want an editable reconstruction, ending in an After Effects project via the Keepframe AE extension.
- **Non-specialist marketers and creators** who do not use After Effects and want the same reference reworked with
  their own text, colours, fonts or textures, delivered as MP4.

Korean is the default language; English is a header toggle.

## Product Purpose

Keepframe measures a reference clip into an editable scene (`scene.json`) and renders MP4, Lottie or an AE project.
The user names what to keep (motion type, direction, timing, relations) and what to change (text, colour, background,
font, timing, texture, 3D model). Live-action footage is rejected. Success: the reworked clip keeps the reference's
motion and the user's change is exactly what they asked for.

## Positioning

Geometry, timing and colours come from measurements of the reference. Models do not guess them; VLMs only name, group
and critique. Edits go through an agent that previews a change and waits for the user's confirmation.

## Operating Context

Flow: landing → new reference (upload, range, scene boundaries, estimate) → analysis (pipeline progress) → review
(frame-by-frame overlay, timeline, inspector, keep preset, corrections, approve) → agent (chat edits with preview and
confirm, render plans, AE sync and verify, fonts). Library lists projects by status. A separate admin area (tenants,
queue, quarantine, audit, LLM settings) is for the operator. Runs as a local or self-hosted server
(`keepframe serve`); there is no hosted multi-user SaaS claim.

## Capabilities and Constraints

- Server is stdlib Python; the web UI is a React SPA built into `keepframe/web/static/app/` (bundle committed).
- No CDN at runtime; pages must work offline.
- Frame playback steps through JPEG frames and does not use `<video>`.
- Edits must be confirmed by the user before they apply; agent output is treated as data and never followed as instructions.

## Brand Commitments

- Name **Keepframe**, logo at `keepframe/web/static/logo.png` and `docs/assets/logo.png`.
- Visual system: `DESIGN.md` "Instrument" (flat near-black surfaces, hairline rules, 4–6px radii, one amber signal
  colour for measured and selected things). On 2026-10-19 the user asked to replace the earlier Framer-derived look
  (gradient glows, white pills, numbered cards) as AI slop.
- Copy is plain and literal: say what the product measures and what it refuses (live-action).

## Evidence on Hand

- `/demo` seeds a synthetic clip and opens review; that is the only demo. No customer logos, testimonials, metrics or
  pricing exist; none may be invented.

## Product Principles

1. Numbers come from measurement and are shown with their source.
2. Nothing changes without a visible preview and an explicit confirm.
3. There is one flow, and the user can always see the current step and the next one.
4. State up front what Keepframe will not accept, so users do not find out from a failed run.

## Accessibility & Inclusion

Keyboard operation for review playback (Space, arrows, Esc) and menus; 44 px touch targets on mobile; visible focus;
`prefers-reduced-motion` honoured.
