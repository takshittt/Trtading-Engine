import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { EconEventRow } from '../types'
import { Badge, Card } from './ui'

const impactTone = (i: string): 'red' | 'amber' | 'gray' => (i === 'high' ? 'red' : i === 'medium' ? 'amber' : 'gray')

/** Events / market-regime panel: upcoming macro data (EIA, expiries, roll
 * days) for this week + next — the context behind the regime. */
export function EventsPanel() {
  const [events, setEvents] = useState<EconEventRow[]>([])
  const [adding, setAdding] = useState(false)

  const load = useCallback(() => { api.events(14).then(setEvents).catch(() => {}) }, [])
  useEffect(() => {
    load()
    const t = setInterval(load, 60000)
    return () => clearInterval(t)
  }, [load])

  const fmt = (iso: string) => {
    const d = new Date(iso)
    return `${d.toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' })} ${d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false })}`
  }

  return (
    <Card title="Events / Market Regime (current → next week)"
      right={<button onClick={() => setAdding(true)}
        className="px-2 py-1 rounded border border-gray-300 text-gray-700 hover:border-sky-500 hover:text-sky-700 text-[11px]">+ Add</button>}>
      <div className="space-y-1 max-h-56 overflow-y-auto">
        {events.length === 0 && <div className="text-gray-500 text-xs px-2 py-3">No upcoming events in the next 14 days.</div>}
        {events.map((e) => (
          <div key={e.id} className="flex items-start gap-2 px-2 py-1 rounded hover:bg-gray-100 text-[12px] group">
            <span className="text-gray-500 font-mono shrink-0 w-32">{fmt(e.dt)}</span>
            <Badge tone={impactTone(e.impact)}>{e.impact}</Badge>
            <Badge tone="gray">{e.region}</Badge>
            <div className="min-w-0">
              <span className="text-gray-800">{e.title}</span>
              {e.note && <div className="text-[11px] text-gray-500">{e.note}</div>}
            </div>
            {!e.auto && (
              <button onClick={() => api.deleteEvent(e.id).then(load)}
                className="ml-auto opacity-0 group-hover:opacity-100 text-gray-500 hover:text-red-600 text-xs">✕</button>
            )}
          </div>
        ))}
      </div>
      {adding && <AddEventModal onClose={() => setAdding(false)} onSaved={load} />}
    </Card>
  )
}

/** The Events / Market-Regime panel shown as a modal (opened from the bottom
 * "Events / Regime" button). */
export function EventsModal({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 bg-gray-900/50 flex items-start justify-center p-4 overflow-y-auto" onClick={onClose}>
      <div className="w-full max-w-2xl mt-10" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm font-semibold text-gray-800">Events / Market Regime</span>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-800 text-2xl leading-none px-1">×</button>
        </div>
        <EventsPanel />
      </div>
    </div>
  )
}

function AddEventModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ dt: '', title: '', impact: 'medium', region: 'IN', note: '' })
  const [err, setErr] = useState('')
  const inputCls = 'w-full bg-gray-50 border border-gray-300 rounded-md px-2 py-1.5 text-sm mt-1 focus:outline-none focus:border-sky-500'

  const save = async () => {
    try {
      if (!form.dt || !form.title) { setErr('date-time and title are required'); return }
      await api.addEvent(form)
      onSaved()
      onClose()
    } catch (e) { setErr(e instanceof Error ? e.message : 'failed') }
  }

  return (
    <div className="fixed inset-0 z-50 bg-gray-900/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white border border-gray-300 rounded-2xl w-full max-w-sm p-5" onClick={(e) => e.stopPropagation()}>
        <div className="font-semibold text-gray-900 mb-3">Add economic event</div>
        <label className="text-[11px] uppercase text-gray-500">When (IST)</label>
        <input type="datetime-local" value={form.dt} onChange={(e) => setForm({ ...form, dt: e.target.value })} className={inputCls} />
        <label className="text-[11px] uppercase text-gray-500 mt-3 block">Title</label>
        <input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="RBI MPC decision" className={inputCls} />
        <div className="grid grid-cols-2 gap-3 mt-3">
          <div>
            <label className="text-[11px] uppercase text-gray-500">Impact</label>
            <select value={form.impact} onChange={(e) => setForm({ ...form, impact: e.target.value })} className={inputCls}>
              <option>low</option><option>medium</option><option>high</option>
            </select>
          </div>
          <div>
            <label className="text-[11px] uppercase text-gray-500">Region</label>
            <select value={form.region} onChange={(e) => setForm({ ...form, region: e.target.value })} className={inputCls}>
              <option>IN</option><option>US</option><option>EU</option><option>CN</option>
            </select>
          </div>
        </div>
        <label className="text-[11px] uppercase text-gray-500 mt-3 block">Note</label>
        <input value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} className={inputCls} />
        {err && <div className="text-red-600 text-xs mt-2">{err}</div>}
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} className="px-3 py-1.5 rounded-lg border border-gray-300 text-gray-700 text-sm">Cancel</button>
          <button onClick={save} className="px-4 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-semibold">Add</button>
        </div>
      </div>
    </div>
  )
}
