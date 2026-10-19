import { useEffect, useState } from 'react'
import { AdminShell } from '@/components/app/admin-shell'
import { useI18n } from '@/i18n'
import { initialJob, isFailed, isQuarantined, jobStatusLabel, jobStatusTone, loadErrorText, queueCounts, retriesExhausted, retryLabel, type QueueJob } from '@/lib/admin'
import { fetchAdminOrRedirect } from '@/lib/api'
import { cn } from '@/lib/utils'
import { panel, rowKeys, sectionLabel, td, th } from './common'

type Job = QueueJob & { tenant: string; kind: string; target: string }
const TONE: Record<string, string> = { active: 'bg-brand/10 text-brand-soft', error: 'bg-danger/10 text-danger', done: 'bg-surface-3 text-ink' }

export default function AdminQueue() {
  const { T } = useI18n()
  const [jobs, setJobs] = useState<Job[]>([])
  const [retryCap, setRetryCap] = useState(0)
  const [selected, setSelected] = useState<Job | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    Promise.all([fetchAdminOrRedirect('/admin/api/jobs'), fetchAdminOrRedirect('/admin/api/policy')])
      .then(([data, policy]) => { setJobs(data.jobs); setRetryCap(policy.retry_cap); setSelected(initialJob(data.jobs) || null) })
      .catch((err) => { const text = loadErrorText(err); if (text) setError(text) })
  }, [])

  const counts = queueCounts(jobs)
  const fail = selected && isFailed(selected)
  const metric = (dot: string, key: string, value: number, id: string) => (
    <span className="inline-flex items-center gap-2 text-body-sm">
      <span aria-hidden className={cn('size-2 rounded-full', dot)} /><span className="text-ink-muted">{T(key)}</span><strong id={id} className="tabular-nums">{value}</strong>
    </span>
  )
  return (
    <AdminShell page="queue" title={T('admin.queue')}>
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        <section className={cn(panel, 'flex min-w-0 flex-col')}>
          <div className="metrics-bar flex flex-wrap items-center justify-between gap-x-6 gap-y-2 border-b border-hairline-soft p-4">
            <h2 className="m-0 text-body font-semibold">{T('admin.queue.operationDesk')}</h2>
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
              {metric('bg-brand', 'admin.queue.queued', counts.queued, 'queue-queued')}
              {metric('bg-ink', 'admin.queue.running', counts.running, 'queue-running')}
              {metric('bg-danger', 'admin.queue.failed', counts.failed, 'queue-failed')}
              <span className="inline-flex items-center gap-2 text-body-sm"><span className="text-ink-muted">{T('admin.queue.gpuUsage')}</span><strong id="queue-gpu" className="font-mono">{counts.gpu}</strong></span>
            </div>
          </div>
          <div className="admin-data-scroll overflow-x-auto">
            <table className="data-table w-full min-w-[760px] border-collapse text-body-sm">
              <thead className="border-b border-hairline-soft">
                <tr>{['admin.queue.jobId', 'admin.table.tenant', 'admin.queue.task', 'admin.queue.target', 'admin.table.status', 'admin.queue.resource', 'admin.queue.duration', 'admin.queue.retries'].map((k) => <th key={k} className={th}>{T(k)}</th>)}</tr>
              </thead>
              <tbody id="job-rows">
                {error ? <tr><td colSpan={8} className={cn(td, 'admin-load-error text-danger')}>{error}</td></tr> : jobs.map((j) => {
                  const quarantined = isQuarantined(j)
                  const exhausted = retriesExhausted(j, retryCap)
                  return (
                    <tr key={j.id} aria-label={`${j.id} ${j.target}`} {...rowKeys(() => setSelected(j))}
                      className={cn('state-transition cursor-pointer border-b border-hairline-soft outline-none last:border-0 focus-visible:bg-white/[0.06]', selected?.id === j.id ? 'row-selected bg-white/[0.06]' : 'hover:bg-white/[0.03]', quarantined && 'row-dimmed opacity-60')}>
                      <td className={cn(td, 'font-mono text-caption text-ink-muted')}>{j.id}</td>
                      <td className={td}>{j.tenant}</td>
                      <td className={cn(td, 'font-mono text-caption text-ink-muted')}>{j.kind}</td>
                      <td className={td}>{j.target}</td>
                      {quarantined ? (
                        <td className={td} colSpan={4}><span className="badge rounded-md bg-warning/10 px-2.5 py-1 text-micro text-warning">{T('admin.queue.movedToQuarantine')}</span></td>
                      ) : <>
                        <td className={td}><span className={cn('badge rounded-md px-2.5 py-1 text-micro font-medium', TONE[jobStatusTone(j.status)] || 'bg-surface-3 text-ink-muted')}>{jobStatusLabel(j.status)}</span></td>
                        <td className={cn(td, 'font-mono text-caption text-ink-muted')}>{j.gpu || T('common.none')}</td>
                        <td className={cn(td, 'text-ink-muted')}>{T('common.none')}</td>
                        <td className={cn(td, 'tabular-nums', exhausted ? 'retry--exhausted text-danger' : 'text-ink-muted')}>{retryLabel(j, retryCap)}</td>
                      </>}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>

        <aside className={cn(panel, 'job-drawer flex flex-col gap-5 p-5')}>
          <div>
            <div id="drawer-id" className="mb-1 font-mono text-micro text-ink-muted">{selected?.id || '—'}</div>
            <h3 id="drawer-target" className="m-0 text-headline font-semibold">{selected?.target || '—'}</h3>
          </div>
          <div className="flex flex-col gap-2">
            <div className={sectionLabel}>{T('admin.queue.failureReason')}</div>
            <div id="drawer-error" className={cn('error-box rounded-xl p-3 text-body-sm', fail ? 'bg-danger/10 text-danger' : 'bg-surface-2 text-ink-muted')}>{fail ? (selected!.error || T('admin.queue.failedDefault')) : '—'}</div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-xl bg-surface-2/60 p-3"><div className="text-micro text-ink-muted">{T('admin.queue.retries')}</div>
              <div id="drawer-retries" className={cn('text-headline font-semibold tabular-nums', fail && retriesExhausted(selected!, retryCap) && 'retry--exhausted text-danger')}>{fail ? retryLabel(selected!, retryCap) : '—'}</div></div>
            <div className="rounded-xl bg-surface-2/60 p-3"><div className="text-micro text-ink-muted">{T('admin.queue.priority')}</div><div className="text-headline font-semibold">{T('admin.queue.priorityDefault')}</div></div>
          </div>
          <p id="drawer-policy" className="m-0 text-body-sm text-ink-muted italic">{fail ? `"${T('admin.policy')}"` : ''}</p>
        </aside>
      </div>
    </AdminShell>
  )
}
