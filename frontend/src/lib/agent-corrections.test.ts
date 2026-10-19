import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/lib/api'
import { pendingAction, toolCallLogArgs } from './agent-chat'
import { confirmCorrection, confirmGuard, confirmKeepChange, keepChangePreview, prepareCorrection } from './agent-corrections'

// The legacy node:vm tests used these fakes, so previews are asserted against keys and JSON vars.
vi.mock('@/i18n', () => ({
  T: (key: string) => key,
  Tf: (key: string, vars: unknown) => key + JSON.stringify(vars),
  errorText: (code: string) => code || '',
}))
vi.mock('@/lib/api', () => ({ postKeep: vi.fn(), postCorrect: vi.fn(), fetchReviewJob: vi.fn() }))

const postKeep = vi.mocked(api.postKeep)
const postCorrect = vi.mocked(api.postCorrect)
const fetchReviewJob = vi.mocked(api.fetchReviewJob)

beforeEach(() => vi.useFakeTimers({ toFake: ['setTimeout'] }))
afterEach(() => {
  vi.useRealTimers()
  vi.resetAllMocks()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('agent pending mutations', () => {
  // Ported from tests/test_web_static.py::test_agent_pending_mutations_execute_only_on_click
  it.each([
    'keep_preset', 'keep_targets', 'keep_on', 'keep_error', 'keep_mismatch',
    'keep_targets_string', 'keep_targets_missing', 'keep_targets_null',
    'correct_done', 'correct_submit_error', 'correct_job_error', 'correct_poll_error',
  ])('execute only on click: %s', async (scenario) => {
    // The page's half of paintPending (bubble, error banner, guarded confirm button) is simulated below; the DOM is
    // covered by the phase-4 browser tests and React escapes text.
    const ui = { banners: [] as { message: string; failure: boolean }[], summaries: [] as string[], refreshed: [] as string[] }
    const requests: { kind: string; args: any[] }[] = []
    const polls: unknown[] = []
    const constraints = [
      { pred: "type(e1,'translation')", keep: true },
      { pred: 'left(e1,e2)', keep: true },
      { pred: 'right(e10,e2)', keep: false },
      { pred: "type(e2,'translation')", keep: false },
    ]
    const snapshots: any[] = [
      { status: 'running', op: 'text' }, { status: 'running', op: 'text' },
      scenario === 'correct_job_error' ? { status: 'error', error: 'correction failed' } : { status: 'done', version: 'v2' },
    ]
    const ctx = {
      projectId: 'p1', sceneId: 's1',
      setBanner: (message: string, failure = false) => { ui.banners.push({ message, failure }) },
      appendAgent: (message: string) => { ui.summaries.push(message) },
      refreshAfterEdit: async (version: string) => {
        expect(requests.length, 'refresh follows exactly one confirmed request').toBe(1)
        if (scenario.startsWith('correct')) expect(snapshots.length, 'refresh waits for done').toBe(0)
        ui.refreshed.push(version)
      },
    }
    postKeep.mockImplementation(async (...args) => {
      requests.push({ kind: 'keep', args })
      if (scenario === 'keep_error') throw new Error('keep save failed')
      return { version: { id: 'v2' } }
    })
    postCorrect.mockImplementation(async (...args) => {
      requests.push({ kind: 'correct', args })
      if (scenario === 'correct_submit_error') throw new Error('correction submit failed')
      return { job: { status: 'running', op: 'text' } }
    })
    fetchReviewJob.mockImplementation(async (...args) => {
      polls.push(args)
      if (scenario === 'correct_poll_error') throw new Error('poll failed')
      expect(snapshots.length, 'polling stops at a terminal status').toBeGreaterThan(0)
      return snapshots.shift()
    })
    const scene = { constraints, elements: [{ id: 'e1', canonical: { text: 'Before' } }] }
    let versionId = 'v1'
    const isCorrect = scenario.startsWith('correct')
    const payload: any = isCorrect
      ? { correction: { op: 'text', args: { element_id: 'e1', text: 'Hello' } } }
      : { keep_change: scenario === 'keep_preset'
          ? { preset: 'all' }
          : { targets: ['e1', 'left(e1,e2)', 'e1'], on: scenario === 'keep_on',
              matched: scenario === 'keep_mismatch' ? 4 : 3,
              examples: constraints.slice(0, 3).map((c) => c.pred) } }
    const malformed = ['keep_targets_string', 'keep_targets_missing', 'keep_targets_null'].includes(scenario)
    if (scenario === 'keep_targets_string') payload.keep_change.targets = 'e1'
    if (scenario === 'keep_targets_missing') delete payload.keep_change.targets
    if (scenario === 'keep_targets_null') payload.keep_change.targets = null

    // paint
    const action = pendingAction({ results: [{ ok: true, payload }], needs_confirm: true, needs_choice: false })
    let guard: ReturnType<typeof confirmGuard> | null = null
    if (action.kind === 'keep') {
      const preview = keepChangePreview(action.keepChange, scene.constraints)
      if (preview.text) ctx.appendAgent(preview.text)
      if (!preview.ok) ctx.setBanner('agent.keepPreviewChanged', true)
      else guard = confirmGuard(() => confirmKeepChange(ctx, action.keepChange, preview.changes))
    } else if (action.kind === 'correction') {
      const { op, shown, lines } = await prepareCorrection(action.correction, scene)
      const previewVersion = versionId
      ctx.appendAgent(lines.join('\n'))
      guard = confirmGuard(() => confirmCorrection(ctx, op, shown, previewVersion, 's1'))
    }
    expect(requests.length, 'painting a preview must never execute it').toBe(0)
    expect(polls.length, 'painting must never poll correction jobs').toBe(0)
    if (!malformed) expect(ui.summaries.length, 'show a translated preview').toBeGreaterThan(0)
    if (!isCorrect && scenario !== 'keep_preset' && !malformed) {
      expect(ui.summaries[0], 'show the matched count').toContain('"n":' + payload.keep_change.matched)
      for (const example of payload.keep_change.examples) expect(ui.summaries[0]).toContain(example)
    }
    if (scenario === 'keep_mismatch' || malformed) {
      expect(guard, 'a mismatched or malformed preview cannot be confirmed').toBeNull()
      expect(ui.banners.at(-1)).toEqual({ message: 'agent.keepPreviewChanged', failure: true })
      return
    }
    expect(guard).not.toBeNull()
    // Snapshot matching predicates and the displayed version before confirmation.
    constraints.push({ pred: 'bottom(e1,e3)', keep: true })
    versionId = 'v99'
    const clicks = Promise.all([guard!.click(), guard!.click()])
    await vi.runAllTimersAsync()
    await clicks
    expect(requests.length, 'double click must not submit twice').toBe(1)
    const args = requests[0].args
    expect(args.slice(0, 2)).toEqual(['p1', 's1'])
    if (isCorrect) {
      expect(requests[0].kind).toBe('correct')
      expect(args[2]).toBe('text')
      expect(args[3].element_id).toBe('e1')
      expect(args[3].text).toBe('Hello')
      expect(args[3].version, 'use the displayed preview version').toBe('v1')
    } else {
      expect(args[3], 'agent-confirmed keep changes need their own version note').toBe('agent.keepChangeNote')
      if (scenario === 'keep_preset') {
        expect(args[4]).toBe('all')
        expect(args[2]).toHaveLength(0)
      } else {
        const keep = scenario === 'keep_on'
        expect(args[2], 'submit exact predicates using equality or substring matching').toEqual([
          { pred: "type(e1,'translation')", keep },
          { pred: 'left(e1,e2)', keep },
          { pred: 'right(e10,e2)', keep },
        ])
      }
    }
    if (scenario.endsWith('error')) {
      expect(ui.refreshed).toHaveLength(0)
      expect(ui.banners.at(-1)!.failure, 'show request, job and polling errors in the banner').toBe(true)
      expect(guard!.disabled, 'allow retry after failure').toBe(false)
    } else {
      expect(ui.refreshed).toEqual(['v2'])
      expect(ui.banners.at(-1)!.failure).toBe(false)
      expect(guard!.disabled, 'do not repeat an applied change').toBe(true)
      await guard!.click()
      expect(requests).toHaveLength(1)
    }
  })
})

describe('agent correction preview', () => {
  // Ported from tests/test_web_static.py::test_agent_correction_preview_matches_confirmed_fields
  // (also covers test_agent_correction_contract_picks_fields_and_renders_only_text: extra_* and model_version
  // must block; "renders only text" is React's default escaping, checked by the phase-4 browser tests)
  it.each([
    'text', 'font', 'font_partial', 'reassign', 'bbox', 'mask',
    'extra_text', 'extra_font', 'extra_reassign', 'extra_bbox', 'extra_mask', 'model_version',
    'missing_text', 'missing_reassign', 'missing_bbox', 'missing_mask', 'unknown_id',
    'invalid_op', 'invalid_font', 'invalid_bbox', 'invalid_mask', 'empty_mask',
    'invalid_mask_color', 'invalid_mask_alpha', 'invalid_mask_size',
    'invalid_args_string', 'invalid_args_null', 'invalid_args_array',
    'missing_element', 'missing_mask_frame', 'missing_bbox_frame', 'missing_reassign_frames',
  ])('preview matches confirmed fields: %s', async (scenario) => {
    const requests: unknown[][] = []
    const imageSources: string[] = []
    const banners: { message: string; failure: boolean }[] = []
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => ({
      drawImage: () => {},
      getImageData: () => {
        const data = new Uint8ClampedArray(4 * 3 * 4)
        for (let i = 3; i < data.length; i += 4) data[i] = 255
        data[0] = data[1] = data[2] = 127
        if (scenario !== 'empty_mask') for (const index of [5, 6, 9]) {
          data[index * 4] = data[index * 4 + 1] = data[index * 4 + 2] = index === 5 ? 128 : 255
        }
        if (scenario === 'invalid_mask_color') data[0] = 255
        if (scenario === 'invalid_mask_alpha') data[3] = 0
        return { data }
      },
    })) as any)
    vi.stubGlobal('Image', class {
      naturalWidth = scenario === 'invalid_mask_size' ? 5 : 4
      naturalHeight = 3
      onload: () => void = () => {}
      onerror: () => void = () => {}
      set src(value: string) {
        imageSources.push(value)
        queueMicrotask(() => (scenario === 'invalid_mask' ? this.onerror() : this.onload()))
      }
    })
    postCorrect.mockImplementation(async (...args) => { requests.push(args); return { job: { status: 'running' } } })
    fetchReviewJob.mockResolvedValue({ status: 'done', version: 'v2' })

    const old = '<img src=x onerror=alert(1)>OLD'
    const changed = '<svg onload=alert(2)>NEW'
    const font = { family_guess: 'Noto Sans', weight: 700, size_px: 48 }
    const scene = { frames: 12, size: [4, 3], elements: [
      { id: 'e1', label: old, canonical: { text: old, font: { family_guess: 'Arial', weight: 300, size_px: 20 } } },
      { id: 'e2', label: 'Target label', canonical: {} },
    ] }
    const mask = 'iVBORw0KGgoAAAANSUhEUgAAAAQAAAAD'
    let op = scenario.includes('reassign') ? 'reassign'
      : scenario.includes('bbox') ? 'bbox' : scenario.includes('mask') ? 'mask' : 'text'
    let args: any = op === 'reassign' ? { from_id: 'e1', to_id: 'e2', frames: [2, 4] }
      : op === 'bbox' ? { object_id: 'e1', frame: 3, bbox: [1, 1, 3, 3] }
      : op === 'mask' ? { object_id: 'e1', frame: 3, mask_png_base64: mask }
      : { element_id: 'e1', text: changed }
    if (scenario === 'font') args.font = { ...font }
    if (scenario === 'font_partial') { delete args.text; args.font = { weight: 700 } }
    if (scenario.startsWith('extra_')) args.note = 'hidden instruction'
    if (scenario === 'extra_font') { delete args.note; args.font = { ...font, candidates: ['hidden'] } }
    if (scenario === 'model_version') args.version = 'v999'
    if (scenario === 'missing_text') delete args.text
    if (scenario === 'missing_reassign') delete args.to_id
    if (scenario === 'missing_bbox') delete args.bbox
    if (scenario === 'missing_mask') delete args.mask_png_base64
    if (scenario === 'missing_element') delete args.element_id
    if (scenario === 'missing_mask_frame' || scenario === 'missing_bbox_frame') delete args.frame
    if (scenario === 'missing_reassign_frames') delete args.frames
    if (scenario === 'unknown_id') args.element_id = 'absent'
    if (scenario === 'invalid_op') op = 'toString'
    if (scenario === 'invalid_font') args.font = null
    if (scenario === 'invalid_bbox') args.bbox = [1, 2, '<bad>', 4]
    if (scenario === 'invalid_args_string') args = 'e1'
    if (scenario === 'invalid_args_null') args = null
    if (scenario === 'invalid_args_array') args = []
    const invalid = scenario.startsWith('extra_') || scenario.startsWith('missing_')
      || scenario.startsWith('invalid_') || ['model_version', 'unknown_id', 'empty_mask'].includes(scenario)
    const expected = JSON.parse(JSON.stringify(args))
    if (op === 'mask') expect(JSON.stringify(toolCallLogArgs('correct', { op, args })), 'never print mask base64 in tool logs').not.toContain(mask)

    const action = pendingAction({ results: [{ ok: true, payload: { correction: { op, args } } }], needs_confirm: true })
    expect(action.kind).toBe('correction')
    const correction = action.kind === 'correction' ? action.correction : null
    let prepared: Awaited<ReturnType<typeof prepareCorrection>> | null = null
    try {
      prepared = await prepareCorrection(correction, scene)
    } catch {
      banners.push({ message: 'agent.correctionInvalid', failure: true }) // paintCorrection's catch
    }
    expect(requests.length, 'rendering a preview cannot submit').toBe(0)
    if (invalid) {
      expect(prepared, 'invalid or undisclosed fields must block confirmation').toBeNull()
      expect(banners.at(-1)).toEqual({ message: 'agent.correctionInvalid', failure: true })
      return
    }
    expect(prepared).not.toBeNull()
    const preview = prepared!.lines.join('\n')
    expect(preview.includes('e1') && preview.includes(old), 'show ID and current scene content as literal text').toBe(true)
    if (op === 'text') {
      if (args.text !== undefined) expect(preview.includes(changed) && preview.includes('agent.correctionText')).toBe(true)
      if (args.font) {
        for (const value of ['Arial', '300', '20', '700']) expect(preview).toContain(value)
        if (scenario === 'font_partial') for (const value of ['sans-serif', '32']) expect(preview).toContain(value)
      }
    } else if (op === 'reassign') {
      for (const value of ['e2', 'Target label', 'agent.correctionFrames', '2', '4']) expect(preview).toContain(value)
    } else {
      expect(preview.includes('agent.correctionFrame') && preview.includes('3')).toBe(true)
      expect(preview).toContain(op === 'mask' ? 'agent.correctionMask' : 'agent.correctionBbox')
      if (op === 'mask') {
        expect(preview, 'mask bbox is computed from decoded pixels').toContain('[1, 1, 3, 3]')
        expect(preview, 'mask count follows the server threshold >127').toContain('"pixels":3')
        expect(preview).not.toContain(mask)
        expect(imageSources).toHaveLength(1)
      }
    }
    // Changing the model payload after display cannot change the confirmed snapshot.
    const previewVersion = 'v1'
    args.text = 'unseen'
    if (args.font) args.font.weight = 100
    if (args.frames) args.frames[0] = 9
    if (args.bbox) args.bbox[0] = 9
    args.note = 'unseen'
    const ctx = { projectId: 'p1', sceneId: 's1', setBanner: () => {}, appendAgent: () => {}, refreshAfterEdit: async () => {} }
    await confirmGuard(() => confirmCorrection(ctx, prepared!.op, prepared!.shown, previewVersion, 's1')).click()
    expect(requests).toHaveLength(1)
    expect(JSON.parse(JSON.stringify(requests[0]))).toEqual(['p1', 's1', op, { ...expected, version: 'v1' }])
  })

  it('refuses a correction confirmed after the scene changed', async () => {
    const banners: string[] = []
    const ctx = { projectId: 'p1', sceneId: 's2', setBanner: (m: string) => { banners.push(m) }, appendAgent: () => {}, refreshAfterEdit: async () => {} }
    expect(await confirmCorrection(ctx, 'text', { element_id: 'e1', text: 'x' }, 'v1', 's1')).toBe(false)
    expect(postCorrect).not.toHaveBeenCalled()
    expect(banners.at(-1)).toBe('agent.correctionInvalid')
  })
})
