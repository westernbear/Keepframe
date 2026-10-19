// Pure chat-log logic of the agent page, ported from keepframe/web/static/js/agent.js.
import { T, Tf } from '@/i18n'
import { readFileAsDataUrl } from '@/lib/files'

const KEEP_PASS_RATE = 0.95
const CONFIDENCE_PERCENT = 100
const TOOL_PROMPTS = ['analyze', 'correct', 'set_keep', 'edit', 'render', 'verify', 'export', 'report']

/** fillToolPrompt: the composer text a tool-menu item fills in ("" for an unknown tool). */
export function toolPrompt(tool: string) {
  return TOOL_PROMPTS.includes(tool) ? T(`agent.toolPrompt.${tool}`) : ''
}

export function toolStatus(name: string, ok: boolean) {
  if (name === 'edit') return T(ok ? 'agent.toolApplied' : 'agent.toolFailed')
  const tool = T(`agent.tool.${name === 'set_keep' ? 'keep' : name}`)
  return Tf(ok ? 'agent.toolCompleted' : 'agent.toolActionFailed', { tool })
}

/** appendToolCall: the arguments a tool card prints; a correction mask's base64 is never shown. */
export function toolCallLogArgs(name: string, args) {
  return name === 'correct' && args && args.args && typeof args.args === 'object' && 'mask_png_base64' in args.args
    ? { ...args, args: { ...args.args, mask_png_base64: T('agent.correctionMaskHidden') } }
    : args
}

/** appendToolCall: a tool message is shown only when the chat reply does not already say it. */
export function toolCallAddsInformation(message: string | undefined, reply: string) {
  const text = (message || '').trim()
  return Boolean(text) && !reply.trim().includes(text)
}

function isKeepPassed(verify) {
  return verify.passed !== false && verify.keep_pass_rate >= KEEP_PASS_RATE
}

export interface VerifyChip { tone: 'pass' | 'fail' | 'warn'; total: number; keepPassed: boolean; rate: number; error: number }

/** appendVerify: counts win over keep_results (which the server may truncate); no keep rules is a warning. */
export function verifyChip(verify): VerifyChip | null {
  if (!verify) return null
  const total = verify.keep_total ?? (verify.keep_results || []).length
  const failed = verify.keep_failed ?? (verify.keep_results || []).filter((r) => !r.passed).length
  const rate = total > 0 ? (total - failed) / total : 0
  const keepPassed = total > 0 && failed === 0 && isKeepPassed({ ...verify, keep_pass_rate: rate })
  const tone = total === 0 ? 'warn' : keepPassed ? 'pass' : 'fail'
  return { tone, total, keepPassed, rate, error: verify.layer_max_err_px ?? 0 }
}

/** paintVerifyChip: text and title (also the aria-label) in the current language. */
export function verifyChipText({ total, keepPassed, rate, error }: VerifyChip) {
  const key = keepPassed ? 'agent.verifyPassed' : 'agent.verifyFailed'
  const text = total === 0 ? T('agent.verifyNoKeep') : Tf(key, { rate: Math.round(rate * CONFIDENCE_PERCENT) })
  return { text, title: Tf('agent.verifyDetails', { summary: text, n: total, error: error.toFixed(2) }) }
}

// R61: Keepframe's own summary of a background picture from the attachment stays in the version history and the
// tool card, never a chat bubble; the LLM agent writes the chat replies.
function attachedBackground(intent) {
  return ((intent && intent.targets) || []).some((t) => t && t.property === 'background' && t.value === 'attachment')
}

export function confirmedLine(res) {
  return attachedBackground(res.intent) ? T('agent.applied') : (res.summary || T('agent.applied'))
}

export function chatReply(turn) {
  const reply = (turn.reply || '').trim()
  const own = (turn.results || []).some((r) => r && r.payload && attachedBackground(r.payload.intent)
    && (r.message || '').trim() === reply)
  return own ? '' : (turn.reply || '')
}

export function payloadOf(results, key: string) {
  const hit = results.find((r) => r.payload && r.payload[key])
  return hit && hit.payload ? hit.payload[key] : null
}

/** paintTurn + paintToolCalls: what one agent turn shows. `pending` is set only for an actionable pending turn. */
export function turnView(turn, actionable = true) {
  const reply = chatReply(turn)
  return {
    toolCalls: (turn.tool_calls || []).map((tc, i) => ({
      name: tc.name, args: tc.arguments, result: (turn.results && turn.results[i]) || { ok: false, message: '' },
    })),
    reply,
    verify: verifyChip((turn.results || []).map((r) => r.payload && r.payload.verify).find(Boolean)),
    pending: turn.status === 'pending' && actionable ? pendingAction(turn) : null,
    error: turn.status === 'error' ? (turn.reply || T('agent.failed')) : null,
  }
}

/** paintPending: which confirmation a pending turn asks for, checked in the legacy order. */
export function pendingAction(turn) {
  const renderPlan = payloadOf(turn.results, 'render_plan')
  if (renderPlan) return { kind: 'render' as const, renderPlan }
  const keepChange = payloadOf(turn.results, 'keep_change')
  if (keepChange) return { kind: 'keep' as const, keepChange }
  const correction = payloadOf(turn.results, 'correction')
  if (correction) return { kind: 'correction' as const, correction }
  return {
    kind: 'edit' as const,
    intent: payloadOf(turn.results, 'intent'),
    plan: payloadOf(turn.results, 'plan'),
    needsChoice: Boolean(turn.needs_choice),
    needsConfirm: Boolean(turn.needs_confirm),
  }
}

/** appendChoices: each conflict starts on its first choice. */
export function defaultEditChoices(plan): Record<string, string> {
  return Object.fromEntries(((plan && plan.conflicts) || []).filter((c) => (c.choices || []).length).map((c) => [c.id, c.choices[0]]))
}

export const MAX_ATTACHMENTS = 4 // keepframe/edit/intent.py MAX_ATTACHMENTS

/** The confirmed edit: `attachments` in the order shown; a target's 'attachment:N' picks the Nth. */
export function confirmEditBody(f: { project; scene; v; prompt; attachments: string[]; element; intent }, choices: Record<string, string> = {}) {
  return {
    project: f.project, scene: f.scene, v: f.v, prompt: f.prompt, attachments: f.attachments, element: f.element,
    intent: f.intent, confirm: true, choices,
  }
}

function editErrorMessage(error?: string) {
  return error === 'attachment_required' ? T('agent.attachmentRequired')
    : error === 'invalid_glb' ? T('agent.invalidGlb')
    : error === 'unsafe_svg' || error === 'invalid_svg' ? T('agent.invalidSvg')
    : (error || T('agent.failed'))
}

/** applyConfirmedEdit: needs_choice keeps the pending intent (unless the server sent a new one); anything else clears it. */
export function confirmedEditOutcome(res, pendingIntent) {
  if (res.status === 'needs_choice') return { kind: 'choice' as const, intent: res.intent || pendingIntent, plan: res.plan }
  if (res.status === 'done' && res.version) {
    return { kind: 'done' as const, intent: null, version: res.version.id, verify: verifyChip(res.verify), line: confirmedLine(res) }
  }
  return { kind: 'error' as const, intent: null, message: editErrorMessage(res.error) }
}

/** readChatAttachment: browsers often give .glb/.svg an empty or generic MIME; the server checks it. */
export async function readChatAttachment(file: File) {
  const data = (await readFileAsDataUrl(file))!
  const mime = /\.glb$/i.test(file.name) ? 'model/gltf-binary'
    : /\.svg$/i.test(file.name) ? 'image/svg+xml' : null
  return mime ? data.replace(/^data:[^,]*,/, `data:${mime};base64,`) : data
}
