import { beforeEach, describe, expect, it } from 'vitest'
import { T, Tf } from '@/i18n'
import {
  PIPELINE, STAGE_I18N, STEP_STAGES, formatEta, rangeLabel, readAnalyzeStart, reviewHref, stageLabel, stepState,
} from './analyze'

describe('analyze', () => {
  beforeEach(() => sessionStorage.clear())

  // Ported from tests/test_plate_stage.py::test_stage_order_has_plate_between_tracking_and_solids (the JS half;
  // the backend STAGES comparison stays in Python)
  it('steps group the pipeline contiguously, with plate between tracking and solids', () => {
    expect(PIPELINE.indexOf('tracking') + 1).toBe(PIPELINE.indexOf('plate'))
    expect(PIPELINE.indexOf('plate')).toBe(PIPELINE.indexOf('solids') - 1)
    expect(Object.values(STEP_STAGES).flat()).toEqual(PIPELINE) // contiguous groups, one step active at a time
    expect(STEP_STAGES.bg).toEqual(['background'])
    expect(STEP_STAGES.regions).toEqual(['regions', 'tracking', 'plate', 'solids'])
    expect(STAGE_I18N.plate).toBe('analyze.step.regions')
    expect(Object.keys(STAGE_I18N)).toEqual(PIPELINE)
  })

  // Ported from tests/test_web_static.py::test_analyze_steps_follow_job_stage
  it('steps follow the job stage', () => {
    expect(Object.keys(STEP_STAGES).map((step) => stepState(step, 'plate')))
      .toEqual(['done', 'done', 'done', 'active', 'waiting', 'waiting', 'waiting', 'waiting'])
    expect(stepState('shots', 'frames')).toBe('active')
    expect(stepState('report', 'report')).toBe('active')
    expect(stepState('shots', undefined)).toBe('waiting')
    expect(stepState('shots', 'queued')).toBe('waiting')
  })

  it('labels the stage and the ETA', () => {
    expect(stageLabel({ stage: 'tracking' })).toBe(T('analyze.step.regions'))
    expect(stageLabel({ stage: 'semantics', detail: '3/5' })).toBe(`${T('analyze.step.predicates')} 3/5`)
    expect(stageLabel({ stage: 'odd' })).toBe('odd')
    expect(stageLabel({ status: 'queued' })).toBe('queued')
    expect(stageLabel({})).toBe('—')
    expect(formatEta(null)).toBe('—')
    expect(formatEta(undefined)).toBe('—')
    expect(formatEta(10)).toBe(Tf('analyze.etaLeft', { m: 1 }))
    expect(formatEta(150)).toBe(Tf('analyze.etaLeft', { m: 3 }))
    expect(reviewHref('p1')).toBe('/review?project=p1&scene=s1')
  })

  it('shows the explicit range, else the project range', () => {
    expect(rangeLabel(new URLSearchParams('mode=range&start=3&end=9'), { range: [0, 1] })).toBe('3–9')
    expect(rangeLabel(new URLSearchParams('mode=range&start=&end=9'), { range: [0, 1] })).toBe('0–1')
    expect(rangeLabel(new URLSearchParams('mode=full&start=3&end=9'), null)).toBeNull()
  })

  it('starts a job only when opened by the ingest start button', () => {
    sessionStorage.setItem('keepframe.analyze-start', '?job=p1')
    sessionStorage.setItem('keepframe.analyze-estimate', '{"boundary_digest":"d"}')
    expect(readAnalyzeStart('?job=p2')).toEqual({ explicitStart: false, estimateSnapshot: { boundary_digest: 'd' } })
    expect(sessionStorage.getItem('keepframe.analyze-start')).toBe('?job=p1')
    expect(readAnalyzeStart('?job=p1')).toEqual({ explicitStart: true, estimateSnapshot: { boundary_digest: 'd' } })
    expect(sessionStorage.length).toBe(0)
    sessionStorage.setItem('keepframe.analyze-estimate', '{broken')
    expect(readAnalyzeStart('?job=p1').estimateSnapshot).toBeNull()
  })
})
