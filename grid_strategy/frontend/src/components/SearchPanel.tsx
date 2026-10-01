import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { InstrumentSnap, SearchResult } from '../types'
import { Badge, Card, Spinner } from './ui'

/** Discovery panel — Zerodha-style tokenized search over MCX / NFO FUTURES
 * ("naturalgas jul", "nifty aug", "crudeoil"). Every result can be added to the
 * single shared watchlist EITHER as an automated-signal stock or as a
 * manual-ladder stock (two buttons); the chosen mode drives its strategy. */
export function SearchPanel({ onAdded, instruments, onCollapse }: {
  onAdded: () => void
  instruments: InstrumentSnap[]
  onCollapse?: () => void
}) {
  const [q, setQ] = useState('')
  const [exch, setExch] = useState<'ALL' | 'MCX' | 'NFO'>('ALL')
  const [results, setResults] = useState<SearchResult[]>([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [addingKey, setAddingKey] = useState('')      // `${token}|${mode}` in flight
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current)
    if (q.trim().length < 2) { setResults([]); return }
    timer.current = setTimeout(async () => {
      setLoading(true)
      setErr('')
      try {
        const r = await api.search(q.trim(), exch)
        setResults(r.results)
      } catch (e) {
        setErr(e instanceof Error ? e.message : 'search failed')
      } finally {
        setLoading(false)
      }
    }, 300)
  }, [q, exch])

  /** already in the watchlist for a given mode? futures match on the root symbol. */
  const isWatched = (r: SearchResult, mode: 'auto' | 'ladder'): boolean => {
    const root = (r.sym || r.tsym).toUpperCase().split(' ')[0]
    return instruments.some((i) =>
      (i.mode ?? 'auto') === mode && i.exch === r.exch &&
      i.instr_type !== 'OPT' && (i.sym === root || i.underlying === root))
  }

  const add = async (r: SearchResult, mode: 'auto' | 'ladder') => {
    setAddingKey(`${r.token}|${mode}`)
    setErr('')
    try {
      await api.addInstrument({
        sym: r.sym || r.tsym, exch: r.exch, tsym: r.tsym, token: r.token,
        lot_size: parseInt(r.lotsize || '1') || 1, expiry: r.expd, mode,
        instr_type: 'FUT',
      })
      onAdded()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'add failed')
    } finally {
      setAddingKey('')
    }
  }

  const AddBtn = ({ r, mode, label, tone }: {
    r: SearchResult; mode: 'auto' | 'ladder'; label: string; tone: 'sky' | 'violet'
  }) => {
    const watched = isWatched(r, mode)
    const busy = addingKey === `${r.token}|${mode}`
    const cls = tone === 'sky'
      ? 'border-sky-500 text-sky-700 hover:bg-sky-500/10'
      : 'border-violet-600 text-violet-700 hover:bg-violet-500/10'
    return (
      <button
        disabled={watched || busy}
        onClick={() => add(r, mode)}
        title={watched ? `already in the ${mode === 'auto' ? 'automated' : 'ladder'} watchlist` : `add as ${label}`}
        className={`shrink-0 px-2 py-1 rounded-md text-[11px] font-semibold border
          ${watched ? 'border-gray-200 text-gray-500 cursor-default' : cls}`}>
        {watched ? '✓ ' + label : busy ? '…' : '+ ' + label}
      </button>
    )
  }

  return (
    <Card title="Add Instruments (futures)" className="h-full flex flex-col"
      right={
        <div className="flex items-center gap-1">
          {(['ALL', 'MCX', 'NFO'] as const).map((e) => (
            <button key={e} onClick={() => setExch(e)}
              className={`px-2 py-0.5 rounded text-[11px] font-medium border
                ${exch === e ? 'bg-sky-500/20 border-sky-500 text-sky-700' : 'border-gray-300 text-gray-600 hover:text-gray-800'}`}>
              {e}
            </button>
          ))}
          {onCollapse && (
            <button onClick={onCollapse} title="minimize search — give the watchlist more room"
              className="ml-1 px-1.5 py-0.5 rounded border border-gray-300 text-gray-600 hover:border-sky-500 hover:text-sky-700 text-xs leading-none">
              ◀
            </button>
          )}
        </div>
      }>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search futures… NATURALGAS · CRUDEOIL JUL · NIFTY AUG"
        className="w-full bg-gray-50 border border-gray-300 rounded-lg px-3 py-2 text-sm
          placeholder:text-gray-500 focus:outline-none focus:border-sky-500"
      />
      <div className="text-[10px] text-gray-500 mt-1">
        Type words in any order (e.g. <span className="text-gray-600">crudeoil jul</span>). Add each result as{' '}
        <span className="text-sky-700">Automated</span> or <span className="text-violet-700">Ladder</span>.
      </div>
      {err && <div className="text-red-600 text-xs mt-2">{err}</div>}
      <div className="mt-2 overflow-y-auto flex-1 min-h-0 space-y-1" style={{ maxHeight: '360px' }}>
        {loading && <div className="p-2 text-gray-500 text-xs flex items-center gap-2"><Spinner /> searching…</div>}
        {!loading && q.length >= 2 && results.length === 0 && (
          <div className="p-2 text-gray-500 text-xs">No futures found. Try a root name (NIFTY, NATURALGAS, CRUDEOIL) with or without a month.</div>
        )}
        {results.map((r) => (
          <div key={`${r.exch}|${r.token}`} className="flex items-center justify-between gap-2 px-2 py-1.5 rounded-lg
            bg-gray-50 border border-gray-200 hover:border-gray-300">
            <div className="min-w-0">
              <div className="text-[12px] text-gray-900 font-medium leading-tight break-all">{r.tsym}</div>
              <div className="text-[11px] text-gray-500 flex gap-2 items-center flex-wrap mt-0.5">
                <Badge tone={r.exch === 'MCX' ? 'amber' : 'blue'}>{r.exch}</Badge>
                <Badge tone="gray">FUT</Badge>
                <span>exp {r.expd || '—'}</span>
                <span>lot {r.lotsize || '1'}</span>
              </div>
            </div>
            <div className="flex gap-1 shrink-0">
              <AddBtn r={r} mode="auto" label="Auto" tone="sky" />
              <AddBtn r={r} mode="ladder" label="Ladder" tone="violet" />
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}
