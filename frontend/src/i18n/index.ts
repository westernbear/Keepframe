import { useSyncExternalStore } from 'react'
import ko from './ko.json'
import en from './en.json'

export type Lang = 'ko' | 'en'
const STRINGS: Record<Lang, Record<string, string>> = { ko, en }
const KEY = 'keepframe.lang'
const listeners = new Set<() => void>()

export function currentLang(): Lang {
  try {
    return localStorage.getItem(KEY) === 'en' ? 'en' : 'ko'
  } catch {
    return 'ko'
  }
}

if (typeof document !== 'undefined') document.documentElement.lang = currentLang() // the saved language, from the first paint

export function T(key: string): string {
  return STRINGS[currentLang()][key] || STRINGS.ko[key] || key
}

export function Tf(key: string, vars?: Record<string, unknown>): string {
  return T(key).replace(/\{(\w+)\}/g, (_, name) => (vars && vars[name] != null ? String(vars[name]) : ''))
}

const ERROR_CODES: Record<string, string> = { // errors that arrive as codes (the detail stays in the server log)
  export_failed: 'agent.renderExportFailed',
  job_failed: 'err.jobFailed',
  correction_failed: 'err.correctionFailed',
  internal_error: 'err.internalError',
  invalid_request: 'err.invalidRequest',
  request_too_large: 'err.requestTooLarge',
  invalid_length: 'err.invalidRequest',
  agent_failed: 'err.agentFailed',
  model_list_failed: 'err.modelListFailed',
  invalid_settings: 'err.invalidSettings',
}

export function errorText(code: string | null | undefined): string {
  if (!code) return ''
  return ERROR_CODES[code] ? T(ERROR_CODES[code]) : code
}

export function setLang(next: Lang) {
  try {
    localStorage.setItem(KEY, next)
  } catch { /* private mode: language lasts for this page only */ }
  document.documentElement.lang = next
  listeners.forEach((fn) => fn())
  window.dispatchEvent(new CustomEvent('keepframe:lang', { detail: { lang: next } }))
}

export function toggleLang() {
  setLang(currentLang() === 'ko' ? 'en' : 'ko')
}

function subscribe(fn: () => void) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** Re-renders the caller when the language changes. */
export function useLang(): Lang {
  return useSyncExternalStore(subscribe, currentLang)
}

export function useI18n() {
  const lang = useLang()
  return { lang, T, Tf, errorText }
}
