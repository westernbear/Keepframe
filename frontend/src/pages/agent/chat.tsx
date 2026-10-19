import { BarChart3, ChevronDown, Download, FileText, Lock, Paperclip, PencilLine, Play, ShieldCheck, Wrench, Workflow, X } from 'lucide-react'
import { motion, useReducedMotion } from 'motion/react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Reasoning } from '@/components/ai-elements/reasoning'
import { Markdown } from '@/components/app/markdown'
import AILoader from '@/components/smoothui/ai-loader'
import { ShimmeringText } from '@/components/unlumen-ui/shimmering-text'
import { Button } from '@/components/app/button'
import { useI18n } from '@/i18n'
import { toolCallAddsInformation, toolCallLogArgs, toolStatus, verifyChipText, type VerifyChip } from '@/lib/agent-chat'
import { confirmGuard } from '@/lib/agent-corrections'
import { cn } from '@/lib/utils'

export type Entry = { fresh?: boolean } & (
  | { id: number; kind: 'user' | 'agent'; text: string }
  | { id: number; kind: 'reasoning'; steps: string[]; seconds: number | null }
  | { id: number; kind: 'tool'; name: string; args: any; result: any; reply: string }
  | { id: number; kind: 'verify'; chip: VerifyChip }
  | { id: number; kind: 'choices'; plan: any }
  | { id: number; kind: 'confirm'; onConfirm: () => Promise<boolean | undefined> })

const EASE_OUT = [0.23, 1, 0.32, 1] as const

/** The assistant's slot while a turn runs: what it is doing, a breathing loader and the elapsed time. */
function Thinking({ label, steps }: { label: string; steps: string[] }) {
  const { T } = useI18n()
  const reduced = useReducedMotion()
  const trace = steps.filter((s) => s.trim()).join('\n\n')
  const box = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => { if (box.current) box.current.scrollTop = box.current.scrollHeight }, [trace])
  return (
    <motion.div className="msg msg--thinking flex flex-col items-start gap-1" data-thinking
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: 4, filter: 'blur(4px)' }} animate={{ opacity: 1, y: 0, filter: 'blur(0px)' }}
      transition={{ duration: 0.24, ease: EASE_OUT }}>
      <div className="msg__label text-micro text-ink-muted">{T('agent.assistant')}</div>
      <div className="flex min-h-8 items-center text-body-sm">
        <AILoader variant="dots" showElapsed srLabel={label} className="gap-2.5 text-caption text-ink-muted"
          label={reduced ? <span className="text-ink">{label}</span>
            : <motion.span key={label} initial={{ opacity: 0, filter: 'blur(3px)' }} animate={{ opacity: 1, filter: 'blur(0px)' }} transition={{ duration: 0.2 }}>
                <ShimmeringText text={label} duration={1.6} repeatDelay={0.2} startOnView={false} className="text-body-sm" />
              </motion.span>} />
      </div>
      {trace && (
        <div ref={box} className="thinking__live max-h-40 w-full overflow-y-auto border-l border-hairline py-0.5 pl-3 text-caption leading-relaxed text-ink-muted" aria-live="off">
          <Markdown text={trace} />
        </div>
      )}
    </motion.div>
  )
}

const TOOLS = [
  ['analyze', 'agent.tool.analyze', BarChart3], ['correct', 'agent.tool.correct', Wrench], ['set_keep', 'agent.tool.keep', Lock],
  ['edit', 'agent.tool.edit', PencilLine], ['render', 'agent.tool.render', Play], ['verify', 'agent.tool.verify', ShieldCheck],
  ['export', 'agent.tool.export', Download], ['report', 'agent.tool.report', FileText],
] as const

function ConfirmButton({ onConfirm, label }: { onConfirm: () => Promise<boolean | undefined>; label: string }) {
  const [guard] = useState(() => confirmGuard(onConfirm))
  const [disabled, setDisabled] = useState(false)
  return (
    <Button tone="primary" size="sm" className="self-start" disabled={disabled}
      onClick={async () => { setDisabled(true); await guard.click(); setDisabled(guard.disabled) }}>{label}</Button>
  )
}

function Choices({ plan, choices, onChoose }: { plan: any; choices: Record<string, string>; onChoose: () => void }) {
  const { T } = useI18n()
  const [, setTick] = useState(0)
  return (
    <div className="agent-choice flex flex-col gap-2 rounded-lg border border-hairline bg-surface-1 p-3">
      {(plan?.conflicts || []).map((c: any) => (
        <fieldset key={c.id} className="m-0 flex flex-col gap-1 border-0 p-0">
          <legend className="agent-choice__reason mb-1 font-mono text-caption text-ink-muted">{c.reason || c.element}</legend>
          {(c.choices || []).map((ch: string) => (
            <label key={ch} className="inline-flex min-h-9 cursor-pointer items-center gap-2 text-body-sm">
              <input type="radio" name={`agent-${c.id}`} value={ch} checked={choices[c.id] === ch} className="accent-[var(--brand)]"
                onChange={() => { choices[c.id] = ch; setTick((n) => n + 1) }} />
              <span>{T(`review.editChoice.${ch}`)}</span>
            </label>
          ))}
        </fieldset>
      ))}
      <Button tone="primary" size="sm" className="self-start" onClick={onChoose}>{T('agent.choose')}</Button>
    </div>
  )
}

function ToolCall({ e }: { e: Extract<Entry, { kind: 'tool' }> }) {
  const { T } = useI18n()
  const ok = Boolean(e.result.ok)
  return (
    <details className="toolcall group/tc text-caption" data-tool={e.name} data-ok={String(ok)}>
      <summary className="flex min-h-7 cursor-pointer list-none items-center gap-2 text-ink-muted hover:text-ink [&::-webkit-details-marker]:hidden">
        <span className="toolcall__head inline-flex items-center gap-2">
          <span aria-hidden className={cn('size-1.5 rounded-full', ok ? 'bg-success' : 'bg-danger')} />
          <span className={cn('toolcall__status', ok ? 'toolcall__status--ok text-ink' : 'toolcall__status--fail text-danger')}>{toolStatus(e.name, ok)}</span>
        </span>
        <span data-i18n="agent.details" className="text-micro">{T('agent.details')}</span>
        <ChevronDown aria-hidden className="size-3 transition-transform duration-150 group-open/tc:rotate-180" />
      </summary>
      <div className="mt-1 ml-[3px] flex flex-col gap-1 border-l border-hairline pl-3.5 pb-1">
        <span className="toolcall__name block font-mono text-micro text-ink-muted">{e.name}</span>
        <div className="toolcall__args font-mono text-micro break-all text-ink-muted">{JSON.stringify(toolCallLogArgs(e.name, e.args) || {}, null, 0)}</div>
        {toolCallAddsInformation(e.result.message, e.reply) && <div className="toolcall__msg whitespace-pre-line text-caption text-ink">{e.result.message}</div>}
      </div>
    </details>
  )
}

export function ChatLog({ entries, empty, choices, onChoose, thinking, liveSteps = [] }: { entries: Entry[]; empty: string | null; choices: Record<string, string>;
  onChoose: () => void; thinking: string | null; liveSteps?: string[] }) {
  const { T, Tf } = useI18n()
  const reduced = useReducedMotion()
  const log = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => { if (log.current) log.current.scrollTop = log.current.scrollHeight }, [entries.length, thinking, liveSteps.length])
  // Only entries painted by a live turn arrive; replayed history is simply there.
  const arrive = (e: Entry, node: React.ReactNode) => !e.fresh ? node : (
    <motion.div key={e.id} className="flex flex-col"
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: e.kind === 'user' ? 8 : 4, filter: 'blur(4px)' }}
      animate={{ opacity: 1, y: 0, filter: 'blur(0px)' }} transition={{ duration: 0.22, ease: EASE_OUT }}>{node}</motion.div>
  )
  return (
    <div className="agent-chat__log flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-4 pb-4 pt-2" id="agent-log" ref={log}>
      <p className="agent-empty m-0 text-body-sm text-ink-muted" id="agent-empty" hidden={!empty}>{empty}</p>
      {entries.map((e) => arrive(e, ((): React.ReactNode => {
        if (e.kind === 'user' || e.kind === 'agent') return (
          <div key={e.id} className={cn('msg flex flex-col gap-1', e.kind === 'user' ? 'msg--user mt-4 items-end first:mt-2' : 'msg--agent items-stretch')}>
            <div className={cn('msg__label text-micro text-ink-muted', e.kind === 'user' && 'sr-only')}>{T(e.kind === 'user' ? 'agent.you' : 'agent.assistant')}</div>
            <div className={cn('msg__body text-body-sm leading-relaxed break-words', e.kind === 'user'
              ? 'max-w-[88%] rounded-md border border-hairline bg-surface-2 px-3 py-2 whitespace-pre-line text-ink' : 'text-ink')}>
              {e.kind === 'agent' ? <Markdown text={e.text} /> : e.text}
            </div>
          </div>
        )
        if (e.kind === 'reasoning') return (
          <Reasoning key={e.id} steps={e.steps} label={e.seconds == null ? T('agent.reasoning') : Tf('agent.reasoningFor', { n: e.seconds })} />
        )
        if (e.kind === 'tool') return <ToolCall key={e.id} e={e} />
        if (e.kind === 'verify') {
          const { text, title } = verifyChipText(e.chip)
          return (
            <span key={e.id} title={title} aria-label={title}
              className={cn('verify-chip inline-flex min-h-7 items-center gap-2 self-start text-caption', `verify-chip--${e.chip.tone}`,
                e.chip.tone === 'pass' ? 'text-ink-muted' : e.chip.tone === 'warn' ? 'text-warning' : 'text-danger')}>
              <ShieldCheck aria-hidden className={cn('size-3.5', e.chip.tone === 'pass' ? 'text-success' : 'text-current')} />{text}</span>
          )
        }
        if (e.kind === 'choices') return <Choices key={e.id} plan={e.plan} choices={choices} onChoose={onChoose} />
        const c = e as Extract<Entry, { kind: 'confirm' }>
        return <ConfirmButton key={c.id} onConfirm={c.onConfirm} label={T('agent.confirm')} />
      })()))}
      {thinking && <Thinking label={thinking} steps={liveSteps} />}
    </div>
  )
}

/** Message box, tools menu (fills a prompt) and the attachment picker. */
export function Composer({ onSend, sending, attachments, onAttach, onRemoveAttachment }: { onSend: (text: string) => void; sending: boolean;
  attachments: string[]; onAttach: (files: File[]) => void; onRemoveAttachment: (index: number) => void }) {
  const { T, Tf } = useI18n()
  const input = useRef<HTMLTextAreaElement>(null)
  const toggle = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const attach = useRef<HTMLInputElement>(null)
  const [open, setOpen] = useState<{ index: number; left: number; bottom: number } | null>(null)
  const items = () => [...(menu.current?.querySelectorAll<HTMLButtonElement>('.tool') || [])]

  useLayoutEffect(() => {
    const node = menu.current
    if (!node) return
    if (open) { node.showPopover?.(); items()[open.index]?.focus() }
    else if (node.matches(':popover-open')) node.hidePopover()
  }, [open])
  useEffect(() => {
    const close = (event: MouseEvent) => { if (!(event.target as HTMLElement).closest('.agent-tools-menu')) setOpen(null) }
    document.addEventListener('click', close)
    return () => document.removeEventListener('click', close)
  }, [])

  function openTools(index = 0) {
    const rect = toggle.current!.getBoundingClientRect()
    setOpen({ index, left: rect.left, bottom: window.innerHeight - rect.top + 8 })
  }
  function closeTools(returnFocus = false) {
    setOpen(null)
    if (returnFocus) toggle.current?.focus()
  }
  function send() {
    const text = input.current!.value.trim()
    if (!text) return
    input.current!.value = ''
    onSend(text)
  }

  return (
    <div className="agent-compose flex flex-col gap-2 border-t border-hairline p-3">
      <label className="sr-only" htmlFor="agent-input">{T('agent.inputLabel')}</label>
      <textarea id="agent-input" ref={input} rows={2} placeholder={T('agent.placeholder')}
        className="min-h-16 w-full resize-none rounded-md border border-hairline bg-surface-1 px-3 py-2.5 text-body-sm outline-none placeholder:text-ink-muted focus-visible:border-brand"
        onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send() }} />
      <p className="agent-compose__context m-0 text-micro text-ink-muted">{T('agent.composeContext')}</p>
      <div className="agent-compose__row flex items-center gap-2">
        <div className="agent-tools-menu">
          <Button tone="secondary" size="sm" ref={toggle} id="agent-tools-toggle" className="btn--inline" aria-haspopup="menu" aria-expanded={Boolean(open)} aria-controls="agent-tools"
            onClick={() => (open ? closeTools() : openTools())}
            onKeyDown={(e) => { if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); openTools(e.key === 'ArrowUp' ? TOOLS.length - 1 : 0) } }}>
            <Workflow className="size-4" aria-hidden />{T('agent.tools')}
          </Button>
          <div ref={menu} id="agent-tools" role="menu" aria-labelledby="agent-tools-toggle" popover="manual" hidden={!open}
            className="agent-tools fixed inset-auto m-0 flex max-h-[calc(100vh-16px)] min-w-44 flex-col gap-0.5 overflow-y-auto rounded-lg border border-hairline bg-surface-2 p-1 shadow-[0_12px_32px_-12px_rgb(0_0_0/0.7)]"
            style={open ? { left: open.left, bottom: open.bottom } : undefined}
            onKeyDown={(e) => {
              const list = items()
              const index = list.indexOf(document.activeElement as HTMLButtonElement)
              if (e.key === 'Escape') { e.preventDefault(); closeTools(true) }
              else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
                e.preventDefault()
                const next = e.key === 'Home' ? 0 : e.key === 'End' ? list.length - 1 : (index + (e.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length
                list[next].focus()
              } else if (e.key === 'Tab') closeTools(true)
            }}>
            {TOOLS.map(([tool, key, Icon]) => (
              <button key={tool} type="button" role="menuitem" tabIndex={-1} data-tool={tool}
                className="tool flex min-h-10 items-center gap-2.5 rounded-sm px-2.5 text-left text-body-sm text-ink hover:bg-surface-3 focus:bg-surface-3 focus:outline-none"
                onClick={() => { closeTools(); input.current!.value = T(`agent.toolPrompt.${tool}`); input.current!.focus() }}>
                <Icon className="size-4 text-ink-muted" aria-hidden /><span>{T(key)}</span>
              </button>
            ))}
          </div>
        </div>
        <input type="file" id="agent-attach" ref={attach} accept="image/png,image/jpeg,image/svg+xml,.svg,model/gltf-binary,.glb" multiple hidden
          onChange={(e) => { onAttach([...(e.target.files || [])]); e.target.value = '' }} />
        <Button tone="secondary" size="sm" id="agent-attach-btn" className="btn--inline" aria-label={T('agent.attachmentFormats')} onClick={() => attach.current?.click()}>
          <Paperclip className="size-4" aria-hidden />{T('agent.attach')}
        </Button>
        <ul id="agent-attach-name" className="m-0 flex min-w-0 list-none flex-wrap gap-1 p-0" aria-label={T('agent.attachmentList')}>
          {attachments.map((name, i) => (
            <li key={`${i}-${name}`} className="flex min-w-0 max-w-40 items-center gap-0.5 rounded-sm border border-hairline bg-surface-2 py-0.5 pl-2 pr-0.5 text-micro text-ink-muted">
              <span className="truncate" title={name}>{i + 1}. {name}</span>
              <button type="button" className="grid size-6 shrink-0 place-items-center rounded-sm hover:bg-surface-3 focus-visible:outline focus-visible:outline-brand"
                aria-label={Tf('agent.removeAttachment', { name })} onClick={() => onRemoveAttachment(i)}><X className="size-3.5" aria-hidden /></button>
            </li>
          ))}
        </ul>
        <Button tone="primary" size="sm" id="agent-send" className="ml-auto" title="Ctrl+Enter" aria-keyshortcuts="Control+Enter" disabled={sending} onClick={send}>{T('agent.send')}</Button>
      </div>
    </div>
  )
}

/** Clears the hidden file input after applied attachments. */
export function clearAttachInput() {
  const input = document.getElementById('agent-attach') as HTMLInputElement | null
  if (input) input.value = ''
}
