import { Building2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { AdminShell } from '@/components/app/admin-shell'
import { Button } from '@/components/app/button'
import { confirmAction } from '@/components/app/confirm'
import { TabsIndicator, TabsList, TabsRoot, TabsTab } from '@/components/smoothui/tabs'
import { useI18n } from '@/i18n'
import { filterTenants, isOverQuota, isSuspended, isTenantWarn, loadErrorText, quotaPct, roleCounts, roleLabel, tenantStatusLabel, type Tenant } from '@/lib/admin'
import { fetchAdminOrRedirect } from '@/lib/api'
import { cn } from '@/lib/utils'
import { LoadError, panel, rowKeys, sectionLabel } from './common'

type Row = Tenant & { members: number; created: string }
const cols = 'grid grid-cols-[minmax(160px,2fr)_72px_minmax(180px,2fr)_112px] items-center gap-4'

export default function AdminTenants() {
  const { T, Tf } = useI18n()
  const [tenants, setTenants] = useState<Row[]>([])
  const [filter, setFilter] = useState('ok')
  const [selected, setSelected] = useState<Row | null>(null)
  const [members, setMembers] = useState<Record<string, any[]>>({})
  const [error, setError] = useState('')

  async function loadMembers(t: Row) {
    if (members[t.id]) return
    try {
      const data = await fetchAdminOrRedirect(`/admin/api/tenants/${encodeURIComponent(t.id)}`)
      setMembers((m) => ({ ...m, [t.id]: data.members }))
    } catch { /* the role box stays empty */ }
  }
  function select(t: Row) { setSelected(t); loadMembers(t) }

  useEffect(() => {
    Promise.all([fetchAdminOrRedirect('/admin/api/tenants'), fetchAdminOrRedirect('/admin/api/policy')])
      .then(([data]) => { setTenants(data.tenants); if (data.tenants.length) select(data.tenants[0]) })
      .catch((err) => { const text = loadErrorText(err); if (text) setError(text) })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once
  }, [])

  async function suspend() {
    if (!selected || isSuspended(selected)) return
    if (!await confirmAction({ title: Tf('admin.tenant.confirmSuspend', { name: selected.name }), confirmLabel: T('admin.tenant.suspend'), danger: true })) return
    const data = await fetchAdminOrRedirect(`/admin/api/tenants/${encodeURIComponent(selected.id)}/suspend`, { method: 'POST', body: '{}' })
    setTenants((list) => list.map((t) => (t.id === data.tenant.id ? data.tenant : t)))
    setSelected(data.tenant)
  }

  const rows = filterTenants(tenants, filter)
  const counts = selected ? roleCounts(members[selected.id] || []) : []
  return (
    <AdminShell page="tenants" title={T('admin.tenants')}>
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        <section className={cn(panel, 'flex min-w-0 flex-col')}>
          <div className="admin-panel__toolbar flex items-center gap-3 border-b border-hairline-soft p-3">
            <TabsRoot value={filter} onValueChange={(v) => setFilter(String(v))} className="w-auto">
              <TabsList variant="pill">
                {(['ok', 'quota', 'suspended'] as const).map((f) => <TabsTab key={f} value={f} data-filter={f} className="min-h-9 px-3.5 py-1.5 text-body-sm">{T(`admin.filter.${f}`)}</TabsTab>)}
                <TabsIndicator />
              </TabsList>
            </TabsRoot>
          </div>
          <div className="overflow-x-auto">
            <div className="min-w-[620px]">
              <div className={cn(cols, 'admin-table-head border-b border-hairline-soft px-4 py-2.5 text-micro font-semibold tracking-wide text-ink-muted uppercase')}>
                <div>{T('admin.table.organization')}</div><div>{T('admin.table.members')}</div><div>{T('admin.table.usage')}</div><div className="text-right">{T('admin.table.status')}</div>
              </div>
              <div className="admin-table-scroll flex flex-col p-1.5" id="tenant-rows">
                {error ? <LoadError message={error} /> : rows.map((t) => {
                  const over = isOverQuota(t)
                  return (
                    <div key={t.id} role="button" aria-label={t.name} {...rowKeys(() => select(t))}
                      className={cn(cols, 'admin-table-row state-transition cursor-pointer rounded-xl px-2.5 py-3 text-body-sm outline-none focus-visible:ring-2 focus-visible:ring-brand', selected?.id === t.id ? 'admin-table-row--selected bg-white/[0.06]' : 'hover:bg-white/[0.03]')}>
                      <div className="min-w-0"><div className="tenant-name truncate font-medium">{t.name}</div><div className="tenant-id truncate font-mono text-micro text-ink-muted">{t.id}</div></div>
                      <div className="tabular-nums">{t.members}</div>
                      <div className="flex flex-col gap-1.5">
                        <div className={cn('quota-meta text-caption tabular-nums', over && 'text-danger')}>
                          {Tf('admin.usage.minutes', { n: t.analyze_min })} <span className="text-ink-muted">/ {Tf('admin.usage.minutes', { n: t.analyze_quota_min })}</span>
                          {t.render_min ? <span className="text-ink-muted"> · {Tf('admin.usage.render', { n: t.render_min })}</span> : null}
                        </div>
                        <div className="h-1.5 overflow-hidden rounded-full bg-surface-3"><div className={cn('h-full rounded-full', over ? 'bg-danger' : 'bg-brand')} style={{ width: `${quotaPct(t)}%` }} /></div>
                      </div>
                      <div className="text-right">
                        <span className={cn('inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-micro font-medium', isTenantWarn(t) ? 'bg-warning/10 text-warning' : 'bg-success/10 text-success')}>
                          {t.status === 'ok' && <span aria-hidden className="size-1.5 rounded-full bg-current" />}{tenantStatusLabel(t.status)}
                        </span>
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          </div>
        </section>

        <aside className={cn(panel, 'inspector-panel flex flex-col gap-6 p-5')}>
          <div>
            <span className="mb-3 grid size-11 place-items-center rounded-xl bg-surface-2 text-ink-muted"><Building2 className="size-5" aria-hidden /></span>
            <h2 id="inspector-name" className="m-0 text-headline font-semibold">{selected?.name || '—'}</h2>
            <div id="inspector-meta" className="mt-1.5 font-mono text-micro text-ink-muted">{selected ? `${selected.id} · ${selected.created}` : '—'}</div>
          </div>
          <div className="flex flex-col gap-2">
            <div className={sectionLabel}>{T('admin.roles.title')}</div>
            <div className="role-box flex flex-col rounded-xl bg-surface-2/60 px-3.5 py-1" id="role-box">
              {(counts.length ? counts : [[T('admin.members.zero'), 0]] as [string, number][]).map(([role, n]) => (
                <div key={role} className="role-row flex justify-between border-b border-hairline-soft py-2 text-body-sm last:border-0">
                  <span className="text-ink-muted">{counts.length ? roleLabel(role) : role}</span><span className="tabular-nums">{n}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="flex flex-col gap-2">
            <div className={sectionLabel}>{T('admin.policy.title')}</div>
            <p className="policy-note m-0 text-body-sm text-ink-muted" id="policy-note">{T('admin.policy')}</p>
          </div>
          <div className="mt-auto flex flex-wrap gap-2">
            <Button tone="primary" id="btn-members" onClick={() => selected && loadMembers(selected)}>{T('admin.members.view')}</Button>
            <Button tone="danger" id="btn-suspend" disabled={!selected || isSuspended(selected)} onClick={suspend}>{T('admin.tenant.suspend')}</Button>
          </div>
        </aside>
      </div>
    </AdminShell>
  )
}
