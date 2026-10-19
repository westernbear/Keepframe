import { motion } from 'motion/react'
import { Link, useLocation } from 'react-router'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { LangToggle } from './lang-toggle'

const PRIMARY = [
  { href: '/', key: 'nav.home' },
  { href: '/library', key: 'nav.library' },
  { href: '/new', key: 'nav.new' },
  { href: '/agent', key: 'nav.agent' },
]

export interface NavExtra { href: string; label: string }

/** Shared maker navigation; the active pill slides between links. */
export function MakerNav({ extra = [], actions }: { extra?: NavExtra[]; actions?: React.ReactNode }) {
  const { T } = useI18n()
  const { pathname } = useLocation()
  const link = 'relative inline-flex h-9 shrink-0 items-center rounded-full px-3.5 text-body-sm font-medium transition-colors'
  return (
    <nav className="header__nav maker-nav flex min-w-0 items-center gap-1" aria-label={T('nav.primary')}>
      {actions}
      <div className="-mx-1 flex min-w-0 items-center gap-0.5 overflow-x-auto px-1 [scrollbar-width:none]">
        {PRIMARY.map(({ href, key }) => {
          const active = pathname === href
          return (
            <Link key={href} to={href} aria-current={active ? 'page' : undefined} className={cn(link, active ? 'text-ink' : 'text-ink-muted hover:text-ink')}>
              {active && <motion.span layoutId="maker-nav-pill" className="absolute inset-0 -z-10 rounded-full bg-surface-2" transition={{ type: 'spring', bounce: 0.15, duration: 0.35 }} />}
              {T(key)}
            </Link>
          )
        })}
        {extra.map((x) => (
          <Link key={x.href} to={x.href} className={cn(link, 'text-ink-muted hover:text-ink')}>{x.label}</Link>
        ))}
      </div>
      <a href="/admin" className={cn(link, 'text-ink-muted hover:text-ink')}>{T('admin.manage')}</a>
      <LangToggle />
    </nav>
  )
}
