import { Download } from 'lucide-react'
import { useEffect, useState } from 'react'
import { AdminShell } from '@/components/app/admin-shell'
import { Button } from '@/components/app/button'
import { useI18n } from '@/i18n'
import { actionLabel, actionTone, loadErrorText } from '@/lib/admin'
import { fetchAdminOrRedirect } from '@/lib/api'
import { cn } from '@/lib/utils'
import { panel, td, th } from './common'

type Event = { ts: string; actor: string; action: string; target: string; detail?: string }

export default function AdminAudit() {
  const { T } = useI18n()
  const [events, setEvents] = useState<Event[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    fetchAdminOrRedirect('/admin/api/audit').then((data) => setEvents(data.events))
      .catch((err) => { const text = loadErrorText(err); if (text) setError(text) })
  }, [])
  const export_ = <Button tone="secondary" size="sm" asChild><a href="/admin/api/audit.csv"><Download className="size-4" aria-hidden />{T('admin.audit.export')}</a></Button>
  return (
    <AdminShell page="audit" title={T('admin.audit')} actions={export_}>
      <section className={cn(panel, 'audit-content flex min-w-0 flex-col')}>
        <div className="admin-data-scroll overflow-x-auto">
          <table className="audit-table w-full min-w-[720px] border-collapse text-body-sm">
            <thead className="border-b border-hairline-soft">
              <tr>{['admin.audit.time', 'admin.audit.actor', 'admin.audit.action', 'admin.audit.targetTenant', 'admin.audit.details'].map((k) => <th key={k} className={th}>{T(k)}</th>)}</tr>
            </thead>
            <tbody id="audit-rows">
              {error ? <tr><td colSpan={5} className={cn(td, 'admin-load-error text-danger')}>{error}</td></tr> : events.map((e, i) => {
                const tone = actionTone(e.action)
                return (
                  <tr key={i} className="border-b border-hairline-soft last:border-0">
                    <td className={cn(td, 'font-mono text-caption whitespace-nowrap text-ink-muted')}>{e.ts}</td>
                    <td className={td}>{e.actor}</td>
                    <td className={cn(td, tone === 'warn' ? 'action--warn text-warning' : tone === 'keep' ? 'action--keep text-brand-soft' : '')}>{actionLabel(e.action)}</td>
                    <td className={cn(td, 'text-ink-muted')}>{e.target}</td>
                    <td className={cn(td, 'text-ink-muted')}>{e.detail || '-'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <p className="audit-footnote m-0 border-t border-hairline-soft px-4 py-3 text-caption text-ink-muted">{T('admin.audit.appendOnly')}</p>
      </section>
    </AdminShell>
  )
}
