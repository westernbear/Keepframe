import { describe, expect, it, vi } from 'vitest'
import { createFrameTransport, createPreviewCache } from './playback'

// Ported from tests/test_playback_transport.py (node:vm slice of playback.js).
describe('playback', () => {
  it('original-only cache follows version/scene and a stopped transport rejects a late frame', async () => {
    const urls: string[] = []
    class FakeImage {
      complete = true
      naturalWidth = 640
      decoding = ''
      addEventListener() {}
      decode() { return Promise.resolve() }
      set src(url: string) { urls.push(url) }
    }
    vi.stubGlobal('Image', FakeImage)
    let callback: (ts: number) => void = () => {}
    vi.stubGlobal('requestAnimationFrame', (fn: (ts: number) => void) => { callback = fn; return 1 })
    vi.stubGlobal('cancelAnimationFrame', () => {})

    let version = 'v1'
    let sceneId = 's'
    const previews = createPreviewCache({ project: 'p', scene: () => sceneId, version: () => version, kinds: ['orig'] })
    previews.prefetch(0, 8)
    expect(await previews.wait(0)).toBe(true)
    expect(urls.length > 1 && urls.every((u) => u.startsWith('/frame/orig/') && u.includes('v=v1'))).toBe(true)
    version = 'v2'
    await previews.wait(0)
    expect(urls.at(-1)).toContain('v=v2')
    sceneId = 's2'
    await previews.wait(0)
    expect(urls.at(-1)).toContain('scene=s2')

    let frame = 0
    let finish: (ok: boolean) => void = () => {}
    const transport = createFrameTransport({
      getFrame: () => frame, setFrameIndex: (f: number) => { frame = f }, getFps: () => 30, getFrameCount: () => 10,
      prefetch: () => {}, isReady: () => false, wait: () => new Promise<boolean>((r) => { finish = r }), showFrame: () => {}, canPlay: () => true,
    })
    transport.setPlaying(true); callback(1); callback(50)
    transport.setPlaying(false); frame = 5; transport.setPlaying(true)
    finish(true); await Promise.resolve()
    expect(frame).toBe(5) // stale wait must not reset the frame after a seek/restart
    transport.setPlaying(false)
    vi.unstubAllGlobals()
  })
})
