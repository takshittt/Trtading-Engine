import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Signal, Summary, SearchResult, PreviousGroup, Position } from '../types'
import { api } from '../api'
import { Badge } from './ui'

/** Essential = actionable only. All = every signal, nothing hidden. */
type Scope = 'essential' | 'all'
import { ColumnCells, ColumnHead, ColumnPicker, useColumns, useSort, sortRows, type SortState } from './ColumnPicker'
import { SIGNAL_COLUMNS, rowTint, type SignalCtx } from './signalColumns'

function SearchAdd({ onAdded }: { onAdded: () => void }) {
  const [q, setQ] = useState('')
  const [results, setResults] = useState<SearchResult[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (q.trim().length < 2) { setResults([]); return }
    setLoading(true)
    const t = setTimeout(() => api.search(q.trim()).then(r => { setResults(r); setOpen(true); setLoading(false) }).catch(() => setLoading(false)), 300)
    return () => clearTimeout(t)
  }, [q])

  useEffect(() => {
    const h = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', h); return () => document.removeEventListener('mousedown', h)
  }, [])

  // Add a manually-selected stock → becomes a MANUAL signal in Today's feed.
  const add = async (r: SearchResult) => {
    setBusy(r.tsym)
    await api.manualAdd({ symbol: r.tsym, signal_type: 'BUY', lot_size: r.lot_size, expiry: r.expiry })
    setBusy(''); setQ(''); setResults([]); setOpen(false); onAdded()
  }

  return (
    <div ref={box} className="relative">
      <div className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5">
        <input value={q} onChange={e => setQ(e.target.value)} onFocus={() => results.length && setOpen(true)}
          placeholder="Search futures / options to add… (RELIANCE, NIFTY)"
          className="w-56 bg-transparent text-xs text-slate-700 outline-none placeholder:text-slate-400" />
      </div>
      {open && (
        <div className="absolute right-0 z-30 mt-1 max-h-96 w-96 overflow-auto rounded-xl border border-slate-200 bg-white p-1.5 shadow-xl">
          <div className="px-2.5 pb-1 pt-1 text-[10px] font-bold uppercase tracking-wide text-slate-400">Add to Today's Manual Signals</div>
          {loading && <div className="px-3 py-2 text-xs text-slate-400">Searching Shoonya…</div>}
          {!loading && results.length === 0 && q.length >= 2 && (
            <div className="px-3 py-2 text-xs text-slate-400">No matches. Try a root name (RELIANCE, NIFTY, BANKNIFTY).</div>
          )}
          {results.map(r => (
            <div key={`${r.exch}|${r.token}|${r.tsym}`} className="flex items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 hover:bg-slate-50">
              <div className="min-w-0">
                <div className="truncate text-[12px] font-semibold text-slate-800">{r.tsym}</div>
                <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[10px] text-slate-400">
                  <Badge className="border-sky-200 bg-sky-50 text-sky-700">{r.exch}</Badge>
                  <Badge className={r.instr_type === 'OPT' ? 'border-violet-200 bg-violet-50 text-violet-700' : 'border-slate-200 bg-slate-50 text-slate-500'}>{r.instr_type}{r.opttype ? ` ${r.opttype}` : ''}{r.strike ? ` ${r.strike}` : ''}</Badge>
                  <span>exp {r.expiry || '—'}</span>
                  <span>lot {r.lot_size}</span>
                </div>
              </div>
              <button disabled={!!busy} onClick={() => add(r)}
                className="rounded-lg bg-sky-600 px-3.5 py-1 text-[11px] font-bold text-white hover:bg-sky-700 disabled:opacity-40">
                {busy === r.tsym ? '…' : '+ Add'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function DayGroup({ group, defaultOpen, cols, ctx, sort, onSort, matches }: {
  group: PreviousGroup; defaultOpen: boolean
  cols: typeof SIGNAL_COLUMNS; ctx: SignalCtx
  sort: SortState | null; onSort: (key: string) => void
  matches: (s: Signal) => boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  // Same predicate as Today's feed — timeframe, direction and Essential/All all
  // have to mean the same thing on both tabs.
  const rows = group.signals.filter(matches)
  if (rows.length === 0) return null
  const sortedRows = sortRows(rows, SIGNAL_COLUMNS, sort)
  // These are still tradable: LTP / Target / SL are re-priced live by the backend.
  const open_ = rows.filter(s => ['NEW', 'AVERAGING'].includes(s.status)).length
  return (
    <div className="mb-2 overflow-hidden rounded-xl border border-slate-200">
      <button onClick={() => setOpen(o => !o)}
        className="flex w-full items-center justify-between bg-slate-50 px-4 py-2.5 text-left hover:bg-slate-100">
        <span className="flex items-center gap-2 text-sm font-bold text-slate-700">
          <span className={`transition-transform ${open ? 'rotate-90' : ''}`}>▸</span>
          {new Date(group.date).toLocaleDateString('en-IN', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' })}
        </span>
        <span className="flex items-center gap-2 text-[11px] font-medium text-slate-400">
          {open_ > 0 && <Badge className="border-emerald-200 bg-emerald-50 text-emerald-700">{open_} still actionable</Badge>}
          {rows.length} signal{rows.length > 1 ? 's' : ''}
        </span>
      </button>
      {open && (
        <div className="overflow-auto">
          <table className="w-full text-left text-[11px]">
            <ColumnHead cols={cols} sort={sort} onSort={onSort} />
            <tbody>
              {sortedRows.map(s => (
                <tr key={s.id} className={`border-b border-slate-50 ${rowTint(s)}`}>
                  <ColumnCells cols={cols} row={s} ctx={ctx} />
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export function SignalPanel({ signals, summary, positions, onBuy, onSell, onStock, refresh, tick }: {
  signals: Signal[]
  summary: Summary | null
  positions: Position[]
  onBuy: (s: Signal) => void
  onSell: (p: Position) => void
  onStock: (symbol: string) => void
  refresh: () => void
  tick: number
}) {
  const [view, setView] = useState<'today' | 'previous'>('today')
  const [scope, setScope] = useState<Scope>('essential')
  const [tf, setTf] = useState('ALL')
  const [type, setType] = useState('ALL')
  const [prev, setPrev] = useState<PreviousGroup[]>([])
  const [prevLoaded, setPrevLoaded] = useState(false)
  const [prevErr, setPrevErr] = useState(false)

  // Previous signals are live too (re-priced Target/SL), so keep them polling
  // on the same cadence as Today's feed while that tab is open. One request in
  // flight at a time — a slow backend must not stack up polls — and a failure
  // has to surface, or an empty list reads as "nothing stored" when it really
  // means "never arrived".
  useEffect(() => {
    if (view !== 'previous') return
    let alive = true
    let inFlight = false
    const load = () => {
      if (inFlight) return
      inFlight = true
      api.previousSignals()
        .then(g => { if (alive) { setPrev(g); setPrevErr(false); setPrevLoaded(true) } })
        .catch(() => { if (alive) setPrevErr(true) })
        .finally(() => { inFlight = false })
    }
    load()
    const iv = setInterval(load, 5000)
    return () => { alive = false; clearInterval(iv) }
  }, [view, tick])

  // Live book only: the Buy/Average button trades real money, so a simulated
  // holding must not relabel it "Average" when a click would open a fresh
  // live position. Paper positions still show in the Open Positions table.
  const held = useMemo(
    () => new Map(positions.filter(p => p.status === 'OPEN' && !p.is_paper).map(p => [p.symbol, p])),
    [positions])

  // Every stock currently on the book, paper included. A SELL is an exit
  // instruction, and whether it is worth reading depends on holding the stock —
  // not on which book it sits in.
  const openSymbols = useMemo(
    () => new Set(positions.filter(p => p.status === 'OPEN').map(p => p.symbol)),
    [positions])

  // ESSENTIAL = only what can be acted on right now. A BUY that failed the
  // reward:risk gate is not tradable, and a SELL on a stock that isn't held has
  // nothing to close — this is a long-only strategy, so it is pure noise.
  //
  // The backend owns this rule (signals.is_essential) and stamps it on every
  // listed row, because automated execution trades exactly this set — the list
  // on screen has to be the same list the machine acts on, and two copies of the
  // predicate would eventually stop agreeing. The local fallback covers rows
  // that arrive by websocket broadcast, which carry no open-book context.
  const isEssential = useCallback((s: Signal) => (
    s.essential ?? (s.signal_type === 'SELL' ? openSymbols.has(s.symbol) : s.status !== 'LOW_RR')
  ), [openSymbols])

  const matches = useCallback((s: Signal) => (
    (tf === 'ALL' || s.timeframe === tf) &&
    (type === 'ALL' || s.signal_type === type) &&
    (scope === 'all' || isEssential(s))
  ), [tf, type, scope, isEssential])

  const todayRows = useMemo(() => signals.filter(matches), [signals, matches])
  const mutedCount = useMemo(
    () => signals.filter(s => !isEssential(s)).length, [signals, isEssential])

  const hardCap = summary?.budget.cap_state === 'HARD'

  // One column choice covers both tabs — the two show the same kind of row, so
  // configuring them separately would just be two places to keep in sync.
  const { visible, hidden, toggle, reset } = useColumns<Signal, SignalCtx>('signals', SIGNAL_COLUMNS)
  const { sort, toggle: toggleSort } = useSort('signals')
  const ctx: SignalCtx = { hardCap, held, onBuy, onSell, onStock }

  const sortedToday = useMemo(() => sortRows(todayRows, SIGNAL_COLUMNS, sort), [todayRows, sort])

  return (
    <section className="flex h-full flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-3 py-1.5">
        <div className="flex items-center gap-1 rounded-lg bg-slate-100 p-0.5">
          <button onClick={() => setView('today')}
            className={`rounded-md px-3 py-1 text-xs font-bold ${view === 'today' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'}`}>
            Today's Signals {view === 'today' && <span className="text-slate-400">({todayRows.length})</span>}
          </button>
          <button onClick={() => setView('previous')}
            className={`rounded-md px-3 py-1 text-xs font-bold ${view === 'previous' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'}`}>
            Previous Signals
          </button>
        </div>
        <div className="flex gap-1 rounded-lg bg-slate-100 p-0.5">
          <button onClick={() => setScope('essential')}
            title="Buys that cleared the reward:risk minimum, and sells only for stocks you hold"
            className={`rounded-md px-2.5 py-1 text-[11px] font-bold ${scope === 'essential' ? 'bg-white text-emerald-700 shadow-sm' : 'text-slate-500'}`}>
            Essential
          </button>
          <button onClick={() => setScope('all')}
            title="Everything, including low reward:risk buys and sells on stocks you do not hold"
            className={`rounded-md px-2.5 py-1 text-[11px] font-bold ${scope === 'all' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'}`}>
            All{scope === 'essential' && mutedCount > 0 && (
              <span className="ml-1 rounded bg-slate-200 px-1 text-[9px] text-slate-600">+{mutedCount}</span>
            )}
          </button>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex gap-1 rounded-lg bg-slate-100 p-0.5">
            {['ALL', '1H', '4H', '1D'].map(t => (
              <button key={t} onClick={() => setTf(t)}
                className={`rounded-md px-2 py-0.5 text-[11px] font-semibold ${tf === t ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'}`}>{t}</button>
            ))}
          </div>
          <div className="flex gap-1 rounded-lg bg-slate-100 p-0.5">
            {['ALL', 'BUY', 'SELL'].map(t => (
              <button key={t} onClick={() => setType(t)}
                className={`rounded-md px-2 py-0.5 text-[11px] font-semibold ${type === t ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'}`}>{t}</button>
            ))}
          </div>
          <SearchAdd onAdded={refresh} />
          <ColumnPicker defs={SIGNAL_COLUMNS} hidden={hidden} onToggle={toggle} onReset={reset}
            label="Signal columns" />
        </div>
      </div>

      <div className="flex-1 overflow-auto">
        {view === 'today' ? (
          <table className="w-full text-left text-[11px]">
            <ColumnHead cols={visible} sort={sort} onSort={toggleSort} />
            <tbody>
              {todayRows.length === 0 && (
                <tr><td colSpan={visible.length} className="px-3 py-10 text-center text-slate-400">Waiting for signals… (use Search &amp; Add)</td></tr>
              )}
              {sortedToday.map(s => (
                <tr key={s.id} className={`border-b border-slate-50 ${rowTint(s)}`}>
                  <ColumnCells cols={visible} row={s} ctx={ctx} />
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="p-3">
            {prev.length === 0 && (
              <div className="py-12 text-center text-slate-400">
                {prevErr
                  ? <>Couldn’t reach the backend for previous signals — retrying…</>
                  : prevLoaded ? 'No previous signals stored yet.' : 'Loading previous signals…'}
              </div>
            )}
            {prev.length > 0 && (
              <p className="mb-2 px-1 text-[11px] text-slate-400">
                Previous signals stay tradable — LTP, Target and SL are re-priced live, and a stock you already hold is offered as an averaging buy.
              </p>
            )}
            {prev.map((g, i) => (
              <DayGroup key={g.date} group={g}
                defaultOpen={i === 0} cols={visible} ctx={ctx}
                sort={sort} onSort={toggleSort} matches={matches} />
            ))}
          </div>
        )}
      </div>
    </section>
  )
}
