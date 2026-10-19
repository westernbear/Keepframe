import { T, Tf } from '@/i18n'

// Pure logic of the After Effects card (legacy keepframe/web/static/js/ae.js). No DOM.

export const JOB_HISTORY_LIMIT = 4
export const STATE_IDLE_MS = 15000
export const STATE_ACTIVE_MS = 2000
export const DEVICE_POLL_MS = 2000
export const MAX_BACKOFF_MS = 60000

export type AeDevice = {
  id: string
  connected?: boolean
  last_seen?: number | null
  created: number
  ae_version?: string
  project_name?: string
  panel_build?: string
  host_build?: string
}
export type AeJob = {
  id: string
  kind: string
  state: string
  error?: string
  result?: any
  created?: number
  started?: number
  finished?: number
}
export type AeSnapshot = {
  devices: AeDevice[]
  jobs: AeJob[]
  last_synced: Record<string, string>
  progress: Record<string, any>
  verify?: any
}
export type Pairing = { code: string; expires_at: number; before: Set<string> }

/** Without a project and scene the card only polls devices. */
export const stateUrl = (project: string | null | undefined, scene: string | null | undefined) =>
  project && scene ? `/api/ae/state?${new URLSearchParams({ project, scene })}` : '/api/ae/devices'

export const toSnapshot = (data: any): AeSnapshot => ({ jobs: [], last_synced: {}, progress: {}, ...data })

export const sendBody = (project: string, scene: string, version: string, force = false) =>
  ({ project, scene, version, ...(force ? { force: true } : {}) })

/** A failed poll doubles its delay up to a minute. */
export const backoff = (delay: number) => Math.min(delay * 2, MAX_BACKOFF_MS)

const isActiveJob = (job: AeJob) => ['queued', 'running'].includes(job.state)
const isVerifying = (report: any) => ['rendering', 'verifying'].includes(report?.state)

/** State poll delay after a successful refresh: fast while a job runs or AE is verifying. */
export function stateDelay(snapshot: AeSnapshot) {
  return snapshot.jobs.some(isActiveJob) || isVerifying(snapshot.verify) ? STATE_ACTIVE_MS : STATE_IDLE_MS
}

/** Polling pauses while the page is hidden; device polling also needs a pairing in progress. */
export const shouldPollState = (hidden: boolean) => !hidden
export const shouldPollDevices = (pairing: Pairing | null, hidden: boolean) => Boolean(pairing) && !hidden

/** A device that was not there when the code was issued ends the pairing. */
export const pairingDone = (pairing: Pairing | null, devices: AeDevice[]) =>
  Boolean(pairing && devices.some((device) => !pairing.before.has(device.id)))

/** Milliseconds left on a pairing code and its "mm:ss" countdown. */
export function codeCountdown(expiresAt: number, now = Date.now()) {
  const milliseconds = expiresAt * 1000 - now
  const remaining = Math.ceil(milliseconds / 1000)
  const text = `${Math.floor(remaining / 60).toString().padStart(2, '0')}:${(remaining % 60).toString().padStart(2, '0')}`
  return { milliseconds, text }
}

export function relative(time: number | null | undefined, now = Date.now() / 1000) {
  if (time == null) return T('ae.neverSeen')
  const seconds = Math.max(0, now - time)
  if (seconds < 60) return T('ae.justNow')
  if (seconds < 3600) return Tf('ae.minutesAgo', { n: Math.floor(seconds / 60) })
  if (seconds < 86400) return Tf('ae.hoursAgo', { n: Math.floor(seconds / 3600) })
  return Tf('ae.daysAgo', { n: Math.floor(seconds / 86400) })
}

export function editMessage(result: any) {
  const interrupted = Array.isArray(result.interrupted) ? result.interrupted : []
  const edited = (Array.isArray(result.hand_edited) ? result.hand_edited : []).filter((id) => !interrupted.includes(id))
  return [interrupted.length && Tf('ae.interrupted', { ids: interrupted.join(', ') }),
    edited.length && Tf('ae.handEdited', { n: edited.length, ids: edited.join(', ') })].filter(Boolean).join('\n')
}

export function jobOutcome(job: AeJob | undefined | null) {
  if (!job) return ''
  if (job.error) return job.error
  if (job.state !== 'done') return T(`ae.${job.state}`)
  if (job.result?.applied !== true) {
    if (job.result?.applied === false) return '' // The hand-edit prompt owns this outcome.
    return T('ae.done')
  }
  const counts = ['created', 'updated', 'deleted'].flatMap((key) => {
    const value = job.result[key]
    const n = Array.isArray(value) ? value.length : Number(value) || 0
    return n > 0 ? [Tf(`ae.count.${key}`, { n })] : []
  })
  return counts.join(', ') || T('ae.noChanges')
}

export const percent = (value: number) => (value * 100).toFixed(1)

export function verifyOutcome(report: any) {
  if (report.state === 'rendering') return T('ae.verifyRendering')
  if (report.state === 'verifying') return T('ae.verifyVerifying')
  if (report.state === 'failed') {
    if (report.error === 'the saved report could not be read — verify again') return T('ae.verifyUnreadable')
    return report.error || T('ae.verifyFailed')
  }
  if (report.state === 'interrupted') return T('ae.verifyInterrupted')
  const worst = report.worst[0]
  let text = Tf(report.passed ? 'ae.verifyPassed' : 'ae.verifyDiffers', {
    version: report.version, mean: percent(report.mean), frame: worst.frame, max: percent(report.max),
  })
  if (!report.passed) {
    const n = report.frames.filter((row) => row.l1 > report.thresholds.frame).length
    if (n) text += Tf(n === 1 ? 'ae.verifyOverOne' : 'ae.verifyOver', {
      n, threshold: Number(percent(report.thresholds.frame)),
    })
    else text += Tf('ae.verifyMeanOver', { mean: percent(report.mean), threshold: Number(percent(report.thresholds.mean)) })
  }
  return text
}

export const verifyLimits = (report: any) => Tf('ae.verifyLimits', {
  mean: Number(percent(report.thresholds.mean)), frame: Number(percent(report.thresholds.frame)),
})

export function verifyNotes(report: any): string[] {
  if (!Array.isArray(report.masked)) return report.notes || []
  return report.masked.map((row) => {
    // Saved reports from earlier builds have only id and worst_l1.
    if (!row.font) return Tf('ae.verifyMaskedLegacy', { id: row.id, l1: percent(row.worst_l1) })
    return Tf('ae.verifyMasked', { name: row.name, font: row.font, l1: percent(row.worst_l1) })
  })
}

export const verifyImageUrl = (params: { project: string; scene: string; version: string; name: string; job: string }) =>
  `/api/ae/verify-image?${new URLSearchParams(params)}`

/** Newest seen first, then newest created. */
export const sortDevices = (devices: AeDevice[]) =>
  [...devices].sort((a, b) => (b.last_seen ?? 0) - (a.last_seen ?? 0) || b.created - a.created)

export function connectionStatus(connected: AeDevice | undefined) {
  if (!connected) return T('ae.notConnected')
  const version = connected.ae_version?.match(/^\d+(?:\.\d+)?/)?.[0]
  let status = Tf('ae.connectedDevice', { version: version ? ` ${version}` : '' })
  if (connected.project_name) status += ` · ${connected.project_name}`
  return status
}

/** The install section opens when the connection drops and closes when it returns; otherwise it stays as the user left it. */
export const installOpen = (isConnected: boolean, hadConnection: boolean, open: boolean) =>
  isConnected !== hadConnection ? !isConnected : open

export const verifyActive = (snapshot: AeSnapshot) => isVerifying(snapshot.verify)
  || snapshot.jobs.some((job) => job.kind === 'render_frames' && isActiveJob(job))

/** The one-line card summary and whether it reads as an error. `devices` are sorted. */
export function syncSummary(snapshot: AeSnapshot, devices: AeDevice[]) {
  const syncDevice = devices.find((device) => device.connected) || devices[0]
  const synced = syncDevice && snapshot.last_synced[syncDevice.id]
  const latestJob = snapshot.jobs[0]
  const syncedText = synced ? Tf('ae.hasVersion', { version: synced }) : T('ae.nothingSynced')
  const outcome = jobOutcome(latestJob)
  const report = snapshot.verify
  const showVerify = report && (!latestJob || latestJob.id === report.job)
  if (showVerify) return { text: verifyOutcome(report), failed: report.state === 'failed' }
  return { text: outcome ? `${syncedText}. ${outcome}` : syncedText, failed: Boolean(latestJob?.error) || latestJob?.state === 'failed' }
}

export const buildsDiffer = (devices: AeDevice[]) =>
  devices.some((device) => device.panel_build && device.host_build && device.panel_build !== device.host_build)

export function latestWarnings(snapshot: AeSnapshot): string[] {
  const warnings = snapshot.jobs[0]?.result?.warnings
  return Array.isArray(warnings) ? warnings : []
}

export const warningsSummary = (n: number) => (n ? Tf(n === 1 ? 'ae.warningOne' : 'ae.warningCount', { n }) : '')

export const syncTimings = (snapshot: AeSnapshot) => snapshot.jobs.find((job) => job.kind === 'sync')?.result?.timings

export const deviceText = (device: AeDevice, now?: number) => Tf('ae.device', {
  version: device.ae_version || T('ae.unknownBuild'), seen: relative(device.last_seen, now),
})

export const buildText = (device: AeDevice) => Tf('ae.build', {
  panel: device.panel_build || T('ae.unknownBuild'), host: device.host_build || T('ae.unknownBuild'),
})

/** The newest sync, when AE refused it over hand edits and is waiting for an overwrite. */
export function handEditJob(snapshot: AeSnapshot) {
  const newestSync = snapshot.jobs.find((job) => job.kind === 'sync')
  return newestSync?.state === 'done' && newestSync.result?.applied === false ? newestSync : null
}

export const historyJobs = (snapshot: AeSnapshot) => snapshot.jobs.slice(1, JOB_HISTORY_LIMIT + 1)

export function jobLine(job: AeJob, now?: number) {
  const outcome = job.result?.applied === false ? editMessage(job.result) : jobOutcome(job)
  return Tf('ae.job', {
    kind: T(`ae.${job.kind}`),
    outcome: (outcome || T(`ae.${job.state}`)).replace(/\s+/g, ' '),
    time: relative(job.finished ?? job.started ?? job.created, now),
  })
}
