import { useEffect } from 'react'
import { createBrowserRouter, useLocation } from 'react-router'
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

// shortcut: routes not yet ported fall through to a full page load (the server still serves their legacy HTML);
// remove once every page lives here.
function LegacyPage() {
  const { pathname, search } = useLocation()
  useEffect(() => { window.location.replace(pathname + search) }, [pathname, search])
  return null
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
      { path: '*', element: <LegacyPage /> },
    ],
  },
])
