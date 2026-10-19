import { toggleLang, useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

export function LangToggle({ className }: { className?: string }) {
  const { T } = useI18n()
  return (
    <button
      type="button"
      data-lang-toggle
      onClick={toggleLang}
      className={cn('inline-flex h-9 min-w-11 items-center justify-center rounded-md px-3 text-caption font-medium text-ink-muted ring-1 ring-inset ring-hairline transition-colors hover:text-ink hover:ring-white/20', className)}
    >
      {T('lang.toggle')}
    </button>
  )
}
