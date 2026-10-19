---
version: beta
name: Keepframe Instrument
description: "A measuring instrument for motion graphics: the flat, hairline-ruled surfaces of a compositing app's comp viewer and timeline, near-black neutrals, small control radii, and one amber signal colour reserved for what Keepframe measured or what the user has selected. No glows, gradients or decorative tiles; the clip being reconstructed is the only colourful thing on screen."

colors:
  canvas: "#0b0b0c"
  surface-1: "#121214"
  surface-2: "#19191c"
  surface-3: "#222226"
  hairline: "#2c2c31"
  hairline-soft: "#1d1d21"
  ink: "#eeeef0"
  ink-muted: "#9c9ca5"
  signal: "#ffb020"      # --brand: measured values, selection, playhead, focus ring, the current step
  signal-soft: "#ffc761"
  on-signal: "#1a1200"
  success: "#3dd68c"
  warning: "#ff8a4c"
  danger: "#ff8f87"
  sample-violet: "#6a4cf5"   # landing demo clip content only
  sample-magenta: "#c94be4"
  sample-orange: "#f2662f"

typography:
  family-sans: "Inter Variable, Pretendard Variable"   # Korean falls through to Pretendard
  family-mono: "ui-monospace, SF Mono, Menlo"           # frame numbers, timecode, measured values only
  display: { size: "clamp(40px, 6.4vw, 72px)", weight: 600, lineHeight: 1.02, letterSpacing: "-0.03em" }
  display-md: { size: 32px, weight: 500, lineHeight: 1.13, letterSpacing: "-0.02em" }
  headline: { size: 22px, weight: 600, lineHeight: 1.2, letterSpacing: "-0.015em" }
  body-lg: { size: 18px, lineHeight: 1.4 }
  body: { size: 15px, lineHeight: 1.45 }
  body-sm: { size: 14px, lineHeight: 1.4 }
  caption: { size: 13px, lineHeight: 1.25 }
  micro: { size: 12px, lineHeight: 1.2 }

rounded:
  xs: 2px
  sm: 3px      # chips, badges, swatches, tab thumbs
  md: 4px      # buttons, inputs, selects, nav items
  lg: 6px      # panels, cards, viewers
  xl: 8px
  2xl: 10px
  full: 9999px # status dots and the switch only

spacing: { hair: 1px, xxs: 4px, xs: 8px, sm: 12px, md: 16px, lg: 24px, xl: 32px, section: 96px }
---

# Keepframe Instrument

## Idea

Keepframe measures a clip and hands back something editable. The interface should feel like the tools that clip came
from: a comp viewer with safe-area guides, a timeline with keyframes on a ruled track, panels separated by 1px rules.
It is an instrument, so its own surfaces stay neutral and quiet; colour belongs to the user's footage.

## Rules

- **One signal colour.** Amber (`--brand`) marks what was measured (overlay boxes, measured numbers), what is selected,
  the playhead, focus rings and the current workflow step. It is never a background wash or decoration.
- **Flat surfaces, hairline edges.** Panels are `surface-1` with a `hairline` border. No translucent white rings, no
  radial or linear gradient glows, no blurred colour blobs, no glass.
- **Small radii.** Controls 4px, panels 6px. Pills are gone: `rounded-full` is for status dots and the switch track.
- **Primary action** is a solid ink rectangle with dark text; secondary is `surface-2` with a hairline; ghost is text.
- **Type** is Inter for Latin and Pretendard for Korean. Display tracking stops at -0.03em; body is untracked. Mono is
  for frame numbers, timecode and measurements, not for decoration.
- **Sequences** are drawn as a track (a rule with keyframe diamonds), not as numbered cards.
- **Motion** explains state: the agent's thinking slot, a reply arriving, a panel folding. 150–300 ms, ease-out, and a
  `prefers-reduced-motion` path that keeps opacity changes and drops movement.

## Components

| Component | Shape |
| --- | --- |
| Button | `rounded-md`, h-9/h-10/h-11, tones primary (ink) / secondary (surface-2 + hairline) / ghost / danger |
| Input, select | `rounded-md`, `surface-1`, hairline border, amber focus border |
| Chip, badge | `rounded-sm`, tinted text on a 10–15% tint of the same colour |
| Panel, card | `rounded-lg`, `surface-1`, hairline border |
| Tabs (pill variant) | `rounded-md` track, `rounded-sm` thumb |
| Comp viewer | `rounded-lg`, near-black, dashed action-safe / title-safe guides |
| Track | 1px rule, 10px rotated-square keyframes; the first or current one filled amber |
| Status dot | `size-1.5`/`size-2`, `rounded-full`, `bg-current` |
