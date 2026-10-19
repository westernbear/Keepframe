// Adapted from React Bits "BlurText" (MIT, reactbits.dev): words arrive out of a blur, one after another.
import { motion, useReducedMotion } from 'motion/react'
import { cn } from '@/lib/utils'

const EASE_OUT = [0.23, 1, 0.32, 1] as const
// Past this many words a per-word stagger reads as latency; the block arrives as one.
const MAX_WORDS = 120

export function BlurText({ text, className, step = 0.018, maxDelay = 0.45 }: { text: string; className?: string; step?: number; maxDelay?: number }) {
  const reduced = useReducedMotion()
  const parts = text.split(/(\s+)/)
  if (reduced || parts.length > MAX_WORDS * 2) return (
    <motion.span className={className} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.2 }}>{text}</motion.span>
  )
  let word = 0
  return (
    <span className={className}>
      {/* whitespace stays a plain text node, so textContent and pre-line breaks are the original text */}
      {parts.map((part, i) => /^\s*$/.test(part) ? part : (
        <motion.span key={i} className={cn('inline-block max-w-full [overflow-wrap:anywhere]')}
          initial={{ opacity: 0, filter: 'blur(6px)', y: 3 }} animate={{ opacity: 1, filter: 'blur(0px)', y: 0 }}
          transition={{ duration: 0.32, ease: EASE_OUT, delay: Math.min(word++ * step, maxDelay) }}>{part}</motion.span>
      ))}
    </span>
  )
}
