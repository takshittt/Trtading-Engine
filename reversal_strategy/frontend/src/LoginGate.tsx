import { useState, type ReactNode, type FormEvent } from 'react'
import { getToken, login } from './auth'
import './auth.css'

/**
 * Gates the dashboard behind the shared Gateway login (always on).
 *
 * A stored token is trusted optimistically — no pre-check round-trip. If it is
 * stale, the first API call 401s and api.ts clears the token and reloads, which
 * drops us back to the login form. Signing in posts to Swing's /api/auth/login
 * proxy, which verifies against the Gateway and returns the user token.
 */
export function LoginGate({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState(() => !!getToken())
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  if (authed) return <>{children}</>

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await login(email.trim(), password)
      setAuthed(true)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  // Same screen as grid's LoginPage and the Gateway sign-in; auth.css is kept
  // identical across the three apps and ignores this dashboard's palette remap.
  return (
    <div className="bauth">
      <aside className="bauth-aside">
        <div className="bauth-brand">
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <path fill="currentColor" d="M3.5 18.5V14c0-2 1.3-3.4 3.2-3.6C8 7.2 10.8 5 14.3 5c3.6 0 5.8 2.2 8.7 4.4 2.8.6 5 2.6 5 5.8V26h-2.6l-.6-6h-2.3l-.4 6h-2.6l-.4-5.6h-5.2l-.4 5.6H11l-.4-4.6H9.2c-1.4 0-2.4-.8-2.9-2l-.5-.4z" />
            <path fill="var(--aside-accent)" d="M7.6 10.6c-1.4-1-1.8-2.6-1.1-4.2.4 1.2 1.2 1.9 2.4 2.3z" />
          </svg>
          Bison <span className="bauth-product">Reversal</span>
        </div>
        <div className="bauth-pitch">
          <span className="bauth-eyebrow">Reversal strategy · NSE futures</span>
          <h2>You pick the entry. <em>It handles the exit.</em></h2>
          <ul className="bauth-points">
            <li><b>3</b>Signal timeframes: 1H, 4H and daily</li>
            <li><b>SPAN</b>Budget counted in margin, not notional</li>
            <li><b>Trail</b>A hit target arms a trailing stop instead of selling</li>
          </ul>
        </div>
        <p className="bauth-fine">Trading futures and options carries a high risk of loss. You are responsible for every order placed from your account.</p>
      </aside>
      <main className="bauth-main">
        <div className="bauth-card">
          <div className="bauth-head">
            <h1>Sign in</h1>
            <p>Use your Gateway account to open the Reversal dashboard.</p>
          </div>
          <form onSubmit={submit} className="bauth-form">
            {error && <div className="bauth-error" role="alert">{error}</div>}
            <label className="bauth-field">
              <span>Email</span>
              <input
                type="email"
                autoComplete="username"
                autoFocus
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </label>
            <label className="bauth-field">
              <span>Password</span>
              <input
                type="password"
                autoComplete="current-password"
                placeholder="Your password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </label>
            <button className="bauth-btn" type="submit" disabled={busy}>
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        </div>
      </main>
    </div>
  )
}
