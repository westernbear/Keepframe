// The `keepframe.ui-context/1` payload the agent page sends with each message (a server contract: see
// keepframe/session/ui_context.py). Ported from keepframe/web/static/js/agent.js buildUIContext.
import { renderPayloadStatus } from '@/lib/agent-render'

const MAX_TEXT = 12000
const MAX_SUMMARY_BYTES = 16 * 1024 - 512 // keepframe/session/ui_context.py MAX_SUMMARY_BYTES, with margin

/** Visible page text, skipping [data-ai-private], scripts, styles and hidden nodes. */
export function visibleText(root: Node = document.body) {
  const values: string[] = []
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  while (walker.nextNode() && values.join(' ').length < MAX_TEXT) {
    const parent = walker.currentNode.parentElement
    if (!parent || parent.closest('[data-ai-private],script,style,[hidden]') || getComputedStyle(parent).visibility === 'hidden') continue
    const text = (walker.currentNode.textContent || '').trim()
    if (text) values.push(text)
  }
  return values.join(' ').slice(0, MAX_TEXT)
}

/** Rendered inputs and their values; never passwords, file pickers or [data-ai-private]. */
export function visibleInputs(root: ParentNode = document) {
  return [...root.querySelectorAll<HTMLInputElement>('input,textarea,select')]
    .filter((el) => !el.closest('[data-ai-private]') && !['password', 'file'].includes(el.type) && el.offsetParent !== null)
    .map((el) => ({ id: el.id || null, value: String(el.value || '').slice(0, 1000) }))
}

/** An SVG blob cannot become an ImageBitmap in Chromium; an <img> decodes it. */
async function decodeImage(blob: Blob): Promise<ImageBitmap | HTMLImageElement> {
  try {
    return await createImageBitmap(blob)
  } catch {
    const url = URL.createObjectURL(blob)
    try {
      const img = new Image()
      img.src = url
      await img.decode()
      return img
    } finally { URL.revokeObjectURL(url) }
  }
}

/** The preview image (or an attached picture) re-encoded as a JPEG of at most 768 px on the long side. */
export async function previewForAI(source: HTMLImageElement | Blob, kind: string) {
  let blob = source as Blob
  if (!(source instanceof Blob)) {
    const response = await fetch(source.currentSrc || source.src, { cache: 'no-store' })
    if (!response.ok) throw new Error(`preview ${kind} unavailable`)
    blob = await response.blob()
  }
  const bitmap = await decodeImage(blob)
  const width = bitmap.width || 768, height = bitmap.height || 768 // an SVG without width/height decodes as 0x0
  const scale = Math.min(1, 768 / Math.max(width, height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(width * scale))
  canvas.height = Math.max(1, Math.round(height * scale))
  canvas.getContext('2d')!.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
  if ('close' in bitmap) bitmap.close()
  const jpeg = await new Promise<Blob>((resolve) => canvas.toBlob(resolve as BlobCallback, 'image/jpeg', 0.72))
  const data = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1])
    reader.onerror = reject
    reader.readAsDataURL(jpeg)
  })
  return { kind, mime: 'image/jpeg', data }
}

export function attachmentMeta(file: File | null) {
  return file ? { name: file.name, type: file.type, size: file.size } : null
}

export interface UIContextInput {
  project: string | null
  scene: string
  version: string | null
  frame: number
  selectedElement: string | null
  attachmentFiles: File[]
  reviewState: { status?: string } | null // the loaded review state (`state` in agent.js)
  renderPayload: any
  orig: HTMLImageElement
  recon: HTMLImageElement
}

/** Field order and values match the legacy payload exactly. */
export async function buildUIContext(s: UIContextInput) {
  const summary = {
    path: location.pathname,
    language: document.documentElement.lang,
    project: s.project,
    scene: s.scene,
    version: s.version,
    frame: s.frame,
    selected_element: s.selectedElement,
    attachments: s.attachmentFiles.map((file, i) => ({ index: i + 1, ...attachmentMeta(file) })),
    workflow: s.reviewState && s.reviewState.status,
    inputs: visibleInputs(),
    render: s.renderPayload ? { plan: s.renderPayload.plan, status: renderPayloadStatus(s.renderPayload) } : null,
    visible_text: visibleText(),
  }
  // MAX_TEXT counts characters but the server caps UTF-8 bytes (Hangul is 3 each): trim until the summary fits
  const bytes = () => new TextEncoder().encode(JSON.stringify(summary)).length
  while (bytes() > MAX_SUMMARY_BYTES && summary.visible_text) summary.visible_text = summary.visible_text.slice(0, Math.floor(summary.visible_text.length * 0.8))
  if (bytes() > MAX_SUMMARY_BYTES) summary.inputs = []
  if (bytes() > MAX_SUMMARY_BYTES) summary.render = null
  // the agent sees attached pictures too; a GLB (or a picture that fails to decode) is described by its summary only
  const attached = await Promise.all(s.attachmentFiles.map((file, i) => /^image\/(png|jpeg|svg\+xml)$/.test(file.type) || /\.(png|jpe?g|svg)$/i.test(file.name)
    ? previewForAI(file, `attachment-${i + 1}`).catch(() => null) : null))
  const images = [...await Promise.all([previewForAI(s.orig, 'original'), previewForAI(s.recon, 'reconstruction')]), ...attached.filter((x) => x !== null)]
  return { schema: 'keepframe.ui-context/1', summary, images }
}
