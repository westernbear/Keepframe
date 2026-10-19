import { BarChart3, Download, FileText, Lock, Paperclip, PencilLine, Play, ShieldCheck, Wrench, Workflow } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Button } from '@/components/app/button'
import { useI18n } from '@/i18n'
import { toolCallAddsInformation, toolCallLogArgs, toolStatus, verifyChipText, type VerifyChip } from '@/lib/agent-chat'
import { confirmGuard } from '@/lib/agent-corrections'
import { cn } from '@/lib/utils'

export type Entry =
  | { id: number; kind: 'user' | 'agent'; text: string }
  | { id: number; kind: 'tool'; name: string; args: any; result: any; reply: string }
  | { id: number; kind: 'verify'; chip: VerifyChip }
  | { id: number; kind: 'choices'; plan: any }
  | { id: number; kind: 'confirm'; onConfirm: () => Promise<boolean | undefined> }

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
    <div className="agent-choice flex flex-col gap-2 rounded-xl bg-surface-2 p-3">
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
    <div className="toolcall rounded-xl bg-surface-2/60 px-3 py-2 ring-1 ring-inset ring-white/5" data-tool={e.name} data-ok={String(ok)}>
      <div className="toolcall__head flex items-center gap-2 text-caption">
        <span className={cn('toolcall__status inline-flex items-center gap-1.5 font-medium', ok ? 'toolcall__status--ok text-success' : 'toolcall__status--fail text-danger')}>
          <span aria-hidden className="size-1.5 rounded-full bg-current" />{toolStatus(e.name, ok)}
        </span>
      </div>
      <details className="group/tc">
        <summary data-i18n="agent.details" className="mt-1 cursor-pointer text-micro text-ink-muted hover:text-ink">{T('agent.details')}</summary>
        <span className="toolcall__name mt-1 block font-mono text-micro text-ink-muted">{e.name}</span>
        <div className="toolcall__args mt-1 font-mono text-micro break-all text-ink-muted">{JSON.stringify(toolCallLogArgs(e.name, e.args) || {}, null, 0)}</div>
        {toolCallAddsInformation(e.result.message, e.reply) && <div className="toolcall__msg mt-1 whitespace-pre-line text-caption">{e.result.message}</div>}
      </details>
    </div>
  )
}

export function ChatLog({ entries, empty, choices, onChoose }: { entries: Entry[]; empty: string | null; choices: Record<string, string>; onChoose: () => void }) {
  const { T } = useI18n()
  const log = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => { if (log.current) log.current.scrollTop = log.current.scrollHeight }, [entries.length])
  return (
    <div className="agent-chat__log flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4" id="agent-log" ref={log}>
      <p className="agent-empty m-0 text-body-sm text-ink-muted" id="agent-empty" hidden={!empty}>{empty}</p>
      {entries.map((e) => {
        if (e.kind === 'user' || e.kind === 'agent') return (
          <div key={e.id} className={cn('msg flex flex-col gap-1', e.kind === 'user' ? 'msg--user items-end' : 'msg--agent items-start')}>
            <div className="msg__label text-micro text-ink-muted">{T(e.kind === 'user' ? 'agent.you' : 'agent.assistant')}</div>
            <div className={cn('msg__body max-w-[92%] rounded-2xl px-3.5 py-2.5 text-body-sm whitespace-pre-line break-words', e.kind === 'user' ? 'bg-ink text-black' : 'bg-surface-2 text-ink')}>{e.text}</div>
          </div>
        )
        if (e.kind === 'tool') return <ToolCall key={e.id} e={e} />
        if (e.kind === 'verify') {
          const { text, title } = verifyChipText(e.chip)
          return (
            <span key={e.id} title={title} aria-label={title}
              className={cn('verify-chip self-start rounded-full px-3 py-1 text-caption font-medium', `verify-chip--${e.chip.tone}`,
                e.chip.tone === 'pass' ? 'bg-success/15 text-success' : e.chip.tone === 'warn' ? 'bg-warning/15 text-warning' : 'bg-danger/15 text-danger')}>{text}</span>
          )
        }
        if (e.kind === 'choices') return <Choices key={e.id} plan={e.plan} choices={choices} onChoose={onChoose} />
        const c = e as Extract<Entry, { kind: 'confirm' }>
        return <ConfirmButton key={c.id} onConfirm={c.onConfirm} label={T('agent.confirm')} />
      })}
    </div>
  )
}

/** Message box, tools menu (fills a prompt) and the attachment picker. */
export function Composer({ onSend, sending, attachmentName, onAttach }: { onSend: (text: string) => void; sending: boolean; attachmentName: string; onAttach: (file: File | null) => void }) {
  const { T } = useI18n()
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
    <div className="agent-compose flex flex-col gap-2 border-t border-hairline-soft p-3">
      <label className="sr-only" htmlFor="agent-input">{T('agent.inputLabel')}</label>
      <textarea id="agent-input" ref={input} rows={2} placeholder={T('agent.placeholder')}
        className="min-h-16 w-full resize-none rounded-xl border border-hairline bg-canvas px-3.5 py-2.5 text-body-sm outline-none focus-visible:border-brand"
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
            className="agent-tools fixed inset-auto m-0 flex max-h-[calc(100vh-16px)] min-w-44 flex-col gap-0.5 overflow-y-auto rounded-xl border border-hairline bg-surface-2 p-1.5 shadow-[0_16px_40px_-12px_rgb(0_0_0/0.7)]"
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
                className="tool flex min-h-10 items-center gap-2.5 rounded-lg px-3 text-left text-body-sm text-ink hover:bg-white/5 focus:bg-white/[0.07] focus:outline-none"
                onClick={() => { closeTools(); input.current!.value = T(`agent.toolPrompt.${tool}`); input.current!.focus() }}>
                <Icon className="size-4 text-ink-muted" aria-hidden /><span>{T(key)}</span>
              </button>
            ))}
          </div>
        </div>
        <input type="file" id="agent-attach" ref={attach} accept="image/png,image/jpeg,image/svg+xml,.svg,model/gltf-binary,.glb" hidden
          onChange={(e) => onAttach(e.target.files?.[0] || null)} />
        <Button tone="secondary" size="sm" id="agent-attach-btn" className="btn--inline" aria-label={T('agent.attachmentFormats')} onClick={() => attach.current?.click()}>
          <Paperclip className="size-4" aria-hidden />{T('agent.attach')}
        </Button>
        <span id="agent-attach-name" className="min-w-0 truncate font-mono text-micro text-ink-muted">{attachmentName}</span>
        <Button tone="primary" size="sm" id="agent-send" className="ml-auto" title="Ctrl+Enter" aria-keyshortcuts="Control+Enter" disabled={sending} onClick={send}>{T('agent.send')}</Button>
      </div>
    </div>
  )
}

/** Clears the hidden file input after an applied attachment. */
export function clearAttachInput() {
  const input = document.getElementById('agent-attach') as HTMLInputElement | null
  if (input) input.value = ''
}
