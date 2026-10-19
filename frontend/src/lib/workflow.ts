export const STEPS = ['ingest', 'analyze', 'review'] as const
export type Stage = (typeof STEPS)[number]

/** How many leading steps are complete, given the page's stage and the project's status. */
export function completedCount(current: Stage | null, projectStatus: string) {
  if (current === 'review') return ['review', 'approved'].includes(projectStatus) ? 2 : 0
  if (current === 'analyze') return ['uploaded', 'analyzing', 'review', 'approved'].includes(projectStatus) ? 1 : 0
  return 0
}

export function stepState(index: number, current: Stage | null, projectStatus: string) {
  const currentIndex = current ? STEPS.indexOf(current) : -1
  const complete = currentIndex >= 0 && index < completedCount(current, projectStatus)
  return complete ? 'complete' : index === currentIndex ? 'current' : 'upcoming'
}
