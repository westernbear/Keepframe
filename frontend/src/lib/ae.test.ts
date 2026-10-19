import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  backoff, codeCountdown, connectionStatus, editMessage, handEditJob, installOpen, jobLine, jobOutcome, pairingDone,
  relative, sendBody, shouldPollDevices, shouldPollState, sortDevices, stateDelay, stateUrl, syncSummary, toSnapshot,
  verifyActive, verifyLimits, verifyNotes, verifyOutcome, warningsSummary,
} from './ae'

// Expected strings come from tests/test_ae_card_browser.py.
function verifyReport(passed = true) {
  const rows = passed
    ? [{ frame: 42, l1: 0.011 }, { frame: 7, l1: 0.004 }, { frame: 1, l1: 0.003 }]
    : [{ frame: 42, l1: 0.081 }, { frame: 7, l1: 0.067 }, { frame: 1, l1: 0.051 }]
  return {
    passed, mean: passed ? 0.003 : 0.036, max: rows[0].l1, thresholds: { mean: 0.025, frame: 0.04 }, frames: rows,
    worst: rows, notes: ['e6 · title: Arial instead of sans-serif — text region differs by 5.3%'],
    masked: [{ id: 'e6', name: 'e6 · title', font: 'Arial', requested: 'sans-serif', worst_l1: 0.053 }],
    job: 'verify-v4', version: 'v4', state: 'done',
  }
}
const snap = (data: any) => toSnapshot({ devices: [], ...data })

describe('ae', () => {
  beforeEach(() => localStorage.setItem('keepframe.lang', 'en'))
  afterEach(() => localStorage.clear())

  it('polls fast while work is active, backs off to a minute, and pauses while hidden', () => {
    expect(stateDelay(snap({}))).toBe(15000)
    expect(stateDelay(snap({ jobs: [{ id: 'j', kind: 'sync', state: 'running' }] }))).toBe(2000)
    expect(stateDelay(snap({ jobs: [{ id: 'j', kind: 'sync', state: 'done' }], verify: { state: 'verifying' } }))).toBe(2000)
    expect(stateDelay(snap({ verify: { state: 'interrupted' } }))).toBe(15000)
    const schedule = [2000]
    while (schedule.length < 7) schedule.push(backoff(schedule.at(-1)!))
    expect(schedule).toEqual([2000, 4000, 8000, 16000, 32000, 60000, 60000])
    expect(backoff(15000)).toBe(30000)
    const pairing = { code: 'ABC', expires_at: 0, before: new Set(['old']) }
    expect([shouldPollState(false), shouldPollState(true)]).toEqual([true, false])
    expect([shouldPollDevices(pairing, false), shouldPollDevices(pairing, true), shouldPollDevices(null, false)]).toEqual([true, false, false])
    expect(pairingDone(pairing, [{ id: 'old', created: 0 }])).toBe(false)
    expect(pairingDone(pairing, [{ id: 'old', created: 0 }, { id: 'new', created: 1 }])).toBe(true)
  })

  it('formats the pairing countdown and relative times', () => {
    expect(codeCountdown(100, 100000 - 125400)).toEqual({ milliseconds: 125400, text: '02:06' })
    expect(codeCountdown(100, 100000).milliseconds).toBe(0)
    expect([null, 990, 900, 1000 - 7200, 1000 - 3 * 86400].map((t) => relative(t, 1000)))
      .toEqual(['never', 'just now', '1 min ago', '2 hr ago', '3 days ago'])
  })

  it('maps job outcomes and history lines', () => {
    expect(jobOutcome(null)).toBe('')
    expect(jobOutcome({ id: 'j', kind: 'sync', state: 'queued' })).toBe('Queued')
    expect(jobOutcome({ id: 'j', kind: 'sync', state: 'failed', error: 'boom' })).toBe('boom')
    expect(jobOutcome({ id: 'j', kind: 'sync', state: 'done', result: {} })).toBe('Done')
    expect(jobOutcome({ id: 'j', kind: 'sync', state: 'done', result: { applied: false } })).toBe('')
    expect(jobOutcome({ id: 'j', kind: 'sync', state: 'done', result: { applied: true } })).toBe('No changes')
    expect(jobOutcome({ id: 'j', kind: 'sync', state: 'done', result: { applied: true, created: 2, updated: ['title'] } }))
      .toBe('2 created, 1 updated')
    expect(jobLine({ id: 'j', kind: 'sync', state: 'done', result: { applied: true }, finished: 995 }, 1000))
      .toBe('Sync: No changes. just now')
    expect(editMessage({ interrupted: ['kf:title'], hand_edited: ['kf:title', 'kf:logo'] }))
      .toBe('a previous sync was interrupted — overwrite to finish it: kf:title\n1 layers were edited by hand in AE: kf:logo')
    const sync = { id: 's', kind: 'sync', state: 'done', result: { applied: false, hand_edited: ['kf:logo'] } }
    expect(handEditJob(snap({ jobs: [{ id: 'r', kind: 'render_frames', state: 'done' }, sync] }))).toBe(sync)
    expect(handEditJob(snap({ jobs: [{ ...sync, result: { applied: true } }] }))).toBeNull()
  })

  it('reports verify states and failures in both languages', () => {
    expect(verifyOutcome(verifyReport())).toBe('Matches Keepframe (v4): mean 0.3%, worst frame 42 at 1.1%')
    expect(verifyOutcome(verifyReport(false)))
      .toBe('Differs from Keepframe (v4): mean 3.6%, worst frame 42 at 8.1%, 3 frames over 4%')
    const meanOnly = { ...verifyReport(false), mean: 0.03, frames: [{ frame: 1, l1: 0.03 }] }
    expect(verifyOutcome(meanOnly)).toContain(', mean 3.0% is over the 2.5% limit')
    expect(verifyOutcome({ ...meanOnly, frames: [{ frame: 1, l1: 0.05 }] })).toContain(', 1 frame over 4%')
    expect(verifyOutcome({ state: 'failed', error: 'Font missing' })).toBe('Font missing')
    expect(verifyOutcome({ state: 'interrupted' })).toBe('Verification was interrupted — verify again')
    expect(verifyLimits({ thresholds: { mean: 0.025, frame: 0.07 } })).toBe('Limits: mean 2.5%, frame 7%')
    expect(verifyNotes(verifyReport())).toEqual(['e6 · title: shown in Arial, text area differs by 5.3%'])
    expect(verifyNotes({ notes: ['n'] })).toEqual(['n'])
    expect(verifyNotes({ masked: [{ id: 'e6', worst_l1: 0.053 }] })).toEqual(['Masked text e6: worst difference 5.3%'])
    localStorage.setItem('keepframe.lang', 'ko')
    expect(verifyOutcome(verifyReport(false)))
      .toBe('Keepframe과 다릅니다 (v4): 평균 3.6%, 최대 차이 42번 프레임 8.1%, 4%를 넘는 프레임 3개')
    expect(verifyOutcome({ state: 'failed', error: 'the saved report could not be read — verify again' }))
      .toBe('저장된 비교 결과를 읽을 수 없습니다. 다시 비교하세요')
  })

  it('derives the connection chip, summary and button state', () => {
    const now = 1000
    const devices = sortDevices([
      { id: 'a', created: 1, last_seen: now - 100 },
      { id: 'b', created: 2, last_seen: now, connected: true, ae_version: '26.5x89', project_name: 'Example.aep' },
    ])
    expect(devices.map((d) => d.id)).toEqual(['b', 'a'])
    expect(connectionStatus(devices[0])).toBe('AE 26.5 connected · Example.aep')
    expect(connectionStatus({ id: 'x', created: 0, connected: true, project_name: 'Example.aep' })).toBe('AE connected · Example.aep')
    expect(connectionStatus(undefined)).toBe('Not connected')
    expect(syncSummary(snap({}), devices)).toEqual({ text: 'Nothing sent yet', failed: false })
    const synced = snap({ last_synced: { b: 'v1' }, jobs: [{ id: 'j', kind: 'sync', state: 'queued' }], verify: verifyReport() })
    expect(syncSummary(synced, devices)).toEqual({ text: 'AE has v1. Queued', failed: false })
    expect(syncSummary({ ...synced, jobs: [] }, devices).text).toBe('Matches Keepframe (v4): mean 0.3%, worst frame 42 at 1.1%')
    expect(syncSummary(snap({ verify: { job: 'x', state: 'failed', error: 'e' } }), devices)).toEqual({ text: 'e', failed: true })
    expect(verifyActive(snap({ jobs: [{ id: 'j', kind: 'render_frames', state: 'queued' }] }))).toBe(true)
    expect(verifyActive(snap({ jobs: [{ id: 'j', kind: 'sync', state: 'running' }] }))).toBe(false)
    expect([installOpen(true, false, true), installOpen(false, true, false), installOpen(true, true, true)]).toEqual([false, true, true])
    expect([warningsSummary(0), warningsSummary(1), warningsSummary(30)]).toEqual(['', '1 warning', '30 warnings'])
  })

  it('builds request URLs and bodies', () => {
    expect(stateUrl('p1', 's 1')).toBe('/api/ae/state?project=p1&scene=s+1')
    expect(stateUrl('p1', '')).toBe('/api/ae/devices')
    expect(sendBody('p', 's', 'v1')).toEqual({ project: 'p', scene: 's', version: 'v1' })
    expect(sendBody('p', 's', 'v1', true)).toEqual({ project: 'p', scene: 's', version: 'v1', force: true })
  })
})
