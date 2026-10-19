import { Check } from 'lucide-react'
import { motion } from 'motion/react'
import { useEffect, useRef, useState } from 'react'
import { AppHeader } from '@/components/app/app-header'
import { Button } from '@/components/app/button'
import { WorkflowStepper } from '@/components/app/workflow-stepper'
import Progress from '@/components/smoothui/progress'
import { DEFAULT_FILMSTRIP_COUNT, fetchFilmstrip, fetchJob, fetchProject, fetchProjects, postAnalyze } from '@/lib/api'
import { useI18n } from '@/i18n'
import {
  ANALYZE_POLL_INTERVAL_MS, PIPELINE, STEP_STAGES, analyzePayload, formatEta, rangeLabel, readAnalyzeStart, reviewHref,
  stageLabel, stepState,
} from '@/lib/analyze'
import { cn } from '@/lib/utils'

const projectIdOf = (params: URLSearchParams) => params.get('job') || params.get('project')
const card = 'rounded-2xl bg-surface-1 ring-1 ring-inset ring-white/5'
type Job = { id: string; project_id: string; status: string; stage?: string; detail?: string; eta_s?: number; error?: string }

function goToReview(projectId: string) {
  location.assign(reviewHref(projectId)) // shortcut: full load while /review is a legacy page; navigate() once it lives here
}

export default function Analyze() {
  const { T, errorText } = useI18n()
  const [title, setTitle] = useState('')
  const [projectStatus, setProjectStatus] = useState('')
  const [range, setRange] = useState<string | null>(null)
  const [frames, setFrames] = useState<string[]>([])
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState(() => projectIdOf(new URLSearchParams(location.search)) ? '' : T('analyze.noProject'))
  const booted = useRef(false)

  useEffect(() => { document.title = `${T('nav.analyze')} · Keepframe` }, [T])

  // Boot once (the ingest hand-off is read and cleared on first read, so StrictMode's second run must not repeat it).
  useEffect(() => {
    if (booted.current) return
    booted.current = true
    const params = new URLSearchParams(location.search)
    const projectId = projectIdOf(params)
    if (!projectId) return
    const { explicitStart, estimateSnapshot } = readAnalyzeStart(location.search)
    ;(async () => {
      try {
        const { project: p } = await fetchProject(projectId)
        setTitle(p.title || projectId)
        setProjectStatus(p.status || '')
        setRange(rangeLabel(params, p))
      } catch (err: any) { setError(err.message || T('analyze.chromeFailed')) }
      fetchFilmstrip(projectId, DEFAULT_FILMSTRIP_COUNT).then(({ frames: f }) => setFrames(f || []))
        .catch((err) => setError(err.message || T('analyze.filmstripFailed')))
      try {
        if (explicitStart) {
          setJob((await postAnalyze(analyzePayload(params, estimateSnapshot))).job)
          return
        }
        const { projects } = await fetchProjects()
        const row = (projects || []).find((r: any) => r.id === projectId)
        if (!row || !row.job_id) throw new Error(T('analyze.noProject'))
        setJob(await fetchJob(row.job_id))
      } catch (err: any) { setError(err.message || T(explicitStart ? 'analyze.startFailed' : 'analyze.noProject')) }
    })()
  }, [T])

  const jobId = job?.id
  const finished = job?.status === 'done' || job?.status === 'error'
  useEffect(() => {
    if (!jobId || finished) return
    const timer = setInterval(() => {
      fetchJob(jobId).then(setJob).catch((err) => setError(err.message || T('analyze.pollFailed')))
    }, ANALYZE_POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [jobId, finished, T])

  useEffect(() => {
    if (job?.status === 'done') goToReview(job.project_id)
  }, [job?.status, job?.project_id])

  const failed = job?.status === 'error'
  const message = failed ? errorText(job?.error) || T('analyze.failed') : error
  const stage = job?.status === 'done' ? 'report' : job?.stage || ''
  const at = PIPELINE.indexOf(stage)
  const idle = !job && Boolean(message) // nothing to follow: show the reason and the way back, keep the hooks mounted
  const percent = job?.status === 'done' ? 100 : at < 0 ? 0 : Math.round((at / PIPELINE.length) * 100)

  return (
    <div className="min-h-dvh">
      <AppHeader>
        <WorkflowStepper stage={job?.status === 'done' ? 'review' : 'analyze'} projectName={title || undefined} projectStatus={job?.status ?? projectStatus} />
      </AppHeader>
      <main className="mx-auto grid w-full max-w-[1200px] gap-6 px-4 pt-8 pb-16 md:px-8 md:pt-12 lg:grid-cols-[minmax(0,1fr)_380px] lg:gap-10">
        <section className="flex min-w-0 flex-col gap-4">
          <h1 className="m-0 font-display text-display-md font-medium tracking-[-0.03em]">{T('analyze.running')}</h1>
          <div className={cn(card, 'p-3')} hidden={idle}>
            <div className="grid grid-cols-4 gap-1.5 sm:grid-cols-8" data-filmstrip>
              {(frames.length && !failed ? frames : Array.from({ length: DEFAULT_FILMSTRIP_COUNT }, () => '')).map((url, i, all) => (
                <div key={i} className={cn('filmstrip__frame relative aspect-video overflow-hidden rounded-md bg-surface-2', i === Math.floor(all.length / 2) && url && 'ring-2 ring-brand')}>
                  {url
                    ? <img src={url} alt="" className="size-full object-cover" />
                    : !failed && <span aria-hidden className="absolute inset-0 animate-pulse bg-surface-3/60" />}
                </div>
              ))}
            </div>
          </div>
          <div className="flex items-center justify-between gap-4 text-body-sm text-ink-muted" hidden={idle}>
            <span>{T('analyze.selectedRange')}</span>
            <span className="font-mono tabular-nums text-ink" data-range-label>{range || '—'}</span>
          </div>
        </section>

        <aside className="lg:sticky lg:top-24 lg:self-start">
          <div className={cn(card, 'flex flex-col gap-5 p-5')} aria-busy={!finished && !message}>
            <div className="flex flex-col gap-5" hidden={idle}>
              <Progress value={failed ? null : percent} size="sm" aria-label={T('analyze.running')} className={cn(failed && 'opacity-40')} />
              <ul className="progress-steps m-0 flex list-none flex-col p-0" data-steps>
                {Object.keys(STEP_STAGES).map((key) => {
                  const state = failed && stepState(key, stage) === 'active' ? 'error' : stepState(key, stage)
                  return (
                    <li key={key} data-step={key} data-state={state} className={cn('flex min-h-10 items-center gap-3 text-body-sm', `is-${state}`)}>
                      <span className={cn(
                        'step-dot relative grid size-5 shrink-0 place-items-center rounded-full transition-colors duration-200',
                        state === 'done' && 'bg-ink text-black',
                        state === 'active' && 'bg-brand/20',
                        state === 'waiting' && 'ring-1 ring-inset ring-hairline',
                        state === 'error' && 'bg-danger/20',
                      )}>
                        {state === 'done' && <Check className="size-3" strokeWidth={3} aria-hidden />}
                        {state === 'active' && (
                          <motion.span aria-hidden className="size-2 rounded-full bg-brand" animate={{ opacity: [1, 0.35, 1] }} transition={{ duration: 1.4, repeat: Infinity, ease: 'easeInOut' }} />
                        )}
                        {state === 'error' && <span aria-hidden className="size-2 rounded-full bg-danger" />}
                      </span>
                      <span className="flex min-w-0 flex-col">
                        <span className={state === 'waiting' ? 'text-ink-muted' : 'text-ink'}>{T(`analyze.step.${key}`)}</span>
                        {key === 'text' && <span className="step-hint text-micro text-ink-muted">{T('analyze.step.textHint')}</span>}
                      </span>
                    </li>
                  )
                })}
              </ul>
              <div className="flex items-center justify-between gap-3 border-t border-hairline-soft pt-4 text-body-sm text-ink-muted">
                <span className="tabular-nums" data-eta>{job ? formatEta(job.eta_s) : '—'}</span>
                <span className="max-w-[60%] truncate rounded-md bg-surface-2 px-2 py-0.5 font-mono text-micro text-ink" data-stage>{job ? stageLabel(job) : '—'}</span>
              </div>
            </div>
            <p className="m-0 rounded-xl bg-danger/10 px-4 py-3 text-body-sm text-danger" data-status role="status" hidden={!message}>{message}</p>
            <Button tone={idle ? 'primary' : 'secondary'} className="w-full" asChild><a href="/library">{T(idle ? 'nav.library' : 'common.cancel')}</a></Button>
          </div>
        </aside>
      </main>
    </div>
  )
}
