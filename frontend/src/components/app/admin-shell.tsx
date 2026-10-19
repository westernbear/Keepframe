import { ArrowLeft, Bot, Building2, ListOrdered, ScrollText, ShieldAlert } from 'lucide-react'
import { useEffect } from 'react'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { Brand } from './app-header'
import { LangToggle } from './lang-toggle'

export type AdminPage = 'tenants' | 'queue' | 'quarantine' | 'audit' | 'llm'
const LINKS: { page: AdminPage; icon: typeof Bot }[] = [
  { page: 'tenants', icon: Building2 },
  { page: 'queue', icon: ListOrdered },
  { page: 'quarantine', icon: ShieldAlert },
  { page: 'audit', icon: ScrollText },
  { page: 'llm', icon: Bot },
]

/** One layout for the operator's pages: a sidebar on wide screens, a wrapped link row on narrow ones. */
export function AdminShell({ page, title, actions, children }: {
  page: AdminPage
  title: string
  actions?: React.ReactNode
  children: React.ReactNode
}) {
  const { T } = useI18n()
  useEffect(() => { document.title = `${title} · Keepframe` }, [title])
  return (
    <div className="admin-shell min-h-dvh md:grid md:grid-cols-[232px_minmax(0,1fr)]">
      <aside className="admin-sidebar border-b border-hairline-soft md:sticky md:top-0 md:flex md:h-dvh md:flex-col md:border-r md:border-b-0 md:px-3 md:py-4">
        <div className="admin-sidebar__brand hidden px-2 pb-5 md:block"><Brand /></div>
        <nav className="flex flex-wrap items-center gap-1 px-3 py-2 md:flex-1 md:flex-col md:flex-nowrap md:items-stretch md:gap-0.5 md:p-0" aria-label={T('admin.ariaLabel')}>
          {LINKS.map(({ page: p, icon: Icon }) => (
            <a
              key={p}
              href={`/admin/${p}`}
              aria-current={p === page ? 'page' : undefined}
              className={cn(
                'admin-sidebar__link inline-flex min-h-11 min-w-11 items-center justify-center gap-2.5 rounded-full px-3 text-body-sm font-medium transition-colors md:justify-start md:rounded-lg',
                p === page ? 'admin-sidebar__link--active bg-surface-2 text-ink' : 'text-ink-muted hover:bg-white/5 hover:text-ink',
              )}
            >
              <Icon className="size-4 shrink-0" aria-hidden />
              <span>{T(`admin.${p}`)}</span>
            </a>
          ))}
          <a href="/" className="inline-flex min-h-11 min-w-11 items-center justify-center gap-2.5 rounded-full px-3 text-body-sm text-ink-muted transition-colors hover:text-ink md:mt-auto md:justify-start md:rounded-lg">
            <ArrowLeft className="size-4 shrink-0" aria-hidden />
            <span className="max-md:sr-only">{T('admin.backToApp')}</span>
          </a>
        </nav>
      </aside>
      <div className="admin-main min-w-0">
        <header className="header sticky top-0 z-30 flex min-h-14 flex-wrap items-center gap-3 border-b border-hairline-soft bg-canvas/85 px-4 py-2 backdrop-blur-md md:px-8">
          <h1 className="m-0 min-w-0 flex-1 truncate text-headline font-semibold tracking-[-0.02em]">{title}</h1>
          {actions}
          <LangToggle />
        </header>
        <main className="admin-content px-4 py-6 md:px-8 md:py-8">{children}</main>
      </div>
    </div>
  )
}
