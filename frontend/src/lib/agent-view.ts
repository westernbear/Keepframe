// Agent page side-panel rules (scene chrome, elements, fonts, disclosures), ported from keepframe/web/static/js/agent.js.
import { T } from '@/i18n'

const CONFIDENCE_PERCENT = 100
const FONT_MAX_BYTES = 20 * 1024 * 1024
const FONT_NAME = /\.(ttf|otf|woff2)$/i
const FONT_ERRORS = new Set(['too_large', 'bad_type', 'bad_tables', 'unsupported', 'too_many_fonts'])

/** applySceneChrome: one scene-select option. */
export function sceneOptionLabel(scene) {
  return `${scene.id} · ${scene.frames[0]}–${scene.frames[1]}${scene.transition_out ? ` · ${scene.transition_out.transition || 'unknown'}` : ''}`
}

/** renderElements: the confidence column ("" when unknown). */
export function confidenceLabel(el) {
  return el.confidence != null ? Math.round(el.confidence * CONFIDENCE_PERCENT) + '%' : ''
}

export function fontMeta(font) {
  const [lo, hi] = font.weight_range || [400, 400]
  const parts = [font.style, lo === hi ? String(lo) : `${lo}–${hi}`, String(font.ext || '').toUpperCase()]
  if (font.hangul) parts.push(T('fonts.hangul'))
  return parts.filter(Boolean).join(' · ')
}

/** sendFont's checks before uploading: an i18n key, or null when the file may be sent. */
export function fontFileError(file: { name: string; size: number }) {
  if (!FONT_NAME.test(file.name)) return 'fonts.error.bad_type'
  if (file.size > FONT_MAX_BYTES) return 'fonts.error.too_large'
  return null
}

/** sendFont: the i18n key for a failed upload's server error code. */
export function fontUploadErrorKey(code?: string) {
  return code && FONT_ERRORS.has(code) ? `fonts.error.${code}` : 'fonts.error.failed'
}

/** initDisclosure: the remembered open state (storage may be unavailable). */
export function storedOpen(key: string, defaultOpen: boolean) {
  try {
    const saved = localStorage.getItem(key)
    if (saved !== null) return saved === 'true'
  } catch { /* private mode: use the default */ }
  return defaultOpen
}
