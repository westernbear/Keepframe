import { Outlet } from 'react-router'
import Toaster from '@/components/smoothui/toast'
import { CommandMenu } from './command-menu'
import { ConfirmHost } from './confirm'

/** Root layout of every SPA route: the page, plus the toaster, the confirm dialog and the command menu. */
export function AppShell() {
  return (
    <Toaster>
      <Outlet />
      <ConfirmHost />
      <CommandMenu />
    </Toaster>
  )
}
