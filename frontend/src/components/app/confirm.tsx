import { useSyncExternalStore } from 'react'
import { AlertDialog } from '@/components/smoothui/dialog'
import { useI18n } from '@/i18n'
import { Button } from './button'

export interface ConfirmOptions {
  title: string
  description?: string
  confirmLabel?: string
  danger?: boolean
}

type Pending = ConfirmOptions & { resolve: (ok: boolean) => void }
let pending: Pending | null = null
const listeners = new Set<() => void>()
const emit = () => listeners.forEach((fn) => fn())

/** Replaces window.confirm(): resolves true only on the explicit confirm button. */
export function confirmAction(options: ConfirmOptions): Promise<boolean> {
  pending?.resolve(false) // a newer question supersedes an unanswered one
  return new Promise((resolve) => {
    pending = { ...options, resolve }
    emit()
  })
}

function settle(ok: boolean) {
  const p = pending
  pending = null
  emit()
  p?.resolve(ok)
}

/** Mounted once in the shell. */
export function ConfirmHost() {
  const { T } = useI18n()
  const current = useSyncExternalStore((fn) => { listeners.add(fn); return () => listeners.delete(fn) }, () => pending)
  return (
    <AlertDialog
      open={!!current}
      onOpenChange={(open) => { if (!open) settle(false) }}
      title={current?.title}
      description={current?.description}
      footer={
        <>
          <Button tone="ghost" onClick={() => settle(false)}>{T('common.cancel')}</Button>
          <Button tone={current?.danger ? 'danger' : 'primary'} onClick={() => settle(true)} data-confirm-ok>
            {current?.confirmLabel || T('shell.confirm.ok')}
          </Button>
        </>
      }
    />
  )
}
