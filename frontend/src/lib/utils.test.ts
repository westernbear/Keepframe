import { expect, it } from 'vitest'
import { cn } from './utils'

it('keeps a theme type size next to a colour', () => {
  expect(cn('text-caption', 'text-ink-muted')).toBe('text-caption text-ink-muted')
  expect(cn('text-caption', 'text-body-sm')).toBe('text-body-sm')
})
