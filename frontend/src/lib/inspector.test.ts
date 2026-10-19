import { describe, expect, it } from 'vitest'
import { T, Tf } from '@/i18n'
import ko from '@/i18n/ko.json'
import en from '@/i18n/en.json'
import {
  agentUrl, approveLabelKey, clampProgress, elementLabel, fontCorrection, fontPicker, listProgress, matchesElementFilter,
  nextSelection, nextUnapprovedScene, objectDetail, reviewUrl, sceneMeta, selectionForm, versionLabel,
} from './inspector'

const versions = [{ id: 'v1' }]

describe('inspector', () => {
  // Ported from tests/test_ir_spin.py::test_inspector_pending_3d_badge_is_bilingual_and_only_for_candidates
  it('the pending 3D badge is bilingual and only for candidates', () => {
    expect(ko['review.pending3d']).toBe('3D 후보')
    expect(en['review.pending3d']).toBe('3D candidate')
    const item = { id: 'candidate', kind: 'sprite', pending_asset: '3d', canonical: {}, visible: [0, 10] }
    expect(objectDetail(item, undefined, undefined, undefined, 'v1', versions).pending3d).toEqual({ disabled: false, title: '' })
    expect(objectDetail({ ...item, pending_asset: null }, undefined, undefined, undefined, 'v1', versions).pending3d).toBeNull()
  })

  // Ported from tests/test_web_static.py::test_pending_solid_button_uses_edit_preview_and_confirmation (the
  // inspector half; the edit-form half is in corrections.test.ts)
  it('the 3D generation button is read-only on a historical version and gone without a candidate', () => {
    const item = { id: 'e1', kind: 'sprite', pending_asset: '3d', canonical: {}, visible: [0, 11] }
    expect(objectDetail(item, undefined, undefined, undefined, 'v1', versions).pending3d!.disabled).toBe(false)
    expect(objectDetail(item, undefined, undefined, undefined, 'v0', versions).pending3d)
      .toEqual({ disabled: true, title: T('review.latestOnly') })
    expect(objectDetail({ ...item, pending_asset: null }, undefined, undefined, undefined, 'v0', versions).pending3d).toBeNull()
  })

  // Ported from tests/test_web_static.py::test_review_font_picker_preserves_font_and_corrects_selected_text
  // (the label/select/button DOM and the explicit-apply click are covered by the phase-4 browser tests)
  it('font picker preserves the font and corrects the selected text', () => {
    const font = { family_guess: 'DejaVu Serif', weight: 700, size_px: 40, candidates: ['DejaVu Sans', 'DejaVu Serif', 'Liberation Sans'] }
    const item: any = { id: 'e1', kind: 'text', canonical: { text: 'Launch faster', font }, visible: [0, 10] }
    const picker = fontPicker(item)!
    expect(picker, 'selected text needs a font picker').toBeTruthy()
    expect(picker.value).toBe('DejaVu Serif')
    expect(picker.families).toEqual(font.candidates)
    expect(ko['review.fontCandidates']).toBe('폰트 후보')
    expect(ko['review.applyFont']).toBe('폰트 적용')
    const args = fontCorrection(item.id, picker.font, 'Liberation Sans')
    expect(args.element_id).toBe('e1')
    expect(args.font).toEqual({ ...font, family_guess: 'Liberation Sans' })
    expect(font.family_guess, 'must not mutate loaded scene').toBe('DejaVu Serif')
    item.canonical.font = { ...font, family_guess: 'Inter' }
    expect(fontPicker(item)!.value, 'show the current family even after manual font edits').toBe('Inter')
    expect(fontPicker(item)!.families).toEqual(['Inter', ...font.candidates])
    item.canonical.font = { ...font, candidates: [] }
    expect(fontPicker(item)).toBeNull()
    item.kind = 'sprite'; item.canonical.font = font
    expect(fontPicker(item)).toBeNull()
    item.kind = 'text'; delete item.canonical.font
    expect(fontPicker(item)).toBeNull()
  })

  it('object detail rows', () => {
    const item = { id: 'e1', kind: 'text', canonical: { text: 'Hi' }, visible: [0, 5] }
    const detected = { ocr: [{ text: 'Hi', confidence: 0.912 }, { text: 'there' }], regions: [{ bbox: [10, 20, 50, 40] }, { bbox: [5, 25, 30, 60] }] }
    const detail = objectDetail(item, detected, { intervals: [[0, 2], [4, 5]] }, {}, 'v1', versions)
    expect(detail.title).toBe(`e1 · ${T('review.objectDetails')}`)
    expect(detail.lines).toEqual([
      ['review.objectType', T('review.kind.text')], ['review.recognizedText', 'Hi / there'], ['review.savedText', 'Hi'],
      ['review.ocrConfidence', '91.2%'], ['review.position', '5, 20 px'], ['review.size', '45 × 40 px'],
      ['review.appearance', '0–2 f, 4–5 f'],
    ])
    const bare = objectDetail({ id: 'e2', kind: 'sprite', canonical: {}, visible: [1, 3] }, undefined, undefined, undefined, 'v1', versions)
    expect(bare.lines).toEqual([
      ['review.objectType', T('review.kind.sprite')], ['review.recognizedText', '—'],
      ['review.position', T('review.noAnalysis')], ['review.appearance', '1–3 f'],
    ])
    const analysed = objectDetail({ id: 'e2', kind: 'sprite', canonical: {}, visible: [1, 3] }, undefined, undefined, {}, 'v1', versions)
    expect(analysed.lines.slice(2)).toEqual([['review.position', T('review.notDetected')], ['review.appearance', '—']])
  })

  it('scene meta uses i18n for the transition and link count', () => {
    const scenes = [{ id: 's1', frames: [0, 9], transition_out: { transition: 'cut' } }, { id: 's2', frames: [10, 19] }]
    const links = [{ from: { scene: 's1' }, to: { scene: 's2' } }, { from: { scene: 's3' } }]
    expect(sceneMeta(scenes, links, 's1')).toBe(Tf('review.sceneMeta', { range: '0–9', transition: 'cut', links: 1 }))
    expect(sceneMeta(scenes, undefined, 's2')).toBe(Tf('review.sceneMeta', { range: '10–19', transition: T('review.transitionNone'), links: 0 }))
    expect(sceneMeta(scenes, links, 's9')).toBe('')
    // the Korean wording is unchanged from the legacy hard-coded string
    expect(sceneMeta(scenes, links, 's1')).toBe('0–9 · 전환 cut · 링크 1')
    expect(sceneMeta(scenes, [], 's2')).toBe('10–19 · 전환 없음 · 링크 0')
  })

  // Ported from tests/test_web_review.py::test_review_page_has_progress_bar and
  // ::test_review_page_has_approve_button (the decisions behind setProgress and paintApprove)
  it('progress clamps to 0–100 and approve turns into "open agent" once approved', () => {
    expect(clampProgress(-4)).toBe(0)
    expect(clampProgress(45.6)).toBe(46)
    expect(clampProgress(140)).toBe(100)
    expect(listProgress(16, 32)).toBe(60)
    expect(listProgress(0, 0)).toBe(45)
    expect(approveLabelKey('approved')).toBe('review.openAgent')
    expect(approveLabelKey('review')).toBe('review.approve')
    expect(nextUnapprovedScene([{ id: 's1' }, { id: 's2' }], { s1: 'v1' })).toEqual({ id: 's2' })
    expect(nextUnapprovedScene([{ id: 's1' }], { s1: 'v1' })).toBeUndefined()
    expect(agentUrl('p 1', 's1', 'v2')).toBe('/agent?project=p%201&scene=s1&v=v2')
    expect(agentUrl('p', 's1', null)).toBe('/agent?project=p&scene=s1')
    expect(reviewUrl('p', 's1', 'v2')).toBe('/review?project=p&scene=s1&v=v2')
  })

  it('selection, filter and form state', () => {
    const els = [{ id: 'e1', kind: 'sprite', canonical: {} }, { id: 'e2', kind: 'text', canonical: { text: 'Go' } }]
    expect(nextSelection('e2', els)).toBe('e2')
    expect(nextSelection('gone', els)).toBe('e1')
    expect(nextSelection(null, [])).toBeNull()
    expect(elementLabel(els[1])).toBe(`e2 · ${T('review.kind.text')} · Go`)
    expect(elementLabel(els[0])).toBe(`e1 · ${T('review.kind.sprite')}`)
    expect(matchesElementFilter(' E2 ', 'e2', '')).toBe(true)
    expect(matchesElementFilter('go', 'e2', 'e2 · text · Go')).toBe(true)
    expect(matchesElementFilter('zz', 'e2', 'e2 · text · Go')).toBe(false)
    expect(matchesElementFilter('', 'e2', '')).toBe(true)
    expect(selectionForm(els[1], 'v1', versions)).toEqual({ text: 'Go', textRunDisabled: false, maskRunDisabled: true, regionModeDisabled: false })
    expect(selectionForm(els[0], 'v0', versions)).toEqual({ text: '', textRunDisabled: true, maskRunDisabled: false, regionModeDisabled: true })
    expect(selectionForm(undefined, 'v1', versions).regionModeDisabled).toBe(true)
    expect(versionLabel({ id: 'v2' })).toBe('v2 · ')
  })
})
