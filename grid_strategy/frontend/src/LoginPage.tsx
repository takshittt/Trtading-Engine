import { useState, type FormEvent } from 'react'
import { login } from './auth'

/** grid's own sign-in. Credentials are verified against the Gateway (the
 * single identity provider) via grid's /api/auth/login proxy. Independent of
 * the Gateway's own browser session. */
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
    <div className="min-h-screen bg-gray-50 text-gray-800 flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="flex items-center gap-3 justify-center mb-8">
          <div className="w-9 h-9 rounded-lg bg-blue-600 flex items-center justify-center font-bold text-white select-none">S</div>
          <span className="font-bold tracking-tight text-lg text-gray-900">Grid</span>
        </div>
        <form onSubmit={handleSubmit} className="bg-white border border-gray-200 rounded-xl p-6 space-y-4">
          <div>
            <h1 className="text-lg font-semibold text-gray-900">Sign in</h1>
            <p className="text-sm text-gray-500 mt-1">Use your Gateway account.</p>
          </div>
          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 text-sm px-4 py-3 rounded-lg">
              {error}
            </div>
          )}
          <label className="block">
            <span className="text-xs text-gray-500">Email</span>
            <input
              type="email"
              value={email}
              autoFocus
              placeholder="you@example.com"
              onChange={e => setEmail(e.target.value)}
              className="mt-1 w-full bg-gray-50 border border-gray-300 rounded-md px-3 py-2 text-sm text-gray-800 focus:border-sky-500 focus:outline-none placeholder:text-gray-400"
            />
          </label>
          <label className="block">
            <span className="text-xs text-gray-500">Password</span>
            <input
              type="password"
              value={password}
              placeholder="Your password"
              onChange={e => setPassword(e.target.value)}
              className="mt-1 w-full bg-gray-50 border border-gray-300 rounded-md px-3 py-2 text-sm text-gray-800 focus:border-sky-500 focus:outline-none placeholder:text-gray-400"
            />
          </label>
          <button
            type="submit"
            disabled={busy}
            className="w-full px-4 py-2 rounded-md bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-bold transition-colors cursor-pointer"
          >
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
