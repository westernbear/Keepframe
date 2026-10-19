import { useEffect } from 'react'
import { createBrowserRouter, useLocation } from 'react-router'
import { AppShell } from './components/app/shell'
import Landing from './pages/landing'
import Analyze from './pages/analyze'
import Ingest from './pages/ingest'
import Library from './pages/library'

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
      { path: '*', element: <LegacyPage /> },
    ],
  },
])
