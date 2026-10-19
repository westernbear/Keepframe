// The `keepframe.ui-context/1` payload the agent page sends with each message (a server contract: see
// keepframe/session/ui_context.py). Ported from keepframe/web/static/js/agent.js buildUIContext.
import { renderPayloadStatus } from '@/lib/agent-render'

const MAX_TEXT = 12000

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

/** The preview image re-encoded as a JPEG of at most 768 px on the long side. */
export async function previewForAI(img: HTMLImageElement, kind: string) {
  const response = await fetch(img.currentSrc || img.src, { cache: 'no-store' })
  if (!response.ok) throw new Error(`preview ${kind} unavailable`)
  const bitmap = await createImageBitmap(await response.blob())
  const scale = Math.min(1, 768 / Math.max(bitmap.width, bitmap.height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(bitmap.width * scale))
  canvas.height = Math.max(1, Math.round(bitmap.height * scale))
  canvas.getContext('2d')!.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
  bitmap.close()
  const blob = await new Promise<Blob>((resolve) => canvas.toBlob(resolve as BlobCallback, 'image/jpeg', 0.72))
  const data = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1])
    reader.onerror = reject
    reader.readAsDataURL(blob)
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
  attachmentFile: File | null
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
    attachment: attachmentMeta(s.attachmentFile),
    workflow: s.reviewState && s.reviewState.status,
    inputs: visibleInputs(),
    render: s.renderPayload ? { plan: s.renderPayload.plan, status: renderPayloadStatus(s.renderPayload) } : null,
    visible_text: visibleText(),
  }
  const images = await Promise.all([previewForAI(s.orig, 'original'), previewForAI(s.recon, 'reconstruction')])
  return { schema: 'keepframe.ui-context/1', summary, images }
}
