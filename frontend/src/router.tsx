import { useEffect } from 'react'
import { createBrowserRouter, useLocation } from 'react-router'
import Landing from './pages/landing'

// shortcut: routes not yet ported fall through to a full page load (the server still serves their legacy HTML);
// remove once every page lives here.
function LegacyPage() {
  const { pathname, search } = useLocation()
  useEffect(() => { window.location.replace(pathname + search) }, [pathname, search])
  return null
}

export const router = createBrowserRouter([
  { path: '/', element: <Landing /> },
  { path: '*', element: <LegacyPage /> },
])
