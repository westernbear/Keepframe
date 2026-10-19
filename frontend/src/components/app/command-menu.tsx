import { Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router'
import {
  CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from '@/components/smoothui/command'
import Kbd from '@/components/smoothui/kbd'
import { api } from '@/lib/api'
import { hrefForProject } from '@/lib/library'
import { useI18n } from '@/i18n'

const PAGES = [
  { href: '/', key: 'nav.home' },
  { href: '/library', key: 'nav.library' },
  { href: '/new', key: 'nav.new' },
  { href: '/agent', key: 'nav.agent' },
  { href: '/admin', key: 'admin.manage' },
]
const RECENT = 8
const OPEN_EVENT = 'keepframe:command'

export const openCommandMenu = () => window.dispatchEvent(new Event(OPEN_EVENT))
const isMac = () => /Mac|iPhone|iPad/.test(navigator.platform)

/** ⌘K / Ctrl+K: jump to a page or one of the recent projects. Mounted once in the shell. */
export function CommandMenu() {
  const { T } = useI18n()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [projects, setProjects] = useState<any[] | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === 'k' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); setOpen((o) => !o) }
    }
    const onOpen = () => setOpen(true)
    window.addEventListener('keydown', onKey)
    window.addEventListener(OPEN_EVENT, onOpen)
    return () => { window.removeEventListener('keydown', onKey); window.removeEventListener(OPEN_EVENT, onOpen) }
  }, [])

  useEffect(() => {
    if (!open || projects) return
    api('/api/projects').then((d) => setProjects((d?.projects || d || []).slice(0, RECENT))).catch(() => setProjects([]))
  }, [open, projects])

  const go = (href: string) => {
    setOpen(false)
    if (href.startsWith('/admin')) window.location.assign(href) // the admin area authenticates on the server
    else navigate(href)
  }

  return (
    <CommandDialog open={open} onOpenChange={setOpen} title={T('shell.command.title')} description={T('shell.command.placeholder')}>
      <CommandInput placeholder={T('shell.command.placeholder')} />
      <CommandList>
        <CommandEmpty>{T('shell.command.empty')}</CommandEmpty>
        <CommandGroup heading={T('shell.command.pages')}>
          {PAGES.map((p) => (
            <CommandItem key={p.href} value={`${T(p.key)} ${p.href}`} onSelect={() => go(p.href)}>{T(p.key)}</CommandItem>
          ))}
        </CommandGroup>
        <CommandGroup heading={T('shell.command.projects')}>
          {projects === null && <CommandItem disabled value="loading">{T('shell.command.loading')}</CommandItem>}
          {projects?.map((p) => (
            <CommandItem key={p.id} value={`${p.title || p.id} ${p.id}`} onSelect={() => { const href = hrefForProject(p); if (href) go(href) }}>
              <span className="min-w-0 flex-1 truncate">{p.title || p.id}</span>
              <span className="text-micro text-ink-muted">{p.status}</span>
            </CommandItem>
          ))}
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  )
}

/** Header trigger, so the palette is findable without knowing the shortcut. */
export function CommandTrigger() {
  const { T } = useI18n()
  return (
    <button
      type="button"
      onClick={openCommandMenu}
      aria-label={T('shell.command.title')}
      aria-keyshortcuts="Meta+K Control+K"
      className="inline-flex h-9 min-w-11 items-center justify-center gap-2 rounded-md px-3 text-body-sm text-ink-muted ring-1 ring-inset ring-hairline transition-colors hover:text-ink hover:ring-white/20"
    >
      <Search className="size-4" aria-hidden />
      <Kbd className="hidden md:inline-flex">{isMac() ? '⌘K' : 'Ctrl K'}</Kbd>
    </button>
  )
}
