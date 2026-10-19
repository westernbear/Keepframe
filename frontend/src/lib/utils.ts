import { type ClassValue, clsx } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

// The theme's own type sizes (styles.css --text-*): unknown to tailwind-merge, `text-caption text-ink-muted` read as
// two colours and the size was dropped.
const twMerge = extendTailwindMerge({
  extend: { classGroups: { 'font-size': [{ text: ['micro', 'caption', 'body-sm', 'body', 'body-lg', 'headline', 'display-md'] }] } },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
