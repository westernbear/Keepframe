import { ChevronDown, ChevronLeft, ChevronRight, Pause, Play } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { AppHeader } from '@/components/app/app-header'
import { Button } from '@/components/app/button'
import { useI18n } from '@/i18n'
import { MAX_ATTACHMENTS, confirmEditBody, confirmedEditOutcome, defaultEditChoices, readChatAttachment, turnView } from '@/lib/agent-chat'
import { confirmCorrection, confirmKeepChange, keepChangePreview, prepareCorrection } from '@/lib/agent-corrections'
import { confidenceLabel, fontFileError, fontMeta, fontUploadErrorKey, sceneOptionLabel } from '@/lib/agent-view'
import { fetchAgentHistory, fetchFonts, fetchReviewState, postAgent, postEdit, reviewAssetUrl, uploadFont } from '@/lib/api'
import { createFrameTransport, createPreviewCache } from '@/lib/playback'
import { buildUIContext } from '@/lib/ui-context'
import { cn } from '@/lib/utils'
import { AeCard } from './ae-card'
import { ChatLog, Composer, clearAttachInput, type Entry } from './chat'
import { RenderCard, useRenderCard } from './render-card'
import { toggleCls, useStoredOpen } from './ui'

const BLANK = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'
const DEFAULT_SCENE_ID = 's1'

export default function Agent() {
  const { T, Tf } = useI18n()
  const [params] = useState(() => new URLSearchParams(location.search))
  const projectId = params.get('project')
  const sceneId = params.get('scene') || DEFAULT_SCENE_ID
  const [st, setSt] = useState<any>(null)
  const [versionId, setVersionId] = useState<string | null>(params.get('v') || null)
  const [frame, setFrameState] = useState(0)
  const [shown, setShown] = useState(-1)
  const [playing, setPlaying] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [banner, setBannerState] = useState<{ msg: string; error: boolean; retry?: () => void } | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])
  const [sending, setSending] = useState(false)
  const [thinking, setThinking] = useState<string | null>(null)
  const [attachmentNames, setAttachmentNames] = useState<string[]>([])
  const [fonts, setFonts] = useState<any[]>([])
  const [fontStatus, setFontStatus] = useState<{ key: string; vars?: any; error?: boolean } | null>(null)
  const [fontBusy, setFontBusy] = useState(false)
  const [elementsOpen, toggleElements] = useStoredOpen('kf.agent.elementsOpen', false)
  const [fontsOpen, toggleFonts] = useStoredOpen('kf.agent.fontsOpen', false)
  const live = useRef({ st: null as any, versionId: params.get('v') || null as string | null, frame: 0, selectedId: null as string | null,
    pendingIntent: null as any, pendingPrompt: '', attachments: [] as { file: File; data: string }[], nextId: 0, fresh: false })
  const choices = useRef<Record<string, string>>({})
  const aeRefresh = useRef<() => Promise<void>>(async () => {})
  const origImg = useRef<HTMLImageElement>(null)
  const reconImg = useRef<HTMLImageElement>(null)
  const fontsInput = useRef<HTMLInputElement>(null)

  const setBanner = useCallback((msg: string, error = false) => setBannerState(msg ? { msg, error } : null), [])
  const rc = useRenderCard({ projectId, sceneId, getVersion: () => live.current.versionId, setBanner })
  const rcRef = useRef(rc)
  rcRef.current = rc

  const [media] = useState(() => {
    const previews = createPreviewCache({ project: projectId, scene: sceneId, version: () => live.current.versionId })
    const apply = (f: number) => { setShown(f); previews.prefetch(f + 1, live.current.st.scene.frames) }
    const transport = createFrameTransport({
      getFrame: () => live.current.frame, setFrameIndex: (n: number) => { live.current.frame = n },
      getFps: () => live.current.st.scene.fps, getFrameCount: () => live.current.st.scene.frames,
      prefetch: (from: number, total: number) => previews.prefetch(from, total), isReady: (i: number) => previews.isReady(i), wait: (i: number) => previews.wait(i),
      showFrame: (n: number) => { setFrameState(n); apply(n) }, canPlay: () => Boolean(live.current.st), onPlayingChange: setPlaying,
    })
    const setFrame = (f: number) => {
      const s = live.current.st
      if (!s) return
      const target = live.current.frame = Math.max(0, Math.min(s.scene.frames - 1, f))
      setFrameState(target)
      previews.prefetch(target, s.scene.frames)
      previews.wait(target).then((ok: boolean) => { if (ok && live.current.frame === target) apply(target) })
    }
    return { previews, transport, setFrame }
  })

  const append = (e: any) => setEntries((list) => [...list, { ...e, id: live.current.nextId++, fresh: live.current.fresh }])
  const appendAgent = (text: string) => append({ kind: 'agent', text })
  const appendConfirm = (onConfirm: () => Promise<boolean | undefined>) => append({ kind: 'confirm', onConfirm })
  const appendChoices = (plan: any) => {
    Object.assign(choices.current, defaultEditChoices(plan))
    append({ kind: 'choices', plan })
  }

  async function loadState() {
    const state = await fetchReviewState(projectId!, sceneId, live.current.versionId)
    live.current.st = state
    live.current.versionId = state.version.id
    setSt(state)
    setVersionId(state.version.id)
    media.setFrame(0)
  }

  async function refreshAfterEdit(version: string) {
    live.current.versionId = version
    setVersionId(version)
    await loadState()
    await aeRefresh.current()
    await rcRef.current.load()
  }
  const ctx = () => ({ projectId: projectId!, sceneId, setBanner, appendAgent, refreshAfterEdit })

  async function applyConfirmedEdit(res: any) {
    const out = confirmedEditOutcome(res, live.current.pendingIntent)
    live.current.pendingIntent = out.intent
    if (out.kind === 'choice') { appendChoices(out.plan); return false }
    if (out.kind === 'done') {
      live.current.attachments = []
      clearAttachInput()
      setAttachmentNames([])
      if (out.verify) append({ kind: 'verify', chip: out.verify })
      appendAgent(out.line)
      await refreshAfterEdit(out.version)
      return true
    }
    setBanner(out.message, true)
    return false
  }

  async function runConfirm(withChoices: boolean) {
    setBanner('')
    try {
      const l = live.current
      const res = await postEdit(confirmEditBody({ project: projectId, scene: sceneId, v: l.versionId, prompt: l.pendingPrompt, attachments: l.attachments.map((a) => a.data),
        element: l.selectedId, intent: l.pendingIntent }, withChoices ? { ...choices.current } : {}))
      return await applyConfirmedEdit(res)
    } catch (err: any) {
      setBannerState({ msg: err.message || T('agent.failed'), error: true, retry: () => runConfirm(withChoices) })
    }
  }

  function paintPending(p: NonNullable<ReturnType<typeof turnView>['pending']>) {
    if (p.kind === 'render') { rcRef.current.select(p.renderPlan).catch((err) => setBanner(err.message || T('agent.failed'), true)); return }
    if (p.kind === 'keep') {
      const preview = keepChangePreview(p.keepChange, live.current.st?.scene.constraints || [])
      if (!preview.text) { setBanner(T('agent.keepPreviewChanged'), true); return }
      appendAgent(preview.text)
      if (!preview.ok) { setBanner(T('agent.keepPreviewChanged'), true); return }
      appendConfirm(() => confirmKeepChange(ctx(), p.keepChange, preview.changes))
      return
    }
    if (p.kind === 'correction') {
      const previewVersion = live.current.versionId!
      prepareCorrection(p.correction, live.current.st.scene).then(({ op, shown: snapshot, lines }) => {
        appendAgent(lines.join('\n'))
        appendConfirm(() => confirmCorrection(ctx(), op, snapshot, previewVersion, sceneId))
      }).catch(() => setBanner(T('agent.correctionInvalid'), true))
      return
    }
    live.current.pendingIntent = p.intent
    if (p.needsChoice) appendChoices(p.plan)
    if (p.needsConfirm) appendConfirm(() => runConfirm(false))
  }

  function paintTurn(turn: any, actionable = true, seconds: number | null = null) {
    const view = turnView(turn, actionable)
    const steps = (turn.reasoning || []).filter((s: unknown) => typeof s === 'string' && s.trim())
    if (steps.length) append({ kind: 'reasoning', steps, seconds })
    view.toolCalls.forEach((tc) => append({ kind: 'tool', ...tc, reply: view.reply }))
    if (view.reply) appendAgent(view.reply)
    if (view.verify) append({ kind: 'verify', chip: view.verify })
    if (view.pending) paintPending(view.pending)
    if (view.error) setBanner(view.error, true)
  }

  async function send(text: string, again = false) {
    if (!text || !projectId) return
    live.current.fresh = true
    live.current.pendingPrompt = text
    if (!again) append({ kind: 'user', text })
    setBanner('')
    setThinking(T('agent.readingScreen'))
    setSending(true)
    try {
      const l = live.current
      const uiContext = await buildUIContext({ project: projectId, scene: sceneId, version: l.versionId, frame: l.frame, selectedElement: l.selectedId,
        attachmentFiles: l.attachments.map((a) => a.file), reviewState: l.st, renderPayload: rcRef.current.payload, orig: origImg.current!, recon: reconImg.current! })
      setThinking(T('agent.thinking'))
      const started = performance.now()
      const turn = await postAgent({ project: projectId, scene: sceneId, v: l.versionId, message: text, ui_context: uiContext })
      paintTurn(turn, true, Math.max(1, Math.round((performance.now() - started) / 1000)))
    } catch (err: any) {
      setBannerState({ msg: err.message || T('agent.failed'), error: true, retry: () => send(text, true) })
    } finally { setSending(false); setThinking(null) }
  }

  async function attach(files: File[]) {
    const room = MAX_ATTACHMENTS - live.current.attachments.length
    if (files.length > room) setBanner(Tf('agent.attachmentLimit', { n: MAX_ATTACHMENTS }), true)
    try {
      for (const file of files.slice(0, Math.max(0, room))) live.current.attachments.push({ file, data: await readChatAttachment(file) })
    } catch {
      setBanner(T('agent.failed'), true)
    }
    setAttachmentNames(live.current.attachments.map((a) => a.file.name))
  }

  function removeAttachment(index: number) {
    live.current.attachments.splice(index, 1)
    setAttachmentNames(live.current.attachments.map((a) => a.file.name))
  }

  async function sendFont(file: File) {
    const refused = fontFileError(file)
    if (refused) { setFontStatus({ key: refused, error: true }); return }
    setFontBusy(true)
    setFontStatus({ key: 'fonts.uploading' })
    try {
      const { font, created } = await uploadFont(projectId!, file)
      if (created) setFonts((list) => [...list, font])
      setFontStatus({ key: created ? 'fonts.added' : 'fonts.duplicate', vars: { family: font.family } })
    } catch (err: any) {
      setFontStatus({ key: fontUploadErrorKey(err.body && err.body.error), error: true })
    } finally {
      setFontBusy(false)
      if (fontsInput.current) fontsInput.current.value = ''
    }
  }

  const onAeReady = useCallback((refresh: () => Promise<void>) => { aeRefresh.current = refresh }, [])

  useEffect(() => { document.title = `${T('agent.title')} · Keepframe` }, [T])
  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    fetchFonts(projectId).then((data) => !cancelled && setFonts((data && data.fonts) || [])).catch(() => setFontStatus({ key: 'fonts.error.load', error: true }))
    loadState()
      .then(() => aeRefresh.current())
      .then(async () => {
        const history = await fetchAgentHistory(projectId, sceneId, null, 50)
        if (cancelled) return
        for (const record of history.turns || []) {
          append({ kind: 'user', text: record.user || '' })
          if (record.actionable) live.current.pendingPrompt = record.user || ''
          paintTurn(record.turn || {}, record.actionable !== false)
        }
        live.current.fresh = true
      })
      .then(() => rcRef.current.load())
      .catch((err) => setBanner(err.message || T('agent.failed'), true))
    return () => { cancelled = true; media.transport.setPlaying(false) }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- boot once per page load
  }, [])

  const scenes = st?.project?.scenes || []
  const elements = st?.scene.elements || []
  const empty = !projectId ? T('agent.needProject') : entries.length ? null : T('agent.empty')
  const step = (d: number) => { media.transport.setPlaying(false); media.setFrame(live.current.frame + d) }
  const reviewHref = `/review?project=${encodeURIComponent(projectId || '')}&scene=${encodeURIComponent(sceneId)}`
  const disclosureHead = cn(toggleCls, 'agent-elements__head w-full justify-between')

  const context = (
    <div className="flex min-w-0 items-center gap-2">
      <span id="agent-scene" className="rounded-md bg-surface-1 px-3 py-1.5 font-mono text-caption text-ink-muted">{st ? `${sceneId} · ${st.scene.frames}f` : '—'}</span>
      <select id="agent-scene-select" aria-label={T('agent.sceneSelect')} hidden={scenes.length < 2} value={sceneId}
        className="h-9 min-w-0 rounded-md border border-hairline bg-surface-1 px-3 text-body-sm text-ink outline-none focus-visible:border-brand"
        onChange={(e) => location.assign(`/agent?project=${encodeURIComponent(projectId!)}&scene=${encodeURIComponent(e.target.value)}`)}>
        {scenes.map((s: any) => <option key={s.id} value={s.id}>{sceneOptionLabel(s)}</option>)}
      </select>
    </div>
  )

  return (
    <div className="flex h-dvh flex-col max-[860px]:h-auto max-[860px]:min-h-dvh">
      <AppHeader context={context} extra={[{ href: reviewHref, label: T('agent.backReview') }]} />
      <main id="agent-root" className="agent-shell flex min-h-0 flex-1 overflow-hidden max-[860px]:flex-col max-[860px]:overflow-visible">
        <aside className="agent-chat flex w-[400px] shrink-0 flex-col border-r border-hairline-soft max-[860px]:h-[50vh] max-[860px]:w-full max-[860px]:border-r-0 max-[860px]:border-b">
          <div className="agent-chat__head flex min-h-12 items-center justify-between gap-3 border-b border-hairline-soft px-4">
            <h2 className="m-0 text-body-sm font-semibold">{T('agent.title')}</h2>
            <span className="agent-chat__meta font-mono text-micro text-ink-muted" id="agent-model">{st ? T('agent.modelLabel') : '—'}</span>
          </div>
          <ChatLog entries={entries} empty={empty} choices={choices.current} onChoose={() => runConfirm(true)} thinking={thinking} />
          <Composer onSend={send} sending={sending} attachments={attachmentNames} onAttach={attach} onRemoveAttachment={removeAttachment} />
          <div id="agent-banner" role="status" hidden={!banner}
            className={cn('agent-banner flex items-center justify-between gap-3 border-t border-hairline-soft px-4 py-2.5 text-caption', banner?.error ? 'agent-banner--error bg-danger/10 text-danger' : 'text-ink-muted')}>
            <span className="min-w-0">{banner?.msg}</span>
            {banner?.retry && <Button tone="secondary" size="sm" className="shrink-0" disabled={sending} onClick={banner.retry} data-retry>{T('common.retry')}</Button>}
          </div>
        </aside>

        <section className="agent-viewer flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-y-auto p-4 md:p-5 max-[860px]:h-[70vh] max-[860px]:min-h-[50vh] max-[860px]:flex-none">
          <div className="compare-panes grid shrink-0 gap-3 sm:grid-cols-2">
            {([['orig', origImg, 'agent.orig', 'agent.origAlt'], ['recon', reconImg, 'agent.recon', 'agent.reconAlt']] as const).map(([kind, ref, label, alt]) => (
              <div key={kind} className="compare-pane flex min-w-0 flex-col gap-1.5">
                <div className={cn('compare-pane__head text-caption', kind === 'recon' ? 'text-ink' : 'text-ink-muted')}>{T(label)}</div>
                <div className="compare-pane__body overflow-hidden rounded-xl bg-black ring-1 ring-inset ring-hairline">
                  <img id={`agent-${kind}`} ref={ref} alt={T(alt)} className="block h-auto w-full" src={shown >= 0 ? media.previews.src(kind, shown) : BLANK} />
                </div>
              </div>
            ))}
          </div>
          <div className="agent-transport flex shrink-0 items-center gap-1.5">
            <button type="button" id="agent-play" aria-label={T(playing ? 'review.pause' : 'review.play')} onClick={() => media.transport.setPlaying(!media.transport.isPlaying())}
              className={cn('grid size-11 place-items-center rounded-md bg-ink text-black transition-transform active:scale-95', playing && 'is-playing')}>
              <Play id="agent-play-icon" className="size-4 translate-x-px" fill="currentColor" aria-hidden {...{ hidden: playing }} />
              <Pause id="agent-pause-icon" className="size-4" fill="currentColor" aria-hidden {...{ hidden: !playing }} />
            </button>
            <button type="button" id="agent-prev" aria-label={T('review.stepBack')} onClick={() => step(-1)} className="transport__step grid size-11 place-items-center rounded-md text-ink-muted hover:bg-white/5 hover:text-ink"><ChevronLeft className="size-4" aria-hidden /></button>
            <button type="button" id="agent-next" aria-label={T('review.stepFwd')} onClick={() => step(1)} className="transport__step grid size-11 place-items-center rounded-md text-ink-muted hover:bg-white/5 hover:text-ink"><ChevronRight className="size-4" aria-hidden /></button>
            <span className="ml-2 font-mono text-body-sm font-semibold tabular-nums"><span id="agent-frame">{frame}</span> <span className="font-normal text-ink-muted">/ <span id="agent-total">{st?.scene.frames ?? 0}</span></span></span>
          </div>

          <RenderCard r={rc} projectId={projectId} reviewStatus={st?.status} />
          <AeCard projectId={projectId} sceneId={sceneId} versionId={versionId} onReady={onAeReady} />

          <div className="agent-elements rounded-2xl bg-surface-1 px-4 py-1 ring-1 ring-inset ring-hairline">
            <button type="button" id="agent-elements-toggle" className={disclosureHead} aria-expanded={elementsOpen} aria-controls="agent-elements-list" onClick={toggleElements}>
              <span id="agent-count">{Tf('agent.elementCount', { n: elements.length })}</span>
              <ChevronDown aria-hidden className={cn('size-4 text-ink-muted transition-transform', !elementsOpen && '-rotate-90')} />
            </button>
            <div id="agent-elements-list" className="agent-elements__list flex flex-col gap-0.5 pb-3" hidden={!elementsOpen}>
              {elements.map((el: any) => {
                const tex = el.canonical && el.canonical.texture
                return (
                  <button key={el.id} type="button" data-id={el.id} onClick={() => { live.current.selectedId = el.id; setSelectedId(el.id) }}
                    className={cn('element-row state-transition flex min-h-10 items-center gap-3 rounded-lg px-2 text-left text-caption', el.id === selectedId ? 'element-row--selected bg-white/[0.07]' : 'hover:bg-white/[0.04]')}>
                    {tex ? <img className="element-thumb size-7 rounded bg-surface-2 object-contain" alt="" src={reviewAssetUrl(tex, projectId!, sceneId)} /> : <span className="element-thumb size-7 rounded bg-surface-2" />}
                    <span className="element-row__id font-mono text-brand-soft">{el.id}</span>
                    <span className="element-row__kind font-mono text-ink-muted">{el.kind}</span>
                    <span className="element-row__conf ml-auto font-mono text-micro text-ink-muted tabular-nums">{confidenceLabel(el)}</span>
                  </button>
                )
              })}
            </div>
          </div>

          <div className="agent-elements agent-fonts rounded-2xl bg-surface-1 px-4 py-1 ring-1 ring-inset ring-hairline">
            <button type="button" id="agent-fonts-toggle" className={disclosureHead} aria-expanded={fontsOpen} aria-controls="agent-fonts-body" onClick={toggleFonts}>
              <span id="agent-fonts-count">{fonts.length ? Tf('fonts.count', { n: fonts.length }) : T('fonts.title')}</span>
              <ChevronDown aria-hidden className={cn('size-4 text-ink-muted transition-transform', !fontsOpen && '-rotate-90')} />
            </button>
            <div id="agent-fonts-body" className="agent-fonts__body flex flex-col gap-3 pb-4" hidden={!fontsOpen}>
              <ul id="agent-fonts-list" className="agent-fonts__list m-0 flex list-none flex-col p-0" aria-label={T('fonts.listLabel')}>
                {fonts.map((font, i) => (
                  <li key={`${font.family}-${i}`} className="font-row flex min-h-9 items-baseline gap-2.5 border-b border-hairline-soft py-2 text-body-sm last:border-0">
                    <span className="font-row__family min-w-0 truncate" title={font.family}>{font.family}</span>
                    <span className="font-row__meta mono ml-auto shrink-0 font-mono text-micro text-ink-muted">{fontMeta(font)}</span>
                  </li>
                ))}
              </ul>
              <p id="agent-fonts-empty" className="agent-fonts__empty m-0 text-caption text-ink-muted" hidden={fonts.length > 0}>{T('fonts.empty')}</p>
              <div className="agent-fonts__actions flex flex-wrap items-center gap-3">
                <input type="file" id="agent-fonts-input" ref={fontsInput} accept=".ttf,.otf,.woff2" hidden onChange={(e) => e.target.files?.[0] && sendFont(e.target.files[0])} />
                <Button tone="secondary" size="sm" id="agent-fonts-upload" className="btn--inline" aria-describedby="agent-fonts-formats" disabled={fontBusy} onClick={() => fontsInput.current?.click()}>{T('fonts.upload')}</Button>
                <span id="agent-fonts-formats" className="agent-fonts__hint text-caption text-ink-muted">{T('fonts.formats')}</span>
              </div>
              <p id="agent-fonts-note" className="render-card__lock m-0 text-caption text-ink-muted">{T('fonts.note')}</p>
              <p id="agent-fonts-status" role="status" aria-live="polite" hidden={!fontStatus}
                className={fontStatus?.error ? 'agent-fonts__status agent-fonts__status--error' : 'agent-fonts__status'}>
                {fontStatus ? Tf(fontStatus.key, fontStatus.vars || {}) : ''}
              </p>
            </div>
          </div>
        </section>
      </main>
    </div>
  )
}
