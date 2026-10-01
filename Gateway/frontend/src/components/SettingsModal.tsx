import { useEffect, useState, type FormEvent } from 'react'
import type { MarketHoursSettings } from '../types'
import {
  getMarketHours,
  updateExchangeHours,
  resetExchangeHours,
  setMarketHoursEnforced,
  addMarketHoliday,
  removeMarketHoliday,
  getCredentials,
  revealCredentials,
  saveCredentials,
  updateCredentials,
  activateCredentials,
  deleteCredentials,
  testCredentials,
  testStoredCredentials,
  type ConfiguredBroker,
} from '../api'
import { BROKER_FIELDS, isBrokerName, type BrokerName } from '../brokerFields'

interface SettingsModalProps {
  show: boolean
  onClose: () => void
}

type Tab = 'hours' | 'credentials'

export default function SettingsModal({ show, onClose }: SettingsModalProps) {
  const [tab, setTab] = useState<Tab>('hours')

  const [settings, setSettings] = useState<MarketHoursSettings | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Inline hours editing
  const [editExch, setEditExch] = useState<string | null>(null)
  const [editOpen, setEditOpen] = useState('')
  const [editClose, setEditClose] = useState('')

  // Add-holiday form
  const [newDate, setNewDate] = useState('')
  const [newLabel, setNewLabel] = useState('')

  useEffect(() => {
    if (!show) return
    setLoading(true)
    setError(null)
    getMarketHours()
      .then(setSettings)
      .catch(e => setError(e.message || 'Failed to load settings'))
      .finally(() => setLoading(false))
  }, [show])

  if (!show) return null

  // Wrap a mutating api call: it returns the full settings, so we just adopt it.
  async function run(fn: () => Promise<MarketHoursSettings>) {
    setBusy(true)
    setError(null)
    try {
      setSettings(await fn())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Action failed')
    } finally {
      setBusy(false)
    }
  }

  function startEdit(exch: string, open: string, close: string) {
    setEditExch(exch)
    setEditOpen(open)
    setEditClose(close)
  }

  async function saveEdit(exch: string) {
    await run(() => updateExchangeHours(exch, editOpen, editClose))
    setEditExch(null)
  }

  async function addHoliday() {
    if (!newDate) return
    await run(() => addMarketHoliday(newDate, newLabel.trim()))
    setNewDate('')
    setNewLabel('')
  }

  const enforced = settings?.enforced ?? true

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full max-w-2xl mx-4 rounded-xl overflow-hidden shadow-2xl border border-gray-700 bg-gray-900 max-h-[90vh] flex flex-col">
        <div className="px-5 py-3 border-b border-gray-800 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-2">
            <svg className="w-5 h-5 text-gray-400" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M10.5 6h9.75M10.5 12h9.75m-9.75 6h9.75M3.75 6h.007v.008H3.75V6Zm.375 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Z" />
            </svg>
            <span className="font-bold text-white">Settings</span>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-xl leading-none cursor-pointer">×</button>
        </div>

        {/* Tab bar */}
        <div className="px-5 pt-3 border-b border-gray-800 flex gap-1 shrink-0">
          <TabButton active={tab === 'hours'} onClick={() => setTab('hours')}>Market Hours</TabButton>
          <TabButton active={tab === 'credentials'} onClick={() => setTab('credentials')}>Broker Credentials</TabButton>
        </div>

        <div className="overflow-y-auto flex-1 p-5 space-y-5">
          {tab === 'hours' ? (
            <>
              <p className="text-xs text-gray-500 leading-relaxed">
                Target auto-exits and persistent orders only fire while the exchange is open, so a stale
                pre-open price can't trigger an order the broker would reject. National holidays are applied
                automatically; MCX's evening close auto-adjusts for US daylight saving unless you pin it below.
              </p>

              {loading ? (
                <div className="py-12 text-center text-gray-500 text-sm">Loading settings…</div>
              ) : !settings ? (
                <div className="py-12 text-center text-red-400 text-sm">{error || 'No settings available'}</div>
              ) : (
                <>
                  {error && (
                    <div className="bg-red-950/60 border border-red-800 text-red-300 text-xs px-3 py-2 rounded">{error}</div>
                  )}

                  {/* Enforce toggle */}
                  <div className="flex items-center justify-between rounded-lg border border-gray-800 px-4 py-3">
                    <div>
                      <div className="text-sm font-medium text-gray-200">Enforce market hours</div>
                      <div className="text-xs text-gray-500">When off, auto-exits fire at any time (orders may be rejected outside hours).</div>
                    </div>
                    <button
                      role="switch"
                      aria-checked={enforced}
                      disabled={busy}
                      onClick={() => run(() => setMarketHoursEnforced(!enforced))}
                      className={`relative w-11 h-6 rounded-full transition-colors cursor-pointer disabled:opacity-50 ${enforced ? 'bg-blue-600' : 'bg-gray-700'}`}
                    >
                      <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform ${enforced ? 'translate-x-5' : ''}`} />
                    </button>
                  </div>

                  {!enforced && (
                    <div className="bg-amber-950/50 border border-amber-800/60 text-amber-300 text-xs px-3 py-2 rounded">
                      Market-hours enforcement is off. The time gate is bypassed for all exchanges.
                    </div>
                  )}

                  {/* Trading hours table */}
                  <div>
                    <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Trading Hours (IST)</div>
                    <div className="rounded-lg border border-gray-800 overflow-hidden">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="text-left text-xs text-gray-600 bg-gray-900/60 border-b border-gray-800">
                            <th className="py-2 px-3 font-medium">Exchange</th>
                            <th className="py-2 px-3 font-medium">Open</th>
                            <th className="py-2 px-3 font-medium">Close</th>
                            <th className="py-2 px-3 font-medium text-right">Actions</th>
                          </tr>
                        </thead>
                        <tbody>
                          {settings.exchanges.map(ex => {
                            const editing = editExch === ex.exch
                            return (
                              <tr key={ex.exch} className="border-b border-gray-800/50 last:border-0">
                                <td className="py-2 px-3">
                                  <span className="font-mono text-gray-200">{ex.exch}</span>
                                  {ex.is_custom && (
                                    <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-blue-950 border border-blue-800 text-blue-300 uppercase tracking-wide">Custom</span>
                                  )}
                                </td>
                                {editing ? (
                                  <>
                                    <td className="py-2 px-3">
                                      <input type="time" value={editOpen} onChange={e => setEditOpen(e.target.value)}
                                        className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-gray-100 text-sm font-mono focus:border-blue-500 outline-none" />
                                    </td>
                                    <td className="py-2 px-3">
                                      <input type="time" value={editClose} onChange={e => setEditClose(e.target.value)}
                                        className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-gray-100 text-sm font-mono focus:border-blue-500 outline-none" />
                                    </td>
                                    <td className="py-2 px-3 text-right whitespace-nowrap">
                                      <button disabled={busy} onClick={() => saveEdit(ex.exch)}
                                        className="text-xs px-2.5 py-1 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white cursor-pointer">Save</button>
                                      <button disabled={busy} onClick={() => setEditExch(null)}
                                        className="ml-1.5 text-xs px-2.5 py-1 rounded border border-gray-700 text-gray-400 hover:text-gray-200 cursor-pointer">Cancel</button>
                                    </td>
                                  </>
                                ) : (
                                  <>
                                    <td className="py-2 px-3 font-mono text-gray-300">{ex.open}</td>
                                    <td className="py-2 px-3 font-mono text-gray-300">{ex.close}</td>
                                    <td className="py-2 px-3 text-right whitespace-nowrap">
                                      <button disabled={busy} onClick={() => startEdit(ex.exch, ex.open, ex.close)}
                                        className="text-xs px-2.5 py-1 rounded border border-gray-700 text-gray-300 hover:border-gray-500 hover:text-gray-100 cursor-pointer">Edit</button>
                                      {ex.is_custom && (
                                        <button disabled={busy} onClick={() => run(() => resetExchangeHours(ex.exch))}
                                          title={`Reset to default (${ex.default_open}–${ex.default_close})`}
                                          className="ml-1.5 text-xs px-2.5 py-1 rounded border border-gray-800 text-gray-500 hover:text-gray-300 cursor-pointer">Reset</button>
                                      )}
                                    </td>
                                  </>
                                )}
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>

                  {/* Extra holidays */}
                  <div>
                    <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Extra Holidays</div>
                    <p className="text-xs text-gray-600 mb-2">Added on top of the built-in national holiday calendar — use this to extend into a new year.</p>
                    <div className="rounded-lg border border-gray-800 divide-y divide-gray-800/60">
                      {settings.holidays.length === 0 ? (
                        <div className="px-3 py-3 text-xs text-gray-600">No custom holidays added.</div>
                      ) : (
                        settings.holidays.map(h => (
                          <div key={h.date} className="flex items-center justify-between px-3 py-2">
                            <div className="flex items-center gap-3">
                              <span className="font-mono text-sm text-gray-300">{h.date}</span>
                              {h.label && <span className="text-xs text-gray-500">{h.label}</span>}
                            </div>
                            <button disabled={busy} onClick={() => run(() => removeMarketHoliday(h.date))}
                              className="text-gray-600 hover:text-red-400 text-lg leading-none cursor-pointer" aria-label="Remove">×</button>
                          </div>
                        ))
                      )}
                    </div>
                    <div className="flex items-center gap-2 mt-2">
                      <input type="date" value={newDate} onChange={e => setNewDate(e.target.value)}
                        className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-gray-100 text-sm font-mono focus:border-blue-500 outline-none" />
                      <input type="text" value={newLabel} onChange={e => setNewLabel(e.target.value)} placeholder="Label (optional)"
                        className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-gray-100 text-sm focus:border-blue-500 outline-none" />
                      <button disabled={busy || !newDate} onClick={addHoliday}
                        className="text-xs px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium cursor-pointer">Add</button>
                    </div>
                  </div>
                </>
              )}
            </>
          ) : (
            <CredentialsTab active={show && tab === 'credentials'} />
          )}
        </div>
      </div>
    </div>
  )
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors cursor-pointer ${
        active ? 'border-blue-500 text-white' : 'border-transparent text-gray-500 hover:text-gray-300'
      }`}
    >
      {children}
    </button>
  )
}

// ---------------------------------------------------------------------------
// Broker credentials tab: a list of saved accounts, one marked active, each
// editable/testable/deletable; plus a form to add a new one.
// ---------------------------------------------------------------------------

type TestResult = { success: boolean; message: string }

function CredentialsTab({ active }: { active: boolean }) {
  const [accounts, setAccounts] = useState<ConfiguredBroker[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)

  // 'list' | 'add' | edit-account-id
  const [view, setView] = useState<'list' | 'add' | number>('list')

  function loadAccounts() {
    setLoading(true)
    setListError(null)
    getCredentials()
      .then(setAccounts)
      .catch(e => setListError(e instanceof Error ? e.message : 'Failed to load accounts'))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    if (!active) {
      setView('list')
      return
    }
    loadAccounts()
  }, [active])

  async function handleActivate(id: number) {
    setBusyId(id)
    setListError(null)
    try {
      await activateCredentials(id)
      loadAccounts()
    } catch (e) {
      setListError(e instanceof Error ? e.message : 'Failed to activate account')
    } finally {
      setBusyId(null)
    }
  }

  async function handleDelete(id: number) {
    setBusyId(id)
    setListError(null)
    try {
      await deleteCredentials(id)
      loadAccounts()
    } catch (e) {
      setListError(e instanceof Error ? e.message : 'Failed to delete account')
    } finally {
      setBusyId(null)
    }
  }

  if (view === 'add') {
    return (
      <CredentialForm
        mode="add"
        onDone={() => { setView('list'); loadAccounts() }}
        onCancel={() => setView('list')}
      />
    )
  }

  if (typeof view === 'number') {
    return (
      <CredentialForm
        mode="edit"
        accountId={view}
        onDone={() => { setView('list'); loadAccounts() }}
        onCancel={() => setView('list')}
      />
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-gray-500 leading-relaxed">
        Save as many broker accounts as you like. The one marked <span className="text-gray-300">Active</span> is
        what Connect and the trading engine log in with next — switching it never touches a session that's
        already live.
      </p>

      {listError && (
        <div className="bg-red-950/60 border border-red-800 text-red-300 text-xs px-3 py-2 rounded">{listError}</div>
      )}

      {loading ? (
        <div className="py-8 text-center text-gray-500 text-sm">Loading accounts…</div>
      ) : !accounts || accounts.length === 0 ? (
        <div className="py-8 text-center text-gray-600 text-sm">No broker accounts saved yet.</div>
      ) : (
        <div className="rounded-lg border border-gray-800 divide-y divide-gray-800/60">
          {accounts.map(acc => (
            <AccountRow
              key={acc.id}
              account={acc}
              busy={busyId === acc.id}
              onActivate={() => handleActivate(acc.id)}
              onEdit={() => setView(acc.id)}
              onDelete={() => handleDelete(acc.id)}
            />
          ))}
        </div>
      )}

      <button
        onClick={() => setView('add')}
        className="text-xs px-3 py-2 rounded border border-gray-700 text-gray-300 hover:border-gray-500 hover:text-gray-100 cursor-pointer"
      >
        + Add account
      </button>
    </div>
  )
}

// One saved account: activate / edit (password-gated reveal) / delete, plus an
// inline password-gated test that never exposes the decrypted values.
function AccountRow({
  account,
  busy,
  onActivate,
  onEdit,
  onDelete,
}: {
  account: ConfiguredBroker
  busy: boolean
  onActivate: () => void
  onEdit: () => void
  onDelete: () => void
}) {
  const [testing, setTesting] = useState(false)
  const [testPassword, setTestPassword] = useState('')
  const [testResult, setTestResult] = useState<TestResult | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  async function runTest(e: FormEvent) {
    e.preventDefault()
    setTesting(true)
    setTestResult(null)
    try {
      const res = await testStoredCredentials(account.id, testPassword)
      setTestResult(res)
    } catch (err) {
      setTestResult({ success: false, message: err instanceof Error ? err.message : 'Test failed' })
    } finally {
      setTestPassword('')
      setTesting(false)
    }
  }

  return (
    <div className="px-3 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-gray-200 truncate">{account.label}</span>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 border border-gray-700 text-gray-400 uppercase tracking-wide">
              {account.broker_name}
            </span>
            {account.is_active && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-950 border border-blue-800 text-blue-300 uppercase tracking-wide">
                Active
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {!account.is_active && (
            <button disabled={busy} onClick={onActivate}
              className="text-xs px-2.5 py-1 rounded border border-gray-700 text-gray-300 hover:border-blue-600 hover:text-blue-300 disabled:opacity-50 cursor-pointer">
              Set active
            </button>
          )}
          <button disabled={busy} onClick={() => { setTesting(t => !t); setTestResult(null) }}
            className="text-xs px-2.5 py-1 rounded border border-gray-700 text-gray-300 hover:border-gray-500 hover:text-gray-100 disabled:opacity-50 cursor-pointer">
            Test
          </button>
          <button disabled={busy} onClick={onEdit}
            className="text-xs px-2.5 py-1 rounded border border-gray-700 text-gray-300 hover:border-gray-500 hover:text-gray-100 disabled:opacity-50 cursor-pointer">
            Edit
          </button>
          {confirmDelete ? (
            <>
              <button disabled={busy} onClick={onDelete}
                className="text-xs px-2.5 py-1 rounded bg-red-900/60 border border-red-800 text-red-300 hover:bg-red-900 disabled:opacity-50 cursor-pointer">
                Confirm
              </button>
              <button disabled={busy} onClick={() => setConfirmDelete(false)}
                className="text-xs px-2 py-1 rounded text-gray-500 hover:text-gray-300 cursor-pointer">
                ×
              </button>
            </>
          ) : (
            <button disabled={busy} onClick={() => setConfirmDelete(true)}
              className="text-xs px-2.5 py-1 rounded border border-gray-800 text-gray-500 hover:border-red-800 hover:text-red-400 disabled:opacity-50 cursor-pointer">
              Delete
            </button>
          )}
        </div>
      </div>

      {testing && (
        <form onSubmit={runTest} className="mt-2.5 flex items-center gap-2">
          <input
            type="password"
            autoFocus
            placeholder="Account password"
            value={testPassword}
            onChange={e => setTestPassword(e.target.value)}
            className="flex-1 max-w-xs bg-gray-950 border border-gray-700 rounded px-2.5 py-1.5 text-xs text-gray-100 focus:border-blue-500 outline-none"
          />
          <button type="submit" disabled={!testPassword || busy}
            className="text-xs px-2.5 py-1.5 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white cursor-pointer">
            Run test
          </button>
          <span className="text-[11px] text-gray-600">Shoonya logins can take up to ~2 min.</span>
        </form>
      )}

      {testResult && (
        <div className={`mt-2.5 text-xs px-2.5 py-1.5 rounded border ${
          testResult.success
            ? 'bg-green-950/50 border-green-800/60 text-green-300'
            : 'bg-red-950/60 border-red-800 text-red-300'
        }`}>
          {testResult.message}
        </div>
      )}
    </div>
  )
}

// Shared add/edit form. Add: blank, password-free. Edit: unlocks via password
// first (reveal), then edits the decrypted values in place.
function CredentialForm({
  mode,
  accountId,
  onDone,
  onCancel,
}: {
  mode: 'add' | 'edit'
  accountId?: number
  onDone: () => void
  onCancel: () => void
}) {
  const [unlocked, setUnlocked] = useState(mode === 'add')
  const [password, setPassword] = useState('')
  const [unlockBusy, setUnlockBusy] = useState(false)
  const [unlockError, setUnlockError] = useState<string | null>(null)

  const [broker, setBroker] = useState<BrokerName>('shoonya')
  const [label, setLabel] = useState('')
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestResult | null>(null)

  async function handleUnlock(e: FormEvent) {
    e.preventDefault()
    if (accountId === undefined) return
    setUnlockError(null)
    setUnlockBusy(true)
    try {
      const res = await revealCredentials(accountId, password)
      if (isBrokerName(res.broker_name)) setBroker(res.broker_name)
      setLabel(res.label)
      const next: Record<string, string> = {}
      for (const [k, v] of Object.entries(res.credentials)) next[k] = String(v ?? '')
      setValues(next)
      setUnlocked(true)
      setPassword('')
    } catch (err) {
      setUnlockError(err instanceof Error ? err.message : 'Failed to unlock credentials')
    } finally {
      setUnlockBusy(false)
    }
  }

  function setField(key: string, v: string) {
    setValues(prev => ({ ...prev, [key]: v }))
    setTestResult(null)
  }

  function buildCredentials(): Record<string, string> {
    const credentials: Record<string, string> = {}
    for (const f of BROKER_FIELDS[broker]) credentials[f.key] = (values[f.key] || '').trim()
    return credentials
  }

  async function handleTest() {
    setTesting(true)
    setTestResult(null)
    try {
      const res = await testCredentials(broker, buildCredentials())
      setTestResult(res)
    } catch (err) {
      setTestResult({ success: false, message: err instanceof Error ? err.message : 'Test failed' })
    } finally {
      setTesting(false)
    }
  }

  async function handleSave(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const credentials = buildCredentials()
      if (mode === 'add') {
        await saveCredentials(broker, credentials, label.trim() || undefined)
      } else if (accountId !== undefined) {
        await updateCredentials(accountId, broker, credentials, label.trim() || undefined)
      }
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save credentials')
    } finally {
      setBusy(false)
    }
  }

  if (!unlocked) {
    return (
      <form onSubmit={handleUnlock} className="space-y-4 max-w-sm">
        <p className="text-xs text-gray-500 leading-relaxed">
          This account's credentials are encrypted at rest. Enter your app password to view and edit them.
        </p>
        {unlockError && (
          <div className="bg-red-950/60 border border-red-800 text-red-300 text-xs px-3 py-2 rounded">{unlockError}</div>
        )}
        <label className="block">
          <span className="text-xs text-gray-400">Account password</span>
          <input
            type="password"
            autoFocus
            value={password}
            onChange={e => setPassword(e.target.value)}
            className="mt-1 w-full bg-gray-950 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
          />
        </label>
        <div className="flex items-center gap-2">
          <button
            type="submit"
            disabled={unlockBusy || !password}
            className="px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium cursor-pointer"
          >
            {unlockBusy ? 'Unlocking…' : 'Unlock'}
          </button>
          <button type="button" onClick={onCancel}
            className="text-xs px-3 py-2 rounded border border-gray-700 text-gray-400 hover:text-gray-200 cursor-pointer">
            Cancel
          </button>
        </div>
      </form>
    )
  }

  return (
    <form onSubmit={handleSave} className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500">
          {mode === 'add' ? 'New broker account.' : "Editing decrypted credentials — changes replace the stored values."}
        </p>
        <button type="button" onClick={onCancel}
          className="text-xs px-2.5 py-1 rounded border border-gray-700 text-gray-400 hover:text-gray-200 cursor-pointer">
          Cancel
        </button>
      </div>

      {error && (
        <div className="bg-red-950/60 border border-red-800 text-red-300 text-xs px-3 py-2 rounded">{error}</div>
      )}
      {testResult && (
        <div className={`text-xs px-3 py-2 rounded border ${
          testResult.success
            ? 'bg-green-950/50 border-green-800/60 text-green-300'
            : 'bg-red-950/60 border-red-800 text-red-300'
        }`}>
          {testResult.message}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="block">
          <span className="text-xs text-gray-400">Broker</span>
          <select
            value={broker}
            disabled={mode === 'edit'}
            onChange={e => { if (isBrokerName(e.target.value)) { setBroker(e.target.value); setValues({}); setTestResult(null) } }}
            className="mt-1 w-full bg-gray-950 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
          >
            <option value="shoonya">Shoonya</option>
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-gray-400">Label</span>
          <input
            type="text"
            placeholder={`${broker} account`}
            value={label}
            onChange={e => setLabel(e.target.value)}
            className="mt-1 w-full bg-gray-950 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none"
          />
        </label>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {BROKER_FIELDS[broker].filter(f => f.type !== 'checkbox').map(f => (
          <CredentialInput
            key={f.key}
            label={f.label}
            secret={f.type === 'password'}
            value={values[f.key] || ''}
            onChange={v => setField(f.key, v)}
          />
        ))}
      </div>

      {BROKER_FIELDS[broker].filter(f => f.type === 'checkbox').map(f => (
        <label key={f.key} className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer">
          <input
            type="checkbox"
            checked={values[f.key] === 'on'}
            onChange={e => setField(f.key, e.target.checked ? 'on' : '')}
            className="accent-blue-500 cursor-pointer"
          />
          {f.label}
        </label>
      ))}

      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={busy}
          className="px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium cursor-pointer"
        >
          {busy ? 'Saving…' : mode === 'add' ? 'Save account' : 'Save changes'}
        </button>
        <button
          type="button"
          disabled={testing}
          onClick={handleTest}
          className="px-4 py-2 rounded border border-gray-700 text-gray-300 hover:border-gray-500 hover:text-gray-100 disabled:opacity-50 text-sm font-medium cursor-pointer"
        >
          {testing ? 'Testing…' : 'Test'}
        </button>
        <span className="text-[11px] text-gray-600">Shoonya logins can take up to ~2 min.</span>
      </div>
    </form>
  )
}

// Text input with an eye toggle for secret fields (masked by default).
function CredentialInput({
  label,
  value,
  onChange,
  secret,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  secret: boolean
}) {
  const [show, setShow] = useState(false)
  return (
    <label className="block">
      <span className="text-xs text-gray-400">{label}</span>
      <div className="relative mt-1">
        <input
          type={secret && !show ? 'password' : 'text'}
          value={value}
          onChange={e => onChange(e.target.value)}
          className={`w-full bg-gray-950 border border-gray-700 rounded px-3 py-2 ${secret ? 'pr-10' : ''} text-sm text-gray-100 focus:border-blue-500 focus:outline-none`}
        />
        {secret && (
          <button
            type="button"
            onClick={() => setShow(s => !s)}
            aria-label={show ? 'Hide' : 'Show'}
            className="absolute inset-y-0 right-0 px-3 flex items-center text-gray-500 hover:text-gray-300 cursor-pointer"
          >
            {show ? (
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.98 8.223A10.477 10.477 0 0 0 1.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.451 10.451 0 0 1 12 4.5c4.756 0 8.773 3.162 10.065 7.498a10.522 10.522 0 0 1-4.293 5.774M6.228 6.228 3 3m3.228 3.228 3.65 3.65m7.894 7.894L21 21m-3.228-3.228-3.65-3.65m0 0a3 3 0 1 0-4.243-4.243m4.242 4.242L9.88 9.88" />
              </svg>
            ) : (
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M2.036 12.322a1.012 1.012 0 0 1 0-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178Z" />
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
              </svg>
            )}
          </button>
        )}
      </div>
    </label>
  )
}
