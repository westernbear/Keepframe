import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it } from 'vitest'
import { AdminShell } from './admin-shell'
import { ConfirmHost, confirmAction } from './confirm'

// React's act() in a plain jsdom document (no testing-library dependency)
;(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true
let host: HTMLDivElement
const mount = async (ui: React.ReactNode) => {
  host = document.body.appendChild(document.createElement('div'))
  const root = createRoot(host)
  await act(async () => root.render(<MemoryRouter>{ui}</MemoryRouter>))
  return root
}
afterEach(() => { document.body.innerHTML = '' })

it('confirmAction resolves true only on the confirm button, false on cancel', async () => {
  await mount(<ConfirmHost />)
  let first: Promise<boolean>
  await act(async () => { first = confirmAction({ title: 'Suspend Acme?', danger: true }) })
  expect(document.body.textContent).toContain('Suspend Acme?')
  await act(async () => (document.querySelector('[data-confirm-ok]') as HTMLButtonElement).click())
  expect(await first!).toBe(true)

  let second: Promise<boolean>
  await act(async () => { second = confirmAction({ title: 'Again?' }) })
  const cancel = [...document.querySelectorAll('button')].find((b) => b.textContent === '취소')!
  await act(async () => cancel.click())
  expect(await second!).toBe(false)
})

it('a newer confirmation supersedes an unanswered one', async () => {
  await mount(<ConfirmHost />)
  let first: Promise<boolean>
  await act(async () => { first = confirmAction({ title: 'one' }); confirmAction({ title: 'two' }) })
  expect(await first!).toBe(false)
})

it('admin shell marks the current page and links every admin page and the app', async () => {
  await mount(<AdminShell page="queue" title="작업 큐"><p>body</p></AdminShell>)
  const links = [...host.querySelectorAll('nav a')].map((a) => a.getAttribute('href'))
  expect(links).toEqual(['/admin/tenants', '/admin/queue', '/admin/quarantine', '/admin/audit', '/admin/llm', '/'])
  expect(host.querySelector('[aria-current="page"]')!.getAttribute('href')).toBe('/admin/queue')
  expect(host.querySelector('h1')!.textContent).toBe('작업 큐')
  expect(document.title).toBe('작업 큐 · Keepframe')
})
