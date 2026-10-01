import { StrictMode, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import './index.css'
import App from './App.tsx'
import { AuthProvider, useAuth } from './auth/AuthContext'
import SignInPage from './pages/SignInPage'
import SignUpPage from './pages/SignUpPage'
import CredentialFormPage from './pages/CredentialFormPage'

function FullScreenLoader() {
  return (
    <div className="min-h-screen bg-gray-950 text-gray-500 flex items-center justify-center text-sm">
      Loading…
    </div>
  )
}

/** Requires a signed-in user; otherwise bounces to /signin. */
function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  if (loading) return <FullScreenLoader />
  if (!user) return <Navigate to="/signin" replace />
  return <>{children}</>
}

/** Public (sign-in / sign-up) — redirects away when already authed. */
function PublicOnly({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  if (loading) return <FullScreenLoader />
  if (user) return <Navigate to={user.form_filled ? '/' : '/form'} replace />
  return <>{children}</>
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/signin" element={<PublicOnly><SignInPage /></PublicOnly>} />
      <Route path="/signup" element={<PublicOnly><SignUpPage /></PublicOnly>} />
      <Route path="/form" element={<RequireAuth><CredentialFormPage /></RequireAuth>} />
      <Route path="/" element={<RequireAuth><App /></RequireAuth>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
)
