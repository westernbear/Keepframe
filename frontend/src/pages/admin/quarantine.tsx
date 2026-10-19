import { FileWarning } from 'lucide-react'
import { useEffect, useState } from 'react'
import { AdminShell } from '@/components/app/admin-shell'
import { useI18n } from '@/i18n'
import { loadErrorText, quarantineCount, tenantLabel } from '@/lib/admin'
import { fetchAdminOrRedirect } from '@/lib/api'
import { cn } from '@/lib/utils'
import { LoadError, panel, rowKeys, sectionLabel } from './common'

type Item = { id: string; filename: string; tenant_id: string; rejected_at: string; reason: string }
const cols = 'grid grid-cols-[minmax(180px,2fr)_120px_150px_minmax(160px,2fr)] items-center gap-4'

export default function AdminQuarantine() {
  const { T } = useI18n()
  const [items, setItems] = useState<Item[]>([])
  const [selected, setSelected] = useState<Item | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    Promise.all([fetchAdminOrRedirect('/admin/api/quarantine'), fetchAdminOrRedirect('/admin/api/policy')])
      .then(([data]) => { setItems(data.items); setSelected(data.items[0] || null) })
      .catch((err) => { const text = loadErrorText(err); if (text) setError(text) })
  }, [])

  return (
    <AdminShell page="quarantine" title={T('admin.quarantine')}>
      <div className="quarantine-split grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <section className={cn(panel, 'flex min-w-0 flex-col')}>
          <div className="flex items-center justify-between gap-3 border-b border-hairline-soft p-4">
            <h2 className="m-0 text-body font-semibold">{T('admin.quarantine.pendingTitle')}</h2>
            <span id="quarantine-count" className="chip rounded-md bg-surface-2 px-2.5 py-1 text-micro text-ink-muted tabular-nums">{quarantineCount(items.length)}</span>
          </div>
          <div className="quarantine-rows-scroll overflow-x-auto">
            <div className="min-w-[660px]">
              <div className={cn(cols, 'border-b border-hairline-soft px-4 py-2.5 text-micro font-semibold tracking-wide text-ink-muted uppercase')}>
                <div>{T('admin.quarantine.filename')}</div><div>{T('admin.table.tenant')}</div><div>{T('admin.quarantine.rejectedAt')}</div><div>{T('admin.quarantine.reason')}</div>
              </div>
              <div id="quarantine-rows" className="flex flex-col p-1.5">
                {error ? <LoadError message={error} /> : items.map((item) => (
                  <div key={item.id} role="button" aria-label={item.filename} {...rowKeys(() => setSelected(item))}
                    className={cn(cols, 'quarantine-row state-transition cursor-pointer rounded-xl px-2.5 py-3 text-body-sm outline-none focus-visible:ring-2 focus-visible:ring-brand', selected?.id === item.id ? 'quarantine-row--selected bg-white/[0.06]' : 'hover:bg-white/[0.03]')}>
                    <div className="quarantine-row__file flex min-w-0 items-center gap-2"><FileWarning className="size-4 shrink-0 text-ink-muted" aria-hidden /><span className="quarantine-row__name truncate">{item.filename}</span></div>
                    <div className="text-ink-muted">{tenantLabel(item.tenant_id)}</div>
                    <div className="font-mono text-caption text-ink-muted">{item.rejected_at}</div>
                    <div className="quarantine-row__reason truncate text-caption text-ink-muted">{item.reason}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>

        <aside className={cn(panel, 'flex flex-col gap-5 p-5')}>
          <h2 className="m-0 text-body font-semibold">{T('admin.quarantine.details')}</h2>
          <div className="flex flex-col gap-1.5">
            <div className={sectionLabel}>{T('admin.quarantine.targetFile')}</div>
            <div id="detail-filename" className="rounded-xl bg-surface-2 p-2.5 font-mono text-body-sm break-all">{selected?.filename || '—'}</div>
          </div>
          <div className="grid grid-cols-2 gap-3 text-body-sm">
            <div><div className={sectionLabel}>{T('admin.table.tenant')}</div><div id="detail-tenant" className="mt-1">{selected ? tenantLabel(selected.tenant_id) : '—'}</div></div>
            <div><div className={sectionLabel}>{T('admin.quarantine.detectedAt')}</div><div id="detail-time" className="mt-1 font-mono">{selected?.rejected_at || '—'}</div></div>
          </div>
          <div className="flex flex-col gap-1.5">
            <div className={sectionLabel}>{T('admin.quarantine.violation')}</div>
            <div id="detail-reason" className="reason-box rounded-xl bg-danger/10 p-3 text-body-sm text-danger">{selected?.reason || '—'}</div>
          </div>
          <p className="policy-box m-0 rounded-xl bg-surface-2/60 p-3 text-caption text-ink-muted" id="detail-policy">{T('admin.policy')}</p>
        </aside>
      </div>
    </AdminShell>
  )
}
