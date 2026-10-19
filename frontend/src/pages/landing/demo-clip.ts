/** The landing hero's synthetic reference clip: a card that slides in, a title that types on, a badge that pops.
 * Pure frame math, so the measurement overlay reads the same numbers the clip is drawn from. */

export const FPS = 24
export const FRAMES = 48
export const STAGE_W = 960 // reference width the overlay reports pixels in

export const CARD = { from: -0.42, to: 0, start: 0, frames: 14 } // x as a share of the stage width
export const TITLE = { start: 10, stagger: 2 }
export const BADGE = { start: 30, frames: 8 }

const clamp01 = (v: number) => Math.min(1, Math.max(0, v))
export const easeOutCubic = (t: number) => 1 - (1 - clamp01(t)) ** 3

export interface ClipFrame {
  cardX: number // share of stage width
  letters: number // how many title letters show
  badge: number // 0..1 scale
}

export function frameAt(f: number, title: string): ClipFrame {
  const t = easeOutCubic((f - CARD.start) / CARD.frames)
  return {
    cardX: CARD.from + (CARD.to - CARD.from) * t,
    letters: Math.max(0, Math.min(title.length, Math.floor((f - TITLE.start) / TITLE.stagger) + 1)),
    badge: easeOutCubic((f - BADGE.start) / BADGE.frames),
  }
}

/** What the overlay reports: card travel in reference px, its ease length in frames and seconds. */
export const MEASURED = {
  travelPx: Math.round((CARD.to - CARD.from) * STAGE_W),
  easeFrames: CARD.frames,
  easeSeconds: +(CARD.frames / FPS).toFixed(2),
}
