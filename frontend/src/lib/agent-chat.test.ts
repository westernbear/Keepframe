import { afterEach, describe, expect, it } from 'vitest'
import ko from '@/i18n/ko.json'
import en from '@/i18n/en.json'
import { T } from '@/i18n'
import {
  chatReply, confirmEditBody, confirmedEditOutcome, confirmedLine, defaultEditChoices, pendingAction, readChatAttachment,
  toolCallAddsInformation, toolCallLogArgs, toolPrompt, toolStatus, turnView, verifyChip, verifyChipText,
} from './agent-chat'

afterEach(() => localStorage.clear())

describe('agent chat', () => {
  // Ported from tests/test_chat_attachments.py::test_chat_file_reader_normalizes_unknown_svg_and_glb_mime
  it('normalizes the MIME of .glb and .svg attachments and keeps other types', async () => {
    for (const [name, type, mime] of [['MODEL.GLB', '', 'model/gltf-binary'],
      ['model.glb', 'application/octet-stream', 'model/gltf-binary'],
      ['VECTOR.SVG', '', 'image/svg+xml'], ['photo.png', 'image/png', 'image/png']]) {
      const file = new File(['foo'], name, { type })
      expect(await readChatAttachment(file)).toBe(`data:${mime};base64,Zm9v`)
    }
  })

  // Ported from tests/test_edit_background_facts.py::test_background_attachment_summary_is_never_a_chat_bubble
  // (the bubble itself is a React text node: covered by the phase-4 browser tests)
  it('never shows the background-attachment summary as a chat reply', () => {
    const summary = '배경 이미지를 첨부로 바꿉니다. 트랙은 유지합니다.'
    const picture = { targets: [{ property: 'background', value: 'attachment' }] }
    const colour = { targets: [{ property: 'background', value: '#1a2a6c' }] }
    expect(confirmedLine({ status: 'done', summary, intent: picture })).toBe(T('agent.applied'))
    expect(confirmedLine({ status: 'done', summary: 'e1 색을 #ff0000로 바꿉니다.', intent: colour })).toBe('e1 색을 #ff0000로 바꿉니다.')
    expect(confirmedLine({ status: 'done', intent: colour })).toBe('적용 완료.') // legacy EDIT_APPLIED
    const pending = { status: 'pending', reply: summary, results: [{ ok: true, message: summary, payload: { intent: picture } }] }
    expect(chatReply(pending)).toBe('')
    const own = { status: 'pending', reply: 'Shall I use your picture?', results: [{ ok: true, message: summary, payload: { intent: picture } }] }
    expect(chatReply(own)).toBe('Shall I use your picture?')
    const other = { status: 'pending', reply: 'x', results: [{ ok: true, message: 'x', payload: { intent: colour } }] }
    expect(chatReply(other)).toBe('x')
    expect(chatReply({ reply: 'done', results: [] })).toBe('done')
    // legacy grep: paintTurn uses chatReply and the confirmed edit uses confirmedLine
    expect(turnView(pending, false).reply).toBe('')
    expect(confirmedEditOutcome({ status: 'done', version: { id: 'v2' }, summary, intent: picture }, null))
      .toMatchObject({ kind: 'done', line: T('agent.applied') })
  })

  // Ported from tests/test_final_fixes.py::test_verify_chip_uses_counts_and_legacy_fallback
  it('verify chip uses keep counts and falls back to keep_results', () => {
    localStorage.setItem('keepframe.lang', 'en')
    const chip = (verify) => { const c = verifyChip(verify)!; return { ...c, ...verifyChipText(c) } }
    let c = chip({ keep_total: 1000, keep_failed: 0, keep_pass_rate: 1, keep_results: [] })
    expect(c.text).toBe('Checks passed · keep 100%')
    expect(c.title).toBe('Checks passed · keep 100%. 1000 keep rules. Maximum error 0.00px') // also the aria-label
    expect(c.tone).toBe('pass')
    c = chip({ keep_total: 1000, keep_failed: 75, keep_pass_rate: 1, keep_results: [] })
    expect(c.text).toBe('Checks failed · keep 93%')
    expect(c.title).toMatch(/1000 keep rules/)
    expect(c.tone).toBe('fail')
    c = chip({ keep_total: 0, keep_failed: 0, keep_results: [] })
    expect(c.text).toBe('No keep rules to check')
    expect(c.tone).toBe('warn')
    c = chip({ keep_pass_rate: 1, keep_results: [{ passed: true }, { passed: true }] })
    expect(c.text).toBe('Checks passed · keep 100%')
    expect(c.title).toMatch(/2 keep rules/)
    c = chip({ keep_pass_rate: 0.5, keep_results: [{ passed: true }, { passed: false }] })
    expect(c.text).toBe('Checks failed · keep 50%')
    expect(c.title).toMatch(/2 keep rules/)
    expect(verifyChip(undefined)).toBeNull()
  })

  // Ported from tests/test_web_static.py::test_agent_confirm_needs_choice_keeps_intent_and_can_resubmit
  it('confirm needs_choice keeps the intent and resubmits with the chosen options', () => {
    for (const conflict of ['keep_violation', 'timing_overflow', 'font_missing']) {
      for (const intentMode of ['missing', 'null', 'updated']) {
        const initialIntent = { targets: [{ element: 'e1', property: 'color', value: '#ffffff' }] }
        const updatedIntent = { targets: [{ element: 'e1', property: 'color', value: '#222222' }] }
        const plan = { conflicts: [{ id: conflict, choices: ['release_keep'] }] }
        const response: Record<string, unknown> = { status: 'needs_choice', plan }
        if (intentMode !== 'missing') response.intent = intentMode === 'updated' ? updatedIntent : null
        const expectedIntent = intentMode === 'updated' ? updatedIntent : initialIntent
        const fields = { project: 'p1', scene: 's1', v: 'v1', prompt: 'change color', attachments: ['first', 'second'], element: 'e1' }
        const first = confirmEditBody({ ...fields, intent: initialIntent })
        expect(first.confirm).toBe(true)
        expect(Object.keys(first.choices)).toHaveLength(0)
        const outcome = confirmedEditOutcome(response, initialIntent)
        expect(outcome.kind).toBe('choice')
        expect(outcome.kind === 'choice' && outcome.plan).toBe(plan)
        expect(outcome.intent).toBe(expectedIntent)
        const second = confirmEditBody({ ...fields, intent: outcome.intent }, defaultEditChoices(plan))
        expect(second.intent).toBe(expectedIntent)
        expect(second.choices[conflict]).toBe('release_keep')
        expect(second.prompt).toBe('change color')
        expect(second.attachments).toEqual(['first', 'second']) // legacy test_agent_sends_attachment_on_confirm
        const done = confirmedEditOutcome({ status: 'done', version: { id: 'v2' } }, outcome.intent)
        expect(done).toMatchObject({ kind: 'done', intent: null, version: 'v2' })
      }
    }
  })

  // Ported from tests/test_chat_attachments.py::test_chat_accepts_svg_and_glb_and_localizes_attachment_errors (JS part)
  it('localizes confirmed-edit errors', () => {
    const message = (error) => (confirmedEditOutcome({ status: 'failed', error }, {}) as { message: string }).message
    expect(message('attachment_required')).toBe(T('agent.attachmentRequired'))
    expect(message('invalid_glb')).toBe(T('agent.invalidGlb'))
    expect(message('unsafe_svg')).toBe(T('agent.invalidSvg'))
    expect(message('invalid_svg')).toBe(T('agent.invalidSvg'))
    expect(message('other_code')).toBe('other_code')
    expect(message(undefined)).toBe(T('agent.failed'))
    expect(confirmedEditOutcome({ status: 'done' }, {}).kind).toBe('error') // done without a version is a failure
  })

  it('picks the pending confirmation in legacy order and builds the turn view', () => {
    const results = [{ ok: true, payload: { intent: { a: 1 }, correction: { op: 'text' } } }, { ok: true, payload: { keep_change: { preset: 'all' } } }]
    expect(pendingAction({ results }).kind).toBe('keep')
    expect(pendingAction({ results: [...results, { payload: { render_plan: { id: 'r' } } }] }).kind).toBe('render')
    expect(pendingAction({ results: results.slice(0, 1) }).kind).toBe('correction')
    expect(pendingAction({ results: [{ payload: { intent: { a: 1 } } }], needs_choice: 1 }))
      .toEqual({ kind: 'edit', intent: { a: 1 }, plan: null, needsChoice: true, needsConfirm: false })
    const view = turnView({ status: 'pending', reply: 'r', tool_calls: [{ name: 'edit', arguments: {} }, { name: 'verify' }],
      results: [{ ok: true, payload: { verify: { keep_total: 0 } } }] })
    expect(view.toolCalls[1].result).toEqual({ ok: false, message: '' })
    expect(view.verify?.tone).toBe('warn')
    expect(view.pending?.kind).toBe('edit')
    expect(turnView({ status: 'pending', results: [] }, false).pending).toBeNull()
    expect(turnView({ status: 'error', results: [] }).error).toBe(T('agent.failed'))
  })

  it('tool cards hide mask base64, translate status and skip messages the reply already says', () => {
    const mask = 'iVBORw0KGgoAAAA'
    const logged = JSON.stringify(toolCallLogArgs('correct', { op: 'mask', args: { object_id: 'e1', mask_png_base64: mask } }))
    expect(logged).not.toContain(mask)
    expect(logged).toContain(T('agent.correctionMaskHidden'))
    expect(toolCallLogArgs('edit', { a: 1 })).toEqual({ a: 1 })
    expect(toolStatus('edit', true)).toBe(T('agent.toolApplied'))
    expect(toolStatus('set_keep', false)).toBe(`${T('agent.tool.keep')} 실패`)
    expect(toolCallAddsInformation(' done ', 'All done here')).toBe(false)
    expect(toolCallAddsInformation('new fact', 'reply')).toBe(true)
    expect(toolCallAddsInformation('', 'reply')).toBe(false)
    expect(toolPrompt('set_keep')).toBe('유지할 조건을 지정해줘')
    expect(toolPrompt('nope')).toBe('')
  })

  // Ported from tests/test_web_static.py::test_agent_keep_and_correction_copy_is_bilingual (+ the keys added in the port)
  it('agent copy is bilingual', () => {
    for (const key of ['agent.keepPresetPreview', 'agent.keepOnPreview', 'agent.keepOffPreview',
      'agent.keepExamplesPreview', 'agent.keepChangeNote', 'agent.keepPreviewChanged',
      'agent.correctionPreview', 'agent.correctionInvalid', 'agent.correctionElement',
      'agent.correctionText', 'agent.correctionFont', 'agent.correctionFontFamily',
      'agent.correctionFontWeight', 'agent.correctionFontSize', 'agent.correctionFrames',
      'agent.correctionFrame', 'agent.correctionBbox', 'agent.correctionMask',
      'agent.correctionMaskHidden', 'agent.applied', 'agent.modelLabel', 'agent.readingScreen',
      ...['analyze', 'correct', 'set_keep', 'edit', 'render', 'verify', 'export', 'report'].map((t) => `agent.toolPrompt.${t}`)]) {
      expect(ko[key], key).toBeTruthy()
      expect(en[key], key).toBeTruthy()
    }
    expect(ko['agent.keepChangeNote']).toBe('에이전트 keep 변경')
    expect(en['agent.keepChangeNote']).toBe('Agent keep change')
  })
})
