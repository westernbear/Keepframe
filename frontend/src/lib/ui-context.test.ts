import { afterEach, expect, it, vi } from 'vitest'
import { buildUIContext } from './ui-context'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
  delete (HTMLElement.prototype as any).offsetParent
})

it('builds the keepframe.ui-context/1 payload with the legacy fields in order', async () => {
  document.documentElement.lang = 'ko'
  document.body.innerHTML = `
    <h1>Agent</h1><p hidden>gone</p><p style="visibility:hidden">ghost</p><script>var x = 1</script>
    <div data-ai-private>secret <input id="code" value="123"></div>
    <input id="q" value="hello"><input type="password" value="pw"><input type="file">
    <textarea>${'y'.repeat(1200)}</textarea><select hidden id="s"></select>
    <img id="orig" src="/frame/orig/0"><img id="recon" src="/frame/recon/0">`
  // jsdom never lays out, so offsetParent is null; emulate "rendered unless [hidden]".
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true, get() { return this.closest('[hidden]') ? null : document.body },
  })
  const fetched: string[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    expect(init).toEqual({ cache: 'no-store' })
    fetched.push(url)
    return { ok: true, blob: async () => new Blob(['img']) }
  })
  vi.stubGlobal('createImageBitmap', async () => ({ width: 1536, height: 768, close: () => {} }))
  const sizes: number[][] = []
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (this: HTMLCanvasElement) {
    sizes.push([this.width, this.height])
    return { drawImage: () => {} } as any
  } as any)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((cb, type, quality) => {
    expect([type, quality]).toEqual(['image/jpeg', 0.72])
    cb(new Blob(['jpg'], { type: 'image/jpeg' }))
  })
  const file = new File(['abc'], 'logo.svg', { type: 'image/svg+xml' })
  const model = new File(['glTF'], 'cup.glb')
  const context = await buildUIContext({
    project: 'p1', scene: 's1', version: 'v2', frame: 7, selectedElement: 'e1', attachmentFiles: [file, model],
    reviewState: { status: 'review' }, renderPayload: { plan: { id: 'r1' }, state: { status: 'queued' }, extra: 1 },
    orig: document.getElementById('orig') as HTMLImageElement, recon: document.getElementById('recon') as HTMLImageElement,
  })
  const image = (kind: string) => ({ kind, mime: 'image/jpeg', data: 'anBn' })
  expect(JSON.stringify(context)).toBe(JSON.stringify({
    schema: 'keepframe.ui-context/1',
    summary: {
      path: location.pathname, language: 'ko', project: 'p1', scene: 's1', version: 'v2', frame: 7, selected_element: 'e1',
      attachments: [{ index: 1, name: 'logo.svg', type: 'image/svg+xml', size: 3 }, { index: 2, name: 'cup.glb', type: '', size: 4 }],
      workflow: 'review',
      inputs: [{ id: 'q', value: 'hello' }, { id: null, value: 'y'.repeat(1000) }],
      render: { plan: { id: 'r1' }, status: 'queued' },
      visible_text: `Agent ${'y'.repeat(1200)}`,
    },
    images: [image('original'), image('reconstruction'), image('attachment-1')],
  }))
  expect(fetched.map((u) => new URL(u).pathname)).toEqual(['/frame/orig/0', '/frame/recon/0'])
  expect(sizes).toEqual([[768, 384], [768, 384], [768, 384]])

  const empty = await buildUIContext({
    project: null, scene: 's1', version: null, frame: 0, selectedElement: null, attachmentFiles: [],
    reviewState: null, renderPayload: null, orig: document.getElementById('orig') as HTMLImageElement,
    recon: document.getElementById('recon') as HTMLImageElement,
  })
  expect(empty.summary).toMatchObject({ attachments: [], workflow: null, render: null })
})

it('trims a Korean page so the summary fits the server byte cap', async () => {
  document.body.innerHTML = `<p>${'가'.repeat(12000)}</p><input id="q" value="${'나'.repeat(1000)}">
    <img id="orig" src="/frame/orig/0"><img id="recon" src="/frame/recon/0">`
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', { configurable: true, get: () => document.body })
  vi.stubGlobal('fetch', async () => ({ ok: true, blob: async () => new Blob(['img']) }))
  vi.stubGlobal('createImageBitmap', async () => ({ width: 8, height: 8, close: () => {} }))
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => ({ drawImage: () => {} })) as any)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((cb) => cb(new Blob(['jpg'], { type: 'image/jpeg' })))
  const context = await buildUIContext({
    project: 'p1', scene: 's1', version: 'v1', frame: 0, selectedElement: null, attachmentFiles: [], reviewState: null,
    renderPayload: null, orig: document.getElementById('orig') as HTMLImageElement, recon: document.getElementById('recon') as HTMLImageElement,
  })
  expect(new TextEncoder().encode(JSON.stringify(context.summary)).length).toBeLessThanOrEqual(16 * 1024)
  expect(context.summary.visible_text).toMatch(/^가{1000,}$/)
  expect(context.summary.inputs).toEqual([{ id: 'q', value: '나'.repeat(1000) }])
})
