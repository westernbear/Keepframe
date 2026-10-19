import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/app/button'
import { useI18n } from '@/i18n'
import { approveRenderPlan, fetchRenderPlans, fetchRenderState, postRenderPlan } from '@/lib/api'
import {
  RENDER_POLL_MS, isCurrentPlan, modeForBackend, planOptionLabel, renderArtifactLinks, renderCardView, renderControls,
  renderPlanId, renderWarningLines, shouldPollRender, upsertRenderPlan,
} from '@/lib/agent-render'
import { cn } from '@/lib/utils'
import { Disclosure, card } from './ui'

const select = 'h-10 rounded-lg border border-hairline bg-canvas px-3 text-body-sm text-ink outline-none focus-visible:border-brand disabled:opacity-60'

/** Render-plan state of the agent page: the selected payload, every plan seen, polling while one runs. */
export function useRenderCard({ projectId, sceneId, getVersion, setBanner }: { projectId: string | null; sceneId: string; getVersion: () => string | null; setBanner: (m: string, e?: boolean) => void }) {
  const { T } = useI18n()
  const [payload, setPayloadState] = useState<any>(null)
  const [plans, setPlans] = useState<any[]>([])
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState('preview')
  const [backend, setBackend] = useState('native')
  const current = useRef<any>(null)
  const timer = useRef<any>(null)

  function setPayload(next: any) {
    current.current = next
    setPayloadState(next)
    setPlans((list) => upsertRenderPlan(list, next))
    if (next?.plan) { setBackend(next.plan.backend); setMode(next.plan.mode) }
    schedule(next)
  }

  function schedule(next: any) {
    clearTimeout(timer.current)
    timer.current = null
    if (!shouldPollRender(next)) return
    timer.current = setTimeout(async () => {
      timer.current = null
      try { await refreshState(); setBanner('') }
      catch (err: any) { setBanner(err.message || T('agent.failed'), true); schedule(current.current) }
    }, RENDER_POLL_MS)
  }

  async function refreshState() {
    const planId = current.current?.plan?.id
    if (!planId) return
    const next = await fetchRenderState(projectId!, planId)
    if (!isCurrentPlan(current.current, planId)) return
    setPayload(next)
  }

  async function select(plan: any) {
    const planId = renderPlanId(plan)
    if (!planId) return
    setBusy(true)
    try { setPayload(await fetchRenderState(projectId!, planId)) } finally { setBusy(false) }
  }

  async function load() {
    const response = await fetchRenderPlans(projectId!, sceneId, getVersion())
    const list = response.plans || []
    setPlans(list)
    setPayload(list[list.length - 1] || null)
  }

  async function run(work: () => Promise<any>) {
    setBusy(true)
    setBanner('')
    try { setPayload(await work()) }
    catch (err: any) { setBanner(err.message || T('agent.failed'), true) }
    finally { setBusy(false) }
  }

  useEffect(() => () => clearTimeout(timer.current), [])

  return {
    payload, plans, busy, mode, backend, load, select,
    setMode, setBackend: (b: string) => { setBackend(b); setMode((m) => modeForBackend(b, m)) },
    choose: (planId: string) => {
      const picked = plans.find((p) => p.plan && p.plan.id === planId)
      if (!picked) { current.current = null; setPayloadState(null); schedule(null); return }
      select(picked).catch((err) => setBanner(err.message || T('agent.failed'), true))
    },
    create: () => run(() => postRenderPlan({ project: projectId, scene: sceneId, version: getVersion(), backend, mode })),
    approve: () => current.current && run(() => approveRenderPlan(current.current.plan.id, {
      project: projectId, digest: current.current.plan.digest, revision: current.current.state.revision,
    })),
  }
}

export type RenderCardState = ReturnType<typeof useRenderCard>

export function RenderCard({ r, projectId, reviewStatus }: { r: RenderCardState; projectId: string | null; reviewStatus?: string | null }) {
  const { T } = useI18n()
  const v = renderCardView(r.payload)
  const controls = renderControls({ payload: r.payload, plans: r.plans, busy: r.busy, mode: r.mode, backend: r.backend, reviewStatus })
  const links = renderArtifactLinks(r.payload?.plan, r.payload, projectId || '')
  const warnings = renderWarningLines(r.payload?.warnings)
  return (
    <section className={card} id="render-card" aria-labelledby="render-card-title">
      <Disclosure id="render-card" storageKey="kf.agent.renderCardOpen" defaultOpen titleKey="agent.renderTitle"
        status={
          <span id="render-status" role="status" aria-live="polite" hidden={!v.hasPlan} title={v.statusTitle}
            className={cn('render-card__status rounded-full px-2.5 py-1 font-mono text-micro',
              v.tone === 'failed' ? 'render-card__status--failed bg-danger/10 text-danger' : v.tone === 'active' ? 'render-card__status--active bg-brand/10 text-brand-soft' : 'bg-surface-2 text-ink-muted')}>
            {v.statusText}
          </span>
        }>
        <div className="render-card__setup flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-1 text-caption text-ink-muted">{T('agent.renderMode')}
            <select id="render-mode" className={select} value={r.mode} onChange={(e) => r.setMode(e.target.value)}>
              <option value="preview">{T('agent.renderPreview')}</option>
              <option value="final">{T('agent.renderFinal')}</option>
            </select>
          </label>
          <label className="flex flex-col gap-1 text-caption text-ink-muted">{T('agent.renderBackend')}
            <select id="render-backend" className={select} value={r.backend} onChange={(e) => r.setBackend(e.target.value)}>
              <option value="native">Native</option>
              <option value="lottie">Lottie</option>
            </select>
          </label>
          <label id="render-plan-field" className="flex min-w-0 flex-col gap-1 text-caption text-ink-muted" hidden={r.plans.length <= 1}>{T('agent.renderPlan')}
            <select id="render-plan-selector" className={cn(select, 'max-w-[260px]')} disabled={controls.selectorDisabled} value={r.payload?.plan?.id || ''} onChange={(e) => r.choose(e.target.value)}>
              {r.plans.map((p) => <option key={p.plan.id} value={p.plan.id}>{planOptionLabel(p)}</option>)}
            </select>
          </label>
          <Button tone={v.awaitsApproval ? 'secondary' : 'primary'} size="sm" className="h-10" id="render-create" disabled={controls.createDisabled} onClick={r.create}>{T('agent.renderCreate')}</Button>
        </div>
        <div className="render-card__facts text-caption" id="render-output-field" hidden={!v.showOutputs}>
          <div className="flex gap-2"><span className="text-ink-muted">{T('agent.renderOutputs')}</span><strong id="render-outputs" className="font-mono font-medium">{v.outputs.join(', ')}</strong></div>
        </div>
        <div id="render-live-status" className="render-card__live-status flex flex-col gap-2" role="status" aria-live="polite" aria-atomic="true">
          <div className="render-card__facts text-caption" id="render-reason-field" hidden={!v.showReason}>
            <div className="flex flex-col gap-0.5"><span className="text-ink-muted">{T('agent.renderReason')}</span><strong id="render-reason" className="font-medium text-danger">{v.reason}</strong></div>
          </div>
          <ul id="render-warnings" className="render-card__warnings m-0 flex list-none flex-col gap-1 p-0 text-caption text-warning" hidden={!warnings.length}>
            {warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
          <div id="render-artifacts" className="render-artifacts flex flex-wrap gap-2" aria-label={T('agent.renderArtifacts')} hidden={!links.length}>
            {links.map((l) => <a key={l.href} href={l.href} rel="noopener" className="rounded-full bg-surface-2 px-3 py-1.5 text-caption text-brand-soft hover:bg-surface-3">{l.label}</a>)}
          </div>
        </div>
        <div className="render-card__actions" id="render-approval" hidden={!v.awaitsApproval}>
          <Button tone="primary" size="sm" id="render-approve" disabled={controls.approveDisabled} onClick={r.approve}>{T('agent.renderApprove')}</Button>
        </div>
      </Disclosure>
    </section>
  )
}
