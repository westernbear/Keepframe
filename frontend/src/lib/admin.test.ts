import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  actionLabel, actionTone, applyProviderDefaults, AZURE_SCOPE, canonicalStatus, chatgptStatusText, filterTenants,
  formFromSettings, initialJob, isFailed, jobStatusLabel, jobStatusTone, loadErrorText, modelsLoadedText,
  oauthReturnNote, quotaPct, queueCounts, retryLabel, roleCounts, settingsPayload, tenantLabel, validAuth, visibleFields,
} from './admin'

const AZURE = { id: 'azure', auth_modes: ['api_key', 'oauth'], default_base_url: 'https://x.openai.azure.com', default_model: 'gpt' }
const CHATGPT = { id: 'chatgpt', auth_modes: ['oauth'], hide_api_key: true, hide_base_url: true }
const form = (over = {}) => ({
  provider: 'azure', model: ' m ', auth: 'oauth', apiKey: ' key ', clientId: ' cid ', clientSecret: ' sec ',
  tenantId: ' t ', tokenUrl: ' u ', scope: ' s ', baseUrl: ' https://b ', extra: '', ...over,
})

describe('admin', () => {
  beforeEach(() => localStorage.setItem('keepframe.lang', 'en'))
  afterEach(() => localStorage.clear())

  it('labels tenants, roles and quota', () => {
    const tenants = [
      { id: 'a', name: 'A', status: 'ok', analyze_min: 30, analyze_quota_min: 120 },
      { id: 'b', name: 'B', status: 'quota', analyze_min: 150, analyze_quota_min: 120 },
      { id: 'c', name: 'C', status: 'ok', analyze_min: 5, analyze_quota_min: 0 },
    ]
    expect(tenants.map(quotaPct)).toEqual([25, 100, 0])
    expect(filterTenants(tenants, 'ok').map((t) => t.id)).toEqual(['a', 'c'])
    expect(roleCounts([{ role: 'maker' }, { role: 'admin' }, { role: 'maker' }])).toEqual([['maker', 2], ['admin', 1]])
    expect(tenantLabel('org_hanbit')).toBe('Hanbit')
    expect(tenantLabel('org_x')).toBe('org_x')
    expect(loadErrorText({ status: 401, message: 'x' })).toBeNull()
    expect(loadErrorText({ status: 500 })).toBe('Could not load.')
  })

  it('maps queue statuses, tones, retries and counts', () => {
    expect(['대기', '실행', '완료', '실패', 'error', 'odd'].map(canonicalStatus)).toEqual(['queued', 'running', 'done', 'failed', 'failed', 'odd'])
    expect(['실행', 'error', 'done', 'odd'].map(jobStatusLabel)).toEqual(['Running', 'Failed', 'Done', 'odd'])
    expect(['running', '실패', 'queued', 'done', '완료'].map(jobStatusTone)).toEqual(['active', 'error', 'done', '', ''])
    expect(retryLabel({ id: 'j', status: 'failed', retries: 4 }, 4)).toBe('4/4 Retry limit reached')
    expect(retryLabel({ id: 'j', status: 'failed' }, 4)).toBe('0/4')
    expect(retryLabel({ id: 'j', status: '검역' }, 4)).toBe('None')
    const jobs = [
      { id: '1', status: '대기', gpu: 'A100' }, { id: '2', status: 'running' }, { id: '3', status: '실패' },
      { id: '4', status: 'error' }, { id: '5', status: 'quarantine' },
    ]
    expect(queueCounts(jobs)).toEqual({ queued: 1, running: 1, failed: 2, gpu: 1 })
    expect(isFailed(jobs[0])).toBe(false)
    expect(initialJob(jobs)?.id).toBe('3')
    expect(initialJob([{ id: 'x', status: 'done' }])?.id).toBe('x')
  })

  it('localizes audit actions', () => {
    expect(actionLabel('keep 해제 orange')).toBe('Keep released orange')
    expect(actionLabel('내보내기 v3')).toBe('Export v3')
    expect(actionLabel('LLM 프로바이더 변경')).toBe('LLM provider changed')
    expect(actionLabel('unknown')).toBe('unknown')
    expect(['테넌트 정지', '업로드 검역', 'keep 변경', '멤버 추가'].map(actionTone)).toEqual(['warn', 'warn', 'keep', ''])
  })

  it('shows LLM fields per provider and auth mode', () => {
    expect(visibleFields('openai', 'api_key', {})).toEqual({
      modes: ['api_key'], hideAuth: true, hideApiKey: false, hideOauth: true, hideChatgptOAuth: true,
      hideOauthAdvanced: true, openOauthAdvanced: false, hideBaseUrl: false,
    })
    expect(visibleFields('azure', 'oauth', AZURE)).toMatchObject({
      hideAuth: false, hideApiKey: true, hideOauth: false, hideOauthAdvanced: false, openOauthAdvanced: true,
    })
    expect(visibleFields('chatgpt', 'oauth', CHATGPT)).toMatchObject({
      hideAuth: true, hideApiKey: true, hideOauth: false, hideChatgptOAuth: false, hideBaseUrl: true,
    })
    expect(validAuth('oauth', {})).toBe('api_key')
    const reset = applyProviderDefaults(form({ provider: 'azure', auth: 'oauth', scope: '' }), AZURE)
    expect(reset).toMatchObject({ auth: 'api_key', baseUrl: AZURE.default_base_url, model: 'gpt', scope: '' })
    const oauthFirst = { ...AZURE, auth_modes: ['oauth'] }
    expect(applyProviderDefaults(form({ scope: '' }), oauthFirst).scope).toBe(AZURE_SCOPE)
    expect(applyProviderDefaults(form({ scope: 'mine' }), oauthFirst).scope).toBe('mine')
  })

  it('fills the form from saved settings', () => {
    expect(formFromSettings({}, [])).toMatchObject({ provider: 'openai', auth: 'api_key', baseUrl: '', extra: '' })
    const filled = formFromSettings({ provider: 'azure', auth: 'oauth', extra: { api_version: '1' } }, [AZURE])
    expect(filled).toMatchObject({ auth: 'oauth', baseUrl: AZURE.default_base_url, extra: '{\n  "api_version": "1"\n}' })
    expect(formFromSettings({ provider: 'azure', auth: 'magic', base_url: 'b' }, [AZURE])).toMatchObject({ auth: 'api_key', baseUrl: 'b' })
    expect(chatgptStatusText({ chatgpt_connected: true })).toBe('ChatGPT connected')
    expect(chatgptStatusText(null)).toBe('')
  })

  it('validates the extra JSON and sends only the chosen auth fields', () => {
    expect(settingsPayload(form({ extra: '{bad' }))).toEqual({ ok: false, error: 'Extra parameters are not valid JSON.' })
    expect(settingsPayload(form())).toEqual({ ok: true, body: { settings: {
      provider: 'azure', model: 'm', auth: 'oauth', api_key: '', client_id: 'cid', client_secret: 'sec',
      tenant_id: 't', token_url: 'u', scope: 's', base_url: 'https://b', extra: {},
    } } })
    const apiKey = settingsPayload(form({ auth: 'api_key', extra: ' {"a": 1} ' }))
    expect(apiKey.ok && apiKey.body.settings).toMatchObject({ api_key: 'key', client_id: '', scope: '', extra: { a: 1 } })
    expect([modelsLoadedText(['a', 'b']), modelsLoadedText([])]).toEqual(['Models loaded: 2.', 'The model list is empty.'])
    expect(oauthReturnNote('err')).toEqual({ msg: 'Could not connect ChatGPT.', isErr: true })
    expect(oauthReturnNote(null)).toBeNull()
    localStorage.setItem('keepframe.lang', 'ko')
    expect(settingsPayload(form({ extra: '[' }))).toEqual({ ok: false, error: '추가 파라미터가 올바른 JSON이 아닙니다.' })
  })
})
