import { ArrowLeft } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Brand } from '@/components/app/app-header'
import { Button } from '@/components/app/button'
import { LangToggle } from '@/components/app/lang-toggle'
import { useI18n } from '@/i18n'
import { TENANTS_PATH } from '@/lib/admin'
import { postAdminLogin } from '@/lib/api'

const field = 'h-11 w-full rounded-xl border border-hairline bg-canvas px-3.5 text-body outline-none focus-visible:border-brand'

export default function AdminLogin() {
  const { T } = useI18n()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { document.title = `${T('admin.login')} · Keepframe` }, [T])

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const form = e.currentTarget.elements as any
    setBusy(true)
    setError('')
    try {
      await postAdminLogin({ email: form.email.value, password: form.password.value })
      location.assign(TENANTS_PATH)
    } catch (err: any) {
      setError(err.message || T('admin.loginFailed'))
      setBusy(false)
    }
  }

  return (
    <main className="login-main grid min-h-dvh place-items-center px-4 py-10">
      <div className="login-card flex w-full max-w-sm flex-col gap-6 rounded-3xl bg-surface-1 p-6 ring-1 ring-inset ring-hairline sm:p-8">
        <header className="login-header flex flex-col gap-5">
          <div className="flex items-center justify-between gap-3">
            <div className="login-brand flex items-center gap-2.5">
              <Brand />
              <span className="login-badge rounded-md bg-surface-2 px-2.5 py-1 text-micro font-medium text-ink-muted">{T('admin.ops')}</span>
            </div>
            <LangToggle />
          </div>
          <h1 className="login-title m-0 font-display text-display-md font-medium tracking-[-0.02em]">{T('admin.login')}</h1>
        </header>
        <form className="login-form flex flex-col gap-4" id="login-form" onSubmit={submit}>
          <label className="flex flex-col gap-1.5 text-body-sm text-ink-muted" htmlFor="email">{T('admin.email')}
            <input className={field} autoComplete="email" id="email" name="email" placeholder={T('admin.emailPlaceholder')} required type="email" />
          </label>
          <label className="flex flex-col gap-1.5 text-body-sm text-ink-muted" htmlFor="password">{T('admin.password')}
            <input className={field} autoComplete="current-password" id="password" name="password" placeholder="••••••••" required type="password" />
          </label>
          {error && <p className="m-0 rounded-xl bg-danger/10 px-3.5 py-2.5 text-body-sm text-danger" role="alert">{error}</p>}
          <Button tone="primary" size="lg" type="submit" className="mt-1 w-full" loading={busy}>{T('admin.enter')}</Button>
        </form>
        <a href="/" className="login-back inline-flex min-h-11 items-center gap-2 self-center text-body-sm text-ink-muted hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden />{T('admin.back')}
        </a>
      </div>
    </main>
  )
}
