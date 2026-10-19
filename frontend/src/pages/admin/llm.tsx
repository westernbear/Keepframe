import { useEffect, useRef, useState } from 'react'
import { AdminShell } from '@/components/app/admin-shell'
import { Button } from '@/components/app/button'
import { useI18n } from '@/i18n'
import {
  LLM_PATH, MODEL_RELOAD_DEBOUNCE_MS, applyProviderDefaults, chatgptConnected, formFromSettings, modelsBody, modelsLoadedText,
  oauthReturnNote, settingsPayload, shouldFillAzureScope, specOf, validAuth, visibleFields, AZURE_SCOPE, type LlmForm, type ProviderSpec,
} from '@/lib/admin'
import { fetchAdminOrRedirect } from '@/lib/api'
import { cn } from '@/lib/utils'
import { panel } from './common'

const input = 'h-10 w-full min-w-0 rounded-lg border border-hairline bg-canvas px-3 text-body-sm text-ink outline-none focus-visible:border-brand'
const row = 'field field--row grid gap-1.5 sm:grid-cols-[140px_minmax(0,1fr)] sm:items-center sm:gap-4'
const label = 'text-body-sm text-ink-muted'
const EMPTY: LlmForm = { provider: '', model: '', auth: 'api_key', apiKey: '', clientId: '', clientSecret: '', tenantId: '', tokenUrl: '', scope: '', baseUrl: '', extra: '' }

export default function AdminLlm() {
  const { T, errorText } = useI18n()
  const [catalog, setCatalog] = useState<ProviderSpec[]>([])
  const [form, setForm] = useState<LlmForm>(EMPTY)
  const [current, setCurrent] = useState<any>(null)
  const [models, setModels] = useState<string[]>([])
  const [status, setStatus] = useState<{ msg: string; isErr: boolean } | null>(null)
  const timer = useRef<any>(0)
  const formRef = useRef(form)
  useEffect(() => { formRef.current = form })
  const note = (msg: string, isErr = false) => setStatus(msg ? { msg, isErr } : null)

  async function loadModels(f: LlmForm, announce: boolean) {
    setModels([])
    try {
      const data = await fetchAdminOrRedirect('/admin/api/llm/models', { method: 'POST', body: JSON.stringify(modelsBody(f)) })
      const ids: string[] = data.models || []
      setModels(ids)
      if (!f.model && ids.length) setForm((x) => (x.model ? x : { ...x, model: ids[0] }))
      if (announce) note(modelsLoadedText(ids))
    } catch (err: any) {
      if (announce) note(errorText(err.message) || T('admin.llm.modelsFailed'), true)
    }
  }
  function fill(settings: any, list: ProviderSpec[]) {
    const f = formFromSettings(settings, list)
    setCurrent(settings)
    setForm(f)
    loadModels(f, false)
  }
  const set = (p: Partial<LlmForm>) => setForm((f) => ({ ...f, ...p }))

  useEffect(() => {
    const flag = new URLSearchParams(location.search).get('oauth')
    fetchAdminOrRedirect('/admin/api/llm').then((data) => {
      setCatalog(data.catalog || [])
      fill(data.settings, data.catalog || [])
      const back = oauthReturnNote(flag)
      if (back) { note(back.msg, back.isErr); history.replaceState({}, '', LLM_PATH) }
    }).catch((err) => { if (err.status !== 401) note(err.message || T('admin.llm.loadFailed'), true) })
    return () => clearTimeout(timer.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once
  }, [])

  const spec = specOf(catalog, form.provider)
  const vis = visibleFields(form.provider, validAuth(form.auth, spec), spec)
  const connected = chatgptConnected(current)
  const scheduleModels = () => { clearTimeout(timer.current); timer.current = setTimeout(() => loadModels(formRef.current, false), MODEL_RELOAD_DEBOUNCE_MS) }

  async function post(path: string, body: unknown, okKey: string, failKey: string, codes = false) {
    try {
      const data = await fetchAdminOrRedirect(path, { method: 'POST', ...(body ? { body: JSON.stringify(body) } : {}) })
      const list = data.catalog || catalog
      if (data.catalog) setCatalog(data.catalog)
      fill(data.settings, list)
      note(T(okKey))
    } catch (err: any) {
      note((codes ? errorText(err.message) : err.message) || T(failKey), true)
    }
  }
  function save() {
    const payload = settingsPayload(form)
    if (!payload.ok) { note(payload.error, true); return }
    post('/admin/api/llm', payload.body, 'admin.llm.saved', 'admin.llm.saveFailed', true)
  }
  async function connectChatgpt() {
    note('')
    try {
      const data = await fetchAdminOrRedirect('/admin/api/llm/oauth/start?provider=chatgpt')
      if (data.url) location.href = data.url
      else note(T('admin.llm.noConnectUrl'), true)
    } catch (err: any) { note(err.message || T('admin.llm.connectStartFailed'), true) }
  }

  return (
    <AdminShell page="llm" title={T('admin.llm.title')}>
      <section className={cn(panel, 'admin-panel flex max-w-3xl flex-col')}>
        <div className="flex flex-col gap-1 border-b border-hairline-soft p-5">
          <p className="m-0 text-body-sm text-ink-muted">{T('admin.llm.desc')}</p>
        </div>
        <div className="flex flex-col gap-4 p-5">
          <div id="status" role="status" hidden={!status} className={cn('status-note rounded-xl px-3.5 py-2.5 text-body-sm', status?.isErr ? 'status-note--err bg-danger/10 text-danger' : 'bg-success/10 text-success')}>{status?.msg}</div>
          <div className={row}>
            <label htmlFor="provider" className={label}>{T('admin.llm.provider')}</label>
            <select id="provider" className={input} value={form.provider} onChange={(e) => {
              const next = applyProviderDefaults({ ...form, provider: e.target.value }, specOf(catalog, e.target.value))
              setForm(next)
              loadModels(next, false)
            }}>
              {catalog.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select>
          </div>
          <div className={row}>
            <label htmlFor="model" className={label}>{T('admin.llm.model')}</label>
            <div className="model-pick flex gap-2">
              <input id="model" className={input} type="text" list="modelOptions" placeholder="gpt-4o-mini" autoComplete="off" value={form.model} onChange={(e) => set({ model: e.target.value })} />
              <datalist id="modelOptions">{models.map((id) => <option key={id} value={id} />)}</datalist>
              <Button tone="secondary" size="sm" className="h-10 shrink-0" id="reloadModels" onClick={() => loadModels(form, true)}>{T('admin.llm.reloadModels')}</Button>
            </div>
          </div>
          <div className={row} id="authField" hidden={vis.hideAuth}>
            <label htmlFor="auth" className={label}>{T('admin.llm.auth')}</label>
            <select id="auth" className={input} value={validAuth(form.auth, spec)} onChange={(e) => {
              const next = { ...form, auth: e.target.value }
              setForm(shouldFillAzureScope(next) ? { ...next, scope: AZURE_SCOPE } : next)
            }}>
              {vis.modes.includes('api_key') && <option value="api_key">{T('admin.llm.auth.apiKey')}</option>}
              {vis.modes.includes('oauth') && <option value="oauth">{T('admin.llm.auth.oauth')}</option>}
            </select>
          </div>
          <div className={cn(row, 'api-key-field')} id="apiKeyField" hidden={vis.hideApiKey}>
            <label htmlFor="apiKey" className={label}>{T('admin.llm.apiKey')}</label>
            <input id="apiKey" className={input} type="password" placeholder="sk-…" autoComplete="off" value={form.apiKey} onChange={(e) => set({ apiKey: e.target.value })} onBlur={scheduleModels} />
          </div>
          <div className="oauth-fields flex flex-col gap-4" id="oauthFields" hidden={vis.hideOauth}>
            <div className="oauth-chatgpt flex flex-col gap-3 rounded-xl bg-surface-2/60 p-4" id="chatgptOAuth" hidden={vis.hideChatgptOAuth}>
              <p className="m-0 text-body-sm text-ink-muted">{T('admin.llm.chatgptHint')}</p>
              <div className="oauth-chatgpt__row flex flex-wrap items-center gap-2">
                <Button tone="primary" size="sm" id="chatgptConnect" disabled={connected} onClick={connectChatgpt}>{T('admin.llm.chatgptConnect')}</Button>
                <Button tone="ghost" size="sm" id="chatgptDisconnect" hidden={!connected} onClick={() => post('/admin/api/llm/oauth/disconnect', null, 'admin.llm.chatgptDisconnected', 'admin.llm.disconnectFailed')}>{T('admin.llm.chatgptDisconnect')}</Button>
                <span className="oauth-status text-caption text-success" id="chatgptStatus" hidden={!connected}>{connected ? T('admin.llm.chatgptConnected') : ''}</span>
              </div>
            </div>
            <details className="oauth-advanced flex flex-col gap-3" id="oauthAdvanced" hidden={vis.hideOauthAdvanced} open={vis.openOauthAdvanced} key={String(vis.openOauthAdvanced)}>
              <summary className="mb-3 cursor-pointer text-body-sm text-ink-muted">{T('admin.llm.oauthAdvanced')}</summary>
              <div className="flex flex-col gap-3">
                {([['clientId', 'admin.llm.clientId', 'text', ''], ['clientSecret', 'admin.llm.clientSecret', 'password', ''], ['tenantId', 'admin.llm.tenantId', 'text', 'Azure Entra'],
                  ['tokenUrl', 'admin.llm.tokenUrl', 'text', 'https://…/oauth2/token'], ['scope', 'admin.llm.scope', 'text', AZURE_SCOPE]] as const).map(([id, key, type, ph]) => (
                  <div key={id} className={row}>
                    <label htmlFor={id} className={label}>{T(key)}</label>
                    <input id={id} className={input} type={type} placeholder={ph} autoComplete="off" value={form[id]} onChange={(e) => set({ [id]: e.target.value })} />
                  </div>
                ))}
                <span className="font-mono text-micro text-ink-muted">{T('admin.llm.oauthHint')}</span>
              </div>
            </details>
          </div>
          <div className={row} id="baseUrlField" hidden={vis.hideBaseUrl}>
            <label htmlFor="baseUrl" className={label}>{T('admin.llm.baseUrl')}</label>
            <input id="baseUrl" className={input} type="text" placeholder="https://…" value={form.baseUrl} onChange={(e) => set({ baseUrl: e.target.value })} onBlur={scheduleModels} />
          </div>
          <div className="field flex flex-col gap-1.5">
            <label htmlFor="extra" className={label}>{T('admin.llm.extra')}</label>
            <textarea id="extra" spellCheck={false} placeholder='{"api_version":"2024-02-15-preview"}' value={form.extra} onChange={(e) => set({ extra: e.target.value })}
              className="min-h-28 w-full rounded-lg border border-hairline bg-canvas px-3 py-2.5 font-mono text-caption text-ink outline-none focus-visible:border-brand" />
            <span className="font-mono text-micro text-ink-muted">{T('admin.llm.extraHint')}</span>
          </div>
        </div>
        <div className="admin-form__foot flex gap-2 border-t border-hairline-soft p-4">
          <Button tone="primary" id="save" onClick={save}>{T('admin.llm.save')}</Button>
          <Button tone="ghost" id="reset" onClick={() => (current ? fill(current, catalog) : note(''))}>{T('admin.llm.reset')}</Button>
        </div>
      </section>
    </AdminShell>
  )
}
