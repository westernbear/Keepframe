import { Link } from 'react-router'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { MakerNav, type NavExtra } from './maker-nav'

export function Brand() {
  const { T } = useI18n()
  return (
    <Link to="/" className="header__brand flex shrink-0 items-center gap-2.5 font-display text-[17px] font-semibold tracking-[-0.03em]" aria-label="Keepframe">
      <img className="header__logo size-7 rounded-md" src="/static/logo.png" width={28} height={28} alt="" />
      <span>{T('app.name')}</span>
    </Link>
  )
}

/** Sticky top bar: brand, optional page context, shared nav. */
export function AppHeader({ context, actions, extra, className, children }: {
  context?: React.ReactNode
  actions?: React.ReactNode
  extra?: NavExtra[]
  className?: string
  children?: React.ReactNode
}) {
  return (
    <header className={cn('header sticky top-0 z-40 border-b border-hairline-soft bg-canvas/85 backdrop-blur-md', className)}>
      <div className="mx-auto flex min-h-14 max-w-[1600px] flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2 md:px-6">
        <Brand />
        {context && <div className="flex min-w-0 flex-1 items-center gap-3">{context}</div>}
        <div className={cn('flex min-w-0 items-center max-sm:contents', !context && 'ml-auto')}>
          <MakerNav extra={extra} actions={actions} />
        </div>
      </div>
      {children}
    </header>
  )
}
