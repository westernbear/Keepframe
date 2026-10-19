import SmoothButton, { type SmoothButtonProps } from '@/components/smoothui/smooth-button'
import { cn } from '@/lib/utils'

type Tone = 'primary' | 'secondary' | 'ghost' | 'danger'
const TONE: Record<Tone, Pick<SmoothButtonProps, 'variant' | 'color'> & { className: string }> = {
  primary: { variant: 'default', className: 'bg-ink text-black hover:bg-[#e6e6e6]' },
  secondary: { variant: 'secondary', className: 'bg-surface-2 text-ink hover:bg-surface-3 ring-1 ring-inset ring-hairline' },
  ghost: { variant: 'ghost', className: 'text-ink-muted hover:text-ink hover:bg-white/5' },
  danger: { variant: 'default', className: 'bg-danger text-black hover:bg-[#ff9f94]' },
}

export type ButtonProps = Omit<SmoothButtonProps, 'variant' | 'color'> & { tone?: Tone; instant?: boolean }

/** DESIGN.md buttons: solid ink primary, charcoal secondary, quiet ghost; all on the 4px control radius. */
export function Button({ tone = 'secondary', shape = 'default', instant, className, ...props }: ButtonProps) {
  const t = TONE[tone]
  // `instant`: colours change without a transition, so a state change is fully painted at once.
  return <SmoothButton variant={t.variant} color={t.color} shape={shape} className={cn(t.className, instant && 'transition-transform', className)} {...props} />
}
