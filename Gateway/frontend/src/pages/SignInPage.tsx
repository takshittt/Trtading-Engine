import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { AuthError, AuthShell, Field } from './SignUpPage'

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
    <AuthShell title="Sign in" subtitle="Welcome back. Sign in to reach your gateway, positions and orders.">
      <form onSubmit={handleSubmit} className="bauth-form">
        {error && <AuthError message={error} />}
        <Field label="Email" type="email" value={email} onChange={setEmail} autoFocus autoComplete="username" placeholder="you@example.com" />
        <Field label="Password" type="password" value={password} onChange={setPassword} autoComplete="current-password" placeholder="Your password" />
        <button
          type="submit"
          disabled={busy}
          className="bauth-btn"
        >
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      <p className="bauth-alt">
        Don&apos;t have an account?{' '}
        <Link to="/signup">Sign up</Link>
      </p>
    </AuthShell>
  )
}
