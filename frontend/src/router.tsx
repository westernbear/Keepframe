import { Link, createBrowserRouter } from 'react-router'
import { AppHeader } from './components/app/app-header'
import { Button } from './components/app/button'
import Empty from './components/smoothui/empty'
import { useI18n } from './i18n'
import { AppShell } from './components/app/shell'
import Landing from './pages/landing'
import AdminAudit from './pages/admin/audit'
import AdminLlm from './pages/admin/llm'
import AdminLogin from './pages/admin/login'
import AdminQuarantine from './pages/admin/quarantine'
import AdminQueue from './pages/admin/queue'
import AdminTenants from './pages/admin/tenants'
import Agent from './pages/agent'
import Analyze from './pages/analyze'
import Ingest from './pages/ingest'
import Library from './pages/library'
import Review from './pages/review'

function NotFound() {
  const { T } = useI18n()
  return (
    <div className="min-h-dvh">
      <AppHeader />
      <Empty className="py-32" title={T('app.notFound')} action={<Button tone="primary" asChild><Link to="/">{T('nav.home')}</Link></Button>} />
    </div>
  )
}

export const router = createBrowserRouter([
  {
    element: <AppShell />,
    children: [
      { path: '/', element: <Landing /> },
      { path: '/library', element: <Library /> },
      { path: '/new', element: <Ingest /> },
      { path: '/analyze', element: <Analyze /> },
      { path: '/review', element: <Review /> },
      { path: '/agent', element: <Agent /> },
      { path: '/admin', element: <AdminLogin /> },
      { path: '/admin/login', element: <AdminLogin /> },
      { path: '/admin/tenants', element: <AdminTenants /> },
      { path: '/admin/queue', element: <AdminQueue /> },
      { path: '/admin/quarantine', element: <AdminQuarantine /> },
      { path: '/admin/audit', element: <AdminAudit /> },
      { path: '/admin/llm', element: <AdminLlm /> },
      { path: '*', element: <NotFound /> },
    ],
  },
])
