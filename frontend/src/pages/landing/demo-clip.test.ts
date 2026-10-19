import { expect, it } from 'vitest'
import { CARD, FRAMES, MEASURED, frameAt } from './demo-clip'

it('slides the card in, types the title, pops the badge, and is settled by the last frame', () => {
  expect(frameAt(0, 'Launch')).toEqual({ cardX: CARD.from, letters: 0, badge: 0 })
  const mid = frameAt(7, 'Launch')
  expect(mid.cardX).toBeGreaterThan(CARD.from)
  expect(mid.cardX).toBeLessThan(CARD.to)
  expect(frameAt(10, 'Launch').letters).toBe(1)
  expect(frameAt(FRAMES - 1, 'Launch')).toEqual({ cardX: CARD.to, letters: 6, badge: 1 })
  expect(frameAt(FRAMES - 1, '출시').letters).toBe(2) // a changed text keeps the timing, not the length
  expect(MEASURED).toEqual({ travelPx: 403, easeFrames: 14, easeSeconds: 0.58 })
})
