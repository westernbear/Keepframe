import { memo } from 'react'
import { useI18n } from '@/i18n'
import { elementLabel, matchesElementFilter } from '@/lib/inspector'
import { frameAtX, playheadPct, rulerLabels, trackBar, trackIntervals } from '@/lib/review-geom'
import { cn } from '@/lib/utils'
import { objectColor } from './colors'
import type { ReviewCore } from './core'

// The label gutter is a CSS length so the lane playhead needs no measuring: 112px on phones, 280px from md up.
const GUTTER = '[--gutter:112px] md:[--gutter:280px]'

const Tracks = memo(function Tracks({ st, selectedId, filter, core }: { st: any; selectedId: string | null; filter: string; core: ReviewCore }) {
  const { T } = useI18n()
  const frames = st.scene.frames
  return (
    <div id="timeline-tracks" className="flex flex-col">
      {(st.scene.elements || []).map((item: any) => {
        return (
          <div key={item.id} data-id={item.id} hidden={!matchesElementFilter(filter, item.id, elementLabel(item))}
            className={cn('track flex h-8 cursor-pointer items-center border-b border-hairline-soft last:border-0', item.id === selectedId ? 'track--selected bg-white/5' : 'hover:bg-white/[0.03]')}
            onClick={() => core.selectElement(item.id)}>
            <div className="track__label w-(--gutter) shrink-0 truncate px-3 text-micro text-ink-muted"><span className="font-mono text-ink">{item.id}</span> · {T(`review.kind.${item.kind}`)}</div>
            <div className="track__lane relative h-full flex-1"
              onClick={(e) => core.setFrame(frameAtX(e.clientX, e.currentTarget.getBoundingClientRect(), frames), true)}>
              {trackIntervals(item, st.analysis).map(([a, b]: number[], i: number) => (
                <div key={i} className="track__bar absolute inset-y-2 rounded-sm border opacity-80"
                  style={{ ...trackBar([a, b], frames), background: objectColor(item.id), borderColor: objectColor(item.id) }} />
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
})

export function Timeline({ st, frame, selectedId, filter, core }: { st: any; frame: number; selectedId: string | null; filter: string; core: ReviewCore }) {
  const { T } = useI18n()
  const frames = st?.scene.frames || 1
  const pct = playheadPct(frame, frames)
  return (
    <div className={cn('timeline overflow-hidden rounded-xl bg-surface-1 ring-1 ring-inset ring-white/5', GUTTER)}>
      <div className="timeline__ruler flex h-[72px] items-stretch overflow-hidden border-b border-hairline-soft">
        <div className="timeline__labels flex w-(--gutter) shrink-0 items-center px-3 text-caption font-medium text-ink-muted">{T('review.tracks')}</div>
        <svg id="timeline-svg" className="h-full flex-1 cursor-pointer" role="slider" tabIndex={0} aria-label="timeline"
          aria-valuemin={0} aria-valuemax={st ? frames - 1 : 0} aria-valuenow={frame}
          onClick={(e) => st && core.setFrame(frameAtX(e.clientX, e.currentTarget.getBoundingClientRect(), frames), true)}>
          {st && rulerLabels(frames).map((l) => (
            <text key={l.x} x={l.x} y="38" textAnchor={l.anchor as 'start' | 'middle' | 'end'} fill="var(--ink-muted)" fontSize="11">{l.text}</text>
          ))}
          {st && <line className="playhead" x1={`${pct}%`} x2={`${pct}%`} y1="0" y2="72" stroke="var(--brand)" strokeWidth="2" />}
        </svg>
      </div>
      <div className="timeline__body relative max-h-64 overflow-y-auto">
        <div id="track-playhead" className="track-playhead pointer-events-none absolute inset-y-0 z-10 w-px bg-brand" hidden={!st}
          style={{ left: `calc(var(--gutter) + (100% - var(--gutter)) * ${pct / 100})` }} />
        {st && <Tracks st={st} selectedId={selectedId} filter={filter} core={core} />}
      </div>
    </div>
  )
}
