import { cn } from '@/lib/utils'

export const panel = 'rounded-2xl bg-surface-1 ring-1 ring-inset ring-hairline'
export const sectionLabel = 'section-label text-micro font-semibold tracking-wide text-ink-muted uppercase'
export const th = 'px-3 py-2.5 text-left text-micro font-semibold tracking-wide whitespace-nowrap text-ink-muted uppercase'
export const td = 'px-3 py-3 align-middle'
/** Selectable row: Enter or Space selects, like a button. */
export function rowKeys(select: () => void) {
  return {
    tabIndex: 0,
    onClick: select,
    onKeyDown: (e: React.KeyboardEvent) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select() } },
  }
}
export function LoadError({ message, className }: { message: string; className?: string }) {
  return <p className={cn('admin-load-error m-0 p-4 text-body-sm text-danger', className)} role="alert">{message}</p>
}
