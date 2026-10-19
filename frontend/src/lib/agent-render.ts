// Render-plan card rules of the agent page, ported from keepframe/web/static/js/agent.js.
import { Tf, errorText } from '@/i18n'
import { lottieArtifactUrl, nativeArtifactUrl } from '@/lib/api'

/** scheduleRenderPoll: delay between polls of a queued/running plan; a failed poll is retried after the same delay. */
export const RENDER_POLL_MS = 1500

export function shouldPollRender(payload) {
  return Boolean(payload && payload.plan && ['queued', 'running'].includes(payload.status))
}

/** refreshRenderState: a poll answer is dropped when another plan was selected meanwhile. */
export function isCurrentPlan(payload, planId: string) {
  return Boolean(payload && payload.plan && payload.plan.id === planId)
}

export function renderPayloadStatus(payload) {
  return (payload && (payload.status || (payload.state && payload.state.status))) || '—'
}

/** selectRenderPlan accepts a render payload or a bare plan. */
export function renderPlanId(plan) {
  return plan && plan.plan ? plan.plan.id : plan && plan.id
}

/** upsertRenderPlan, returning a new list instead of mutating it (React state). */
export function upsertRenderPlan(plans: any[], payload) {
  if (!payload || !payload.plan || !payload.plan.id) return plans
  const index = plans.findIndex((item) => item.plan && item.plan.id === payload.plan.id)
  return index < 0 ? [...plans, payload] : plans.map((item, i) => (i === index ? payload : item))
}

function shortDigest(value) {
  return value ? `${value.slice(0, 10)}…` : '—'
}

/** paintPlanSelector: one option label. */
export function planOptionLabel(payload) {
  const plan = payload.plan
  const backend = plan.backend === 'lottie' ? 'Lottie' : 'Native'
  return `${backend} ${plan.mode} (${renderPayloadStatus(payload)}, ${shortDigest(plan.id)})`
}

/** renderStatusClass: '' | 'failed' | 'active' (the render-card__status--* modifier). */
export function renderStatusTone(status: string) {
  if (status === 'failed') return 'failed'
  return status && !['awaiting_approval', 'approved'].includes(status) ? 'active' : ''
}

function expectedOutputs(plan): string[] {
  const outputs = plan && plan.artifact_contract && plan.artifact_contract.outputs
  return Array.isArray(outputs) ? outputs : []
}

/** paintArtifacts: download links (artifact labels come from the server: render as text). */
export function renderArtifactLinks(plan, payload, projectId: string) {
  if (!plan) return []
  return ((payload && payload.artifacts) || []).filter((a) => a && a.kind).map((artifact) => ({
    href: plan.backend === 'lottie' ? lottieArtifactUrl(plan.id, projectId) : nativeArtifactUrl(plan.id, artifact.kind, projectId),
    label: artifact.label || Tf('agent.renderDownload', { kind: artifact.kind.toUpperCase() }),
  }))
}

const RENDER_WARNINGS = {
  font_substituted: (w) => Tf('agent.renderFontSubstituted', { family: w.family, used: w.used }),
  font_file_missing: (w) => Tf('agent.renderFontFileMissing', { family: w.family }),
}

/** paintRenderWarnings: known warnings only (family names come from font files: text only). */
export function renderWarningLines(warnings): string[] {
  return (Array.isArray(warnings) ? warnings : []).filter((w) => w && RENDER_WARNINGS[w.kind]).map((w) => RENDER_WARNINGS[w.kind](w))
}

/** updateRenderControls. `reviewStatus` is the scene state's status (final renders need an approved scene). */
export function renderControls({ payload, plans, busy, mode, backend, reviewStatus }:
  { payload; plans: any[]; busy: boolean; mode: string; backend: string; reviewStatus?: string | null }) {
  const plan = payload && payload.plan
  const planState = payload && payload.state
  const finalBlocked = mode === 'final' && reviewStatus !== 'approved'
  return {
    selectorDisabled: busy || !plans.length,
    createDisabled: busy || finalBlocked || (backend === 'lottie' && mode !== 'final'),
    approveDisabled: busy || !plan || !planState || planState.status !== 'awaiting_approval',
  }
}

/** The backend select's change handler: Lottie renders only final. */
export function modeForBackend(backend: string, mode: string) {
  return backend === 'lottie' ? 'final' : mode
}

/** paintRenderCard minus the DOM. */
export function renderCardView(payload) {
  const plan = payload && payload.plan
  const planState = payload && payload.state
  const hasPlan = Boolean(plan)
  const status = hasPlan ? renderPayloadStatus(payload) : ''
  const jobError = payload && payload.job && payload.job.error
  const outputs = expectedOutputs(plan)
  const reason = errorText(jobError) || payload?.error || ''
  return {
    hasPlan,
    status,
    statusText: hasPlan ? `${status} (${plan.version_id})` : '',
    statusTitle: planState ? `${status}, r${planState.revision}` : status,
    tone: renderStatusTone(status),
    reason,
    showReason: status === 'failed' && Boolean(reason),
    outputs,
    showOutputs: hasPlan && outputs.length > 0,
    awaitsApproval: hasPlan && planState?.status === 'awaiting_approval',
  }
}
