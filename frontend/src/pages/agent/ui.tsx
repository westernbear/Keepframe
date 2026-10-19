import { ChevronDown } from 'lucide-react'
import { useState } from 'react'
import { useI18n } from '@/i18n'
import { storedOpen } from '@/lib/agent-view'
import { cn } from '@/lib/utils'

export const card = 'render-card flex flex-col gap-3 rounded-2xl bg-surface-1 p-4 ring-1 ring-inset ring-white/5'
export const lock = 'render-card__lock m-0 text-caption text-ink-muted'
// A transparent fill that still differs from a bare <button>: the card's buttons are all visibly styled controls.
export const toggleCls = 'card-toggle inline-flex min-h-11 items-center gap-2 rounded-lg bg-white/0 text-left text-body-sm font-semibold text-ink hover:text-ink-muted'

export function useStoredOpen(key: string, defaultOpen: boolean) {
  const [open, setOpen] = useState(() => storedOpen(key, defaultOpen))
  const toggle = () => setOpen((o) => {
    try { localStorage.setItem(key, String(!o)) } catch { /* private mode */ }
    return !o
  })
  return [open, toggle] as const
}

/** A card whose body folds under its title (`#<id>-toggle`, `#<id>-body`); the open state is remembered. */
export function Disclosure({ id, storageKey, defaultOpen, titleKey, status, children }: {
  id: string; storageKey: string; defaultOpen: boolean; titleKey: string; status?: React.ReactNode; children: React.ReactNode
}) {
  const { T } = useI18n()
  const [open, toggle] = useStoredOpen(storageKey, defaultOpen)
  return (
    <>
      <div className="render-card__head flex flex-wrap items-center justify-between gap-x-3">
        <h2 id={`${id}-title`} className="m-0 text-body-sm">
          <button id={`${id}-toggle`} type="button" className={toggleCls} aria-expanded={open} aria-controls={`${id}-body`} onClick={toggle}>
            <ChevronDown className={cn('size-4 text-ink-muted transition-transform duration-200', !open && '-rotate-90')} aria-hidden />
            {T(titleKey)}
          </button>
        </h2>
        {status}
      </div>
      <div className="render-card__body flex flex-col gap-3" id={`${id}-body`} hidden={!open}>{children}</div>
    </>
  )
}
