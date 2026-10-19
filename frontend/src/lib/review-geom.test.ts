import { describe, expect, it } from 'vitest'
import {
  clampFrame, dragBox, frameAtX, idleOverlayStatusKey, imageRect, isBoxBigEnough, overlayStatusKey, placeLabel, playheadPct,
  regionPath, rulerLabels, scenePoint, timecode, trackBar, trackIntervals, trackPlayheadLeft,
} from './review-geom'

describe('review geometry', () => {
  it('maps frames to timeline positions and back', () => {
    expect(playheadPct(0, 10)).toBe(5)
    expect(playheadPct(9, 10)).toBe(95)
    expect(playheadPct(0, 0)).toBe(50)
    expect(trackPlayheadLeft(50, 680, 280)).toBe(480)
    expect(trackPlayheadLeft(50, 100, 280)).toBe(280)
    expect(frameAtX(150, { left: 100, width: 200 }, 10)).toBe(2)
    expect(frameAtX(299.9, { left: 100, width: 200 }, 10)).toBe(9)
    expect(clampFrame(4.6, 10)).toBe(5)
    expect(clampFrame(-3, 10)).toBe(0)
    expect(clampFrame(42, 10)).toBe(9)
    expect(trackBar([0, 4], 10)).toEqual({ left: '0%', right: '50%' })
    expect(rulerLabels(101).map((l) => [l.x, l.anchor, l.text])).toEqual([
      ['1%', 'start', '0 f'], ['20.6%', 'middle', '20 f'], ['40.2%', 'middle', '40 f'],
      ['59.800000000000004%', 'middle', '60 f'], ['79.4%', 'middle', '80 f'], ['99%', 'end', '100 f'],
    ])
  })

  it('tracks use analysis intervals, else the visible span without analysis', () => {
    const item = { id: 'e1', visible: [2, 8] }
    expect(trackIntervals(item, null)).toEqual([[2, 8]])
    expect(trackIntervals(item, { objects: [{ id: 'e1', intervals: [[0, 1], [5, 6]] }] })).toEqual([[0, 1], [5, 6]])
    expect(trackIntervals(item, { objects: [] })).toEqual([])
  })

  it('formats the timecode as mm:ss:ff', () => {
    expect(timecode(0, 30)).toBe('00:00:00')
    expect(timecode(95, 30)).toBe('00:03:05')
    expect(timecode(30 * 61 + 7, 30)).toBe('01:01:07')
  })

  it('letterboxes the frame and maps pointers into clamped scene pixels', () => {
    const r = imageRect({ left: 10, top: 20, width: 400, height: 400 }, [200, 100])
    expect(r).toEqual({ left: 10, top: 120, width: 400, height: 200 })
    expect(scenePoint(210, 220, r, [200, 100])).toEqual([100, 50])
    expect(scenePoint(-50, 999, r, [200, 100])).toEqual([0, 100])
    expect(dragBox({ x0: 30, y0: 5, x1: 10, y1: 25 })).toEqual([10, 5, 30, 25])
    expect(isBoxBigEnough([10, 5, 12, 7])).toBe(true)
    expect(isBoxBigEnough([10, 5, 11, 30])).toBe(false)
  })

  it('draws region rings and keeps overlay captions apart', () => {
    expect(regionPath([{ points: [[0, 0], [4, 0], [4, 4]] }, { points: [[1, 1], [2, 1], [2, 2]] }]))
      .toBe('M0,0 L4,0 L4,4 Z M1,1 L2,1 L2,2 Z')
    const placed = []
    const a = placeLabel('e1 · text', [100, 100], 640, 640, placed)
    expect(a).toEqual({ x: 100, y: 96, fontSize: 12, caption: 'e1 · text' })
    const b = placeLabel('e2 · text', [100, 100], 640, 640, placed)
    expect(b.y).toBeCloseTo(96 - 12 * 1.3)
    expect(placed).toHaveLength(2)
    const long = placeLabel('x'.repeat(200), [0, 0], 640, 320, [])
    expect(long.fontSize).toBe(24)
    expect(long.caption).toBe(`${'x'.repeat(37)}…`)
    expect(long.y).toBe(24)
  })

  it('picks the overlay status', () => {
    expect(overlayStatusKey({ available: false, objects: [] })).toBe('review.noAnalysis')
    expect(overlayStatusKey({ available: true, objects: [{}] })).toBe('review.selectRegion')
    expect(overlayStatusKey({ available: true, objects: [] })).toBe('review.noDetections')
    expect(idleOverlayStatusKey({ objects: [{}] }, null)).toBe('review.selectRegion')
    expect(idleOverlayStatusKey(null, {})).toBe('review.noDetections')
    expect(idleOverlayStatusKey(null, null)).toBe('review.noAnalysis')
  })
})
