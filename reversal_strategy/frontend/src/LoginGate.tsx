import { useState, type ReactNode, type FormEvent } from 'react'
import { getToken, login } from './auth'
import { inputCls } from './components/ui'

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

  // Same layout as grid's LoginPage, in this dashboard's palette (index.css
  // remaps slate/white/sky to the dark Gateway theme) and with TopBar's brand mark.
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex items-center justify-center gap-2.5">
          <div className="grid h-9 w-9 place-items-center rounded-lg bg-sky-600 text-sm font-bold text-white select-none">S</div>
          <span className="text-lg font-bold tracking-tight text-slate-800">Reversal Strategy</span>
        </div>
        <form onSubmit={submit} className="space-y-4 rounded-xl border border-slate-200 bg-white p-6">
          <div>
            <h1 className="text-lg font-semibold text-slate-800">Sign in</h1>
            <p className="mt-1 text-sm text-slate-500">Use your Gateway account.</p>
          </div>
          {error && (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
              {error}
            </div>
          )}
          <label className="block">
            <span className="text-xs text-slate-500">Email</span>
            <input
              className={`${inputCls} mt-1 placeholder:text-slate-400`}
              type="email"
              autoComplete="username"
              autoFocus
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label className="block">
            <span className="text-xs text-slate-500">Password</span>
            <input
              className={`${inputCls} mt-1 placeholder:text-slate-400`}
              type="password"
              autoComplete="current-password"
              placeholder="Your password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          <button className="w-full rounded-lg bg-sky-600 px-4 py-2 text-sm font-bold text-white transition-colors hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-50" type="submit" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
