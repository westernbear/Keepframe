import { Check } from 'lucide-react'
import { motion } from 'motion/react'
import { useI18n } from '@/i18n'
import { STEPS, type Stage, stepState } from '@/lib/workflow'
import { cn } from '@/lib/utils'

/** Upload → analyze → review. Adapted from Smooth UI's animated-stepper; completion follows project status, not index. */
export function WorkflowStepper({ stage, projectName, projectStatus = '' }: { stage: Stage | null; projectName?: string; projectStatus?: string }) {
  const { T } = useI18n()
  return (
    <nav className="workflow border-b border-hairline-soft" aria-label={T('workflow.title')} data-workflow>
      <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-2.5 md:px-6">
        <div className="workflow__project min-w-0 truncate text-body-sm font-medium text-ink" data-workflow-project>
          {projectName || T('workflow.projectFallback')}
        </div>
        <ol className="workflow__steps m-0 flex list-none items-center gap-1 p-0">
          {STEPS.map((step, i) => {
            const state = stepState(i, stage, projectStatus)
            return (
              <li
                key={step}
                className="workflow__step flex items-center gap-2 text-caption"
                data-workflow-step={step}
                data-state={state}
                aria-current={state === 'current' ? 'step' : undefined}
                data-complete={state === 'complete' ? 'true' : undefined}
              >
                {i > 0 && <span aria-hidden className={cn('h-px w-5 sm:w-8', state === 'upcoming' ? 'bg-hairline' : 'bg-ink/60')} />}
                <span className={cn(
                  'workflow__number relative grid size-6 place-items-center rounded-full text-micro font-semibold transition-colors',
                  state === 'complete' && 'bg-ink text-black',
                  state === 'current' && 'bg-brand text-black',
                  state === 'upcoming' && 'bg-surface-2 text-ink-muted',
                )}>
                  {state === 'complete' ? (
                    <motion.span initial={{ scale: 0.6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ type: 'spring', bounce: 0.2, duration: 0.3 }}>
                      <Check className="size-3.5" strokeWidth={3} aria-hidden />
                    </motion.span>
                  ) : i + 1}
                </span>
                <span className={state === 'upcoming' ? 'text-ink-muted' : 'text-ink'}>{T(`workflow.${step}`)}</span>
              </li>
            )
          })}
        </ol>
        <span className="workflow__status ml-auto text-caption text-ink-muted" data-workflow-status role="status">
          {stage ? T(`workflow.status.${stage}`) : ''}
        </span>
      </div>
    </nav>
  )
}
