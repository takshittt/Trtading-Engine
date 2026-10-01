import { useState } from 'react'
import { api } from '../api'
import type { InstrumentSnap } from '../types'
import { ConfigFields, configFormFromInst } from './ConfigFields'

/** Per-instrument strategy configuration — everything configurable lives here
 * (target/SL in points or %, volatility sizing, circuit breaker, rollover).
 * The field grid itself is shared with the manual-ladder modal (ConfigFields). */
export function ConfigModal({ inst, onClose, onSaved }: {
  inst: InstrumentSnap
  onClose: () => void
  onSaved: () => void
}) {
  const [form, setForm] = useState(() => configFormFromInst(inst))
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')

  const set = (k: string, v: unknown) => setForm((f) => ({ ...f, [k]: v }))

  const save = async () => {
    setSaving(true)
    setErr('')
    try {
      // For ladder instruments the target/SL offsets are owned by the LEVELS
      // view (first-buy target/SL on Confirm & arm) — never send them from
      // here, or a fresh instrument's 0 offset trips the backend validation
      // in the wrong dialog.
      // rollover mode/date is owned by the watchlist rollover cell — never
      // send it from here or a stale form would clobber a pill/drawer change
      let payload: Record<string, unknown> = { ...form }
      delete payload.rollover_date_override
      if (inst.mode === 'ladder') {
        const { target_value: _tv, target_mode: _tm, sl_value: _sv, sl_mode: _sm, ...rest } = payload
        payload = rest
      }
      await api.updateInstrument(inst.id, payload)
      onSaved()
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'save failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-gray-900/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white border border-gray-300 rounded-2xl w-full max-w-2xl max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}>
        <header className="flex items-center justify-between px-5 py-3 border-b border-gray-200 sticky top-0 bg-white">
          <div>
            <div className="font-semibold text-gray-900">{inst.sym} — strategy config</div>
            <div className="text-[11px] text-gray-500">{inst.tsym} · {inst.exch} · lot size {inst.lot_size}</div>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-800 text-xl leading-none">×</button>
        </header>

        <div className="p-5">
          <ConfigFields form={form} set={set} isLadder={inst.mode === 'ladder'} />
        </div>

        <footer className="flex items-center justify-between px-5 py-3 border-t border-gray-200 sticky bottom-0 bg-white">
          <span className="text-red-600 text-xs">{err}</span>
          <div className="flex gap-2">
            <button onClick={onClose} className="px-3 py-1.5 rounded-lg border border-gray-300 text-gray-700 text-sm">Cancel</button>
            <button onClick={save} disabled={saving}
              className="px-4 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-semibold disabled:opacity-50">
              {saving ? 'Saving…' : 'Save config'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}
