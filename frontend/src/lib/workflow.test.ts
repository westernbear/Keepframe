import { expect, it } from 'vitest'
import { completedCount, stepState } from './workflow'

it('completes earlier steps only when the project status has reached them', () => {
  expect(completedCount('review', 'approved')).toBe(2)
  expect(completedCount('review', 'failed')).toBe(0)
  expect(completedCount('analyze', 'analyzing')).toBe(1)
  expect(completedCount('ingest', 'review')).toBe(0)
  expect([0, 1, 2].map((i) => stepState(i, 'analyze', 'uploaded'))).toEqual(['complete', 'current', 'upcoming'])
  expect([0, 1, 2].map((i) => stepState(i, 'review', ''))).toEqual(['upcoming', 'upcoming', 'current'])
})
