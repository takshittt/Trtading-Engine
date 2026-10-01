import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { AuthShell, Field } from './SignUpPage'

export default function SignInPage() {
  const { signin } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const user = await signin(email.trim(), password)
      navigate(user.form_filled ? '/' : '/form', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell title="Welcome back" subtitle="Sign in to access your gateway.">
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && (
          <div className="bg-red-950/60 border border-red-800 text-red-300 text-sm px-4 py-3 rounded-lg">
            {error}
          </div>
        )}
        <Field label="Email" type="email" value={email} onChange={setEmail} autoFocus placeholder="you@example.com" />
        <Field label="Password" type="password" value={password} onChange={setPassword} placeholder="Your password" />
        <button
          type="submit"
          disabled={busy}
          className="w-full px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium transition-colors cursor-pointer"
        >
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      <p className="text-sm text-gray-500 text-center mt-6">
        Don&apos;t have an account?{' '}
        <Link to="/signup" className="text-blue-400 hover:text-blue-300">Sign up</Link>
      </p>
    </AuthShell>
  )
}
