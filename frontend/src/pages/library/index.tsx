import { ChevronRight, Film, Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import { AppHeader } from '@/components/app/app-header'
import { Button } from '@/components/app/button'
import Empty from '@/components/smoothui/empty'
import Input from '@/components/smoothui/input'
import Skeleton from '@/components/smoothui/skeleton'
import { TabsIndicator, TabsList, TabsRoot, TabsTab } from '@/components/smoothui/tabs'
import { fetchProjects } from '@/lib/api'
import { useI18n } from '@/i18n'
import { badgeText, formatConfidence, formatVersion, hrefForProject, thumbUrl, visibleProjects } from '@/lib/library'
import { cn } from '@/lib/utils'

type Project = { id: string; title?: string; status: string; version?: string; confidence?: number | string; [k: string]: unknown }
type Load = { state: 'loading' } | { state: 'ready'; projects: Project[] } | { state: 'error'; message: string }

const TONE: Record<string, string> = {
  analyzing: 'text-brand-soft bg-brand/10 ring-brand/30',
  review: 'text-warning bg-warning/10 ring-warning/30',
  approved: 'text-success bg-success/10 ring-success/30',
  error: 'text-danger bg-danger/10 ring-danger/30',
  rejected: 'text-danger bg-danger/10 ring-danger/30',
}

function Thumb({ p }: { p: Project }) {
  const [failed, setFailed] = useState(false)
  const src = thumbUrl(p)
  return (
    <div className="row__thumb relative grid aspect-[16/10] w-24 shrink-0 place-items-center overflow-hidden rounded-md bg-surface-2 text-ink-muted/50 ring-1 ring-inset ring-white/5 sm:w-32">
      <Film className="size-5" aria-hidden />
      {src && !failed && <img className="absolute inset-0 size-full object-cover" src={src} alt="" loading="lazy" onError={() => setFailed(true)} />}
    </div>
  )
}

function Row({ p }: { p: Project }) {
  const { Tf } = useI18n()
  const href = hrefForProject(p)
  const version = formatVersion(p.version)
  const conf = formatConfidence(p.confidence)
  const body = (
    <>
      <Thumb p={p} />
      <div className="row__body flex min-w-0 flex-1 flex-col gap-1.5">
        <p className="row__title m-0 truncate text-body font-semibold tracking-[-0.01em]">{p.title || p.id}</p>
        <div className="row__meta flex flex-wrap items-center gap-x-3 gap-y-1 text-micro text-ink-muted">
          <span className={cn('badge inline-flex max-w-full min-w-0 items-center gap-1.5 rounded-full px-2 py-0.5 font-medium ring-1 ring-inset', TONE[p.status] || 'bg-surface-2 text-ink ring-hairline')}>
            <span aria-hidden className="size-1.5 shrink-0 rounded-full bg-current" />
            <span className="truncate">{badgeText(p)}</span>
          </span>
          {version && <span className="font-mono">{version}</span>}
          {conf && <span className="tabular-nums">{Tf('library.confidence', { c: conf })}</span>}
        </div>
      </div>
      {href && <ChevronRight className="size-4 shrink-0 text-ink-muted/60 transition-transform duration-150 group-hover:translate-x-0.5 group-hover:text-ink" aria-hidden />}
    </>
  )
  const cls = 'row group flex items-center gap-4 rounded-xl p-2 pr-3'
  // shortcut: a full page load while the targets are legacy pages; switch to <Link> once they live in the SPA.
  return href
    ? <a href={href} className={cn(cls, 'state-transition hover:bg-surface-1 focus-visible:bg-surface-1')}>{body}</a>
    : <div className={cls}>{body}</div>
}

function RowSkeleton() {
  return (
    <li className="flex items-center gap-4 p-2" aria-hidden>
      <Skeleton className="aspect-[16/10] w-24 rounded-md sm:w-32" />
      <div className="flex flex-1 flex-col gap-2">
        <Skeleton className="h-4 w-2/5 rounded" />
        <Skeleton className="h-3 w-1/4 rounded" />
      </div>
    </li>
  )
}

export default function Library() {
  const { lang, T, Tf } = useI18n()
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [filter, setFilter] = useState('all')
  const [query, setQuery] = useState('')
  const [attempt, setAttempt] = useState(0)

  useEffect(() => { document.title = `${T('nav.library')} · Keepframe` }, [lang, T])
  useEffect(() => {
    let live = true
    fetchProjects()
      .then(({ projects }) => live && setLoad({ state: 'ready', projects: projects || [] }))
      .catch((err) => live && setLoad({ state: 'error', message: err?.message || T('library.loadFailed') }))
    return () => { live = false }
  }, [attempt, T])

  const all = load.state === 'ready' ? load.projects : []
  const shown: Project[] = visibleProjects(all, filter, query)
  const reviewCount = all.filter((p) => p.status === 'review').length

  return (
    <div className="min-h-dvh">
      <AppHeader />
      <main className="main mx-auto flex w-full max-w-[960px] flex-col gap-6 px-4 pt-8 pb-16 md:px-8 md:pt-12">
        <div className="flex items-baseline gap-3">
          <h1 className="m-0 font-display text-display-md font-medium tracking-[-0.03em]">{T('nav.library')}</h1>
          {load.state === 'ready' && all.length > 0 && <span className="text-body text-ink-muted tabular-nums">{Tf('library.count', { n: all.length })}</span>}
        </div>

        <div className="toolbar flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <label className="relative block w-full sm:max-w-sm">
            <span className="sr-only">{T('library.search')}</span>
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-ink-muted" aria-hidden />
            <Input
              type="search"
              data-search
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={T('library.search')}
              className="h-11 rounded-full border-hairline bg-surface-1 pl-9 text-body-sm"
            />
          </label>
          <TabsRoot value={filter} onValueChange={(v) => setFilter(String(v))} className="w-auto">
            <TabsList variant="pill" className="toolbar__filters">
              <TabsTab value="all" data-filter="all" className="min-h-9 px-4 py-1.5 text-body-sm">{T('library.filter.recent')}</TabsTab>
              <TabsTab value="review" data-filter="review" className="min-h-9 gap-1.5 px-4 py-1.5 text-body-sm">
                {T('library.filter.review')}
                {reviewCount > 0 && <span className="rounded-full bg-warning/15 px-1.5 text-micro text-warning tabular-nums">{reviewCount}</span>}
              </TabsTab>
              <TabsIndicator />
            </TabsList>
          </TabsRoot>
        </div>

        <section aria-busy={load.state === 'loading'}>
          {load.state === 'loading' && <ul className="m-0 flex list-none flex-col gap-1 p-0">{[0, 1, 2].map((i) => <RowSkeleton key={i} />)}</ul>}

          {load.state === 'error' && (
            <Empty
              id="library-empty"
              role="alert"
              className="rounded-2xl bg-surface-1 ring-1 ring-inset ring-white/5"
              title={load.message}
              action={<Button tone="secondary" onClick={() => { setLoad({ state: 'loading' }); setAttempt((n) => n + 1) }}>{T('library.retry')}</Button>}
            />
          )}

          {load.state === 'ready' && !all.length && (
            <Empty
              id="library-empty"
              className="rounded-2xl bg-surface-1 ring-1 ring-inset ring-white/5"
              icon={<Film />}
              title={T('library.empty')}
              action={
                <div className="flex flex-wrap justify-center gap-3">
                  <Button tone="primary" asChild><a href="/new">{T('nav.new')}</a></Button>
                  <Button tone="secondary" asChild><a href="/demo">{T('nav.demo')}</a></Button>
                </div>
              }
            />
          )}

          {load.state === 'ready' && all.length > 0 && !shown.length && (
            <Empty
              title={T('library.noMatch')}
              action={<Button tone="ghost" onClick={() => { setQuery(''); setFilter('all') }}>{T('library.clearFilters')}</Button>}
            />
          )}

          <ul id="project-list" className="m-0 flex list-none flex-col gap-1 p-0" hidden={!shown.length}>
            {shown.map((p) => <li key={p.id}><Row p={p} /></li>)}
          </ul>
        </section>

        <p className="m-0 border-t border-hairline-soft pt-6 text-caption text-ink-muted">{T('library.scope')}</p>
      </main>
    </div>
  )
}
