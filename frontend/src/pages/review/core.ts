// The review workspace's imperative core (legacy review/*.js): frame transport, overlay requests, state reloads,
// correction jobs. It owns the live values async work reads; React renders from the view it patches.
import { T, Tf, errorText } from '@/i18n'
import {
  fetchAnalysisOverlay, fetchProjects, fetchReviewJob, fetchReviewState, postApprove, postCorrect, postEdit, postKeep,
} from '@/lib/api'
import { editBody, editResultView, formatVerifyPair, keepChanges, reference3dIntent } from '@/lib/corrections'
import { readFileAsDataUrl } from '@/lib/files'
import { agentUrl, isApproved, isLatestVersion, nextSelection, nextUnapprovedScene, reviewUrl } from '@/lib/inspector'
import { createFrameTransport, createPreviewCache, frameStep, seekDelayMs } from '@/lib/playback'
import {
  DEFAULT_SCENE_ID, JOB_POLL_INTERVAL_MS, KEEP_NOTE, LOADING_PCT_LIST_BASE, LOADING_PCT_LIST_SPAN, LOADING_PCT_START, clampFrame,
  dragBox, idleOverlayStatusKey, isBoxBigEnough, overlayStatusKey,
} from '@/lib/review-geom'

export type Tool = 'edit' | 'reassign' | 'mask' | 'bbox' | 'text'
export interface EditView { prompt: string; file: File | null; summary: string; conflicts: any[]; needElement: boolean; needsConfirm: boolean; choices: Record<string, string>; busy: boolean }
export interface ReviewView {
  projectId: string | null
  projectTitle: string
  sceneId: string
  versionId: string | null
  st: any
  loading: { pct: number; msg: string } | null
  banner: { msg: string; error: boolean } | null
  frame: number
  shownFrame: number
  shownVersion: string | null
  playing: boolean
  frameLoading: boolean
  frameError: boolean
  selectedId: string | null
  overlay: any
  overlayEnabled: boolean
  overlayOpacity: number
  statusKey: string
  editRegion: boolean
  drag: number[] | null
  jobRunning: boolean
  approveBusy: boolean
  keepPending: Map<string, boolean>
  filter: string
  tool: Tool
  forms: { reassignFrom: string; reassignTo: string; reassignFrames: string; maskFrame: string; maskFile: File | null; bboxFrame: string; bboxCoords: string; textValue: string }
  edit: EditView
}

const EMPTY_EDIT: EditView = { prompt: '', file: null, summary: '', conflicts: [], needElement: false, needsConfirm: false, choices: {}, busy: false }

export function initialView(search: string): ReviewView {
  const params = new URLSearchParams(search)
  const projectId = params.get('project')
  return {
    projectId, projectTitle: '', sceneId: params.get('scene') || DEFAULT_SCENE_ID, versionId: params.get('v') || null,
    st: null, loading: projectId ? { pct: LOADING_PCT_START, msg: T('review.loading') } : null, banner: null,
    frame: 0, shownFrame: -1, shownVersion: null, playing: false, frameLoading: false, frameError: false,
    selectedId: null, overlay: null, overlayEnabled: true, overlayOpacity: 0.25, statusKey: 'review.overlayLoading',
    editRegion: false, drag: null, jobRunning: false, approveBusy: false, keepPending: new Map(), filter: '', tool: 'bbox',
    forms: { reassignFrom: '', reassignTo: '', reassignFrames: '', maskFrame: '', maskFile: null, bboxFrame: '', bboxCoords: '', textValue: '' },
    edit: EMPTY_EDIT,
  }
}

type Patch = Partial<ReviewView> | ((v: ReviewView) => Partial<ReviewView>)

export function createReviewCore(start: ReviewView, patch: (p: Patch) => void) {
  const live = {
    projectId: start.projectId, sceneId: start.sceneId, versionId: start.versionId, st: null as any,
    frame: 0, shownFrame: -1, selectedId: null as string | null, overlay: null as any, overlayEnabled: true,
    editRegion: false, drag: null as null | { x0: number; y0: number; x1: number; y1: number },
    frameRequest: 0, overlayRequest: 0, stateReq: 0, imgTimer: 0 as any, pollTimer: 0 as any,
    overlayController: null as AbortController | null, waitingForCorrection: false, pendingIntent: null as any,
    keepPending: new Map<string, boolean>(),
  }
  const latest = () => Boolean(live.st) && isLatestVersion(live.versionId, live.st.project.versions)
  const banner = (msg: string, error = false) => patch({ banner: msg ? { msg, error } : null })

  const previews = createPreviewCache({ project: live.projectId, scene: () => live.sceneId, version: () => live.versionId, kinds: ['orig'] })

  function clearOverlay() {
    ++live.overlayRequest
    live.overlayController?.abort()
    live.overlay = null
    patch({ overlay: null })
  }

  async function loadOverlay(f: number, v: string | null) {
    clearOverlay()
    if (!live.overlayEnabled) return
    if (!live.st.analysis) return patch({ statusKey: 'review.noAnalysis' })
    const request = live.overlayRequest
    const controller = live.overlayController = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 5000)
    patch({ statusKey: 'review.overlayLoading' })
    try {
      const data = await fetchAnalysisOverlay(live.projectId!, live.sceneId, f, v, controller.signal)
      if (request !== live.overlayRequest || live.frame !== f || live.shownFrame !== f || live.versionId !== v) return
      if (data.frame !== f || data.version !== v || data.scene !== live.sceneId || data.snapshot !== live.st.analysis.snapshot) throw new Error('overlay mismatch')
      live.overlay = data
      patch({ overlay: data, statusKey: overlayStatusKey(data) })
    } catch {
      if (request !== live.overlayRequest) return
      live.overlay = null
      patch({ overlay: null, statusKey: 'review.overlayFailed' })
    } finally { clearTimeout(timeout) }
  }

  function applyReadyFrame(f: number) {
    clearOverlay()
    live.shownFrame = f
    patch({ frame: live.frame, shownFrame: f, shownVersion: live.versionId, frameLoading: false, frameError: false })
    loadOverlay(f, live.versionId)
    previews.prefetch(f + 1, live.st.scene.frames)
  }

  async function loadFrameImages() {
    live.imgTimer = 0
    if (!live.st) return
    const f = live.frame, v = live.versionId, request = ++live.frameRequest
    previews.prefetch(f, live.st.scene.frames)
    patch({ frameLoading: !previews.isReady(f), frameError: false })
    const ok = await previews.wait(f)
    if (request !== live.frameRequest || live.frame !== f || live.versionId !== v) return
    if (ok) applyReadyFrame(f)
    else { patch({ frameLoading: false, frameError: true }); clearOverlay() }
  }

  const transport = createFrameTransport({
    getFrame: () => live.frame, setFrameIndex: (next: number) => { live.frame = next },
    getFps: () => live.st.scene.fps, getFrameCount: () => live.st.scene.frames,
    prefetch: (from: number, total: number) => previews.prefetch(from, total), isReady: (i: number) => previews.isReady(i), wait: (i: number) => previews.wait(i),
    showFrame: (next: number) => applyReadyFrame(next), canPlay: () => Boolean(live.st), onPlayingChange: (playing: boolean) => patch({ playing }),
    onFrameUnavailable: () => { clearOverlay(); patch({ frameError: true }) },
  })

  function setPlaying(on: boolean) {
    if (on) { clearTimeout(live.imgTimer); ++live.frameRequest; setRegionMode(false) }
    transport.setPlaying(on)
  }

  function setFrame(f: number, immediate = false) {
    if (!live.st) return
    setPlaying(false)
    ++live.frameRequest
    clearOverlay()
    live.drag = null
    live.frame = clampFrame(f, live.st.scene.frames)
    patch({ frame: live.frame, drag: null })
    clearTimeout(live.imgTimer)
    if (immediate) return loadFrameImages()
    live.imgTimer = setTimeout(loadFrameImages, seekDelayMs(false))
  }

  function invalidateFrames() {
    ++live.frameRequest
    clearTimeout(live.imgTimer)
    clearOverlay()
    live.shownFrame = -1
    live.drag = null
    patch({ shownFrame: -1, drag: null })
    setRegionMode(false)
  }

  function setRegionMode(on: boolean) {
    live.editRegion = Boolean(on && live.selectedId && latest())
    if (live.editRegion) transport.setPlaying(false)
    live.drag = null
    patch({
      editRegion: live.editRegion, drag: null,
      statusKey: live.editRegion ? 'review.drawHint' : idleOverlayStatusKey(live.overlay, live.st?.analysis),
    })
  }

  function selectElement(id: string | null) {
    live.selectedId = id
    const item = live.st?.scene.elements.find((e: any) => e.id === id)
    patch((v) => ({
      selectedId: id,
      forms: { ...v.forms, reassignFrom: id || '', bboxFrame: String(live.frame), maskFrame: String(live.frame), textValue: item?.canonical?.text || '' },
    }))
  }

  async function refreshState(v: string | null) {
    const request = ++live.stateReq
    const keepFrame = live.frame, keepSel = live.selectedId
    setPlaying(false)
    invalidateFrames()
    patch({ loading: { pct: LOADING_PCT_START, msg: T('review.loading') } })
    try {
      const state = await fetchReviewState(live.projectId!, live.sceneId, v)
      if (request !== live.stateReq) return
      live.st = state
      live.versionId = state.version.id
      history.replaceState(history.state, '', reviewUrl(live.projectId!, live.sceneId, live.versionId!))
      const historical = !isLatestVersion(live.versionId, state.project.versions)
      patch((view) => ({
        st: state, versionId: live.versionId, sceneId: live.sceneId,
        loading: { pct: LOADING_PCT_LIST_BASE + LOADING_PCT_LIST_SPAN, msg: T('review.loadingList') },
        statusKey: historical && !state.analysis ? 'review.noAnalysis' : 'review.overlayLoading',
        forms: { ...view.forms, reassignTo: state.scene.elements[0]?.id || '' },
      }))
      selectElement(nextSelection(keepSel, state.scene.elements))
      await setFrame(Math.max(0, Math.min(keepFrame, state.scene.frames - 1)), true)
      if (request === live.stateReq) patch({ loading: null })
    } catch (err: any) {
      if (request !== live.stateReq) return
      patch({ loading: null, banner: { msg: err.message || T('review.loadFailed'), error: true } })
    }
  }

  function applyJobSnapshot(job: any) {
    if (job.status === 'running') {
      live.waitingForCorrection = true
      patch({ jobRunning: true, banner: { msg: Tf('review.running', { op: job.op || '' }), error: false } })
      return
    }
    clearInterval(live.pollTimer)
    if (job.status === 'done' && job.version) {
      if (!live.waitingForCorrection) return
      live.waitingForCorrection = false
      patch({ jobRunning: false, banner: null })
      refreshState(job.version)
      return
    }
    if (job.status === 'error') {
      patch({ jobRunning: false, banner: { msg: Tf('review.retry', { err: errorText(job.error) || T('review.error') }), error: true } })
      return
    }
    patch({ jobRunning: false, banner: null })
  }

  async function pollJob() {
    if (!live.projectId) return
    try { applyJobSnapshot(await fetchReviewJob(live.projectId, live.sceneId)) } catch { /* transient poll errors */ }
  }

  async function runCorrect(op: string, args: Record<string, unknown>) {
    if (!latest()) { banner(T('review.latestOnly'), true); return }
    setRegionMode(false)
    try { await postCorrect(live.projectId!, live.sceneId, op, { ...args, version: live.versionId }) }
    catch (err: any) { banner(err.message, true); return }
    live.waitingForCorrection = true
    clearInterval(live.pollTimer)
    live.pollTimer = setInterval(pollJob, JOB_POLL_INTERVAL_MS)
    pollJob()
  }

  async function switchScene(sceneId: string) {
    if (!sceneId || sceneId === live.sceneId) return
    setPlaying(false)
    Object.assign(live, { sceneId, versionId: null, selectedId: null, frame: 0, shownFrame: -1 })
    patch({ sceneId, versionId: null, selectedId: null, frame: 0 })
    await refreshState(null)
    pollJob()
  }

  function goAgent() {
    location.assign(agentUrl(live.projectId!, live.sceneId, live.versionId)) // full page load: the router commits after the URL moves, and callers that wait for the URL read the next page at once
  }

  async function approve() {
    if (!live.projectId || !live.st) return
    if (isApproved(live.st.status)) { goAgent(); return }
    patch({ approveBusy: true })
    try {
      const result = await postApprove(live.projectId, live.sceneId, live.versionId)
      const next = nextUnapprovedScene(live.st.project.scenes, result.project?.approved_scenes || {})
      patch({ approveBusy: false })
      if (next) await switchScene(next.id)
      else goAgent()
    } catch (err: any) {
      patch({ approveBusy: false, banner: { msg: err.message || T('review.approveFailed'), error: true } })
    }
  }

  function setKeep(pred: string, keep: boolean) {
    live.keepPending.set(pred, keep)
    patch({ keepPending: new Map(live.keepPending) })
  }

  async function saveKeep(preset?: string) {
    try {
      const res = await postKeep(live.projectId!, live.sceneId, preset ? [] : keepChanges(live.keepPending), KEEP_NOTE, preset)
      live.keepPending.clear()
      patch({ keepPending: new Map() })
      refreshState(res.version.id)
    } catch (err: any) { banner(err.message, true) }
  }

  function setEdit(p: Partial<EditView>) { patch((v) => ({ edit: { ...v.edit, ...p } })) }
  function resetEdit() { live.pendingIntent = null; setEdit({ summary: '', conflicts: [], needElement: false, needsConfirm: false, choices: {} }) }

  async function runEdit(edit: EditView, confirm: boolean) {
    if (!live.projectId || (confirm && !live.pendingIntent)) return
    setEdit({ busy: true })
    try {
      const res = await postEdit(editBody({
        projectId: live.projectId, sceneId: live.sceneId, versionId: live.versionId, prompt: edit.prompt, selectedId: live.selectedId,
        attachment: await readFileAsDataUrl(edit.file), confirm, pendingIntent: live.pendingIntent, choices: edit.choices,
      }))
      const view = editResultView(res)
      live.pendingIntent = view.pendingIntent
      setEdit({ summary: view.summary, conflicts: view.conflicts, needElement: view.needElement, needsConfirm: view.needsConfirm, choices: {} })
      if (confirm && res.status === 'done' && res.version) {
        banner(Tf('review.editOk', formatVerifyPair(res)))
        resetEdit()
        await refreshState(res.version.id)
      } else if (res.status === 'failed') banner(res.error || T('review.editFailed'), true)
    } catch (err: any) {
      banner(err.message || T('review.editFailed'), true)
    } finally { setEdit({ busy: false }) }
  }

  function previewReference3d(id: string) {
    resetEdit()
    const edit = { ...EMPTY_EDIT, prompt: T('review.generate3dPrompt') }
    patch({ tool: 'edit', edit })
    live.pendingIntent = reference3dIntent(id)
    runEdit(edit, false)
  }

  // region drawing, in scene px
  function dragStart(pt: number[]) {
    if (!live.editRegion || live.shownFrame !== live.frame) return false
    live.drag = { x0: pt[0], y0: pt[1], x1: pt[0], y1: pt[1] }
    patch({ drag: dragBox(live.drag) })
    return true
  }
  function dragMove(pt: number[]) {
    if (!live.drag) return
    live.drag.x1 = pt[0]; live.drag.y1 = pt[1]
    patch({ drag: dragBox(live.drag) })
  }
  function dragEnd(pt: number[] | null) {
    if (!live.drag) return
    if (pt) { live.drag.x1 = pt[0]; live.drag.y1 = pt[1] }
    const box = dragBox(live.drag)
    live.drag = null
    if (pt && isBoxBigEnough(box)) patch((v) => ({ drag: null, tool: 'bbox', forms: { ...v.forms, bboxCoords: box.join(','), bboxFrame: String(live.shownFrame) } }))
    else patch({ drag: null })
  }

  function setOverlayEnabled(on: boolean) {
    live.overlayEnabled = on
    patch({ overlayEnabled: on })
    if (on && live.shownFrame === live.frame && live.st) loadOverlay(live.frame, live.versionId)
    else clearOverlay()
  }

  function onKey(e: KeyboardEvent) {
    const target = e.target as HTMLElement
    if (!live.st || target?.matches?.('input,select,textarea,button,[role=button],[role=slider],[role=tab]')) return
    if (e.code === 'Space') { e.preventDefault(); setPlaying(!transport.isPlaying()) }
    if (e.code === 'ArrowLeft' || e.code === 'ArrowRight') { e.preventDefault(); setFrame(live.frame + (e.code === 'ArrowLeft' ? -1 : 1) * frameStep(e.shiftKey)) }
    if (e.code === 'Escape') setRegionMode(false)
  }

  async function boot() {
    if (!live.projectId) return
    await refreshState(live.versionId)
    fetchProjects().then(({ projects }) => {
      const row = (projects || []).find((p: any) => p.id === live.projectId)
      if (row?.title) patch({ projectTitle: row.title })
    }).catch(() => {})
    pollJob()
  }

  function dispose() {
    transport.setPlaying(false)
    clearTimeout(live.imgTimer)
    clearInterval(live.pollTimer)
    live.overlayController?.abort()
  }

  return {
    live, previews, boot, dispose, onKey, refreshState, setFrame, setPlaying, isPlaying: () => transport.isPlaying(),
    selectElement, setRegionMode, setOverlayEnabled, runCorrect, switchScene, approve, setKeep, saveKeep, runEdit, resetEdit,
    previewReference3d, dragStart, dragMove, dragEnd, setEdit, patch,
    patchForms: (p: Partial<ReviewView['forms']>) => patch((v) => ({ forms: { ...v.forms, ...p } })),
    setOpacity: (n: number) => patch({ overlayOpacity: n }),
    retryFrame: () => { previews.clear(); setFrame(live.frame, true) },
  }
}

export type ReviewCore = ReturnType<typeof createReviewCore>
