import { T, Tf } from '@/i18n'

// Pure logic of the admin pages (legacy keepframe/web/static/js/admin-*.js). No DOM.

export const TENANTS_PATH = '/admin/tenants'

/** The text for a failed page load, or null on 401 (fetchAdminOrRedirect is already redirecting). */
export function loadErrorText(err: { status?: number; message?: string }) {
  if (err.status === 401) return null
  return err.message || T('admin.loadFailed')
}

// The API layer already turned error codes into text, so `message` is shown as is.
export const failureText = (err: { message?: string }, fallbackKey: string) => err.message || T(fallbackKey)

// --- tenants

const TENANT_STATUS_KEYS = { ok: 'admin.status.ok', quota: 'admin.status.quota', suspended: 'admin.status.suspended' }
const ROLE_KEYS = { admin: 'admin.role.admin', billing: 'admin.role.billing', review_lead: 'admin.role.reviewLead', maker: 'admin.role.maker' }
const QUOTA_FULL_PCT = 100

export type Tenant = { id: string; name: string; status: string; analyze_min: number; analyze_quota_min: number; render_min?: number }

export const tenantStatusLabel = (status: string) => (TENANT_STATUS_KEYS[status] ? T(TENANT_STATUS_KEYS[status]) : status)
export const roleLabel = (role: string) => (ROLE_KEYS[role] ? T(ROLE_KEYS[role]) : role)
export const isOverQuota = (t: Tenant) => t.status === 'quota'
export const isSuspended = (t: Tenant) => t.status === 'suspended'
/** The status pill warns for over-quota and suspended tenants. */
export const isTenantWarn = (t: Tenant) => isOverQuota(t) || isSuspended(t)

export function quotaPct(t: Tenant) {
  if (!t.analyze_quota_min) return 0
  return Math.min(QUOTA_FULL_PCT, Math.round((t.analyze_min / t.analyze_quota_min) * QUOTA_FULL_PCT))
}

export const filterTenants = <R extends Tenant>(tenants: R[], filter: string) => tenants.filter((t) => t.status === filter)

/** Member count per role, in first-seen order. */
export function roleCounts(members: { role: string }[]) {
  const counts: Record<string, number> = {}
  members.forEach((m) => { counts[m.role] = (counts[m.role] || 0) + 1 })
  return Object.entries(counts)
}

// --- queue

export type QueueJob = { id: string; status: string; retries?: number; gpu?: string; error?: string }

const QUEUE_STATUS_KEYS = { queued: 'admin.status.queued', running: 'admin.status.running', done: 'admin.status.done', failed: 'admin.status.failed', error: 'admin.status.failed', '대기': 'admin.status.queued', '실행': 'admin.status.running', '완료': 'admin.status.done', '실패': 'admin.status.failed' }
// Legacy badge--* classes; done/완료 has no tone.
const QUEUE_TONES = { running: 'active', failed: 'error', error: 'error', queued: 'done', '실행': 'active', '실패': 'error', '대기': 'done' }

export function canonicalStatus(status: string) {
  if (status === '대기') return 'queued'
  if (status === '실행') return 'running'
  if (status === '완료') return 'done'
  if (status === '실패' || status === 'error') return 'failed'
  return status
}
export const jobStatusLabel = (status: string) => (QUEUE_STATUS_KEYS[status] ? T(QUEUE_STATUS_KEYS[status]) : status)
export const jobStatusTone = (status: string): string => QUEUE_TONES[status] || ''
export const isQuarantined = (j: QueueJob) => j.status === '검역' || j.status === 'quarantine'
export const isFailed = (j: QueueJob) => ['failed', 'error', '실패'].includes(canonicalStatus(j.status))
export const retriesExhausted = (j: QueueJob, retryCap: number) => (j.retries ?? 0) >= retryCap

export function retryLabel(j: QueueJob, retryCap: number) {
  if (isQuarantined(j)) return T('common.none')
  const n = j.retries ?? 0
  return retriesExhausted(j, retryCap) ? `${n}/${retryCap} ${T('admin.queue.retryExhausted')}` : `${n}/${retryCap}`
}

export function queueCounts(jobs: QueueJob[]) {
  const counts = jobs.reduce((out, job) => { out[canonicalStatus(job.status)] = (out[canonicalStatus(job.status)] || 0) + 1; return out }, {} as Record<string, number>)
  return { queued: counts.queued || 0, running: counts.running || 0, failed: counts.failed || 0, gpu: jobs.filter((j) => j.gpu).length }
}

/** The job selected on load: the first failed one, else the first. */
export const initialJob = <J extends QueueJob>(jobs: J[]) => jobs.find(isFailed) || jobs[0]

// --- quarantine

const TENANT_LABEL = { org_solo: 'Solo', org_northwind: 'Northwind', org_hanbit: 'Hanbit' }
export const tenantLabel = (id: string): string => TENANT_LABEL[id] || id
export const quarantineCount = (n: number) => Tf('admin.quarantine.count', { n })

// --- audit (actions arrive as Korean server strings)

const ACTION_KEYS = {
  '업로드 검역': 'admin.audit.action.quarantineUpload',
  '내보내기': 'admin.audit.action.export',
  '멤버 추가': 'admin.audit.action.memberAdded',
  'keep 변경': 'admin.audit.action.keepChanged',
  '테넌트 정지 시도': 'admin.audit.action.tenantSuspendAttempt',
  '테넌트 정지': 'admin.audit.action.tenantSuspended',
  'LLM 프로바이더 변경': 'admin.audit.action.llmProviderChanged',
}

export function actionLabel(action: string) {
  if (action.startsWith('keep 해제 ')) return `${T('admin.audit.action.keepReleased')} ${action.slice('keep 해제 '.length)}`
  if (action.startsWith('내보내기 ')) return `${T('admin.audit.action.export')} ${action.slice('내보내기 '.length)}`
  return ACTION_KEYS[action] ? T(ACTION_KEYS[action]) : action
}

/** Legacy action--warn / action--keep. */
export function actionTone(action: string) {
  if (action.includes('정지') || action.includes('검역')) return 'warn'
  if (action.includes('keep')) return 'keep'
  return ''
}

// --- LLM settings

export const MODEL_RELOAD_DEBOUNCE_MS = 400
export const AZURE_SCOPE = 'https://cognitiveservices.azure.com/.default'
export const LLM_PATH = '/admin/llm'
const DEFAULT_AUTH_MODES = ['api_key']

export type ProviderSpec = {
  id?: string
  label?: string
  auth_modes?: string[]
  hide_api_key?: boolean
  hide_base_url?: boolean
  default_base_url?: string
  default_model?: string
}
export type LlmForm = {
  provider: string
  model: string
  auth: string
  apiKey: string
  clientId: string
  clientSecret: string
  tenantId: string
  tokenUrl: string
  scope: string
  baseUrl: string
  extra: string
}

export const specOf = (catalog: ProviderSpec[], id: string): ProviderSpec => catalog.find((p) => p.id === id) || {}
export const authModes = (spec: ProviderSpec) => spec.auth_modes || DEFAULT_AUTH_MODES

export function visibleFields(provider: string, authMode: string, spec: ProviderSpec) {
  const modes = authModes(spec)
  const isChatgpt = provider === 'chatgpt'
  const isAzure = provider === 'azure'
  const isOauth = authMode === 'oauth'
  const isAzureOauth = isAzure && isOauth
  return {
    modes,
    hideAuth: isChatgpt || modes.length === 1,
    hideApiKey: isChatgpt || Boolean(spec.hide_api_key) || isOauth,
    hideOauth: !isChatgpt && !isAzureOauth,
    hideChatgptOAuth: !isChatgpt,
    hideOauthAdvanced: !isAzure,
    openOauthAdvanced: isAzureOauth,
    hideBaseUrl: Boolean(spec.hide_base_url),
  }
}

/** An auth mode the provider does not offer falls back to its first. */
export function validAuth(authMode: string, spec: ProviderSpec) {
  const modes = authModes(spec)
  return modes.includes(authMode) ? authMode : modes[0]
}

export const shouldFillAzureScope = (form: Pick<LlmForm, 'provider' | 'auth' | 'scope'>) =>
  form.provider === 'azure' && form.auth === 'oauth' && !form.scope

/** Picking a provider resets auth, base URL and model to its defaults; Azure OAuth gets its scope. */
export function applyProviderDefaults(form: LlmForm, spec: ProviderSpec): LlmForm {
  const next = { ...form, auth: authModes(spec)[0], baseUrl: spec.default_base_url || '', model: spec.default_model || '' }
  if (shouldFillAzureScope(next)) next.scope = AZURE_SCOPE
  return next
}

/** Saved settings → form values. */
export function formFromSettings(s: any, catalog: ProviderSpec[]): LlmForm {
  const provider = s.provider || 'openai'
  const spec = specOf(catalog, provider)
  const modes = authModes(spec)
  return {
    provider,
    model: s.model || '',
    auth: s.auth && modes.includes(s.auth) ? s.auth : modes[0],
    apiKey: s.api_key || '',
    clientId: s.client_id || '',
    clientSecret: s.client_secret || '',
    tenantId: s.tenant_id || '',
    tokenUrl: s.token_url || '',
    scope: s.scope || '',
    baseUrl: s.base_url || spec.default_base_url || '',
    extra: s.extra && Object.keys(s.extra).length ? JSON.stringify(s.extra, null, 2) : '',
  }
}

export const chatgptConnected = (s: any) => Boolean(s && s.chatgpt_connected)
export const chatgptStatusText = (s: any) => (chatgptConnected(s) ? T('admin.llm.chatgptConnected') : '')

export function parseExtraJson(text: string): { ok: true; value: any } | { ok: false; value: null } {
  const trimmed = text.trim()
  if (!trimmed) return { ok: true, value: {} }
  try {
    return { ok: true, value: JSON.parse(trimmed) }
  } catch {
    return { ok: false, value: null }
  }
}

/** Only the fields of the chosen auth mode are sent; the others go as "". */
export function readFormSettings(form: LlmForm) {
  const isOauth = form.auth === 'oauth'
  return {
    provider: form.provider,
    model: form.model.trim(),
    auth: form.auth,
    api_key: isOauth ? '' : form.apiKey.trim(),
    client_id: isOauth ? form.clientId.trim() : '',
    client_secret: isOauth ? form.clientSecret.trim() : '',
    tenant_id: isOauth ? form.tenantId.trim() : '',
    token_url: isOauth ? form.tokenUrl.trim() : '',
    scope: isOauth ? form.scope.trim() : '',
    base_url: form.baseUrl.trim(),
  }
}

/** The POST /admin/api/llm body, or the message to show when the extra JSON is invalid. */
export function settingsPayload(form: LlmForm) {
  const parsed = parseExtraJson(form.extra)
  if (!parsed.ok) return { ok: false as const, error: T('admin.llm.extraInvalid') }
  return { ok: true as const, body: { settings: { ...readFormSettings(form), extra: parsed.value } } }
}

export const modelsBody = (form: LlmForm) =>
  ({ provider: form.provider, base_url: form.baseUrl.trim(), api_key: form.apiKey.trim() })

export const modelsLoadedText = (ids: string[]) =>
  (ids.length ? Tf('admin.llm.modelsLoaded', { n: ids.length }) : T('admin.llm.modelsEmpty'))

/** The note after returning from the ChatGPT OAuth page (`?oauth=ok|err`), or null for anything else. */
export function oauthReturnNote(flag: string | null) {
  if (flag === 'ok') return { msg: T('admin.llm.chatgptConnectOk'), isErr: false }
  if (flag === 'err') return { msg: T('admin.llm.chatgptConnectFailed'), isErr: true }
  return null
}
