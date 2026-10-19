import { ChevronLeft, ChevronRight, Pause, Play } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/app/button'
import Slider from '@/components/smoothui/slider'
import Switch from '@/components/smoothui/switch'
import { useI18n } from '@/i18n'
import { imageRect, placeLabel, regionPath, scenePoint, timecode } from '@/lib/review-geom'
import { objectColor } from './colors'
import type { ReviewCore, ReviewView } from './core'

/** Original frame with the analysis layer drawn over its letterboxed area, plus the region-draw surface. */
export function Viewer({ view, core }: { view: ReviewView; core: ReviewCore }) {
  const { T } = useI18n()
  const pane = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const st = view.st
  const scene = st?.scene

  useEffect(() => {
    const node = pane.current
    if (!node) return
    const ro = new ResizeObserver(([e]) => setSize({ width: e.contentRect.width, height: e.contentRect.height }))
    ro.observe(node)
    return () => ro.disconnect()
  }, [])

  const r = scene ? imageRect({ left: 0, top: 0, ...size }, scene.size) : null
  const drawn = Boolean(scene && view.shownFrame === view.frame && view.overlayEnabled && view.overlay?.version === view.versionId && view.overlay.frame === view.shownFrame)
  const placed: any[] = []
  const point = (e: React.PointerEvent) => {
    const rect = pane.current!.getBoundingClientRect()
    return scenePoint(e.clientX, e.clientY, imageRect(rect, scene.size), scene.size)
  }

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="analysis-controls flex flex-wrap items-center gap-x-5 gap-y-2 text-body-sm">
        <strong className="font-semibold">{T('review.analysisTitle')}</strong>
        <label className="inline-flex min-h-11 items-center gap-2 text-ink-muted">
          <Switch checked={view.overlayEnabled} onCheckedChange={core.setOverlayEnabled} aria-label={T('review.analysisLayer')} />
          {T('review.analysisLayer')}
        </label>
        <div className="flex min-w-[180px] flex-1 items-center gap-3 sm:max-w-[260px]">
          <span id="overlay-opacity-label" className="shrink-0 text-ink-muted">{T('review.opacity')}</span>
          <div className="min-w-0 flex-1" id="overlay-opacity">
            <Slider aria-label={T('review.opacity')} min={0} max={100} value={Math.round(view.overlayOpacity * 100)} onValueChange={(n) => core.setOpacity(n / 100)} />
          </div>
          <output id="opacity-value" className="w-10 shrink-0 text-right text-caption text-ink-muted tabular-nums">{Math.round(view.overlayOpacity * 100)}%</output>
        </div>
        <Button tone={view.editRegion ? 'primary' : 'secondary'} size="sm" id="region-mode" aria-pressed={view.editRegion}
          disabled={!view.selectedId || !st || st.version.id !== st.project.versions.at(-1)?.id}
          title={st && st.version.id !== st.project.versions.at(-1)?.id ? T('review.latestOnly') : ''}
          onClick={() => core.setRegionMode(!view.editRegion)}>
          {T('review.regionMode')}
        </Button>
      </div>
      <p id="overlay-status" className="analysis-status m-0 min-h-5 text-caption text-ink-muted" role="status">
        {T(view.overlayEnabled ? view.statusKey : 'review.layerHidden')}
      </p>

      <div
        ref={pane}
        id="orig-pane"
        className="relative w-full overflow-hidden rounded-xl bg-black ring-1 ring-inset ring-hairline"
        style={{ aspectRatio: scene ? `${scene.size[0]} / ${scene.size[1]}` : '16 / 9', maxHeight: '58vh' }}
      >
        <img
          id="orig"
          className="absolute inset-0 size-full object-contain"
          src={view.shownFrame >= 0 ? core.previews.src('orig', view.shownFrame) : undefined}
          alt={view.shownFrame >= 0 ? T('review.orig') : ''}
          data-frame={view.shownFrame >= 0 ? view.shownFrame : undefined}
          data-version={view.shownFrame >= 0 ? view.shownVersion ?? undefined : undefined}
        />
        <svg
          id="orig-overlay"
          className="compare-pane__overlay absolute"
          style={r ? { left: r.left, top: r.top, width: r.width, height: r.height } : undefined}
          viewBox={scene ? `0 0 ${scene.size.join(' ')}` : undefined}
          data-frame={drawn ? view.overlay.frame : undefined}
          data-version={drawn ? view.overlay.version : undefined}
        >
          {drawn && r && view.overlay.objects.map((obj: any) => {
            const color = objectColor(obj.id)
            const selected = obj.id === view.selectedId
            const kind = T(`review.kind.${obj.kind}`)
            const ocr = obj.ocr.map((o: any) => o.text).join(' ')
            const label = placeLabel(`${obj.id} · ${kind}${obj.ocr.length ? ` · ${ocr}` : ''}`, obj.regions[0].bbox, scene.size[0], r.width, placed)
            const select = () => { if (!view.editRegion) core.selectElement(obj.id) }
            return (
              <g key={obj.id} data-object-id={obj.id} role="button" tabIndex={view.editRegion ? -1 : 0} aria-label={`${obj.id} ${kind} ${ocr}`} aria-pressed={selected}
                className="cursor-pointer outline-none" onClick={select}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); select() } }}>
                {obj.regions.map((region: any, i: number) => (
                  <path key={i} d={regionPath(region.rings)} fill={color} fillOpacity={view.overlayOpacity} fillRule="evenodd" stroke={color}
                    strokeWidth={selected ? 3 : 1} vectorEffect="non-scaling-stroke" className="analysis-region" />
                ))}
                <text x={label.x} y={label.y} fill={color} fontSize={label.fontSize} className="analysis-label pointer-events-none select-none" paintOrder="stroke" stroke="rgb(0 0 0 / 0.7)" strokeWidth={label.fontSize / 5}>{label.caption}</text>
              </g>
            )
          })}
          {view.drag && (
            <rect x={view.drag[0]} y={view.drag[1]} width={view.drag[2] - view.drag[0]} height={view.drag[3] - view.drag[1]}
              className="overlay-box overlay-box--draft" fill="rgb(0 153 255 / 0.12)" stroke="var(--brand)" strokeDasharray="6 4" vectorEffect="non-scaling-stroke" />
          )}
        </svg>
        <div
          id="orig-draw"
          className="draw-layer absolute inset-0 cursor-crosshair touch-none"
          hidden={!view.editRegion}
          aria-label={T('review.regionMode')}
          onPointerDown={(e) => { if (e.button === 0 && core.dragStart(point(e))) { e.preventDefault(); e.currentTarget.setPointerCapture(e.pointerId) } }}
          onPointerMove={(e) => core.dragMove(point(e))}
          onPointerUp={(e) => core.dragEnd(point(e))}
          onPointerCancel={() => core.dragEnd(null)}
        />
        <div id="frame-loading" className="pane-loading absolute inset-0 grid place-items-center bg-black/40 text-caption text-ink-muted" hidden={!view.frameLoading}>
          <span>{T('review.loadingFrames')}</span>
        </div>
        <div id="frame-error" className="pane-error absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/70 px-6 text-center text-body-sm" hidden={!view.frameError}>
          <p className="m-0">{T('review.origFailed')}</p>
          <Button tone="secondary" size="sm" id="frame-retry" onClick={core.retryFrame}>{T('review.retryFrame')}</Button>
        </div>
      </div>

      <div className="transport flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1.5">
          <button type="button" id="play-btn" aria-label={T(view.playing ? 'review.pause' : 'review.play')} onClick={() => core.setPlaying(!core.isPlaying())}
            className={`grid size-11 place-items-center rounded-md bg-ink text-black transition-transform active:scale-95 ${view.playing ? 'is-playing' : ''}`}>
            <Play id="play-icon" className="size-4 translate-x-px" fill="currentColor" aria-hidden {...{ hidden: view.playing }} />
            <Pause id="pause-icon" className="size-4" fill="currentColor" aria-hidden {...{ hidden: !view.playing }} />
          </button>
          <button type="button" id="step-back" aria-label={T('review.stepBack')} onClick={() => core.setFrame(view.frame - 1, true)}
            className="transport__step grid size-11 place-items-center rounded-md text-ink-muted hover:bg-white/5 hover:text-ink">
            <ChevronLeft className="size-4" aria-hidden />
          </button>
          <button type="button" id="step-fwd" aria-label={T('review.stepFwd')} onClick={() => core.setFrame(view.frame + 1, true)}
            className="transport__step grid size-11 place-items-center rounded-md text-ink-muted hover:bg-white/5 hover:text-ink">
            <ChevronRight className="size-4" aria-hidden />
          </button>
          <span className="ml-2 font-mono text-body-sm font-semibold tabular-nums">
            <span id="frame-num">{view.frame}</span> <span className="font-normal text-ink-muted">/ <span id="frame-total">{scene?.frames ?? 0}</span></span>
          </span>
          <span className="ml-3 font-mono text-body-sm font-semibold text-brand-soft tabular-nums" id="frame-time">{scene ? timecode(view.frame, scene.fps) : '00:00:00'}</span>
        </div>
        <div className="transport__keys hidden text-caption text-ink-muted sm:block" id="transport-keys">{T('review.keys')}</div>
      </div>
    </div>
  )
}
