import { beforeEach, describe, expect, it } from 'vitest'
import { T, Tf } from '@/i18n'
import { analyzePayload, readAnalyzeStart } from './analyze'
import {
  analyzeStartQuery, editedScenes, estimatePayload, estimateText, saveAnalyzeStart, sceneBoundaries, selectedWindow,
  startButtonState, uploadErrorText, uploadTitle, videoMeta, windowFrameCount,
} from './ingest'

const project = { id: 'p1' }

describe('ingest', () => {
  beforeEach(() => sessionStorage.clear())

  // Ported from tests/test_ui_parse_optin.py::test_ingest_selection_reaches_post_analyze_body
  it('ingest selection reaches the POST /api/analyze body', () => {
    for (const checked of [false, true]) {
      const state = { project, mode: 'range', fullConfirmed: false, shortAcknowledged: false, estimateSnapshot: {
        mode: 'range', start: 0, end: 11, confirm_token: 'token', boundary_digest: 'digest',
      } }
      const query = analyzeStartQuery(state, { mode: 'range', start: 0, end: 11 }, checked)!
      saveAnalyzeStart(query, state.estimateSnapshot)
      expect(new URL(`/analyze${query}`, 'http://localhost').search).toBe(query)
      expect(sessionStorage.getItem('keepframe.analyze-start')).toBe(query)
      const { explicitStart, estimateSnapshot } = readAnalyzeStart(query)
      expect(explicitStart).toBe(true)
      const payload = analyzePayload(new URLSearchParams(query), estimateSnapshot)
      expect(payload.reference).toBe(checked ? 'ui' : 'mg')
      expect(payload.project_id).toBe('p1')
      expect(payload.boundary_digest).toBe('digest')
    }
  })

  it('start stays closed until the estimate matches the window and the confirmations are given', () => {
    const snapshot = { mode: 'full', start: 0, end: 9, confirm_token: 't', warnings: ['short'] }
    const win = { mode: 'full', start: 0, end: 9 }
    const base = { project, mode: 'full', estimateSnapshot: snapshot, fullConfirmed: true, shortAcknowledged: true }
    expect(analyzeStartQuery(base, win, false)).toBe('?job=p1&token=t&mode=full&start=0&end=9&reference=mg')
    expect(analyzeStartQuery({ ...base, project: null }, win, false)).toBeNull()
    expect(analyzeStartQuery({ ...base, estimateSnapshot: null }, win, false)).toBeNull()
    expect(analyzeStartQuery(base, { ...win, end: 8 }, false)).toBeNull()
    expect(analyzeStartQuery({ ...base, fullConfirmed: false }, win, false)).toBeNull()
    expect(analyzeStartQuery({ ...base, shortAcknowledged: false }, win, false)).toBeNull()
  })

  // Ported from tests/test_web_static.py::test_ingest_sends_selected_range_to_analyze
  it('sends the selected range, clamped to the clip, to the estimate and to analyze', () => {
    const state = { project, mode: 'range', totalFrames: 100, fps: 25 }
    expect(selectedWindow(state, '10', '20')).toEqual({ mode: 'range', start: 10, end: 20 })
    expect(selectedWindow(state, '-5', '500')).toEqual({ mode: 'range', start: 0, end: 99 })
    expect(selectedWindow(state, '30', '20')).toEqual({ mode: 'range', start: 30, end: 30 })
    expect(selectedWindow(state, 'x', '')).toEqual({ mode: 'range', start: 0, end: 0 })
    expect(selectedWindow({ ...state, mode: 'full' }, '10', '20')).toEqual({ mode: 'full', start: 0, end: 99 })
    expect(windowFrameCount({ mode: 'range', start: 10, end: 20 }, 100)).toBe(11)
    expect(windowFrameCount({ mode: 'full', start: 0, end: 99 }, 100)).toBe(100)
    expect(estimatePayload({ mode: 'range', start: 10, end: 20 }, state))
      .toEqual({ project_id: 'p1', mode: 'range', start: 10, end: 20, frames: 11, fps: 25 })
    const payload = analyzePayload(new URLSearchParams('job=p1&token=t&mode=range&start=10&end=20'), null)
    expect(payload).toEqual({ project_id: 'p1', confirm_token: 't', reference: 'mg', mode: 'range', start: 10, end: 20 })
  })

  // Ported from tests/test_web_static.py::test_ingest_reestimates_edited_boundaries_and_requires_short_scene_ack
  it('re-estimates edited boundaries and requires the short-scene acknowledgement', () => {
    const snapshot = { scenes: [{ id: 's1', frames: [0, 9] }, { id: 's2', frames: [10, 19] }, { id: 's3', frames: [20, 29] }] }
    expect(editedScenes(snapshot, [12, 25], { start: 0, end: 29 })).toEqual([
      { id: 's1', frames: [0, 11] }, { id: 's2', frames: [12, 24] }, { id: 's3', frames: [25, 29] },
    ])
    expect(editedScenes({ scenes: [] }, [], { start: 0, end: 29 })).toBeNull()
    expect(editedScenes(null, [], { start: 0, end: 29 })).toBeNull()

    const est = { seconds: 120, scene_count: 2, warnings: ['short'] }
    expect(startButtonState({ mode: 'range', fullConfirmed: false, shortAcknowledged: false }, est))
      .toEqual({ disabled: true, needsConfirm: false, confirmCopy: '' })
    expect(startButtonState({ mode: 'range', fullConfirmed: false, shortAcknowledged: true }, est).disabled).toBe(false)
    expect(startButtonState({ mode: 'full', fullConfirmed: false, shortAcknowledged: true }, est))
      .toEqual({ disabled: true, needsConfirm: true, confirmCopy: Tf('ingest.fullConfirm', { m: 2, n: 2 }) })

    for (const ack of [undefined, false, true]) {
      const payload = analyzePayload(new URLSearchParams('job=p1'), { scenes: snapshot.scenes, acknowledge_short_scenes: ack })
      expect(payload.acknowledge_short_scenes).toBe(Boolean(ack))
      expect(payload.scenes).toBe(snapshot.scenes)
      expect(payload.transitions).toEqual([])
      expect(payload.boundary_digest).toBeNull()
    }
  })

  it('offers one boundary input per cut, bounded by the neighbouring scenes', () => {
    const rows = sceneBoundaries({ scenes: [{ id: 's1', frames: [0, 9] }, { id: 's2', frames: [10, 19] }], transitions: [] })
    expect(rows).toEqual([
      { id: 's1', range: 's1 · 0–9', transition: 'unknown', min: 1, max: 19, value: 10, ariaLabel: `s1–s2 ${T('ingest.boundaryFrame')}` },
      { id: 's2', range: 's2 · 10–19' },
    ])
    expect(sceneBoundaries({ scenes: [{ id: 's1', frames: [0, 9] }, { id: 's2', frames: [10, 19] }], transitions: [{ transition: 'cut' }] })[0].transition).toBe('cut')
  })

  it('formats the estimate, video meta and upload errors like the legacy page', () => {
    const est = { seconds: 90, scene_count: 3, note: 'n', seconds_per_scene: 30 }
    expect(estimateText(est, { mode: 'range', start: 0, end: 49 }, { totalFrames: 100, fps: 25 })).toEqual({
      seconds: Tf('ingest.aboutMin', { m: 2 }), scenes: '3', note: 'n' + Tf('ingest.perScene', { s: 30 }),
      rangeMeta: Tf('ingest.rangeSummary', { sec: '2.0', frames: 50, n: 3 }),
    })
    expect(estimateText(est, { mode: 'full', start: 0, end: 99 }, { totalFrames: 100, fps: 25 }).rangeMeta)
      .toBe(Tf('ingest.fullSummary', { sec: '4.0', frames: 100, n: 3 }))
    expect(videoMeta({ width: 1920, height: 1080, duration_s: 2 }, 30)).toBe('1920x1080 · 30 fps · 2.0s')
    expect(videoMeta({}, 30)).toBe('?x? · 30 fps · 0.0s')
    expect(uploadTitle('clip.final.mp4')).toBe('clip.final')
    expect(uploadErrorText({ body: { code: 'live_action' }, message: 'x' })).toBe(T('error.liveaction'))
    expect(uploadErrorText({ message: 'boom' })).toBe('boom')
    expect(uploadErrorText({})).toBe(T('ingest.uploadFailed'))
  })
})
