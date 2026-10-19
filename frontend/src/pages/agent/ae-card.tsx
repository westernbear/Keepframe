import { ChevronDown } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { Button } from '@/components/app/button'
import { useI18n } from '@/i18n'
import {
  buildText, buildsDiffer, connectionStatus, deviceText, editMessage, handEditJob, historyJobs, jobLine, latestWarnings,
  percent, sortDevices, syncSummary, syncTimings, verifyActive, verifyImageUrl, verifyLimits, verifyNotes, verifyOutcome,
  warningsSummary,
} from '@/lib/ae'
import { cn } from '@/lib/utils'
import { createAeCore } from './ae-core'
import { Disclosure, card, lock } from './ui'

const INSTALL_COMMAND = '& "C:\\Program Files\\Common Files\\Adobe\\Adobe Desktop Common\\RemoteComponents\\UPI\\UnifiedPluginInstallerAgent\\UnifiedPluginInstallerAgent.exe" /install "$env:USERPROFILE\\Downloads\\keepframe.zxp"'
const field = 'h-10 min-w-0 flex-1 rounded-lg border border-hairline bg-canvas px-3 font-mono text-caption text-ink outline-none focus-visible:border-brand'
const reason = 'ae-card__reason text-caption text-ink-muted'

/** The After Effects card. `onReady` hands the parent a refresh it awaits after each edit. */
export function AeCard({ projectId, sceneId, versionId, onReady }: { projectId: string | null; sceneId: string; versionId: string | null; onReady: (refresh: () => Promise<void>) => void }) {
  const { T, Tf } = useI18n()
  const [, setTick] = useState(0)
  const ids = useRef({ sceneId, versionId })
  useEffect(() => { ids.current = { sceneId, versionId } }, [sceneId, versionId])
  const [ae] = useState(() => {
    let live = false
    const core = createAeCore({
      projectId, getSceneId: () => ids.current.sceneId, getVersionId: () => ids.current.versionId,
      // Synchronous like the legacy DOM writes, so focus moves (overwrite, disconnect) find their buttons.
      changed: () => (live ? flushSync(() => setTick((n) => n + 1)) : setTick((n) => n + 1)),
    })
    return { core, setLive: (on: boolean) => { live = on } }
  })
  const core = ae.core
  const c = core.c

  useEffect(() => {
    ae.setLive(true)
    core.start()
    onReady(core.refresh)
    return () => { ae.setLive(false); core.dispose() }
  }, [ae, core, onReady])

  const devices = sortDevices(c.snapshot.devices)
  const connected = devices.find((d) => d.connected)
  const isConnected = Boolean(connected)
  const sendDisabled = c.busy || !isConnected || !projectId || !sceneId || !versionId
  const comparing = verifyActive(c.snapshot)
  const summary = syncSummary(c.snapshot, devices)
  const report = c.snapshot.verify
  const hasReport = report?.state === 'done'
  const latestJob = c.snapshot.jobs[0]
  const progress = latestJob && c.snapshot.progress[latestJob.id]
  const timings = syncTimings(c.snapshot)
  const warnings = latestWarnings(c.snapshot)
  const handJob = handEditJob(c.snapshot)
  const notes = hasReport ? verifyNotes(report) : []

  const connect = (
    <Button instant tone="secondary" size="sm" id="ae-connect" disabled={c.busy} onClick={core.connect}>{T('ae.connect')}</Button>
  )
  const focusLater = (id: string) => document.getElementById(id)?.focus()
  const deviceButtons = (id: string) => document.querySelector<HTMLButtonElement>(`#ae-devices [data-device="${CSS.escape(id)}"] button`)

  return (
    <section className={cn(card, 'ae-card')} id="ae-card" aria-labelledby="ae-card-title">
      <Disclosure id="ae-card" storageKey="kf.agent.aeCardOpen" defaultOpen titleKey="ae.title"
        status={<span id="ae-status" className={cn('render-card__status text-caption', isConnected ? 'render-card__status--active text-success' : 'text-ink-muted')} role="status" aria-live="polite">{connectionStatus(connected)}</span>}>
        <details id="ae-install" open={c.installOpen} onToggle={(e) => core.setInstallOpen(e.currentTarget.open)} className="group/install">
          <summary className="flex min-h-11 cursor-pointer list-none items-center gap-1.5 text-caption text-ink-muted hover:text-ink">
            <ChevronDown className="size-3.5 -rotate-90 transition-transform group-open/install:rotate-0" aria-hidden />{T('ae.install')}
          </summary>
          <div className="flex flex-col gap-3 pb-2">
            <div id="ae-install-connect" className="render-card__actions flex flex-wrap gap-2">{isConnected && connect}</div>
            <div className="ae-card__install flex flex-col gap-2">
              <Button instant tone="secondary" size="sm" className="btn--inline self-start" asChild><a href="/ae/keepframe.zxp" download>{T('ae.download')}</a></Button>
              <label htmlFor="ae-install-command" className="text-caption text-ink-muted">{T('ae.installCommand')}</label>
              <div className="ae-card__row flex gap-2">
                <input id="ae-install-command" className={field} readOnly aria-describedby="ae-install-path-hint" value={INSTALL_COMMAND} />
                <Button instant tone="secondary" size="sm" id="ae-install-copy" className="btn--inline h-10" onClick={core.copyInstall}>{T('ae.copy')}</Button>
              </div>
              <p id="ae-install-path-hint" className={lock}>{T('ae.installPathHint')}</p>
              <p className={lock}>{T('ae.installHint')}</p>
            </div>
          </div>
        </details>

        <div className="render-card__actions flex flex-wrap items-center gap-2" id="ae-actions">
          {!isConnected && connect}
          <Button instant tone="primary" size="sm" id="ae-send" aria-describedby="ae-send-reason" disabled={sendDisabled} onClick={core.send}>{T('ae.send')}</Button>
          <Button instant tone="secondary" size="sm" id="ae-verify" className="btn--secondary btn--inline" disabled={sendDisabled || comparing}
            aria-describedby={comparing && isConnected ? 'ae-verify-reason' : 'ae-send-reason'} onClick={core.verify}>{T('ae.verify')}</Button>
          <span id="ae-send-reason" className={reason} hidden={isConnected}>{T('ae.connectionRequired')}</span>
          <span id="ae-verify-reason" className={reason} hidden={!comparing || !isConnected}>{comparing && isConnected ? T('ae.verifyBusy') : ''}</span>
        </div>

        <div id="ae-pairing" className="ae-card__pairing flex flex-col gap-2" data-ai-private>
          <div id="ae-code-field" className="ae-card__row flex items-center gap-2" hidden={!c.pairing || c.codeHidden}>
            <input id="ae-code" className={cn(field, 'ae-card__code text-body-sm tracking-wider')} readOnly aria-label={T('ae.pairingCode')} value={c.pairing && !c.codeHidden ? c.pairing.code : ''} />
            <Button instant tone="secondary" size="sm" id="ae-code-copy" className="btn--inline h-10" onClick={core.copyCode}>{T('ae.copy')}</Button>
            <span id="ae-countdown" className="font-mono text-caption text-ink-muted tabular-nums" aria-label={T('ae.expiresIn')}>{c.countdown}</span>
          </div>
          <p id="ae-pair-message" className={lock} role="status" hidden={!c.pairMessage}>{c.pairMessage ? T(c.pairMessage) : ''}</p>
        </div>
        <p id="ae-copy-status" className={lock} role="status" aria-live="polite" hidden={!c.copyMessage}>{c.copyMessage ? T(c.copyMessage) : ''}</p>
        <p id="ae-synced" className={cn('m-0 text-body-sm text-ink-muted', summary.failed && 'ae-card__error')} aria-live="polite">{summary.text}</p>
        <p id="ae-build-warning" className={cn(lock, 'text-warning')} hidden={!buildsDiffer(devices)}>{T('ae.buildMismatch')}</p>

        <details id="ae-details" className="group/details">
          <summary className="flex min-h-11 cursor-pointer list-none items-center gap-1.5 text-caption text-ink-muted hover:text-ink">
            <ChevronDown className="size-3.5 -rotate-90 transition-transform group-open/details:rotate-0" aria-hidden />{T('ae.details')}
          </summary>
          <div className="flex flex-col gap-3 pb-2">
            <div id="ae-verify-report" className="ae-card__verify-report flex flex-col gap-2" hidden={!hasReport}>
              <p id="ae-verify-outcome" className="m-0 text-body-sm">{hasReport ? verifyOutcome(report) : ''}</p>
              <p id="ae-verify-limits" className={lock}>{hasReport ? verifyLimits(report) : ''}</p>
              <ul id="ae-verify-worst" className="ae-card__list m-0 flex list-none flex-col gap-3 p-0">
                {hasReport && report.worst.slice(0, 3).map((row: any) => (
                  <li key={row.frame}>
                    <p className="m-0 mb-1.5 text-caption text-ink-muted">{Tf('ae.verifyFrame', { frame: row.frame, l1: percent(row.l1) })}</p>
                    <div className="ae-card__verify-images grid grid-cols-3 gap-1.5">
                      {([['ae', 'ae.verifyAE'], ['kf', 'ae.verifyKF'], ['diff', 'ae.verifyDiff']] as const).map(([kind, key]) => (
                        <figure key={kind} className="m-0 flex flex-col gap-1">
                          <figcaption className="text-micro text-ink-muted">{T(key)}</figcaption>
                          <img loading="lazy" className="w-full rounded-md bg-surface-2" alt={Tf('ae.verifyImageAlt', { kind: T(key), frame: row.frame })}
                            src={verifyImageUrl({ project: projectId!, scene: sceneId, version: report.version, name: row[kind], job: report.job })} />
                        </figure>
                      ))}
                    </div>
                  </li>
                ))}
              </ul>
              <ul id="ae-verify-notes" className="ae-card__list m-0 flex list-none flex-col gap-1 p-0 text-caption text-ink-muted" hidden={!notes.length}>
                {notes.map((note, i) => <li key={i}>{note}</li>)}
              </ul>
            </div>
            <ul id="ae-devices" className="ae-card__list m-0 flex list-none flex-col gap-2 p-0">
              {devices.map((device) => (
                <li key={device.id} data-device={device.id} className="ae-card__row flex flex-wrap items-center gap-2 text-caption">
                  <span aria-hidden className={cn('ae-card__dot size-2 rounded-full', device.connected ? 'ae-card__dot--connected bg-success' : 'bg-ink-muted/40')} />
                  <span className="ae-card__device flex min-w-0 flex-1 flex-col">
                    <span>{deviceText(device)}</span>
                    <p className="m-0 text-micro text-ink-muted">{buildText(device)}</p>
                  </span>
                  {c.disconnectId === device.id ? <>
                    <span>{T('ae.disconnectConfirm')}</span>
                    <Button instant tone="danger" size="xs" className="btn--inline ae-card__destructive" disabled={c.busy} onClick={() => core.disconnect(device.id)}>{T('ae.yes')}</Button>
                    <Button instant tone="secondary" size="xs" className="btn--inline" disabled={c.busy} onClick={() => { core.askDisconnect(null); deviceButtons(device.id)?.focus() }}>{T('ae.no')}</Button>
                  </> : (
                    <Button instant tone="secondary" size="xs" className="btn--inline ae-card__destructive" disabled={c.busy} onClick={() => { core.askDisconnect(device.id); deviceButtons(device.id)?.focus() }}>{T('ae.disconnect')}</Button>
                  )}
                </li>
              ))}
            </ul>
            <p id="ae-latest-progress" className={lock} hidden={!progress}>{progress ? Tf('ae.progress', progress) : ''}</p>
            <p id="ae-sync-timings" className={lock} hidden={!timings}>{timings ? Tf('ae.syncTimings', timings) : ''}</p>
            <details id="ae-warnings-details" className="ae-card__warnings" hidden={!warnings.length}>
              <summary id="ae-warnings-summary" className="cursor-pointer text-caption text-warning">{warningsSummary(warnings.length)}</summary>
              <ul id="ae-warnings" className="ae-card__list m-0 mt-1 flex list-none flex-col gap-1 p-0 text-caption text-ink-muted">
                {warnings.map((w, i) => <li key={i}>{w}</li>)}
              </ul>
            </details>
            <ul id="ae-jobs" className="ae-card__list m-0 flex list-none flex-col gap-1 p-0 text-caption text-ink-muted" aria-live="polite">
              {historyJobs(c.snapshot).map((job) => <li key={job.id} className={job.error ? 'ae-card__error' : ''}>{jobLine(job)}</li>)}
            </ul>
          </div>
        </details>
      </Disclosure>

      <p id="ae-error" className="ae-card__error m-0 text-body-sm" role="status" aria-live="polite" hidden={!c.errorMsg}>{c.errorMsg}</p>
      <div id="ae-hand-edits" className="ae-card__hand-edits flex flex-col gap-2 rounded-xl bg-warning/10 p-3" hidden={!handJob}>
        <p id="ae-hand-message" className="m-0 whitespace-pre-line text-body-sm">{handJob ? editMessage(handJob.result) : ''}</p>
        <Button instant tone="secondary" size="sm" id="ae-overwrite" className="btn--inline ae-card__destructive self-start" aria-describedby="ae-overwrite-reason"
          hidden={c.overwriteConfirmed} disabled={c.busy || !connected} onClick={() => { core.overwrite(true); focusLater('ae-overwrite-yes') }}>{T('ae.overwrite')}</Button>
        <div id="ae-overwrite-confirm" className="ae-card__row flex flex-wrap items-center gap-2 text-body-sm" hidden={!c.overwriteConfirmed}>
          <span>{T('ae.overwriteConfirm')}</span>
          <Button instant tone="danger" size="sm" id="ae-overwrite-yes" className="btn--inline ae-card__destructive" aria-describedby="ae-overwrite-reason" disabled={c.busy || !connected} onClick={core.overwriteYes}>{T('ae.yes')}</Button>
          <Button instant tone="secondary" size="sm" id="ae-overwrite-no" className="btn--inline" disabled={c.busy} onClick={() => { core.overwrite(false); focusLater('ae-overwrite') }}>{T('ae.no')}</Button>
        </div>
        <span id="ae-overwrite-reason" className={reason} hidden={isConnected}>{T('ae.connectionRequired')}</span>
      </div>
    </section>
  )
}
