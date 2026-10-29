import { useState, type FormEvent } from 'react'
import { login } from './auth'
import './auth.css'

/** grid's own sign-in. Credentials are verified against the Gateway (the
 * single identity provider) via grid's /api/auth/login proxy. Independent of
 * the Gateway's own browser session. Styled by auth.css, which is kept
 * identical to the Gateway and Reversal copies. */
export default function LoginPage({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await login(email.trim(), password)
      onLoggedIn()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bauth">
      <aside className="bauth-aside">
        <div className="bauth-brand">
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <path fill="currentColor" d="M3.5 18.5V14c0-2 1.3-3.4 3.2-3.6C8 7.2 10.8 5 14.3 5c3.6 0 5.8 2.2 8.7 4.4 2.8.6 5 2.6 5 5.8V26h-2.6l-.6-6h-2.3l-.4 6h-2.6l-.4-5.6h-5.2l-.4 5.6H11l-.4-4.6H9.2c-1.4 0-2.4-.8-2.9-2l-.5-.4z" />
            <path fill="var(--aside-accent)" d="M7.6 10.6c-1.4-1-1.8-2.6-1.1-4.2.4 1.2 1.2 1.9 2.4 2.3z" />
          </svg>
          Bison <span className="bauth-product">Grid</span>
        </div>
        <div className="bauth-pitch">
          <span className="bauth-eyebrow">Grid strategy · Dip ladder</span>
          <h2>Buy the dips. <em>Exit every rung.</em></h2>
          <ul className="bauth-points">
            <li><b>1</b>Lot per rung, never averaged together</li>
            <li><b>5</b>Checks on every tick before an exit fires</li>
            <li><b>60s</b>Positions checked against your broker</li>
          </ul>
        </div>
        <p className="bauth-fine">Trading futures and options carries a high risk of loss. You are responsible for every order placed from your account.</p>
      </aside>
      <main className="bauth-main">
        <div className="bauth-card">
          <div className="bauth-head">
            <h1>Sign in</h1>
            <p>Use your Gateway account to open the Grid dashboard.</p>
          </div>
          <form onSubmit={handleSubmit} className="bauth-form">
            {error && <div className="bauth-error" role="alert">{error}</div>}
            <label className="bauth-field">
              <span>Email</span>
              <input
                type="email"
                value={email}
                autoFocus
                autoComplete="username"
                placeholder="you@example.com"
                onChange={e => setEmail(e.target.value)}
              />
            </label>
            <label className="bauth-field">
              <span>Password</span>
              <input
                type="password"
                value={password}
                autoComplete="current-password"
                placeholder="Your password"
                onChange={e => setPassword(e.target.value)}
              />
            </label>
            <button type="submit" disabled={busy} className="bauth-btn">
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        </div>
      </main>
    </div>
  )
}
