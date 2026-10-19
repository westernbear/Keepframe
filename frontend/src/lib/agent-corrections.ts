// Agent-proposed corrections and keep changes: preview first, execute only on a confirm click.
// Ported from keepframe/web/static/js/agent.js.
import { T, Tf, errorText } from '@/i18n'
import { fetchReviewJob, postCorrect, postKeep } from '@/lib/api'

const CORRECTION_POLL_INTERVAL_MS = 800

export const ALLOWED: Record<string, string[]> = {
  text: ['element_id', 'text', 'font'],
  reassign: ['from_id', 'to_id', 'frames'],
  mask: ['object_id', 'frame', 'mask_png_base64'],
  bbox: ['object_id', 'frame', 'bbox'],
}
export const REQUIRED: Record<string, string[]> = {
  text: ['element_id'],
  reassign: ['from_id', 'to_id', 'frames'],
  mask: ALLOWED.mask,
  bbox: ALLOWED.bbox,
}
const FONT_FIELDS = ['family_guess', 'weight', 'size_px']

/** A copy of exactly the allowed fields; any other field rejects the whole correction. */
export function pick(args, allowed: string[]) {
  if (!args || typeof args !== 'object' || Array.isArray(args)
      || Object.keys(args).some((key) => !allowed.includes(key))) {
    throw new Error(T('agent.correctionInvalid'))
  }
  return Object.fromEntries(allowed.filter((key) => Object.hasOwn(args, key)).map((key) => {
    const value = key === 'font' ? pick(args[key], FONT_FIELDS) : args[key]
    return [key, Array.isArray(value) ? [...value] : value]
  }))
}

function correctionElement(scene, id) {
  const element = (scene.elements || []).find((el) => el.id === id)
  if (typeof id !== 'string' || !element) throw new Error(T('agent.correctionInvalid'))
  return element
}

export function validateShownCorrection(op: string, shown, scene) {
  if (REQUIRED[op].some((key) => !Object.hasOwn(shown, key))) throw new Error(T('agent.correctionInvalid'))
  const validFrame = (value) => Number.isInteger(value) && value >= 0 && value < scene.frames
  for (const key of ['element_id', 'object_id', 'from_id', 'to_id']) {
    if (Object.hasOwn(shown, key)) correctionElement(scene, shown[key])
  }
  if (op === 'text') {
    if ((!Object.hasOwn(shown, 'text') && !Object.hasOwn(shown, 'font'))
        || (Object.hasOwn(shown, 'text') && typeof shown.text !== 'string')) {
      throw new Error(T('agent.correctionInvalid'))
    }
    if (shown.font) {
      const font = shown.font
      if (!Object.keys(font).length
          || (Object.hasOwn(font, 'family_guess') && (typeof font.family_guess !== 'string'
            || !/^[A-Za-z0-9 \-가-힣]{1,64}$/.test(font.family_guess.trim())))
          || (Object.hasOwn(font, 'weight') && !Number.isInteger(font.weight))
          || (Object.hasOwn(font, 'size_px') && (!Number.isFinite(font.size_px) || font.size_px <= 0))) {
        throw new Error(T('agent.correctionInvalid'))
      }
    }
  } else if (op === 'reassign') {
    if (!Array.isArray(shown.frames) || shown.frames.length !== 2
        || !shown.frames.every(validFrame) || shown.frames[0] > shown.frames[1]) {
      throw new Error(T('agent.correctionInvalid'))
    }
  } else {
    if (!validFrame(shown.frame)) throw new Error(T('agent.correctionInvalid'))
    if (op === 'mask') {
      if (typeof shown.mask_png_base64 !== 'string' || !shown.mask_png_base64.startsWith('iVBORw0KGgo')) {
        throw new Error(T('agent.correctionInvalid'))
      }
    } else {
      const box = shown.bbox
      if (!Array.isArray(box) || box.length !== 4 || !box.every(Number.isInteger)
          || box[0] < 0 || box[1] < 0 || box[2] > scene.size[0] || box[3] > scene.size[1]
          || box[0] >= box[2] || box[1] >= box[3]) {
        throw new Error(T('agent.correctionInvalid'))
      }
    }
  }
}

/** Decodes the mask PNG and returns the bbox and pixel count the server will select (>127). */
export async function summarizeCorrectionMask(base64: string, scene) {
  const img = new Image()
  await new Promise((resolve, reject) => {
    img.onload = resolve
    img.onerror = () => reject(new Error(T('agent.correctionInvalid')))
    img.src = `data:image/png;base64,${base64}`
  })
  const width = img.naturalWidth
  const height = img.naturalHeight
  if (width !== scene.size[0] || height !== scene.size[1]) throw new Error(T('agent.correctionInvalid'))
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!
  ctx.drawImage(img, 0, 0)
  const { data } = ctx.getImageData(0, 0, width, height)
  let x0 = width, y0 = height, x1 = 0, y1 = 0, pixels = 0
  for (let offset = 0; offset < data.length; offset += 4) {
    // Opaque grayscale masks have identical canvas/OpenCV samples. The server
    // selects pixels >127; reject color/alpha masks rather than misstate them.
    if (data[offset] !== data[offset + 1] || data[offset] !== data[offset + 2] || data[offset + 3] !== 255) {
      throw new Error(T('agent.correctionInvalid'))
    }
    if (data[offset] <= 127) continue
    const x = (offset / 4) % width
    const y = Math.floor(offset / 4 / width)
    x0 = Math.min(x0, x); y0 = Math.min(y0, y)
    x1 = Math.max(x1, x + 1); y1 = Math.max(y1, y + 1)
    pixels += 1
  }
  if (!pixels) throw new Error(T('agent.correctionInvalid'))
  return { bbox: [x0, y0, x1, y1], pixels }
}

export function correctionPreviewLines(op: string, shown, scene) {
  const lines = [Tf('agent.correctionPreview', { op })]
  for (const key of ['element_id', 'object_id', 'from_id', 'to_id']) {
    if (!Object.hasOwn(shown, key)) continue
    const element = correctionElement(scene, shown[key])
    lines.push(Tf('agent.correctionElement', {
      field: key, id: element.id, current: element.canonical?.text ?? element.label ?? element.kind ?? '—',
    }))
  }
  if (Object.hasOwn(shown, 'text')) {
    lines.push(Tf('agent.correctionText', {
      old: correctionElement(scene, shown.element_id).canonical?.text ?? '—', next: shown.text,
    }))
  }
  if (shown.font) {
    const old = correctionElement(scene, shown.element_id).canonical?.font || {}
    // FontGuess replaces the entire font, including defaults for omitted fields.
    const next = { family_guess: 'sans-serif', weight: 400, size_px: 32, ...shown.font }
    next.family_guess = next.family_guess.trim()
    const labels = ['agent.correctionFontFamily', 'agent.correctionFontWeight', 'agent.correctionFontSize']
    FONT_FIELDS.forEach((field, index) => {
      lines.push(Tf('agent.correctionFont', { field: T(labels[index]), old: old[field] ?? '—', next: next[field] }))
    })
  }
  if (shown.frames) lines.push(Tf('agent.correctionFrames', { start: shown.frames[0], end: shown.frames[1] }))
  if (Object.hasOwn(shown, 'frame')) lines.push(Tf('agent.correctionFrame', { frame: shown.frame }))
  if (shown.bbox) lines.push(Tf('agent.correctionBbox', { bbox: `[${shown.bbox.join(', ')}]` }))
  return lines
}

/** paintCorrection minus the DOM: the snapshot to confirm and its preview lines. Throws when it cannot be shown;
 *  the page then shows T('agent.correctionInvalid') in the banner and no confirm button. */
export async function prepareCorrection(correction, scene) {
  const { op, args } = correction
  if (!Object.hasOwn(ALLOWED, op)) throw new Error(T('agent.correctionInvalid'))
  const shown = pick(args, ALLOWED[op])
  validateShownCorrection(op, shown, scene)
  const lines = correctionPreviewLines(op, shown, scene)
  if (op === 'mask') {
    const summary = await summarizeCorrectionMask(shown.mask_png_base64, scene)
    lines.push(Tf('agent.correctionMask', { bbox: `[${summary.bbox.join(', ')}]`, pixels: summary.pixels }))
  }
  return { op: op as string, shown, lines }
}

/** paintPending's keep branch minus the DOM. `text` is the preview bubble (null for a malformed payload); when
 *  `ok` is false the page shows T('agent.keepPreviewChanged') as an error and no confirm button. */
export function keepChangePreview(keepChange, constraints: { pred: string }[] = []) {
  const preset = keepChange.preset != null
  if (!preset && !Array.isArray(keepChange.targets)) return { text: null, changes: [], ok: false }
  const changes = preset ? [] : constraints
    .filter((c) => keepChange.targets.some((target) => c.pred === target || c.pred.includes(target)))
    .map((c) => ({ pred: c.pred, keep: keepChange.on }))
  const preview = preset
    ? Tf('agent.keepPresetPreview', { preset: keepChange.preset })
    : Tf(keepChange.on ? 'agent.keepOnPreview' : 'agent.keepOffPreview', { n: keepChange.matched })
  const examples = !preset && Array.isArray(keepChange.examples) ? keepChange.examples.slice(0, 5) : []
  const text = examples.length
    ? `${preview}\n${Tf('agent.keepExamplesPreview', { examples: examples.join('; ') })}`
    : preview
  return { text, changes, ok: preset || changes.length === keepChange.matched }
}

export interface ConfirmContext {
  projectId: string
  sceneId: string
  setBanner: (message: string, isError?: boolean) => void
  appendAgent: (text: string) => void
  refreshAfterEdit: (version: string) => Promise<void>
}

/** Both confirm functions resolve true once applied (the button stays disabled), false to allow a retry. */
export async function confirmKeepChange(ctx: ConfirmContext, keepChange, changes) {
  ctx.setBanner('')
  try {
    const res = await postKeep(ctx.projectId, ctx.sceneId, changes, T('agent.keepChangeNote'), keepChange.preset)
    await ctx.refreshAfterEdit(res.version.id)
    ctx.appendAgent(T('agent.applied'))
    return true
  } catch (err) {
    ctx.setBanner((err as Error).message || T('agent.failed'), true)
    return false
  }
}

async function pollCorrection(projectId: string, previewSceneId: string) {
  while (true) {
    const job = await fetchReviewJob(projectId, previewSceneId)
    if (job.status === 'done' && job.version) return job
    if (job.status === 'error') throw new Error(errorText(job.error) || T('review.error'))
    if (!['running', 'queued'].includes(job.status)) throw new Error(T('agent.failed'))
    await new Promise((resolve) => setTimeout(resolve, CORRECTION_POLL_INTERVAL_MS))
  }
}

/** Submits the previewed snapshot against the version that was on screen when it was previewed. */
export async function confirmCorrection(ctx: ConfirmContext, op: string, shown, previewVersion: string, previewSceneId: string) {
  ctx.setBanner(Tf('review.running', { op }))
  try {
    if (ctx.sceneId !== previewSceneId) throw new Error(T('agent.correctionInvalid'))
    await postCorrect(ctx.projectId, ctx.sceneId, op, { ...shown, version: previewVersion })
    const job = await pollCorrection(ctx.projectId, previewSceneId)
    await ctx.refreshAfterEdit(job.version)
    ctx.setBanner('')
    ctx.appendAgent(T('agent.applied'))
    return true
  } catch (err) {
    ctx.setBanner((err as Error).message || T('agent.failed'), true)
    return false
  }
}

/** appendConfirmButton's click guard: one request at a time, disabled for good once onConfirm resolves true. */
export function confirmGuard(onConfirm: () => Promise<boolean | undefined>) {
  const guard = {
    disabled: false,
    async click() {
      if (guard.disabled) return
      guard.disabled = true
      guard.disabled = await onConfirm() === true
    },
  }
  return guard
}
