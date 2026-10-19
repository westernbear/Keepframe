import { describe, expect, it } from 'vitest'
import source from './corrections.ts?raw'
import {
  bboxArgs, constraintChecked, keepLabel, presetDiff, editBody, editChoices, editResultView, formatVerifyPair, keepChanges, maskArgs, reassignArgs,
  reference3dIntent, textArgs,
} from './corrections'

describe('corrections', () => {
  it('builds the correction args from the form fields', () => {
    expect(reassignArgs('e1', 'e2', '3, 7', 20)).toEqual({ from_id: 'e1', to_id: 'e2', frames: [3, 7], object_id: 'e2' })
    expect(reassignArgs('e1', 'e2', '', 20).frames).toEqual([0, 19])
    expect(reassignArgs('e1', 'e2', '1,2,3', 20).frames).toEqual([0, 19])
    expect(bboxArgs('1, 2,30,40', '5', 'e1')).toEqual({ frame: 5, bbox: [1, 2, 30, 40], object_id: 'e1' })
    expect(maskArgs('4', 'AAA', 'e1')).toEqual({ frame: 4, mask_png_base64: 'AAA', object_id: 'e1' })
    expect(textArgs('e1', 'Hi')).toEqual({ element_id: 'e1', text: 'Hi' })
  })

  it('pending keep toggles win over the saved value and become the changes', () => {
    const pending = new Map([['a', false]])
    expect(constraintChecked({ pred: 'a', keep: true }, pending)).toBe(false)
    expect(constraintChecked({ pred: 'b', keep: 1 }, pending)).toBe(true)
    expect(constraintChecked({ pred: 'c' }, pending)).toBe(false)
    pending.set('c', true)
    expect(keepChanges(pending)).toEqual([{ pred: 'a', keep: false }, { pred: 'c', keep: true }])
  })

  // Ported from tests/test_web_static.py::test_pending_solid_button_uses_edit_preview_and_confirmation (the edit
  // form half: preview first, confirm submits the same intent; the inspector half is in inspector.test.ts, the
  // click wiring is a phase-4 browser test)
  it('3D generation previews the edit first and only confirmation submits it', () => {
    const ctx = { projectId: 'p1', sceneId: 's1', versionId: 'v1', prompt: ' make 3d ', selectedId: 'e1', attachment: null, choices: {} }
    const preview = editBody({ ...ctx, confirm: false, pendingIntent: reference3dIntent('e1') })
    expect(preview.confirm).toBe(false)
    expect(preview.intent.targets[0]).toEqual({ element: 'e1', property: 'model', value: 'reference' })
    expect(preview.prompt).toBe('make 3d')
    const view = editResultView({ status: 'needs_confirm', summary: 'preview', intent: preview.intent })
    expect(view.needsConfirm).toBe(true)
    const confirm = editBody({ ...ctx, confirm: true, pendingIntent: view.pendingIntent })
    expect(confirm.confirm).toBe(true)
    expect(confirm.intent.targets[0].value).toBe('reference')
  })

  it('shows conflicts, else asks for an element, and confirm only for confirmable statuses', () => {
    const conflicts = [{ id: 'c1', choices: ['release_keep'] }]
    expect(editResultView({ status: 'needs_choice', plan: { conflicts }, intent: { candidates: ['e1'] } }))
      .toEqual({ pendingIntent: { candidates: ['e1'] }, summary: '', conflicts, needElement: false, needsConfirm: true })
    expect(editResultView({ status: 'failed', intent: { candidates: ['e1'] } }).needElement).toBe(true)
    expect(editResultView({ status: 'done' })).toEqual({ pendingIntent: null, summary: '', conflicts: [], needElement: false, needsConfirm: false })
    expect(editChoices([{ name: 'edit-c1', value: 'release_keep' }])).toEqual({ c1: 'release_keep' })
    expect(formatVerifyPair({ verify: { keep_pass_rate: 0.5, temporal: 0.25 } })).toEqual({ k: '0.50', t: '0.25' })
    expect(formatVerifyPair({ verify: {} })).toEqual({ k: '0.00', t: '—' })
    expect(formatVerifyPair({})).toEqual({ k: '—', t: '—' })
  })

  // Ported from tests/test_edit_background_facts.py::test_no_forced_choice_or_code_made_wording_left (edit-form part)
  it('keeps the removed tint/cancel wording out', () => {
    for (const gone of ['cancelled', 'background_kind', 'editChoice.tint', 'toolPending', 'bgTinted', 'editBgChoose']) {
      expect(source).not.toContain(gone)
    }
  })

  it('labels keep predicates for people and counts what a preset changes', () => {
    expect(keepLabel("type(m_t1_1,'translation')")).toEqual({ key: 'review.keepRel.type', vars: { a: 't1 #1', v: 'translation' } })
    expect(keepLabel('dir(m_logo_2,[1.00,0.00])')).toEqual({ key: 'review.keepRel.dir', vars: { a: 'logo #2', v: '[1.00,0.00]' } })
    expect(keepLabel('before(m_t1_1,m_t2_1)')).toEqual({ key: 'review.keepRel.before', vars: { a: 't1 #1', b: 't2 #1' } })
    expect(keepLabel('left(t1,t2)')).toEqual({ key: 'review.keepRel.left', vars: { a: 't1', b: 't2' } })
    expect(keepLabel('odd')).toBeNull()
    expect(keepLabel('zap(a)')).toBeNull()
    const cs = [{ pred: 'type(m_a_1,"x")', keep: false }, { pred: 'left(a,b)', keep: true }, { pred: 'dur(m_a_1,4)', keep: true }]
    expect(presetDiff('content_only', cs, new Map())).toEqual({ on: 1, off: 1 })
    expect(presetDiff('none', cs, new Map([['dur(m_a_1,4)', false]]))).toEqual({ on: 0, off: 1 })
  })
})
