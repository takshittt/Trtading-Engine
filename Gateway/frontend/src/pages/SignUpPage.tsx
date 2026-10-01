import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

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
    <AuthShell title="Create your account" subtitle="Sign up to configure your broker and access the gateway.">
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && (
          <div className="bg-red-950/60 border border-red-800 text-red-300 text-sm px-4 py-3 rounded-lg">
            {error}
          </div>
        )}
        <Field label="Email" type="email" value={email} onChange={setEmail} autoFocus placeholder="you@example.com" />
        <Field label="Password" type="password" value={password} onChange={setPassword} placeholder="At least 6 characters" />
        <Field label="Confirm password" type="password" value={confirm} onChange={setConfirm} placeholder="Re-enter password" />
        <button
          type="submit"
          disabled={busy}
          className="w-full px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium transition-colors cursor-pointer"
        >
          {busy ? 'Creating account…' : 'Sign up'}
        </button>
      </form>
      <p className="text-sm text-gray-500 text-center mt-6">
        Already have an account?{' '}
        <Link to="/signin" className="text-blue-400 hover:text-blue-300">Sign in</Link>
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
    <div className="min-h-screen bg-gray-950 text-gray-100 flex items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="flex items-center gap-3 justify-center mb-8">
          <div className="w-9 h-9 rounded-lg bg-blue-600 flex items-center justify-center font-bold select-none">G</div>
          <span className="font-semibold tracking-tight text-lg">Gateway</span>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h1 className="text-lg font-semibold">{title}</h1>
          <p className="text-sm text-gray-500 mt-1 mb-6">{subtitle}</p>
          {children}
        </div>
      </div>
    </div>
  )
}

export function Field({
  label,
  type = 'text',
  value,
  onChange,
  placeholder,
  autoFocus,
}: {
  label: string
  type?: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  autoFocus?: boolean
}) {
  return (
    <label className="block">
      <span className="text-xs text-gray-400">{label}</span>
      <input
        type={type}
        value={value}
        autoFocus={autoFocus}
        placeholder={placeholder}
        onChange={e => onChange(e.target.value)}
        className="mt-1 w-full bg-gray-950 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none placeholder:text-gray-600"
      />
    </label>
  )
}
