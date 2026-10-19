import { useEffect, useState } from 'react'
import { AppHeader } from '@/components/app/app-header'
import { Button } from '@/components/app/button'
import { WorkflowStepper } from '@/components/app/workflow-stepper'
import { useI18n } from '@/i18n'
import { approveLabelKey, clampProgress, sceneBadge, sceneMeta, sceneOptionLabel, versionLabel } from '@/lib/inspector'
import { cn } from '@/lib/utils'
import { createReviewCore, initialView, type ReviewView } from './core'
import { Inspector } from './inspector'
import { Timeline } from './timeline'
import { Viewer } from './viewer'

const select = 'h-9 min-w-0 rounded-md border border-hairline bg-surface-1 px-3 text-body-sm text-ink outline-none focus-visible:border-brand'

export default function Review() {
  const { T } = useI18n()
  const [view, setView] = useState<ReviewView>(() => initialView(location.search))
  const [core] = useState(() => createReviewCore(view, (p) => setView((v) => ({ ...v, ...(typeof p === 'function' ? p(v) : p) }))))
  const st = view.st
  const scenes = st?.project.scenes || []
  const pct = clampProgress(view.loading?.pct ?? 100)

  useEffect(() => { document.title = `${T('nav.review')} · Keepframe` }, [T])
  useEffect(() => {
    core.boot()
    document.addEventListener('keydown', core.onKey)
    return () => { document.removeEventListener('keydown', core.onKey); core.dispose() }
  }, [core])

  const approve = (
    <Button tone="primary" size="sm" id="review-approve" hidden={!st} disabled={view.approveBusy} loading={view.approveBusy} onClick={core.approve}>
      {T(approveLabelKey(st?.status))}
    </Button>
  )
  const context = (
    <div className="flex min-w-0 flex-wrap items-center gap-2" hidden={!view.projectId}>
      <span id="scene-badge" className="rounded-md bg-surface-1 px-3 py-1.5 font-mono text-caption text-ink-muted">{st ? sceneBadge(view.sceneId, st.scene.frames) : '—'}</span>
      <select id="scene-select" className={select} aria-label={T('ingest.scenes')} hidden={scenes.length < 2} value={view.sceneId} onChange={(e) => core.switchScene(e.target.value)}>
        {scenes.map((s: any) => <option key={s.id} value={s.id}>{sceneOptionLabel(s)}</option>)}
      </select>
      <span id="scene-meta" className="hidden text-caption text-ink-muted xl:inline" aria-live="polite">{st ? sceneMeta(scenes, st.project.links, view.sceneId) : ''}</span>
      <select id="version-select" className={cn(select, 'max-w-[220px]')} aria-label={T('review.version')} value={view.versionId || ''} onChange={(e) => core.refreshState(e.target.value)}>
        {(st?.project.versions || []).map((v: any) => <option key={v.id} value={v.id}>{versionLabel(v)}</option>)}
      </select>
    </div>
  )

  return (
    <div className="min-h-dvh">
      <AppHeader context={context} actions={approve}>
        <WorkflowStepper stage={st ? 'review' : null} projectName={st ? view.projectTitle || view.projectId || undefined : undefined} projectStatus={st?.status || ''} />
        {view.projectId === 'demo' && <p id="demo-note" className="job-banner m-0 border-t border-hairline-soft px-4 py-2 text-center text-caption text-ink-muted md:px-6">{T('review.demoNote')}</p>}
      </AppHeader>

      <main
        id="review-root"
        aria-busy={view.loading ? 'true' : 'false'}
        className={cn('review-layout mx-auto w-full max-w-[1600px] px-4 pt-5 pb-12 md:px-6', view.loading && 'is-loading', !view.projectId && 'is-empty')}
      >
        <div id="review-missing" className="review-missing mx-auto flex max-w-md flex-col items-center gap-4 py-24 text-center" hidden={!!view.projectId}>
          <p className="m-0 text-body-lg">{T('review.needProject')}</p>
          <Button tone="primary" asChild><a href="/library">{T('review.backLibrary')}</a></Button>
        </div>

        <div className={cn('flex flex-col gap-4', !view.projectId && 'hidden')}>
          <div id="review-loading" className="review-loading flex items-center gap-3 text-caption text-ink-muted" aria-live="polite" hidden={!view.loading}>
            <span id="review-loading-msg">{view.loading?.msg}</span>
            <div id="review-progress" className="review-loading__bar h-1 max-w-xs flex-1 overflow-hidden rounded-full bg-surface-2" role="progressbar"
              aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-labelledby="review-loading-msg">
              <div id="review-progress-fill" className="review-loading__fill h-full origin-left bg-brand transition-transform duration-300" style={{ transform: `scaleX(${pct / 100})` }} />
            </div>
            <span id="review-progress-pct" className="tabular-nums">{pct}%</span>
          </div>

          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
            <div className="review-workspace flex min-w-0 flex-col gap-4">
              <Viewer view={view} core={core} />
              <Timeline st={st} frame={view.frame} selectedId={view.selectedId} filter={view.filter} core={core} />
            </div>
            <Inspector view={view} core={core} />
          </div>
        </div>
      </main>
    </div>
  )
}
