import { Film, Play, Upload, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { AppHeader } from '@/components/app/app-header'
import { Button } from '@/components/app/button'
import { WorkflowStepper } from '@/components/app/workflow-stepper'
import Checkbox from '@/components/smoothui/checkbox'
import { SliderControl, SliderIndicator, SliderRoot, SliderThumb, SliderTrack } from '@/components/smoothui/slider'
import { TabsIndicator, TabsList, TabsRoot, TabsTab } from '@/components/smoothui/tabs'
import { DEFAULT_FILMSTRIP_COUNT, fetchEstimate, fetchFilmstrip, fetchProject, fetchStatus, uploadProject } from '@/lib/api'
import { useI18n } from '@/i18n'
import {
  DEFAULT_FPS, UPLOAD_LIMIT_BYTES, analyzeStartQuery, editedScenes, estimatePayload, estimateText, lastFrameIndex,
  saveAnalyzeStart, sceneBoundaries, selectedWindow, startButtonState, uploadErrorText, uploadTitle, videoMeta,
} from '@/lib/ingest'
import { cn } from '@/lib/utils'

const ESTIMATE_DEBOUNCE_MS = 200 // typing a frame number sends one estimate, not one per digit
const card = 'rounded-2xl bg-surface-1 ring-1 ring-inset ring-white/5'
const numInput = 'h-11 w-20 rounded-lg border border-hairline bg-canvas px-2 text-center font-mono text-body-sm tabular-nums outline-none focus-visible:border-brand'

type Project = { id: string; title?: string; status?: string; video?: any; range?: [number, number]; mode?: string; reference?: string }

export default function Ingest() {
  const { T } = useI18n()
  const [params] = useSearchParams()
  const [project, setProject] = useState<Project | null>(null)
  const [pending, setPending] = useState<string | null>(null) // file name while uploading
  const [error, setError] = useState('')
  const [frames, setFrames] = useState<string[]>([])
  const [mode, setMode] = useState('range')
  const [start, setStart] = useState('0')
  const [end, setEnd] = useState('0')
  const [referenceUi, setReferenceUi] = useState(false)
  const [scenes, setScenes] = useState<any[] | null>(null) // boundary edits sent with the next estimate
  const [est, setEst] = useState<any>(null)
  const [stale, setStale] = useState(true)
  const [fullConfirmed, setFullConfirmed] = useState(false)
  const [shortAck, setShortAck] = useState(false)
  const [gpu, setGpu] = useState('—')
  const [drag, setDrag] = useState(false)
  const request = useRef(0)
  const fileInput = useRef<HTMLInputElement>(null)

  const fps = project?.video?.fps || DEFAULT_FPS
  const totalFrames = project?.video?.frames || 1
  const last = lastFrameIndex(totalFrames)
  const win = selectedWindow({ mode, totalFrames }, start, end)

  useEffect(() => { document.title = `${T('ingest.title')} · Keepframe` }, [T])
  useEffect(() => {
    fetchStatus().then((s) => setGpu(s.cuda ? (s.name || 'ON') : 'OFF')).catch(() => setGpu('OFF'))
    const existing = params.get('project')
    if (!existing) return
    fetchProject(existing).then(({ project: p }) => {
      if (!p || !['uploaded', 'error'].includes(p.status) || !p.video) throw new Error(T('ingest.uploadFailed'))
      show(p)
    }).catch((err) => setError(err.message || T('ingest.uploadFailed')))
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per page load, like the legacy boot
  }, [])

  useEffect(() => {
    if (!project) return
    const id = ++request.current
    const timer = setTimeout(() => {
      const payload = estimatePayload(win, { project, totalFrames, fps })
      if (scenes) payload.scenes = scenes
      fetchEstimate(payload)
        .then((r) => { if (id === request.current) { setEst({ ...payload, ...r }); setStale(false) } })
        .catch((err) => { if (id === request.current) setError(err.message || T('ingest.uploadFailed')) })
    }, ESTIMATE_DEBOUNCE_MS)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- win/fps derive from these
  }, [project, mode, start, end, scenes])

  /** Any change to the window drops the confirmations and the estimate's token until a new estimate lands. */
  function invalidate() {
    setStale(true)
    setFullConfirmed(false)
    setShortAck(false)
  }

  function show(p: Project) {
    const v = p.video || {}
    const range = p.range || [0, lastFrameIndex(v.frames || 1)]
    setProject(p)
    setPending(null)
    setStart(String(range[0]))
    setEnd(String(range[1]))
    setMode(p.mode || 'range')
    setReferenceUi(p.reference === 'ui')
    setScenes(null)
    invalidate()
    fetchFilmstrip(p.id, DEFAULT_FILMSTRIP_COUNT).then(({ frames: f }) => setFrames(f || [])).catch(() => setFrames([]))
  }

  function reset() {
    setProject(null)
    setPending(null)
    setEst(null)
    setFrames([])
    invalidate()
    if (fileInput.current) fileInput.current.value = ''
  }

  async function handleFile(file: File | undefined) {
    if (!file) return
    setError('')
    if (file.size > UPLOAD_LIMIT_BYTES) { setError(T('err.requestTooLarge')); return }
    reset()
    setPending(file.name)
    try {
      const { project: p } = await uploadProject({ title: uploadTitle(file.name), video: file, mode: 'full', start: 0, end: 0 })
      show(p)
    } catch (err) {
      reset()
      setError(uploadErrorText(err))
    }
  }

  function changeWindow(next: { mode?: string; start?: string; end?: string }) {
    if (next.mode !== undefined) setMode(next.mode)
    if (next.start !== undefined) setStart(next.start)
    if (next.end !== undefined) setEnd(next.end)
    setScenes(null)
    invalidate()
  }

  function commitBoundary(index: number, value: string) {
    const rows = sceneBoundaries(est).filter((r) => r.value !== undefined)
    if (!est || String(rows[index]?.value) === value) return
    const values = rows.map((r, i) => Number(i === index ? value : r.value))
    setScenes(editedScenes(est, values, win))
    invalidate()
  }

  function startAnalysis() {
    if (stale || !est) return
    const snapshot = { ...est, acknowledge_short_scenes: shortAck }
    const query = analyzeStartQuery({ project, mode, estimateSnapshot: snapshot, fullConfirmed, shortAcknowledged: shortAck }, win, referenceUi)
    if (!query) return
    saveAnalyzeStart(query, snapshot)
    // A full load, not navigate(): the router commits after the URL moves, and callers that wait for the URL then read
    // the analyze page's nodes at once.
    location.assign(`/analyze${query}`)
  }

  const text = est && project ? estimateText(est, win, { totalFrames, fps }) : null
  const gate = est ? startButtonState({ mode, fullConfirmed, shortAcknowledged: shortAck }, est) : null
  const boundaries = est ? sceneBoundaries(est) : []
  const hasWarnings = Boolean(est?.warnings?.length)
  const startDisabled = !project || stale || !gate || gate.disabled
  const range: [number, number] = mode === 'full' ? [0, last] : [win.start, win.end]
  const pct = (f: number) => `${(f / Math.max(1, last)) * 100}%`

  return (
    <div className="min-h-dvh">
      <AppHeader>
        <WorkflowStepper stage="ingest" projectName={project ? project.title || project.id : undefined} projectStatus={project?.status || ''} />
      </AppHeader>
      <main className="mx-auto grid w-full max-w-[1200px] gap-6 px-4 pt-8 pb-16 md:px-8 md:pt-12 lg:grid-cols-[minmax(0,1fr)_340px] lg:gap-10">
        <section className="flex min-w-0 flex-col gap-6">
          <div>
            <h1 className="m-0 font-display text-display-md font-medium tracking-[-0.03em]">{T('ingest.title')}</h1>
            <p className="mt-2 mb-0 text-body text-ink-muted">{T('ingest.desc')}</p>
          </div>

          <input ref={fileInput} type="file" accept="video/*" hidden data-file-input onChange={(e) => handleFile(e.target.files?.[0])} />

          {!project && !pending && (
            <button
              type="button"
              data-dropzone
              onClick={() => fileInput.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setDrag(true) }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => { e.preventDefault(); setDrag(false); handleFile(e.dataTransfer?.files[0]) }}
              className={cn(
                'state-transition flex min-h-56 flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed px-6 text-center',
                drag ? 'border-brand bg-brand/5' : 'border-hairline bg-surface-1/40 hover:border-ink-muted/40 hover:bg-surface-1',
              )}
            >
              <span className="grid size-12 place-items-center rounded-full bg-surface-2 text-ink"><Upload className="size-5" aria-hidden /></span>
              <span className="text-body-lg font-medium">{T('ingest.pickFile')}</span>
              <span className="text-caption text-ink-muted">{T('ingest.dropHint')}</span>
            </button>
          )}

          {/* stays mounted (hidden) like the legacy markup: scripts and tests read [data-video-name] before a project exists */}
          <div className={cn(card, 'flex items-center gap-4 p-3 pr-2')} data-upload-card hidden={!project && !pending}>
            <div className="relative grid aspect-[16/10] w-20 shrink-0 place-items-center overflow-hidden rounded-lg bg-surface-2 text-ink-muted">
              <Film className={cn('size-5', pending && 'animate-pulse')} aria-hidden />
              {frames[0] && <img src={frames[0]} alt="" className="absolute inset-0 size-full object-cover" />}
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate font-medium" data-video-name>{project ? project.title || project.id : pending}</div>
              <div className="mt-0.5 truncate font-mono text-micro text-ink-muted" data-video-meta>{project ? videoMeta(project.video || {}, fps) : T('ingest.uploading')}</div>
            </div>
            <button type="button" aria-label={T('ingest.removeVideo')} data-remove-video onClick={() => { reset(); setError('') }}
              className="state-transition grid size-11 shrink-0 place-items-center rounded-full text-ink-muted hover:bg-white/5 hover:text-ink">
              <X className="size-4" aria-hidden />
            </button>
          </div>

          {error && <p className="m-0 rounded-xl bg-danger/10 px-4 py-3 text-body-sm text-danger" data-error role="status">{error}</p>}

          {project && (
            <div className={cn(card, 'flex flex-col gap-5 p-4 md:p-5')} data-editor>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <TabsRoot value={mode} onValueChange={(v) => changeWindow({ mode: String(v) })} className="w-auto">
                  <TabsList variant="pill">
                    <TabsTab value="range" data-mode="range" className="min-h-9 px-4 py-1.5 text-body-sm">{T('ingest.range')}</TabsTab>
                    <TabsTab value="full" data-mode="full" className="min-h-9 px-4 py-1.5 text-body-sm">{T('ingest.full')}</TabsTab>
                    <TabsIndicator />
                  </TabsList>
                </TabsRoot>
                <label className="inline-flex min-h-11 cursor-pointer items-center gap-2.5 text-body-sm" data-reference-ui-label>
                  <Checkbox checked={referenceUi} onCheckedChange={setReferenceUi} />
                  {T('ingest.uiReference')}
                </label>
              </div>

              <div className="flex flex-col gap-2">
                <div className="relative grid grid-cols-8 gap-1 overflow-hidden rounded-lg" data-filmstrip>
                  {(frames.length ? frames : Array.from({ length: DEFAULT_FILMSTRIP_COUNT }, () => '')).map((url, i) => (
                    <div key={i} className="filmstrip__frame aspect-video overflow-hidden bg-surface-2">
                      {url && <img src={url} alt="" className="size-full object-cover" />}
                    </div>
                  ))}
                  {mode === 'range' && (
                    <>
                      <div aria-hidden className="pointer-events-none absolute inset-y-0 left-0 bg-canvas/70" style={{ width: pct(range[0]) }} />
                      <div aria-hidden className="pointer-events-none absolute inset-y-0 right-0 bg-canvas/70" style={{ width: `calc(100% - ${pct(range[1])})` }} />
                      <div aria-hidden className="pointer-events-none absolute inset-y-0 rounded-md ring-2 ring-inset ring-brand" style={{ left: pct(range[0]), width: `calc(${pct(range[1])} - ${pct(range[0])})` }} />
                    </>
                  )}
                </div>
                {mode === 'range' && last > 0 && (
                  <SliderRoot value={range} min={0} max={last} step={1} minStepsBetweenValues={0}
                    onValueChange={(v) => Array.isArray(v) && changeWindow({ start: String(v[0]), end: String(v[1]) })}>
                    <SliderControl>
                      <SliderTrack>
                        <SliderIndicator />
                        <SliderThumb index={0} aria-label={T('ingest.startFrame')} />
                        <SliderThumb index={1} aria-label={T('ingest.endFrame')} />
                      </SliderTrack>
                    </SliderControl>
                  </SliderRoot>
                )}
                <div className="text-caption text-ink-muted tabular-nums" data-range-meta>{text?.rangeMeta || '—'}</div>
              </div>

              <div className="flex flex-wrap items-center gap-3" data-range-inputs hidden={mode === 'full'}>
                <span className="mr-1 text-body-sm font-medium">{T('ingest.rangeLabel')}</span>
                <label className="inline-flex items-center gap-2 text-caption text-ink-muted">
                  {T('ingest.startFrame')}
                  <input className={numInput} type="number" min={0} max={last} value={start} data-start onChange={(e) => changeWindow({ start: e.target.value })} />
                </label>
                <label className="inline-flex items-center gap-2 text-caption text-ink-muted">
                  {T('ingest.endFrame')}
                  <input className={numInput} type="number" min={0} max={last} value={end} data-end onChange={(e) => changeWindow({ end: e.target.value })} />
                </label>
              </div>

              {boundaries.length >= 2 && (
                <section className="scene-boundaries flex flex-col gap-3 border-t border-hairline-soft pt-4" data-scene-boundaries aria-labelledby="scene-boundaries-title">
                  <div>
                    <h2 id="scene-boundaries-title" className="m-0 text-body font-semibold">{T('ingest.boundaries')}</h2>
                    <p className="mt-1 mb-0 text-caption text-ink-muted">{T('ingest.boundariesHelp')}</p>
                  </div>
                  <div className="flex flex-col gap-1" data-scene-boundary-list>
                    {boundaries.map((row, i) => (
                      <div key={`${row.id}-${row.range}`} className="scene-boundaries__row flex min-h-11 flex-wrap items-center justify-between gap-3 rounded-lg px-2 text-caption">
                        <span className="font-mono tabular-nums">{row.range}</span>
                        {row.value !== undefined && (
                          <label className="inline-flex items-center gap-2 text-ink-muted">
                            {row.transition} · {T('ingest.boundaryFrame')}
                            <input className={numInput} type="number" min={row.min} max={row.max} defaultValue={row.value} data-boundary-index={i}
                              aria-label={row.ariaLabel} onBlur={(e) => commitBoundary(i, e.target.value)}
                              onKeyDown={(e) => e.key === 'Enter' && commitBoundary(i, e.currentTarget.value)} />
                          </label>
                        )}
                      </div>
                    ))}
                  </div>
                </section>
              )}

              {hasWarnings && (
                <label className="short-scene-confirm inline-flex min-h-11 cursor-pointer items-center gap-2.5 rounded-xl bg-warning/10 px-3 text-body-sm text-warning" data-short-confirm>
                  <Checkbox checked={shortAck} onCheckedChange={setShortAck} />
                  {T('ingest.shortConfirm')}
                </label>
              )}
            </div>
          )}
        </section>

        <aside className="lg:sticky lg:top-24 lg:self-start">
          <div className={cn(card, 'flex flex-col gap-5 p-5')}>
            <h2 className="m-0 text-body font-semibold">{T('ingest.confirm')}</h2>
            <dl className={cn('m-0 flex flex-col text-body-sm transition-opacity', stale && est && 'opacity-60')}>
              {[
                ['ingest.eta', text?.seconds, 'data-est-seconds'],
                ['ingest.gpu', gpu === 'OFF' ? T('ingest.gpuOff') : gpu, 'data-gpu'],
                ['ingest.scenes', text?.scenes, 'data-est-scenes'],
              ].map(([k, v, hook]) => (
                <div key={k} className="flex items-baseline justify-between gap-4 border-b border-hairline-soft py-2.5 last:border-0">
                  <dt className="text-ink-muted">{T(k as string)}</dt>
                  <dd className="m-0 text-right font-medium tabular-nums" {...{ [hook as string]: '' }}>{v || '—'}</dd>
                </div>
              ))}
            </dl>
            {text && <p className="m-0 text-micro leading-relaxed text-ink-muted" data-est-note>{text.note}</p>}

            <div className="flex flex-col gap-3 rounded-xl bg-surface-2 p-3.5 text-body-sm" data-full-confirm hidden={stale || !gate?.needsConfirm}>
              <p className="m-0" data-confirm-copy>{gate?.confirmCopy}</p>
              <Button tone="secondary" size="sm" className="self-start" data-confirm-btn onClick={() => setFullConfirmed(true)}>{T('ingest.confirmGo')}</Button>
            </div>

            <div className="flex flex-col gap-2">
              <Button tone="primary" size="lg" className="w-full" data-start-btn disabled={startDisabled} onClick={startAnalysis}>
                <Play className="size-4" fill="currentColor" aria-hidden />{T('ingest.start')}
              </Button>
              <Button tone="ghost" className="w-full" asChild><a href="/library">{T('common.cancel')}</a></Button>
            </div>
          </div>
        </aside>
      </main>
    </div>
  )
}
