import { ArrowRight } from 'lucide-react'
import { Link } from 'react-router'
import { AppHeader, Brand } from '@/components/app/app-header'
import { Button } from '@/components/app/button'
import { useI18n } from '@/i18n'
import { HeroScrubber } from './hero-scrubber'

const STEPS = [
  { n: 'stepUpload', title: 'uploadTitle', body: 'uploadBody', tint: 'var(--gradient-violet)' },
  { n: 'stepAnalyze', title: 'analyzeTitle', body: 'analyzeBody', tint: 'var(--gradient-magenta)' },
  { n: 'stepReview', title: 'reviewTitle', body: 'reviewBody', tint: 'var(--gradient-orange)' },
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
              <h1 className="land-display m-0 font-display text-[clamp(40px,6.4vw,76px)] leading-[0.98] font-medium tracking-[-0.045em]">
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
            <h2 className="land-section__title m-0 mb-10 font-display text-display-md font-medium tracking-[-0.03em]">{T('landing.howTitle')}</h2>
            <ol className="m-0 grid list-none gap-4 p-0 md:grid-cols-3">
              {STEPS.map((s) => (
                <li key={s.n} className="relative overflow-hidden rounded-2xl bg-surface-1 p-6 ring-1 ring-inset ring-white/5">
                  <div aria-hidden className="absolute -top-16 -right-16 size-48 rounded-full opacity-35 blur-3xl" style={{ background: s.tint }} />
                  <p className="land-step relative m-0 font-mono text-caption text-ink-muted">{T(`landing.${s.n}`)}</p>
                  <h3 className="land-h3 relative mt-8 mb-2 text-headline font-semibold tracking-[-0.02em]">{T(`landing.${s.title}`)}</h3>
                  <p className="land-body relative m-0 text-body text-ink-muted">{T(`landing.${s.body}`)}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section className="land-section border-t border-hairline-soft py-20 md:py-24">
          <div className={`${wrap} grid gap-8 md:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]`}>
            <h2 className="land-section__title m-0 font-display text-display-md font-medium tracking-[-0.03em]">{T('landing.limitsTitle')}</h2>
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
