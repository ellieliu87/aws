import { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { useAuthStore } from './store/authStore'
import AppShell from './components/layout/AppShell'
import LoginPage from './pages/Login/LoginPage'
import CallbackPage from './pages/Login/CallbackPage'
import HomePage from './pages/Home/HomePage'
import WorkspacePage from './pages/Workspace/WorkspacePage'
import SettingsPage from './pages/Settings/SettingsPage'

function AuthGuard({ children }: { children: React.ReactNode }) {
  const token = useAuthStore((s) => s.token)
  const [hydrated, setHydrated] = useState(false)

  useEffect(() => {
    if (useAuthStore.persist.hasHydrated()) setHydrated(true)
    const unsub = useAuthStore.persist.onFinishHydration(() => setHydrated(true))
    return unsub
  }, [])

  if (!hydrated) return null
  if (!token) return <Navigate to="/login" replace />
  return <>{children}</>
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        {/* Outside the AuthGuard on purpose: this is how a token is
            obtained, so requiring one to reach it would be a loop. */}
        <Route path="/auth/callback" element={<CallbackPage />} />
        <Route
          path="/"
          element={
            <AuthGuard>
              <AppShell />
            </AuthGuard>
          }
        >
          <Route index element={<Navigate to="/home" replace />} />
          <Route path="home" element={<HomePage />} />
          <Route path="workspace/:functionId" element={<WorkspacePage />} />
          <Route path="workspace/:functionId/:tab" element={<WorkspacePage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="settings/:tab" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
