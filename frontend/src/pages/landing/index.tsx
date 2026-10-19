import { ArrowRight } from 'lucide-react'
import { Link } from 'react-router'
import { AppHeader, Brand } from '@/components/app/app-header'
import { Button } from '@/components/app/button'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { HeroScrubber } from './hero-scrubber'

const STEPS = [
  { title: 'uploadTitle', body: 'uploadBody' },
  { title: 'analyzeTitle', body: 'analyzeBody' },
  { title: 'reviewTitle', body: 'reviewBody' },
]

export default function Landing() {
  const { T } = useI18n()
  const wrap = 'mx-auto w-full max-w-[1200px] px-4 md:px-8'
  return (
    <div className="land min-h-dvh">
      <AppHeader />
      <main>
        <section className="land-hero pt-12 pb-20 md:pt-20 md:pb-28">
          <div className={`${wrap} grid items-center gap-12 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]`}>
            <div className="flex flex-col gap-6">
              <h1 className="land-display m-0 font-display text-[clamp(40px,6.4vw,72px)] leading-[1.02] font-semibold tracking-[-0.02em]">
                {T('landing.headline')}
              </h1>
              <p className="land-lead m-0 max-w-[34ch] text-body-lg text-ink-muted">{T('landing.subhead')}</p>
              <div className="land-actions flex flex-wrap gap-3">
                <Button tone="primary" size="lg" asChild>
                  <Link to="/new" className="land-cta">{T('nav.new')}<ArrowRight className="size-4" aria-hidden /></Link>
                </Button>
                <Button tone="secondary" size="lg" asChild>
                  <a href="/demo" className="land-cta">{T('nav.demo')}</a>
                </Button>
              </div>
            </div>
            <HeroScrubber />
          </div>
        </section>

        <section className="land-section border-t border-hairline-soft py-20 md:py-24">
          <div className={wrap}>
            <h2 className="land-section__title m-0 mb-10 font-display text-display-md font-medium tracking-[-0.02em]">{T('landing.howTitle')}</h2>
            {/* one flow, drawn as a track: each stage starts at a keyframe on the same line */}
            <ol className="land-track m-0 grid list-none gap-y-8 p-0 md:grid-cols-3">
              {STEPS.map((s, i) => (
                <li key={s.title} className="relative border-t border-hairline pt-6 md:pr-8">
                  <span aria-hidden className={cn('absolute -top-[5px] left-0 size-2.5 rotate-45 border', i === 0 ? 'border-brand bg-brand' : 'border-ink-muted bg-canvas')} />
                  <h3 className="land-h3 m-0 mb-2 text-headline font-semibold tracking-[-0.015em]">{T(`landing.${s.title}`)}</h3>
                  <p className="land-body m-0 max-w-[36ch] text-body text-ink-muted">{T(`landing.${s.body}`)}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section className="land-section border-t border-hairline-soft py-20 md:py-24">
          <div className={`${wrap} grid gap-8 md:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]`}>
            <h2 className="land-section__title m-0 font-display text-display-md font-medium tracking-[-0.02em]">{T('landing.limitsTitle')}</h2>
            <ul className="land-limits m-0 flex list-none flex-col p-0">
              {['limitLive', 'limitScope', 'limitCaps'].map((k) => (
                <li key={k} className="border-b border-hairline-soft py-4 text-body-lg first:pt-0 last:border-0">{T(`landing.${k}`)}</li>
              ))}
            </ul>
          </div>
        </section>
      </main>

      <footer className="land-footer border-t border-hairline-soft py-10">
        <div className={`${wrap} flex flex-wrap items-center justify-between gap-4`}>
          <Brand />
          <nav className="land-footer__links flex flex-wrap gap-5 text-body-sm text-ink-muted">
            <Link to="/library" className="hover:text-ink">{T('nav.library')}</Link>
            <a href="/demo" className="hover:text-ink">{T('nav.demo')}</a>
            <Link to="/new" className="hover:text-ink">{T('nav.new')}</Link>
          </nav>
        </div>
      </footer>
    </div>
  )
}
