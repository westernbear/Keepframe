---
version: alpha
name: Framer-design-analysis
description: "A confident dark-canvas builder marketing site that treats the page like a working artboard — pure black surfaces, white display type set in GT Walsheim Medium with aggressive negative tracking, and a single confident blue (#0099ff) reserved for hyperlinks and selection states. The page rhythm is broken by oversized vibrant gradient atmosphere panels — magenta, violet, orange spotlights — that act as living showcase tiles, not decoration. Every CTA is a white pill on dark; every card is a translucent or charcoal surface; every section title pulls letter-spacing tight enough to feel like a poster."

colors:
  primary: "#ffffff"
  on-primary: "#000000"
  accent-blue: "#0099ff"
  ink: "#ffffff"
  ink-muted: "#999999"
  canvas: "#090909"
  surface-1: "#141414"
  surface-2: "#1c1c1c"
  hairline: "#262626"
  hairline-soft: "#1a1a1a"
  inverse-canvas: "#ffffff"
  inverse-ink: "#000000"
  gradient-magenta: "#d44df0"
  gradient-violet: "#6a4cf5"
  gradient-orange: "#ff7a3d"
  gradient-coral: "#ff5577"
  semantic-success: "#22c55e"

typography:
  display-xxl:
    fontFamily: GT Walsheim Framer Medium
    fontSize: 110px
    fontWeight: 500
    lineHeight: 0.85
    letterSpacing: -5.5px
  display-xl:
    fontFamily: GT Walsheim Medium
    fontSize: 85px
    fontWeight: 500
    lineHeight: 0.95
    letterSpacing: -4.25px
    fontFeature: ss02
  display-lg:
    fontFamily: GT Walsheim Medium
    fontSize: 62px
    fontWeight: 500
    lineHeight: 1.00
    letterSpacing: -3.1px
    fontFeature: ss02
  display-md:
    fontFamily: GT Walsheim Medium
    fontSize: 32px
    fontWeight: 500
    lineHeight: 1.13
    letterSpacing: -1.0px
  headline:
    fontFamily: Inter
    fontSize: 22px
    fontWeight: 700
    lineHeight: 1.20
    letterSpacing: -0.8px
    fontFeature: cv05
  subhead:
    fontFamily: Inter Variable
    fontSize: 24px
    fontWeight: 400
    lineHeight: 1.30
    letterSpacing: -0.01px
    fontFeature: cv11
  body-lg:
    fontFamily: Inter Variable
    fontSize: 18px
    fontWeight: 400
    lineHeight: 1.30
    letterSpacing: -0.18px
    fontFeature: cv11
  body:
    fontFamily: Inter Variable
    fontSize: 15px
    fontWeight: 400
    lineHeight: 1.30
    letterSpacing: -0.15px
    fontFeature: cv11
  body-sm:
    fontFamily: Inter Variable
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.40
    letterSpacing: -0.14px
    fontFeature: cv11
  caption:
    fontFamily: Inter Variable
    fontSize: 13px
    fontWeight: 500
    lineHeight: 1.20
    letterSpacing: -0.13px
    fontFeature: cv11
  micro:
    fontFamily: Inter Variable
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.20
    letterSpacing: -0.12px
    fontFeature: cv11
  button:
    fontFamily: Inter Variable
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.0
    letterSpacing: -0.14px
    fontFeature: cv11

rounded:
  xs: 4px
  sm: 6px
  md: 10px
  lg: 15px
  xl: 20px
  xxl: 30px
  pill: 100px
  full: 9999px

spacing:
  hair: 1px
  xxs: 4px
  xs: 8px
  sm: 12px
  md: 15px
  lg: 20px
  xl: 30px
  xxl: 40px
  section: 96px

components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.button}"
    rounded: "{rounded.pill}"
    padding: 10px 15px
  button-primary-pressed:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.button}"
    rounded: "{rounded.pill}"
  button-secondary:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.pill}"
    padding: 10px 15px
  button-translucent:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.xxl}"
    padding: 8px 14px
  button-icon-circular:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.full}"
    size: 40px
  pricing-tab-default:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.button}"
    rounded: "{rounded.pill}"
    padding: 8px 14px
  pricing-tab-selected:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.pill}"
    padding: 8px 14px
  text-input:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 10px 14px
  text-input-focused:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 10px 14px
  pricing-card:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.xl}"
    padding: 24px
  pricing-card-featured:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.xl}"
    padding: 24px
  template-card:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.lg}"
    padding: 12px
  gradient-spotlight-card:
    backgroundColor: "{colors.gradient-violet}"
    textColor: "{colors.ink}"
    typography: "{typography.subhead}"
    rounded: "{rounded.xl}"
    padding: 32px
  gradient-spotlight-card-magenta:
    backgroundColor: "{colors.gradient-magenta}"
    textColor: "{colors.ink}"
    typography: "{typography.subhead}"
    rounded: "{rounded.xl}"
    padding: 32px
  gradient-spotlight-card-orange:
    backgroundColor: "{colors.gradient-orange}"
    textColor: "{colors.ink}"
    typography: "{typography.subhead}"
    rounded: "{rounded.xl}"
    padding: 32px
  product-mockup-tile:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.xl}"
    padding: 16px
  feature-row:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.xs}"
  comparison-row:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.xs}"
  top-nav:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.xs}"
    height: 56px
  faq-row:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 24px
  footer:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.caption}"
    rounded: "{rounded.xs}"
    padding: 64px 32px
---

## Overview

Framer's marketing canvas is a near-pure black artboard. The dominant surface is `{colors.canvas}` (almost pure black with a faint warmth). Oversized white display type sits on top of it, set in **GT Walsheim Medium** with letter-spacing pulled to extreme negative values (-5.5px on the 110px display, -4.25px on the 85px hero). The page reads like a poster: one assertive statement per band, with generous space above and below.

The single accent is `{colors.accent-blue}`. It is used scarcely, mostly for hyperlinks, selection halos, and a subtle blue-tinted shadow ring on focused inputs. The brand chrome is monochrome: white pill buttons, charcoal cards, gray secondary text. Every few sections the page drops in a **vibrant gradient atmosphere card**: a magenta-violet spotlight, a sunset-orange wash, a coral-pink panel. These are not section backgrounds. They are individual cards in a card grid, each a small poster that shows what Framer can produce.

Body type is **Inter Variable**. Framer uses Inter's character variants (`cv01`, `cv05`, `cv09`, `cv11`, `ss03`, `ss07`, `dlig`), so the body has a single-storey "a", straight-leg "l", and tabular figures. The marketing site has no light mode. The brand is dark.

**Key Characteristics:**
- Black-canvas marketing system: `{colors.canvas}` is the surface for hero, body, pricing, FAQ, and footer. There are no light interludes.
- Massive negative letter-spacing on display sizes (-5.5px / -4.25px / -3.1px) creates a poster-grade headline cadence.
- White pill (`{components.button-primary}`) is the only primary CTA shape across the site. Secondary actions live as charcoal pills (`{components.button-secondary}`) or text links.
- Oversized **gradient spotlight cards** (violet, magenta, orange, coral) act as showcase tiles inside the dark grid. They are individual cards, not section backgrounds.
- Inter Variable with OpenType character variants (`cv01/05/09/11`, `ss03/ss07`, `dlig`) is used everywhere body type appears.
- Border radius scale runs from 4px utility chips up to 100px pills and full circles. Cards default to 15-20px. Atmospheric gradient cards use 30px.
- A single chromatic accent `{colors.accent-blue}` is reserved for hyperlinks, focus, and selection. It is never decorative.

## Colors

> Source pages: framer.com (home), /ai/, /startups/, /marketplace/templates/nudge/, /gallery/a16z-speedrun-×-tonik, /pricing.

### Brand & Accent
- **Pure White** ({colors.primary}): The brand primary surface. Every primary CTA pill, every display headline, every body line on canvas.
- **Sky Blue** ({colors.accent-blue}): The single chromatic accent. Hyperlinks, focused-input rings, and a few selection states. Never used for backgrounds or as a brand fill.

### Surface
- **Canvas** ({colors.canvas}): Default page background. Near-black with a faint warmth. Footer, pricing, hero, and FAQ all sit on it.
- **Surface 1** ({colors.surface-1}): One step above canvas. Used for pricing cards, secondary buttons, and mockup tiles.
- **Surface 2** ({colors.surface-2}): Two steps above. Used for the featured pricing card, hero pill backdrop, and selected pricing tab.
- **Hairline** ({colors.hairline}): 1px borders on input groups, comparison-table dividers.
- **Hairline Soft** ({colors.hairline-soft}): Subtler dividers between FAQ rows and footer column rules.
- **Inverse Canvas** ({colors.inverse-canvas}): Pure white. Used as the surface of light-on-dark pill CTAs and a small set of light-mode template thumbnails embedded in the showcase grid.

### Text
- **Ink** ({colors.ink}): All headline and emphasized body type. Pure white.
- **Ink Muted** ({colors.ink-muted}): Secondary type. Gray (#999999) used for meta info, footer columns, comparison-row labels, and deselected pricing tabs. Hierarchy on the dark canvas is carried by ink → ink-muted contrast, not by weight changes.

### Semantic
- **Success Green** ({colors.semantic-success}): Pricing comparison-table checkmarks. Glyph fill, not surface.

### Brand Gradient (signature)
- **Gradient Magenta** ({colors.gradient-magenta}): Spotlight card variant.
- **Gradient Violet** ({colors.gradient-violet}): Spotlight card variant. Most common.
- **Gradient Orange** ({colors.gradient-orange}): Spotlight card variant. Sunset wash.
- **Gradient Coral** ({colors.gradient-coral}): Spotlight card variant. Coral/pink.

These four sit as oversized atmospheric tiles inside otherwise monochrome card grids. A dark canvas with one or two glowing spotlight cards is a recurring page signature.

## Typography

### Font Family

- **GT Walsheim Framer Medium** / **GT Walsheim Medium**: Framer's display typeface. Geometric, slightly humanist. It holds at large sizes with extreme negative tracking. Fallbacks: `GT Walsheim Medium Placeholder` system font.
- **Inter Variable**: System body typeface. Used with extensive OpenType character variants: `cv01` (alternate "1"), `cv05` (alternate "g"), `cv09` (alternate "i" / "l"), `cv11` (alternate "0"), `ss03` / `ss07` stylistic sets, `dlig` discretionary ligatures, and `tnum` for numerics in tabular contexts. The variants give the body a custom cut without commissioning a new face.
- **Inter**: Used selectively for `{typography.headline}` (the 22px / 20px tier). The non-variable cut catches small tracking targets that the variable file rounds.

### Hierarchy

| Token | Size | Weight | Line Height | Letter Spacing | Use |
|---|---|---|---|---|---|
| `{typography.display-xxl}` | 110px | 500 | 0.85 | -5.5px | Largest hero headline (home, AI page) |
| `{typography.display-xl}` | 85px | 500 | 0.95 | -4.25px | Section opener headlines |
| `{typography.display-lg}` | 62px | 500 | 1.00 | -3.1px | Sub-section openers |
| `{typography.display-md}` | 32px | 500 | 1.13 | -1.0px | Card titles, smaller display |
| `{typography.headline}` | 22px | 700 | 1.20 | -0.8px | Pricing tier headlines, FAQ category titles |
| `{typography.subhead}` | 24px | 400 | 1.30 | -0.01px | Lead body next to display headlines |
| `{typography.body-lg}` | 18px | 400 | 1.30 | -0.18px | Hero subhead, lead paragraphs |
| `{typography.body}` | 15px | 400 | 1.30 | -0.15px | Default body, card descriptions |
| `{typography.body-sm}` | 14px | 500 | 1.40 | -0.14px | Pricing comparison rows, dense data |
| `{typography.caption}` | 13px | 500 | 1.20 | -0.13px | Eyebrows, footer columns, meta |
| `{typography.micro}` | 12px | 400 | 1.20 | -0.12px | Disclaimer, footnote |
| `{typography.button}` | 14px | 500 | 1.0 | -0.14px | Pill buttons |

### Principles

- **Letter-spacing scales with size, hard.** Display-xxl pulls -5.5px (5% of size); body sticks to about -1% (-0.15px on 15px). Posters at the top, comfortable reading at body.
- **OpenType character variants are the brand voice.** Switching off `cv11`, `ss03`, etc. visibly changes the body voice. The brand depends on them.
- **Weight stays in a narrow band.** Display sits at 500, body at 400, body-sm/caption at 500. Hierarchy is carried by size + tracking, not by 700/900 ramps.
- **Tight line-heights everywhere.** Even body runs at 1.30. Framer's editorial tone is denser than typical SaaS marketing.

### Note on Font Substitutes

If implementing without GT Walsheim Medium, suitable open-source substitutes include **Mona Sans**, **Geist**, or **Inter** at weight 600-700 with manually tightened tracking. Mona Sans's hairline weights at 100-300 sit close to Framer's cleaner section openers. Inter Variable is open-source. Keep it as-is and preserve the documented OpenType variants.

## Layout

### Spacing System

- **Base unit**: 5px. Framer uses 5/10/15/20/30 increments. It does not use the 4/8/16/24 scale.
- **Tokens (front matter)**: `{spacing.hair}` 1px · `{spacing.xxs}` 4px · `{spacing.xs}` 8px · `{spacing.sm}` 12px · `{spacing.md}` 15px · `{spacing.lg}` 20px · `{spacing.xl}` 30px · `{spacing.xxl}` 40px · `{spacing.section}` 96px.
- Card interior padding: `{spacing.lg}` 20px on pricing cards; `{spacing.xl}` 30px on gradient spotlight cards.
- Pill button padding: 10px vertical · 15px horizontal (`{components.button-primary}`).
- Section padding (vertical): roughly `{spacing.section}` 96px on home; tighter (~64px) on pricing comparison.

### Grid & Container

- Max content width sits around the 1199px breakpoint, with side gutters that scale toward `{spacing.xl}` on desktop.
- Card grids on the home gallery use 2-up at desktop, collapsing to 1-up below 810px.
- Pricing tier grid is 4-up across the documented breakpoints; comparison table beneath it uses fixed-width left column with horizontally scrolling tier columns at narrow widths.

### Whitespace Philosophy

The dark canvas is the whitespace. Lighter brands separate sections with white air. Framer uses long stretches of black with a single oversized statement in the middle. Sections separate by mode change: a band of charcoal cards, then a band of black with a gradient spotlight, then back to charcoal.

## Elevation & Depth

| Level | Treatment | Use |
|---|---|---|
| 0 (flat) | No shadow, no border | Default for canvas-mounted display type, FAQ rows, footer |
| 1 (charcoal) | `{colors.surface-1}` lift on canvas | Pricing cards, mockup tiles, secondary buttons |
| 2 (light-edge) | `rgba(255,255,255,0.10)` 0.5px top edge + `rgba(0,0,0,0.25)` 0px 10px 30px drop | Floating product cards, modal cards |
| 3 (selected) | `rgba(0,153,255,0.15)` 0px 0px 0px 1px ring | Focused inputs, selected option |

Four shadow signatures recur across the homepage: a 1px subtle drop, a translucent blue ring, a thick near-black 2px outline (used as the active-element marker on sub-nav), and the layered light-edge + drop-shadow used for floating cards.

### Decorative Depth

- **Gradient spotlight cards** are the dominant depth device. Color saturation against black canvas substitutes for shadow-driven elevation.
- **Layered product mockups** (browser frames containing live Framer-built sites) sit inside `{colors.surface-1}` cards with the level-2 light-edge treatment.
- **Subtle blue ring (focus / selected)** is the only chromatic depth signal. It marks the active state of input groups and pricing tier toggles without changing the underlying surface.

## Shapes

### Border Radius Scale

Framer's extracted radius set is unusually granular (1px, 4px, 5px, 6px, 8px, 10px, 12px, 15px, 20px, 30px, 40px, 100px). The named scale below picks the levels the marketing surface actually consumes.

| Token | Value | Use |
|---|---|---|
| `{rounded.xs}` | 4px | Small chip / utility radius |
| `{rounded.sm}` | 6px | Inline tag, badge |
| `{rounded.md}` | 10px | Form input, list item |
| `{rounded.lg}` | 15px | Template card thumbnails |
| `{rounded.xl}` | 20px | Pricing cards, mockup tiles |
| `{rounded.xxl}` | 30px | Gradient spotlight cards, oversized panels |
| `{rounded.pill}` | 100px | All primary text CTAs |
| `{rounded.full}` | 9999px | Circular icon buttons, avatar circles |

### Photography & Illustration Geometry

- Embedded site mockups (browser-chromed previews of Framer-built sites) sit in `{rounded.xl}` 20px tiles with `{spacing.md}` 15px interior padding.
- Gradient spotlight cards use `{rounded.xxl}` 30px corners, softer than the 20px content cards, so they read as atmospheric panels.
- Icon glyphs and sub-nav glyphs render in `{rounded.full}` circles at 32-40px sizes.

## Components

### Buttons

**`button-primary`**: White pill on dark canvas. The primary CTA across home, pricing, AI, and gallery pages.
- Background `{colors.primary}`, text `{colors.on-primary}`, type `{typography.button}`, padding 10px 15px, rounded `{rounded.pill}`.
- Pressed state lives in `button-primary-pressed`. The live site uses a transform-scale shrink, not a darkened fill.

**`button-secondary`**: Charcoal pill. Used for secondary navigation actions ("Sign in", "Talk to sales") and as the visual counterpart to the primary pill.
- Background `{colors.surface-1}`, text `{colors.ink}`, type `{typography.button}`, padding 10px 15px, rounded `{rounded.pill}`.

**`button-translucent`**: Translucent / lifted secondary used on top of busy backgrounds (gallery hero, gradient cards).
- Background `{colors.surface-2}`, text `{colors.ink}`, type `{typography.button}`, rounded `{rounded.xxl}`, padding 8px 14px.

**`button-icon-circular`**: 40px circle for inline icon actions (carousel arrows, social links).
- Background `{colors.surface-1}`, text `{colors.ink}`, rounded `{rounded.full}`, size 40px.

### Pricing Tabs

**`pricing-tab-default`** + **`pricing-tab-selected`**: The pill-toggle that switches between Basic / Pro / Business / Enterprise on `/pricing`.
- Default: `{colors.canvas}` background, `{colors.ink-muted}` text, rounded `{rounded.pill}`.
- Selected: `{colors.surface-2}` background, `{colors.ink}` text. Selected means lift, not color. Surface depth marks "active" without a chromatic fill.

### Inputs & Forms

**`text-input`** + **`text-input-focused`**: Form fields on `/pricing` (seat-count, currency switcher) and the in-product preview surfaces.
- Background `{colors.surface-1}`, text `{colors.ink}`, type `{typography.body}`, rounded `{rounded.md}`, padding 10px 14px.
- Focused state retains the same surface; the focus ring is the level-3 blue-tinted shadow `rgba(0,153,255,0.15)` 0 0 0 1px.

### Cards & Containers

**`pricing-card`**: Each tier on `/pricing`.
- Background `{colors.surface-1}`, text `{colors.ink}`, type `{typography.body}`, rounded `{rounded.xl}`, padding 24px.

**`pricing-card-featured`**: The Pro tier (visually emphasized).
- Background `{colors.surface-2}`, otherwise identical structure. The lift is one surface step up. There is no chromatic outline.

**`template-card`**: Thumbnail tile in the home "Built with Framer" gallery and `/marketplace`.
- Background `{colors.surface-1}`, text `{colors.ink}`, type `{typography.body-sm}`, rounded `{rounded.lg}`, padding 12px.

**`product-mockup-tile`**: Larger tile that frames a live product UI mock (Framer canvas, Workshop video, AI translate panel).
- Background `{colors.surface-1}`, text `{colors.ink}`, type `{typography.body-sm}`, rounded `{rounded.xl}`, padding 16px.

### Gradient Spotlight Cards (signature)

The main decorative surface of Framer's marketing is oversized atmospheric tiles dropped into otherwise monochrome card grids. Variants:

**`gradient-spotlight-card`**: violet ground (most common).
- Background `{colors.gradient-violet}`, text `{colors.ink}`, type `{typography.subhead}`, rounded `{rounded.xl}`, padding 32px. (The on-site card often pushes to `{rounded.xxl}` 30px when it spans a wider tile.)

**`gradient-spotlight-card-magenta`**: magenta-pink ground.
- Background `{colors.gradient-magenta}`, otherwise identical.

**`gradient-spotlight-card-orange`**: sunset-orange wash.
- Background `{colors.gradient-orange}`, otherwise identical.

(Coral pink follows the same shape with `{colors.gradient-coral}`.)

### Comparison & FAQ

**`feature-row`** + **`comparison-row`**: Single rows inside the pricing comparison table.
- `feature-row`: `{colors.canvas}` background, `{colors.ink}` text. Header rows.
- `comparison-row`: `{colors.canvas}` background, `{colors.ink-muted}` text. Data rows with `{typography.body-sm}` and 1px `{colors.hairline-soft}` underlines.

**`faq-row`**: Each accordion line in the pricing-page FAQ.
- Background `{colors.canvas}`, text `{colors.ink}`, type `{typography.body}`, rounded `{rounded.md}`, padding 24px.

### Navigation

**`top-nav`**: Sticky bar on `{colors.canvas}` with the Framer wordmark left, primary nav links centered, and a `button-secondary` ("Sign in") + `button-primary` ("Get started for free") pair right.
- Background `{colors.canvas}`, text `{colors.ink}`, type `{typography.body-sm}`, height 56px.
- Mobile: collapses primary links into a hamburger; the two pill CTAs collapse into a single primary pill on the bar.

### Footer

**`footer`**: Dense link grid on `{colors.canvas}` with the Framer wordmark left and 5-6 columns of caption-sized links.
- Background `{colors.canvas}`, text `{colors.ink-muted}`, type `{typography.caption}`, padding 64px 32px.

## Do's and Don'ts

### Do

- Reserve `{colors.primary}` (white) and `{colors.canvas}` (near-black) as the system's two anchor surfaces. Every band of the page chooses one or the other.
- Push display-size letter-spacing aggressively negative. `{typography.display-xxl}` at -5.5px is the brand signature, not a stylistic accident.
- Use `{colors.accent-blue}` only for hyperlinks, focus rings, and selected indicators. Never as a background or button fill.
- Drop one or two `gradient-spotlight-card` variants into a card grid; they are the brand's atmosphere device. Three or more in the same viewport reads as a moodboard, not a system.
- Compose every CTA as a pill (`{rounded.pill}`); secondary actions live as charcoal pills, never as bordered ghost buttons.
- Keep body type Inter Variable with character variants `cv01`, `cv05`, `cv09`, `cv11`, `ss03`, `ss07` enabled. The brand voice depends on them.
- Use surface lift (canvas → surface-1 → surface-2) to mark hierarchy on dark, not opacity changes on white type.

### Don't

- Don't ship a light-mode marketing page. Framer's identity is dark.
- Don't introduce mid-tone gray text outside `{colors.ink-muted}`. The hierarchy is binary: `ink` or `ink-muted`.
- Don't use `{colors.accent-blue}` as a brand fill (e.g., a blue CTA pill). The blue is a signal color, not a surface.
- Don't square off CTAs. Pill (`{rounded.pill}`) or full circle is the brand vocabulary.
- Don't reduce the negative letter-spacing on display sizes "for accessibility". The compression is intrinsic to the brand voice; reduce the SIZE if needed, but keep the percentage.
- Don't apply gradient backgrounds to whole sections. Gradients are CARDS, not section grounds.
- Don't combine more than one chromatic accent. The palette is monochrome plus one blue plus the gradient family. It is not "blue, green, and red".

## Responsive Behavior

### Breakpoints

| Name | Width | Key Changes |
|---|---|---|
| Desktop | 1199px | Default desktop layout |
| Tablet | 810px | Card grids collapse 4-up → 2-up; nav becomes hamburger |
| Mobile-Lg | 809px | Pricing comparison table becomes per-tier accordion |
| Mobile-XS | 98px | Smallest documented breakpoint: single-column everything |

### Touch Targets

- Pill buttons (`button-primary`, `button-secondary`) maintain a minimum 44px tap height across all viewports. Combine `{typography.button}` 14px line-height with the documented 10px vertical padding.
- Circular icon buttons (`button-icon-circular`) are 40px on desktop and grow to 44px on touch viewports.
- Pricing-tab pills hold ≥40px tap height. Below 810px they may collapse into a horizontal-scroll row. They do not stack.

### Collapsing Strategy

- **Nav**: horizontal nav with a centered link group + right-anchored pill pair collapses to a hamburger overlay below 810px. The `button-primary` stays visible on the bar.
- **Card grids**: the gallery and template-card grids go 2-up on desktop → 1-up on mobile. Gradient spotlight cards retain `{rounded.xxl}` corners at every viewport. They don't bleed.
- **Pricing comparison table**: collapses into per-tier accordions below 810px to avoid horizontal scroll.
- **Display type**: `{typography.display-xxl}` 110px scales down toward `{typography.display-lg}` 62px on tablet and `{typography.display-md}` 32px on mobile, preserving the percentage-negative letter-spacing.

### Image Behavior

- Embedded product mockups (browser frames containing live Framer-built sites) maintain their aspect ratio and never crop.
- Gradient spotlight cards keep their gradient orientations across breakpoints. The gradient direction is part of the brand spec.

## Iteration Guide

1. Focus on ONE component at a time and reference it by its `components:` token name (e.g., `{components.button-primary}`, `{components.gradient-spotlight-card}`).
2. When introducing a new section on the dark canvas, decide first which surface lift it lives on: `{colors.canvas}` for hero/FAQ, `{colors.surface-1}` for cards, `{colors.surface-2}` for featured cards. The depth choice is the most consequential decision.
3. Default body to `{typography.body}` with all the documented OpenType variants; reach for `{typography.subhead}` only inside spotlight cards.
4. Run `npx @google/design.md lint DESIGN.md` after edits. `broken-ref`, `contrast-ratio`, and `orphaned-tokens` warnings flag issues automatically.
5. Add new variants as separate component entries (`-pressed`, `-featured`, `-selected`). Do not bury them in prose.
6. Treat `{colors.accent-blue}` as a single-shot signal color: hyperlinks, focus, and selection. A second blue means the brand is drifting.
7. Gradient spotlight cards are scarce by design. One or two per long page is the spec; three is a moodboard.

## Known Gaps

- The exact gradient stops for the spotlight cards are derived from screenshot pixels. They are not taken from CSS variables. Production gradients are likely `linear-gradient` strings on individual elements, not design tokens. Treat the documented `{colors.gradient-*}` hex values as base anchors, not as exact gradient specs.
- Form-field validation / error styling is not visible on the inspected pages because no error states render in the static screenshots.
- Dark mode is the only mode. No light-mode adaptation is documented because the marketing site does not ship one.
- The marketplace template detail page returned sparser CSS variable data than the other pages. Surface tokens for that page were inferred from the matching home / gallery treatment. They were not extracted from the marketplace page.
