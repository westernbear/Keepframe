import { afterEach, expect, it } from 'vitest'
import { confidenceLabel, fontFileError, fontMeta, fontUploadErrorKey, sceneOptionLabel, storedOpen } from './agent-view'

afterEach(() => localStorage.clear())

it('formats scene options, confidence and font metadata', () => {
  expect(sceneOptionLabel({ id: 's2', frames: [10, 40] })).toBe('s2 · 10–40')
  expect(sceneOptionLabel({ id: 's2', frames: [10, 40], transition_out: {} })).toBe('s2 · 10–40 · unknown')
  expect(confidenceLabel({ confidence: 0.876 })).toBe('88%')
  expect(confidenceLabel({ confidence: null })).toBe('')
  expect(fontMeta({ style: 'italic', weight_range: [100, 900], ext: 'ttf', hangul: true })).toBe('italic · 100–900 · TTF · 한글')
  expect(fontMeta({})).toBe('400')
})

it('checks a font before upload and maps server error codes', () => {
  expect(fontFileError({ name: 'a.WOFF2', size: 10 })).toBeNull()
  expect(fontFileError({ name: 'a.woff', size: 10 })).toBe('fonts.error.bad_type')
  expect(fontFileError({ name: 'a.ttf', size: 20 * 1024 * 1024 + 1 })).toBe('fonts.error.too_large')
  expect(fontUploadErrorKey('bad_tables')).toBe('fonts.error.bad_tables')
  expect(fontUploadErrorKey('toString')).toBe('fonts.error.failed')
  expect(fontUploadErrorKey(undefined)).toBe('fonts.error.failed')
})

it('remembers disclosure state', () => {
  expect(storedOpen('k', true)).toBe(true)
  localStorage.setItem('k', 'false')
  expect(storedOpen('k', true)).toBe(false)
})
