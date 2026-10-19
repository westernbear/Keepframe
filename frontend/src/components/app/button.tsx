import SmoothButton, { type SmoothButtonProps } from '@/components/smoothui/smooth-button'
import { cn } from '@/lib/utils'

type Tone = 'primary' | 'secondary' | 'ghost' | 'danger'
const TONE: Record<Tone, Pick<SmoothButtonProps, 'variant' | 'color'> & { className: string }> = {
  primary: { variant: 'default', className: 'bg-ink text-black hover:bg-white/90' },
  secondary: { variant: 'secondary', className: 'bg-surface-2 text-ink hover:bg-surface-3 ring-1 ring-inset ring-white/5' },
  ghost: { variant: 'ghost', className: 'text-ink-muted hover:text-ink hover:bg-white/5' },
  danger: { variant: 'default', className: 'bg-danger text-black hover:bg-danger/90' },
}

export type ButtonProps = Omit<SmoothButtonProps, 'variant' | 'color'> & { tone?: Tone }

/** DESIGN.md buttons: white pill primary, charcoal pill secondary, quiet ghost. */
export function Button({ tone = 'secondary', shape = 'pill', className, ...props }: ButtonProps) {
  const t = TONE[tone]
  return <SmoothButton variant={t.variant} color={t.color} shape={shape} className={cn(t.className, className)} {...props} />
}
