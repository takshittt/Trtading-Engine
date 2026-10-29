import { useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { getCredentials, saveCredentials } from '../api'
import { BROKER_FIELDS, type BrokerName } from '../brokerFields'
import { AuthError, AuthShell, Field } from './SignUpPage'

export default function CredentialFormPage() {
  const { user, refreshMe } = useAuth()
  const navigate = useNavigate()
  const [broker, setBroker] = useState<BrokerName>('shoonya')
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // If the user already has an active broker account, preselect its broker
  // (fields stay blank — secrets are never returned by the API).
  useEffect(() => {
    getCredentials()
      .then(list => {
        const active = list.find(a => a.is_active) || list[0]
        if (active && active.broker_name === 'shoonya') {
          setBroker(active.broker_name)
        }
      })
      .catch(() => {})
  }, [])

  function setField(key: string, v: string) {
    setValues(prev => ({ ...prev, [key]: v }))
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const credentials: Record<string, string> = {}
      for (const f of BROKER_FIELDS[broker]) credentials[f.key] = (values[f.key] || '').trim()
      await saveCredentials(broker, credentials)
      await refreshMe()
      navigate('/', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save credentials')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell
      title="Configure your broker"
      subtitle={
        user?.form_filled
          ? 'Add another broker account. Manage all your saved accounts from Settings.'
          : 'Enter your broker credentials to access the gateway.'
      }
    >
      <form onSubmit={handleSubmit} className="bauth-form">
        {error && <AuthError message={error} />}

        <label className="bauth-field">
          <span>Broker</span>
          <select
            value={broker}
            onChange={e => {
              setBroker(e.target.value as BrokerName)
              setValues({})
            }}
          >
            <option value="shoonya">Shoonya</option>
          </select>
        </label>

        <div className="bauth-form">
          {BROKER_FIELDS[broker].map(f => (
            <Field
              key={f.key}
              label={f.label}
              type={f.type}
              value={values[f.key] || ''}
              onChange={v => setField(f.key, v)}
            />
          ))}
        </div>

        <button
          type="submit"
          disabled={busy}
          className="bauth-btn"
        >
          {busy ? 'Saving…' : 'Save & continue'}
        </button>
      </form>
    </AuthShell>
  )
}
