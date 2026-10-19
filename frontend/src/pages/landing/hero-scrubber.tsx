import { Check, Pause, Play } from 'lucide-react'
import { useReducedMotion } from 'motion/react'
import { useEffect, useRef, useState } from 'react'
import Slider from '@/components/smoothui/slider'
import Switch from '@/components/smoothui/switch'
import { TabsIndicator, TabsList, TabsRoot, TabsTab } from '@/components/smoothui/tabs'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { BADGE, CARD, FPS, FRAMES, MEASURED, TITLE, easeOutCubic, frameAt } from './demo-clip'

const WORDS = ['Launch', 'Ship it', '출시']
const COLOURS = [
  { key: 'violet', value: 'var(--sample-violet)' },
  { key: 'magenta', value: 'var(--sample-magenta)' },
  { key: 'orange', value: 'var(--sample-orange)' },
] as const
const HOLD = FPS // frames the settled clip rests before it loops
const KEYS = [CARD.start, TITLE.start, CARD.start + CARD.frames, BADGE.start, BADGE.start + BADGE.frames]
const CARD_LEFT = 0.1 // card's resting left edge, share of the stage

/** Plays the clip at FPS until the user takes the scrubber; returns the shown frame. */
function useClipFrame(playing: boolean, frame: number, setFrame: (f: number) => void) {
  const start = useRef(0)
  useEffect(() => {
    if (!playing) return
    start.current = performance.now() - (frame / FPS) * 1000
    let raf = 0
    const tick = (now: number) => {
      const f = Math.floor(((now - start.current) / 1000) * FPS) % (FRAMES + HOLD)
      setFrame(Math.min(f, FRAMES - 1))
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- restart only when play toggles
  }, [playing])
}

/** Mini plot of the card's ease, the dot at the current frame: the overlay's number, drawn. */
function EaseCurve({ frame }: { frame: number }) {
  const pts = Array.from({ length: CARD.frames + 1 }, (_, i) => `${(i / CARD.frames) * 56},${28 - easeOutCubic(i / CARD.frames) * 24}`)
  const t = Math.min(1, Math.max(0, (frame - CARD.start) / CARD.frames))
  return (
    <svg width="60" height="32" viewBox="-2 0 60 32" aria-hidden className="overflow-visible">
      <polyline points={pts.join(' ')} fill="none" stroke="currentColor" strokeOpacity="0.55" strokeWidth="1.5" />
      <circle cx={t * 56} cy={28 - easeOutCubic(t) * 24} r="3" fill="var(--brand)" />
    </svg>
  )
}

export function HeroScrubber() {
  const { T, Tf } = useI18n()
  const reduce = useReducedMotion()
  const [frame, setFrame] = useState(reduce ? FRAMES - 1 : 0)
  const [playing, setPlaying] = useState(!reduce)
  const [measure, setMeasure] = useState(true)
  const [word, setWord] = useState(WORDS[0])
  const [colour, setColour] = useState<(typeof COLOURS)[number]['key']>('violet')
  useClipFrame(playing, frame, setFrame)

  const f = frameAt(frame, word)
  const tint = COLOURS.find((c) => c.key === colour)!.value
  const left = `${(CARD_LEFT + f.cardX) * 100}%`

  return (
    <div className="flex flex-col gap-4" data-hero-demo>
      <figure className="m-0">
        <div
          className="relative aspect-[16/10] overflow-hidden rounded-lg border border-hairline bg-[#0e0e10]"
          role="img"
          aria-label={T('landing.demo.label')}
        >
          {/* a comp viewer: action-safe and title-safe guides, as in the editor the clip came from */}
          <div aria-hidden className="pointer-events-none absolute inset-[5%] border border-dashed border-white/[0.07]" />
          <div aria-hidden className="pointer-events-none absolute inset-[10%] border border-dashed border-white/[0.05]" />
          {/* the clip */}
          <div className="absolute top-[28%] h-[44%] w-[56%]" style={{ left }}>
            <div className="flex h-full flex-col justify-between rounded-md p-[5%] text-white transition-colors duration-300" style={{ background: tint }}>
              <span
                className="inline-flex w-fit origin-left rounded-sm bg-black/80 px-2 py-0.5 text-[clamp(9px,1.2vw,12px)] font-semibold tracking-wide text-white"
                style={{ transform: `scale(${f.badge})`, opacity: f.badge }}
              >NEW</span>
              <span className="font-display text-[clamp(22px,4.4vw,52px)] leading-none font-semibold tracking-[-0.025em]">
                {word.slice(0, f.letters)}<span className="opacity-0">{word.slice(f.letters)}</span>
              </span>
            </div>
            {measure && (
              <>
                <div aria-hidden className="pointer-events-none absolute -inset-1.5 rounded-lg border border-dashed border-brand" />
                <div aria-hidden className="pointer-events-none absolute -top-8 left-0 flex items-center gap-2 whitespace-nowrap rounded-sm bg-black px-2 py-1 font-mono text-[11px] text-ink">
                  <span className="text-brand-soft">{Tf('landing.demo.travel', { px: MEASURED.travelPx })}</span>
                  <span className="text-ink-muted">{Tf('landing.demo.ease', { f: MEASURED.easeFrames, s: MEASURED.easeSeconds })}</span>
                </div>
                <div aria-hidden className="pointer-events-none absolute -bottom-7 left-0 whitespace-nowrap rounded-sm bg-black px-2 py-1 font-mono text-[11px] text-ink-muted">
                  {Tf('landing.demo.letters', { f: TITLE.stagger })}
                </div>
              </>
            )}
          </div>
          {measure && (
            <div aria-hidden className="absolute right-3 bottom-3 flex items-center gap-2 rounded-md border border-hairline bg-black px-2.5 py-1.5 text-ink">
              <EaseCurve frame={frame} />
            </div>
          )}
        </div>
        <figcaption className="mt-2 text-caption text-ink-muted">{T('landing.demo.note')}</figcaption>
      </figure>

      {/* transport */}
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => setPlaying((p) => !p)}
          aria-label={T(playing ? 'landing.demo.pause' : 'landing.demo.play')}
          className="grid size-11 shrink-0 place-items-center rounded-md bg-ink text-black transition-transform active:scale-95"
        >
          {playing ? <Pause className="size-4" fill="currentColor" /> : <Play className="size-4 translate-x-px" fill="currentColor" />}
        </button>
        <div className="relative min-w-0 flex-1">
          <Slider aria-label={T('landing.demo.scrub')} min={0} max={FRAMES - 1} value={frame} onValueChange={(v) => { setPlaying(false); setFrame(v) }} />
          <div aria-hidden className="pointer-events-none absolute inset-x-0 -bottom-1 h-1.5">
            {KEYS.map((k) => (
              <span key={k} className="absolute h-1.5 w-px bg-ink-muted/70" style={{ left: `${(k / (FRAMES - 1)) * 100}%` }} />
            ))}
          </div>
        </div>
        <output className="shrink-0 text-right font-mono text-caption whitespace-nowrap text-ink-muted tabular-nums" aria-live="off">
          {Tf('landing.demo.frame', { f: frame + 1, n: FRAMES })}
        </output>
      </div>

      {/* keep / change */}
      <div className="grid gap-4 rounded-lg border border-hairline bg-surface-1 p-4 sm:grid-cols-[auto_1fr]">
        <div className="flex flex-col gap-2">
          <span className="text-micro font-semibold tracking-wide text-ink-muted uppercase">{T('landing.demo.keep')}</span>
          <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0">
            {['keepMotion', 'keepDirection', 'keepTiming'].map((k) => (
              <li key={k} className="inline-flex items-center gap-1.5 rounded-sm bg-surface-2 px-2.5 py-1.5 text-caption text-ink">
                <Check className="size-3.5 text-success" strokeWidth={3} aria-hidden />{T(`landing.demo.${k}`)}
              </li>
            ))}
          </ul>
        </div>
        <div className="flex flex-col gap-2">
          <span className="text-micro font-semibold tracking-wide text-ink-muted uppercase">{T('landing.demo.change')}</span>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
            <TabsRoot value={word} onValueChange={(v) => setWord(String(v))} className="w-auto">
              <TabsList variant="pill" aria-label={T('landing.demo.text')}>
                {WORDS.map((w) => <TabsTab key={w} value={w} className="px-3 py-1.5 text-caption">{w}</TabsTab>)}
                <TabsIndicator />
              </TabsList>
            </TabsRoot>
            <div role="radiogroup" aria-label={T('landing.demo.colour')} className="flex items-center gap-2">
              {COLOURS.map((c) => (
                <button
                  key={c.key}
                  type="button"
                  role="radio"
                  aria-checked={colour === c.key}
                  aria-label={T(`landing.demo.${c.key}`)}
                  onClick={() => setColour(c.key)}
                  className={cn('size-8 rounded-sm ring-2 ring-offset-2 ring-offset-surface-1 transition-[box-shadow]', colour === c.key ? 'ring-ink' : 'ring-transparent hover:ring-white/30')}
                  style={{ background: c.value }}
                />
              ))}
            </div>
            <label className="ml-auto inline-flex items-center gap-2 text-caption text-ink-muted">
              <Switch checked={measure} onCheckedChange={setMeasure} aria-label={T('landing.demo.measure')} />
              {T('landing.demo.measure')}
            </label>
          </div>
        </div>
      </div>
    </div>
  )
}
