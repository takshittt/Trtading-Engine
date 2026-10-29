import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import './auth.css'

export default function SignUpPage() {
  const { signup } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (password !== confirm) {
      setError('Passwords do not match')
      return
    }
    if (password.length < 6) {
      setError('Password must be at least 6 characters')
      return
    }
    setBusy(true)
    try {
      const user = await signup(email.trim(), password)
      // New users always land on the credential form first.
      navigate(user.form_filled ? '/' : '/form', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign up failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell title="Create your account" subtitle="Next you will add your broker login, then you can start trading through the gateway.">
      <form onSubmit={handleSubmit} className="bauth-form">
        {error && <AuthError message={error} />}
        <Field label="Email" type="email" value={email} onChange={setEmail} autoFocus autoComplete="username" placeholder="you@example.com" />
        <Field label="Password" type="password" value={password} onChange={setPassword} autoComplete="new-password" placeholder="At least 6 characters" />
        <Field label="Confirm password" type="password" value={confirm} onChange={setConfirm} autoComplete="new-password" placeholder="Re-enter password" />
        <button
          type="submit"
          disabled={busy}
          className="bauth-btn"
        >
          {busy ? 'Creating account…' : 'Sign up'}
        </button>
      </form>
      <p className="bauth-alt">
        Already have an account?{' '}
        <Link to="/signin">Sign in</Link>
      </p>
    </AuthShell>
  )
}

// --- Shared auth UI (kept local to avoid new shared files) ---

export function AuthShell({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle: string
  children: React.ReactNode
}) {
  return (
    <div className="bauth">
      <aside className="bauth-aside">
        <Link className="bauth-brand" to="/">
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <path fill="currentColor" d="M3.5 18.5V14c0-2 1.3-3.4 3.2-3.6C8 7.2 10.8 5 14.3 5c3.6 0 5.8 2.2 8.7 4.4 2.8.6 5 2.6 5 5.8V26h-2.6l-.6-6h-2.3l-.4 6h-2.6l-.4-5.6h-5.2l-.4 5.6H11l-.4-4.6H9.2c-1.4 0-2.4-.8-2.9-2l-.5-.4z" />
            <path fill="var(--aside-accent)" d="M7.6 10.6c-1.4-1-1.8-2.6-1.1-4.2.4 1.2 1.2 1.9 2.4 2.3z" />
          </svg>
          Bison <span className="bauth-product">Gateway</span>
        </Link>
        <div className="bauth-pitch">
          <span className="bauth-eyebrow">Self-hosted execution for NSE &amp; MCX</span>
          <h2>One broker session. <em>Every strategy.</em></h2>
          <ul className="bauth-points">
            <li><b>1</b>Shoonya login, shared by Grid and Reversal</li>
            <li><b>60s</b>Positions checked against your broker</li>
            <li><b>7d</b>Sign-in lasts a week on this device</li>
          </ul>
        </div>
        <p className="bauth-fine">Trading futures and options carries a high risk of loss. You are responsible for every order placed from your account.</p>
      </aside>
      <main className="bauth-main">
        <div className="bauth-card">
          <div className="bauth-head">
            <h1>{title}</h1>
            <p>{subtitle}</p>
          </div>
          {children}
        </div>
      </main>
    </div>
  )
}

export function AuthError({ message }: { message: string }) {
  return <div className="bauth-error" role="alert">{message}</div>
}

export function Field({
  label,
  type = 'text',
  value,
  onChange,
  placeholder,
  autoFocus,
  autoComplete,
}: {
  label: string
  type?: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  autoFocus?: boolean
  autoComplete?: string
}) {
  return (
    <label className="bauth-field">
      <span>{label}</span>
      <input
        type={type}
        value={value}
        autoFocus={autoFocus}
        autoComplete={autoComplete}
        placeholder={placeholder}
        onChange={e => onChange(e.target.value)}
      />
    </label>
  )
}
