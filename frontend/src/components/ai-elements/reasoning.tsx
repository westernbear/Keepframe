// Adapted from Vercel AI Elements "Reasoning" (Apache-2.0, ai-sdk.dev/elements), on Base UI's Collapsible instead of Radix.
import { Collapsible } from '@base-ui/react/collapsible'
import { Brain, ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'

/** A folded "Thought for N s" line whose panel holds the model's thinking trace. */
export function Reasoning({ label, steps, className }: { label: string; steps: string[]; className?: string }) {
  return (
    <Collapsible.Root className={cn('reasoning group/rs flex flex-col gap-1.5 self-start', className)}>
      <Collapsible.Trigger className="reasoning__trigger inline-flex min-h-8 items-center gap-1.5 rounded-lg text-caption text-ink-muted outline-none hover:text-ink focus-visible:text-ink focus-visible:ring-2 focus-visible:ring-brand/60">
        <Brain className="size-3.5" aria-hidden />
        <span>{label}</span>
        <ChevronDown className="size-3.5 transition-transform duration-200 group-data-[open]/rs:rotate-180" aria-hidden />
      </Collapsible.Trigger>
      <Collapsible.Panel className="reasoning__panel h-[var(--collapsible-panel-height)] overflow-hidden transition-[height,opacity] duration-200 ease-out data-[ending-style]:h-0 data-[ending-style]:opacity-0 data-[starting-style]:h-0 data-[starting-style]:opacity-0">
        <div className="flex flex-col gap-2 border-l border-hairline py-0.5 pl-3 text-caption leading-relaxed whitespace-pre-line text-ink-muted">
          {steps.map((s, i) => <p key={i} className="m-0">{s}</p>)}
        </div>
      </Collapsible.Panel>
    </Collapsible.Root>
  )
}
