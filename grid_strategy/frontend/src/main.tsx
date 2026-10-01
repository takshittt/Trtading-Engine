import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'
import LoginPage from './LoginPage'
import { getToken } from './auth'
import { applyTheme } from './theme'

// Dark-only dashboard (matches the Gateway UI). Set before first paint so there
// is never a white flash on load.
applyTheme('dark')

/** Gate the dashboard behind grid's own login. No token → show the login
 * form; on success, render the app. (A 401 mid-session clears the token and
 * reloads, landing back here.) */
function Root() {
  const [authed, setAuthed] = useState(() => Boolean(getToken()))
  if (!authed) return <LoginPage onLoggedIn={() => setAuthed(true)} />
  return <App />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
