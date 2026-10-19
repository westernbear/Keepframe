import { afterEach, expect, it } from 'vitest'
import {
  isCurrentPlan, modeForBackend, planOptionLabel, renderArtifactLinks, renderCardView, renderControls, renderPlanId,
  renderWarningLines, shouldPollRender, upsertRenderPlan,
} from './agent-render'

afterEach(() => localStorage.clear())

const plan = { id: 'abcdef0123456789', backend: 'native', mode: 'preview', version_id: 'v2', digest: 'd',
  artifact_contract: { outputs: ['mp4', 'png'] } }

it('polls only queued/running plans and drops answers for a plan no longer selected', () => {
  expect(shouldPollRender({ plan, status: 'running' })).toBe(true)
  expect(shouldPollRender({ plan, status: 'queued' })).toBe(true)
  expect(shouldPollRender({ plan, status: 'done' })).toBe(false)
  expect(shouldPollRender({ status: 'running' })).toBe(false)
  expect(shouldPollRender(null)).toBe(false)
  expect(isCurrentPlan({ plan }, plan.id)).toBe(true)
  expect(isCurrentPlan({ plan: { id: 'other' } }, plan.id)).toBe(false)
  expect(isCurrentPlan(null, plan.id)).toBe(false)
  expect(renderPlanId({ plan })).toBe(plan.id)
  expect(renderPlanId(plan)).toBe(plan.id)
})

it('upserts plans by id without mutating the list', () => {
  const a = { plan, status: 'queued' }
  const plans = upsertRenderPlan([], a)
  expect(plans).toEqual([a])
  const b = { plan, status: 'done' }
  const next = upsertRenderPlan(plans, b)
  expect(next).toEqual([b])
  expect(plans).toEqual([a])
  expect(upsertRenderPlan(next, { plan: {} })).toBe(next)
  expect(planOptionLabel({ plan, state: { status: 'awaiting_approval' } })).toBe('Native preview (awaiting_approval, abcdef0123…)')
})

it('derives the render card and its controls', () => {
  const awaiting = { plan, state: { status: 'awaiting_approval', revision: 3 } }
  expect(renderCardView(awaiting)).toEqual({
    hasPlan: true, status: 'awaiting_approval', statusText: 'awaiting_approval (v2)', statusTitle: 'awaiting_approval, r3',
    tone: '', reason: '', showReason: false, outputs: ['mp4', 'png'], showOutputs: true, awaitsApproval: true,
  })
  const failed = renderCardView({ plan, status: 'failed', job: { error: 'export_failed' } })
  expect(failed.tone).toBe('failed')
  expect(failed.showReason).toBe(true)
  expect(failed.reason).toBe('프로젝트 ZIP을 만들지 못했습니다. 디스크 공간과 파일 권한을 확인하세요')
  expect(renderCardView({ plan, status: 'running' }).tone).toBe('active')
  expect(renderCardView(null)).toMatchObject({ hasPlan: false, status: '', statusText: '', showOutputs: false, awaitsApproval: false })

  const base = { payload: awaiting, plans: [awaiting], busy: false, mode: 'preview', backend: 'native', reviewStatus: 'review' }
  expect(renderControls(base)).toEqual({ selectorDisabled: false, createDisabled: false, approveDisabled: false })
  expect(renderControls({ ...base, busy: true })).toEqual({ selectorDisabled: true, createDisabled: true, approveDisabled: true })
  expect(renderControls({ ...base, mode: 'final' }).createDisabled).toBe(true) // final needs an approved scene
  expect(renderControls({ ...base, mode: 'final', reviewStatus: 'approved' }).createDisabled).toBe(false)
  expect(renderControls({ ...base, backend: 'lottie' }).createDisabled).toBe(true) // Lottie renders only final
  expect(renderControls({ ...base, payload: { plan, state: { status: 'approved' } } }).approveDisabled).toBe(true)
  expect(renderControls({ ...base, plans: [] }).selectorDisabled).toBe(true)
  expect(modeForBackend('lottie', 'preview')).toBe('final')
  expect(modeForBackend('native', 'preview')).toBe('preview')
})

it('lists artifacts and known warnings only', () => {
  localStorage.setItem('keepframe.lang', 'en')
  expect(renderArtifactLinks(plan, { artifacts: [{ kind: 'mp4' }, { kind: 'zip', label: 'Project' }, {}, null] }, 'p 1')).toEqual([
    { href: '/api/native/artifacts/abcdef0123456789/mp4?project=p+1', label: 'Download MP4' },
    { href: '/api/native/artifacts/abcdef0123456789/zip?project=p+1', label: 'Project' },
  ])
  expect(renderArtifactLinks({ ...plan, backend: 'lottie' }, { artifacts: [{ kind: 'json' }] }, 'p')[0].href)
    .toBe('/api/lottie/artifacts/abcdef0123456789/animation?project=p')
  expect(renderArtifactLinks(null, { artifacts: [{ kind: 'mp4' }] }, 'p')).toEqual([])
  expect(renderWarningLines([{ kind: 'font_file_missing', family: '<b>X</b>' }, { kind: 'other' }, null])).toHaveLength(1)
  expect(renderWarningLines('nope')).toEqual([])
})
