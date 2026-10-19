// The After Effects card's polling and actions (legacy ae.js), with the same timers and back-off. The card
// component renders from these fields; `changed` asks it to re-render.
import { T } from '@/i18n'
import { api } from '@/lib/api'
import {
  DEVICE_POLL_MS, STATE_IDLE_MS, backoff, codeCountdown, handEditJob, installOpen, pairingDone, sendBody, sortDevices,
  stateDelay, stateUrl, toSnapshot, type AeDevice, type AeSnapshot, type Pairing,
} from '@/lib/ae'

async function copyText(text: string) {
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch { /* fall back to execCommand */ }
  }
  const focused = document.activeElement as HTMLElement | null
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.readOnly = true
  textarea.style.cssText = 'position:fixed;left:-9999px;top:0'
  document.body.append(textarea)
  try {
    textarea.select()
    return document.execCommand('copy')
  } catch { return false }
  finally {
    textarea.remove()
    focused?.focus({ preventScroll: true })
  }
}

export function createAeCore({ projectId, getSceneId, getVersionId, changed }:
  { projectId: string | null; getSceneId: () => string; getVersionId: () => string | null; changed: () => void }) {
  const c = {
    snapshot: { devices: [], jobs: [], last_synced: {}, progress: {} } as AeSnapshot,
    pairing: null as Pairing | null, codeHidden: false, countdown: '',
    pairMessage: '', copyMessage: '', busy: false, disconnectId: null as string | null,
    handJobId: undefined as string | undefined, overwriteConfirmed: false,
    errorMsg: '', installOpen: true, hadConnection: false,
  }
  let stateTimer: any, deviceTimer: any, countdownTimer: any
  let refreshing = false, refreshAgain = false
  let delay = STATE_IDLE_MS, deviceDelay = DEVICE_POLL_MS, pollError = false
  let disposed = false

  function error(err: any, fromPoll = false) {
    pollError = fromPoll
    c.errorMsg = err.message || T('ae.failedRequest')
    changed()
  }
  function clearError() { pollError = false; c.errorMsg = '' }

  function clearCode(message = '') {
    c.pairing = null
    deviceDelay = DEVICE_POLL_MS
    clearTimeout(deviceTimer)
    clearTimeout(countdownTimer)
    c.countdown = ''
    c.pairMessage = message
    changed()
  }

  function tickCode() {
    clearTimeout(countdownTimer)
    if (!c.pairing) return
    const { milliseconds, text } = codeCountdown(c.pairing.expires_at)
    if (milliseconds <= 0) return clearCode('ae.expired')
    c.codeHidden = document.hidden
    c.countdown = text
    changed()
    if (!document.hidden) countdownTimer = setTimeout(tickCode, Math.min(1000, milliseconds))
  }

  function acceptDevices(devices: AeDevice[]) {
    c.snapshot.devices = devices
    if (pairingDone(c.pairing, devices)) clearCode()
  }

  /** What legacy paint() decided beyond plain rendering: the install disclosure and the hand-edit prompt. */
  function settle() {
    const isConnected = sortDevices(c.snapshot.devices).some((d) => d.connected)
    c.installOpen = installOpen(isConnected, c.hadConnection, c.installOpen)
    c.hadConnection = isConnected
    const handJob = handEditJob(c.snapshot)
    if (c.handJobId !== handJob?.id) c.overwriteConfirmed = false
    c.handJobId = handJob?.id
  }
  function paint() { settle(); changed() }

  async function pollDevices() {
    clearTimeout(deviceTimer)
    if (!c.pairing || document.hidden) return
    try {
      const data = await api('/api/ae/devices')
      acceptDevices(data.devices)
      paint()
      deviceDelay = DEVICE_POLL_MS
      if (pollError) { clearError(); changed() }
      if (!c.pairing) await refresh()
    } catch (err) {
      deviceDelay = backoff(deviceDelay)
      error(err, true)
    }
    if (c.pairing && !document.hidden && !disposed) deviceTimer = setTimeout(pollDevices, deviceDelay)
  }

  async function refresh() {
    clearTimeout(stateTimer)
    if (document.hidden || disposed) return
    if (refreshing) { refreshAgain = true; return }
    refreshing = true
    do {
      refreshAgain = false
      const scene = getSceneId()
      try {
        const data = await api(stateUrl(projectId, scene))
        if (scene !== getSceneId()) { refreshAgain = true; continue }
        c.snapshot = toSnapshot(data)
        acceptDevices(data.devices)
        delay = stateDelay(c.snapshot)
        if (pollError) clearError()
        paint()
      } catch (err) {
        delay = backoff(delay)
        error(err, true)
      }
    } while (refreshAgain && !document.hidden)
    refreshing = false
    if (!document.hidden && !disposed) stateTimer = setTimeout(refresh, delay)
  }

  async function action(work: () => Promise<unknown>) {
    if (c.busy) return
    c.busy = true
    clearError()
    c.copyMessage = ''
    paint()
    try { await work() } catch (err) { error(err) }
    finally { c.busy = false; paint(); await refresh() }
  }

  const send = (force = false) => api('/api/ae/send', { method: 'POST', body: JSON.stringify(sendBody(projectId!, getSceneId(), getVersionId()!, force)) })

  async function copy(fieldId: string) {
    const field = document.getElementById(fieldId) as HTMLInputElement
    const copied = await copyText(field.value)
    if (!copied) { field.focus(); field.select() }
    c.copyMessage = copied ? 'ae.copied' : 'ae.copyFailed'
    changed()
  }

  function onVisibility() {
    clearTimeout(stateTimer)
    clearTimeout(deviceTimer)
    clearTimeout(countdownTimer)
    tickCode()
    if (!document.hidden) { refresh(); pollDevices() }
  }

  const api_ = {
    c, refresh,
    connect: () => action(async () => {
      clearCode()
      const { devices } = await api('/api/ae/devices')
      const code = await api('/api/ae/codes', { method: 'POST', body: '{}' })
      c.pairing = { ...code, before: new Set(devices.map((d: AeDevice) => d.id)) }
      tickCode()
      if (c.pairing && !document.hidden) deviceTimer = setTimeout(pollDevices, DEVICE_POLL_MS)
    }),
    send: () => action(() => send()),
    verify: () => action(() => api('/api/ae/verify', {
      method: 'POST', body: JSON.stringify({ project: projectId, scene: getSceneId(), version: getVersionId() }),
    })),
    overwrite: (on: boolean) => { c.overwriteConfirmed = on; changed() },
    overwriteYes: () => { if (c.handJobId && c.overwriteConfirmed) action(() => send(true)) },
    askDisconnect: (id: string | null) => { c.disconnectId = id; changed() },
    disconnect: (id: string) => action(async () => {
      await api(`/api/ae/devices/${encodeURIComponent(id)}`, { method: 'DELETE' })
      c.disconnectId = null
    }),
    copyInstall: () => copy('ae-install-command'),
    copyCode: () => { tickCode(); if (c.pairing) copy('ae-code') },
    setInstallOpen: (open: boolean) => { c.installOpen = open },
    start() {
      document.addEventListener('visibilitychange', onVisibility)
      settle()
      refresh()
    },
    dispose() {
      disposed = true
      document.removeEventListener('visibilitychange', onVisibility)
      clearTimeout(stateTimer); clearTimeout(deviceTimer); clearTimeout(countdownTimer)
    },
  }
  return api_
}

export type AeCore = ReturnType<typeof createAeCore>
