import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { InstrumentSnap, LotRow, Snapshot, SortMode } from '../types'
import { Badge, Card, fmtPx, fmtTime, inr, pnlClass } from './ui'

/** Open / Closed trades — every buy is an INDEPENDENT row (N1, N2, N3…),
 * never averaged, each with its own entry, target, SL and P&L.
 *
 * Temp Exit / Re-enter: a rung can be manually PAUSED (sold at broker to stop
 * the bleeding) and later RE-ENTERED lower on the SAME row — the loss carried
 * during the pause is folded into the new basis so the economics are preserved.
 * Paused rows stay in the Open tab, faded. */

// optional columns the user can show/hide (persisted). '#','Contract','Actions'
// are always shown. 'ltp'/'temp' are open-tab only; 'exit' is closed-tab only.
const OPTIONAL_COLS: { key: string; label: string }[] = [
  { key: 'lots', label: 'Lots' },
  { key: 'buy', label: 'Buy price' },
  { key: 'ltp', label: 'LTP' },
  { key: 'target', label: 'Target' },
  { key: 'stop', label: 'Stop' },
  { key: 'pnl', label: 'Profit/Loss' },
  { key: 'temp', label: 'Temp Exit / Re-enter' },
  { key: 'entry', label: 'Entry date (closed tab)' },
  { key: 'time', label: 'Since / Closed at' },
  { key: 'status', label: 'Status' },
]
const COLS_KEY = 'grid.tradeCols'

/** Shown while a tab's rows are still loading, so an in-flight fetch never reads
 * as "there is nothing here" — switching tabs used to claim no rungs existed for
 * a moment even when the tab was full. */
function Loading() {
  return (
    <span className="inline-flex items-center gap-2 text-gray-400">
      <span aria-hidden className="h-3.5 w-3.5 rounded-full border-2 border-current border-t-transparent animate-spin" />
      Loading…
    </span>
  )
}

export function TradesTable({ snapshot, onOpenChart }: {
  snapshot: Snapshot | null
  onOpenChart: (inst: InstrumentSnap, focusLot?: number) => void
}) {
  const [tab, setTab] = useState<'open' | 'pending' | 'closed'>('open')
  const [sort, setSort] = useState<SortMode>('latest')
  const [symFilter, setSymFilter] = useState('')
  const [sourceFilter, setSourceFilter] = useState<'' | 'automated' | 'ladder'>('')
  // Rows are stored WITH the query that produced them. Open and Pending are
  // different questions, so a response to one must never be painted under the
  // other's headers — keeping them in one object makes that mismatch
  // unrepresentable rather than something every write has to remember.
  const [data, setData] = useState<{ key: string; rows: LotRow[]; total: number }>(
    { key: '', rows: [], total: 0 })
  const [err, setErr] = useState('')
  const [busyLot, setBusyLot] = useState(0)
  const [editLot, setEditLot] = useState<LotRow | null>(null)
  const [cancelLot, setCancelLot] = useState<LotRow | null>(null)

  const queryKey = `${tab}|${sort}|${symFilter}|${sourceFilter}`
  const loaded = data.key === queryKey
  const rows = loaded ? data.rows : []
  const total = loaded ? data.total : null      // null → the tab shows '…' while loading

  const [visible, setVisible] = useState<Set<string>>(() => {
    try {
      const s = localStorage.getItem(COLS_KEY)
      if (s) return new Set(JSON.parse(s) as string[])
    } catch { /* ignore */ }
    return new Set(OPTIONAL_COLS.map((c) => c.key))
  })
  const show = (k: string) => visible.has(k)
  const toggleCol = (k: string) => {
    const s = new Set(visible)
    s.has(k) ? s.delete(k) : s.add(k)
    setVisible(s)
    try { localStorage.setItem(COLS_KEY, JSON.stringify([...s])) } catch { /* ignore */ }
  }

  /** Immediate refresh after an action (exit, cancel, edit). The response is
   * stamped with the query it was asked for, so if the user has moved on by
   * the time it lands it is simply not rendered. */
  const load = useCallback(async () => {
    const key = `${tab}|${sort}|${symFilter}|${sourceFilter}`
    try {
      const r = await api.lots(tab, sort, symFilter, sourceFilter)
      setData({ key, rows: r.lots, total: r.total })
      setErr('')
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'load failed')
    }
  }, [tab, sort, symFilter, sourceFilter])

  useEffect(() => {
    // Each query owns its own poll loop, and the cleanup tears it down:
    // `cancelled` stops a late reply from overwriting the query that replaced
    // it, abort() cancels the request outright, and `inFlight` keeps a reply
    // slower than the 1s timer from stacking up behind it — during market
    // hours those overlapping replies were landing out of order.
    let cancelled = false
    let inFlight = false
    const ac = new AbortController()

    const run = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const r = await api.lots(tab, sort, symFilter, sourceFilter, undefined, undefined,
                                 { signal: ac.signal })
        if (!cancelled) {
          setData({ key: queryKey, rows: r.lots, total: r.total })
          setErr('')
        }
      } catch (e) {
        // An abort is us cancelling on purpose, not a failure to report.
        if (!cancelled && (e as { name?: string })?.name !== 'AbortError') {
          setErr(e instanceof Error ? e.message : 'load failed')
        }
      } finally {
        inFlight = false
      }
    }

    run()
    const t = setInterval(run, tab === 'closed' ? 5000 : 1000)   // 1s live refresh on open + pending
    return () => { cancelled = true; ac.abort(); clearInterval(t) }
  }, [tab, sort, symFilter, sourceFilter, queryKey])

  const instFor = (lot: LotRow): InstrumentSnap | undefined =>
    snapshot?.instruments.find((i) => i.id === lot.instrument_id)

  const ltpFor = (lot: LotRow): number => {
    const inst = instFor(lot)
    return inst && inst.token === lot.contract_token ? inst.price.lp : 0
  }
  /** True when the shown price is the broker's last reported one rather than a
   * live tick — worth showing, but never worth passing off as live. */
  const ltpIsLastKnown = (lot: LotRow): boolean => {
    const inst = instFor(lot)
    return inst?.price.source === 'broker-last'
  }

  // the REAL price of the broker's pending order: the limit for RESTING lots,
  // the level price for legacy UNCONFIRMED rows (never a pre-trade estimate)
  const pendingPriceOf = (lot: LotRow): number =>
    lot.pending_order_at ?? (lot.status === 'UNCONFIRMED' && lot.level_price != null
      ? lot.level_price : lot.entry_price)

  // Where the ladder buys next if this resting order is skipped: the highest
  // PENDING level strictly below this order's limit price (from the snapshot).
  const nextLevelPriceFor = (lot: LotRow): number | null => {
    const levels = instFor(lot)?.ladder?.levels ?? []
    const below = levels.filter((lv) => lv.status === 'PENDING' && lv.price < pendingPriceOf(lot))
    return below.length ? Math.max(...below.map((lv) => lv.price)) : null
  }

  const exitNow = async (lot: LotRow) => {
    const msg = lot.status === 'TEMP_EXITED'
      ? `Permanently CLOSE paused rung ${lot.label}? (books the carried loss, no re-entry)`
      : `Market-exit rung ${lot.label} (${lot.lots} lot(s) ${lot.contract_tsym})?`
    if (!confirm(msg)) return
    setBusyLot(lot.id)
    try { await api.exitLot(lot.id); await load() }
    catch (e) { setErr(e instanceof Error ? e.message : 'exit failed') }
    finally { setBusyLot(0) }
  }

  const syms = [...new Set((snapshot?.instruments ?? []).map((i) => i.sym))]

  // column count for the empty-state colspan
  const openKeys = ['lots', 'buy', 'ltp', 'target', 'stop', 'pnl', 'temp', 'time', 'status']
  const closedKeys = ['lots', 'buy', 'target', 'stop', 'pnl', 'entry', 'time', 'status']
  const activeOptional = (tab === 'open' ? openKeys : closedKeys).filter(show)
  const colCount = 2 /* # + contract */ + activeOptional.length + (tab === 'closed' ? 1 : 0) /* exit */ + 1 /* actions */

  return (
    <Card
      title={
        <div className="flex items-center gap-3">
          <span>Trades</span>
          <div className="flex rounded-lg overflow-hidden border border-gray-300">
            {(['open', 'pending', 'closed'] as const).map((t) => (
              <button key={t} onClick={() => setTab(t)}
                className={`px-3 py-1 text-xs font-semibold ${tab === t ? 'bg-sky-500/20 text-sky-700' : 'text-gray-600 hover:text-gray-800'}`}>
                {t === 'open' ? `Open (${tab === 'open' ? total ?? '…' : '…'})`
                  : t === 'pending' ? `Pending (${tab === 'pending' ? total ?? '…' : '…'})`
                  : 'Closed'}
              </button>
            ))}
          </div>
        </div>
      }
      right={
        <div className="flex items-center gap-2">
          {err && <span className="text-red-600 text-xs">{err}</span>}
          {/* column visibility toggle */}
          <details className="relative">
            <summary className="cursor-pointer list-none px-2 py-1 rounded-md border border-gray-300 text-xs text-gray-700 hover:border-sky-500">
              Columns
            </summary>
            <div className="absolute right-0 mt-1 bg-white border border-gray-300 rounded-lg shadow-lg p-2 z-20 w-52">
              <div className="text-[10px] uppercase tracking-wide text-gray-400 px-1 pb-1">Show columns</div>
              {OPTIONAL_COLS.map((c) => (
                <label key={c.key} className="flex items-center gap-2 text-xs py-0.5 px-1 rounded hover:bg-gray-50 cursor-pointer">
                  <input type="checkbox" checked={show(c.key)} onChange={() => toggleCol(c.key)}
                    className="w-3.5 h-3.5 accent-sky-500" />
                  {c.label}
                </label>
              ))}
            </div>
          </details>
          <select value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value as '' | 'automated' | 'ladder')}
            className="bg-gray-50 border border-gray-300 rounded-md px-2 py-1 text-xs text-gray-700">
            <option value="">All signals</option>
            <option value="automated">Automated signal</option>
            <option value="ladder">Ladder manual</option>
          </select>
          <select value={symFilter} onChange={(e) => setSymFilter(e.target.value)}
            className="bg-gray-50 border border-gray-300 rounded-md px-2 py-1 text-xs text-gray-700">
            <option value="">All symbols</option>
            {syms.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select value={sort} onChange={(e) => setSort(e.target.value as SortMode)}
            className="bg-gray-50 border border-gray-300 rounded-md px-2 py-1 text-xs text-gray-700">
            <option value="latest">Sort: latest first</option>
            <option value="symbol">Sort: by stock (n1,n2… m1,m2…)</option>
            <option value="pnl">Sort: by P&L</option>
            <option value="entry_price">Sort: by entry price</option>
          </select>
        </div>
      }>
      <div className="overflow-x-auto">
        {tab === 'pending' ? (
          /* Pending tab — resting limit entries waiting at the broker. Fixed
           * column set (column toggles don't apply here). */
          <table className="w-full text-[13px]">
            <thead>
              <tr className="text-gray-500 text-[11px] uppercase tracking-wide">
                <th className="text-left px-2 py-1.5">#</th>
                <th className="text-left px-2">Contract</th>
                <th className="text-right px-2">Lots</th>
                <th className="text-right px-2">Pending order at</th>
                <th className="text-right px-2">Target</th>
                <th className="text-right px-2">SL</th>
                <th className="text-left px-2">Since</th>
                <th className="text-left px-2">Status</th>
                <th className="text-right px-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr><td colSpan={9} className="text-center text-gray-500 py-6 text-sm">
                  {!loaded ? <Loading /> :
                    'No pending orders — resting limit buys appear here as soon as a ladder level is placed at the broker.'}
                </td></tr>
              )}
              {rows.map((l) => (
                <tr key={l.id} className="border-t border-gray-200 hover:bg-gray-100">
                  <td className="px-2 py-1.5">
                    <span className="font-semibold text-gray-900">{l.label}</span>
                  </td>
                  <td className="px-2 text-gray-700 whitespace-nowrap">
                    <div className="flex items-center gap-1.5">
                      {l.contract_tsym}
                      {l.source === 'ladder'
                        ? <Badge tone="purple">LADDER</Badge>
                        : <Badge tone="blue">AUTO</Badge>}
                      {l.level_no != null && <Badge tone="purple">B{l.level_no}</Badge>}
                    </div>
                    <div className="text-[10px] text-gray-500">{l.sym} · {l.exch}</div>
                  </td>
                  <td className="px-2 text-right text-gray-800 font-mono">{l.lots}<div className="text-[10px] text-gray-500">{l.qty}u</div></td>
                  <td className="px-2 text-right font-mono tabular-nums text-gray-900">{fmtPx(pendingPriceOf(l))}</td>
                  <td className="px-2 text-right font-mono tabular-nums text-emerald-600">{fmtPx(l.target_price)}</td>
                  <td className="px-2 text-right font-mono tabular-nums text-red-600">{fmtPx(l.sl_price)}</td>
                  <td className="px-2 text-gray-600 whitespace-nowrap text-xs">{fmtTime(l.entry_time)}</td>
                  <td className="px-2">
                    {l.status === 'RESTING' && <Badge tone="amber">PENDING AT BROKER</Badge>}
                    {l.status === 'UNCONFIRMED' && <Badge tone="amber">PENDING AT BROKER</Badge>}
                  </td>
                  <td className="px-2">
                    <div className="flex justify-end gap-1">
                      <button onClick={() => setEditLot(l)}
                        className="px-2 py-0.5 rounded border border-gray-300 text-gray-700 hover:border-sky-500 hover:text-sky-700 text-[11px]">
                        Edit T/SL
                      </button>
                      <button onClick={() => setCancelLot(l)}
                        className="px-2 py-0.5 rounded border border-red-500 text-red-600 hover:bg-red-500/10 text-[11px]">
                        Cancel
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
        <table className="w-full text-[13px]">
          <thead>
            <tr className="text-gray-500 text-[11px] uppercase tracking-wide">
              <th className="text-left px-2 py-1.5">#</th>
              <th className="text-left px-2">Contract</th>
              {show('lots') && <th className="text-right px-2">Lots</th>}
              {show('buy') && <th className="text-right px-2">Buy at</th>}
              {tab === 'open' && show('ltp') && <th className="text-right px-2">LTP</th>}
              {show('target') && <th className="text-right px-2">Target</th>}
              {show('stop') && <th className="text-right px-2">Stop</th>}
              {tab === 'closed' && <th className="text-right px-2">Exit</th>}
              {show('pnl') && <th className="text-right px-2">Profit/Loss</th>}
              {tab === 'open' && show('temp') && <th className="text-center px-2">Temp Exit / Re-enter</th>}
              {tab === 'closed' && show('entry') && <th className="text-left px-2">Entry at</th>}
              {show('time') && <th className="text-left px-2">{tab === 'open' ? 'Since' : 'Closed at'}</th>}
              {show('status') && <th className="text-left px-2">Status</th>}
              <th className="text-right px-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={colCount} className="text-center text-gray-500 py-6 text-sm">
                {!loaded ? <Loading />
                  : tab === 'open' ? 'No open rungs — waiting for automated buy-the-dip signals or manual ladder levels to trigger.'
                  : 'No closed trades yet.'}
              </td></tr>
            )}
            {rows.map((l) => {
              const ltp = ltpFor(l)
              const paused = l.status === 'TEMP_EXITED'
              const live = l.live_pnl !== null ? l.live_pnl : (l.status === 'OPEN' && ltp > 0 ? (ltp - l.entry_price) * l.qty + l.realized_pnl : null)
              const pnl = tab === 'open' ? live : l.realized_pnl
              const inst = instFor(l)
              return (
                <tr key={l.id}
                  className={`border-t border-gray-200 hover:bg-gray-100 ${paused ? 'opacity-50 bg-gray-100' : ''}`}>
                  <td className="px-2 py-1.5">
                    <span className="font-semibold text-gray-900">{l.label}</span>
                    {l.roll_count > 0 && (
                      <span title={`rolled ${l.roll_count}× · cumulative basis ${l.total_basis >= 0 ? '+' : ''}${l.total_basis}`}
                        className="ml-1 text-[10px] text-amber-600">↻{l.roll_count}</span>
                    )}
                  </td>
                  <td className="px-2 text-gray-700 whitespace-nowrap">
                    <div className="flex items-center gap-1.5">
                      {l.contract_tsym}
                      {l.source === 'ladder'
                        ? <Badge tone="purple">LADDER</Badge>
                        : <Badge tone="blue">AUTO</Badge>}
                    </div>
                    <div className="text-[10px] text-gray-500">{l.sym} · {l.exch}{l.source !== 'ladder' && l.timeframe_min ? ` · ${l.timeframe_min}m signal` : ''}</div>
                  </td>
                  {show('lots') && <td className="px-2 text-right text-gray-800 font-mono">{l.lots}<div className="text-[10px] text-gray-500">{l.qty}u</div></td>}
                  {show('buy') && (
                    <td className="px-2 text-right font-mono tabular-nums text-gray-900">
                      {fmtPx(l.entry_price)}
                      {l.roll_count > 0 && <div className="text-[10px] text-gray-500">raw {fmtPx(l.raw_entry_price)}</div>}
                    </td>
                  )}
                  {tab === 'open' && show('ltp') && (
                    <td className="px-2 text-right font-mono tabular-nums text-gray-900">
                      {paused
                        ? <span title="paused — price frozen at temp-exit fill">{fmtPx(l.temp_exit_price || null)}<span className="text-gray-400 text-[10px]"> ⏸</span></span>
                        : ltpIsLastKnown(l)
                          ? <span className="text-gray-500"
                              title="last price the broker reported — the live feed is not running">
                              {fmtPx(ltp || null)}<span className="text-[10px] text-gray-400"> last</span>
                            </span>
                          : fmtPx(ltp || null)}
                    </td>
                  )}
                  {show('target') && (
                    <td className="px-2 text-right font-mono tabular-nums text-emerald-600">
                      {tab === 'open' && l.status === 'OPEN'
                        ? <InlineTarget lot={l} onSaved={load} onError={setErr} />
                        : fmtPx(l.target_price)}
                    </td>
                  )}
                  {show('stop') && <td className="px-2 text-right font-mono tabular-nums text-red-600">{fmtPx(l.sl_price)}</td>}
                  {tab === 'closed' && (
                    <td className="px-2 text-right font-mono tabular-nums text-gray-800">{fmtPx(l.exit_price || null)}</td>
                  )}
                  {show('pnl') && (
                    <td className={`px-2 text-right font-mono tabular-nums font-semibold ${pnlClass(pnl)}`}>
                      {pnl === null ? '—' : inr(pnl)}
                      {l.entry_price > 0 && pnl !== null && (
                        <div className="text-[10px] text-gray-500 font-normal">
                          {((pnl / (l.entry_price * l.qty)) * 100).toFixed(2)}%
                        </div>
                      )}
                    </td>
                  )}
                  {tab === 'open' && show('temp') && (
                    <td className="px-2 align-middle">
                      {(l.status === 'OPEN' || paused) && (
                        <TempControls lot={l} paused={paused} busy={busyLot === l.id}
                          setBusy={setBusyLot} onDone={load} onError={setErr} />
                      )}
                    </td>
                  )}
                  {tab === 'closed' && show('entry') && (
                    <td className="px-2 text-gray-600 whitespace-nowrap text-xs">
                      {fmtTime(l.entry_time)}
                    </td>
                  )}
                  {show('time') && (
                    <td className="px-2 text-gray-600 whitespace-nowrap text-xs">
                      {fmtTime(tab === 'open' ? l.entry_time : l.exit_time)}
                    </td>
                  )}
                  {show('status') && (
                    <td className="px-2">
                      {l.status === 'OPEN' && !l.exit_pending && <Badge tone="green">OPEN</Badge>}
                      {l.status === 'OPEN' && l.exit_pending && <Badge tone="amber">EXITING…</Badge>}
                      {l.status === 'PENDING' && <Badge tone="amber">PENDING</Badge>}
                      {l.status === 'UNCONFIRMED' && <Badge tone="red">UNCONFIRMED</Badge>}
                      {paused && (
                        <div className="flex flex-col gap-0.5">
                          <Badge tone="gray">⏸ Paused</Badge>
                          <span className={`text-[10px] font-mono ${l.temp_exit_loss > 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                            Carrying {l.temp_exit_loss > 0 ? '' : '+'}{(-l.temp_exit_loss).toFixed(1)} pts
                          </span>
                        </div>
                      )}
                      {l.status === 'CLOSED' && <Badge tone="gray">{l.exit_reason || 'CLOSED'}</Badge>}
                      {l.status === 'EXTERNAL_CLOSED' && <Badge tone="red">EXTERNAL</Badge>}
                      {l.status === 'CANCELLED' && <Badge tone="gray">CANCELLED</Badge>}
                    </td>
                  )}
                  <td className="px-2">
                    <div className="flex justify-end gap-1">
                      {inst && (
                        <button onClick={() => onOpenChart(inst, l.id)}
                          className="px-2 py-0.5 rounded border border-gray-300 text-gray-700 hover:border-sky-500 hover:text-sky-700 text-[11px]">
                          Chart
                        </button>
                      )}
                      {tab === 'open' && l.status === 'OPEN' && (
                        <>
                          <button onClick={() => setEditLot(l)}
                            className="px-2 py-0.5 rounded border border-gray-300 text-gray-700 hover:border-sky-500 hover:text-sky-700 text-[11px]">
                            Edit T/SL
                          </button>
                          <button disabled={busyLot === l.id || l.exit_pending} onClick={() => exitNow(l)}
                            className="px-2 py-0.5 rounded border border-red-500 text-red-600 hover:bg-red-500/10 text-[11px] disabled:opacity-40">
                            Exit
                          </button>
                        </>
                      )}
                      {tab === 'open' && paused && (
                        <button disabled={busyLot === l.id} onClick={() => exitNow(l)}
                          title="permanently close this paused rung (books the carried loss)"
                          className="px-2 py-0.5 rounded border border-gray-300 text-gray-600 hover:border-red-500 hover:text-red-600 text-[11px] disabled:opacity-40">
                          Close
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        )}
      </div>

      {editLot && <EditLotModal lot={editLot} onClose={() => setEditLot(null)} onSaved={load} />}
      {cancelLot && (
        <CancelPendingModal lot={cancelLot} nextLevelPrice={nextLevelPriceFor(cancelLot)}
          pendingPrice={pendingPriceOf(cancelLot)}
          onClose={() => setCancelLot(null)} onDone={load} />
      )}
    </Card>
  )
}

/** Temp Exit / Re-enter controls for one rung. OPEN → pause controls; paused →
 * re-entry controls. Each offers an immediate (market) action and a limit
 * ("when price reaches") action. */
function TempControls({ lot, paused, busy, setBusy, onDone, onError }: {
  lot: LotRow; paused: boolean; busy: boolean
  setBusy: (id: number) => void; onDone: () => Promise<void> | void; onError: (m: string) => void
}) {
  const [price, setPrice] = useState('')
  const armed = paused ? lot.reenter_trigger_price : lot.temp_exit_trigger_price
  const act = paused ? api.reEnter : api.tempExit
  const noun = paused ? 'Re-enter' : 'Temp Exit'

  const fire = async (kind: 'market' | 'limit' | 'cancel') => {
    if (kind === 'limit') {
      const n = Number(price)
      if (!price || isNaN(n) || n <= 0) { onError('enter a valid limit price'); return }
    }
    if (kind === 'market' && !confirm(`${paused ? 'Re-enter' : 'Temporarily exit'} ${lot.label} at market now?`)) return
    setBusy(lot.id)
    try {
      await act(lot.id, kind === 'limit' ? { type: 'limit', price: Number(price) } : { type: kind })
      setPrice('')
      await onDone()
    } catch (e) { onError(e instanceof Error ? e.message : 'action failed') }
    finally { setBusy(0) }
  }

  return (
    <div className="flex flex-col gap-1 items-stretch min-w-[190px]">
      <button disabled={busy} onClick={() => fire('market')}
        className={`px-2 py-0.5 rounded text-[11px] font-semibold border disabled:opacity-40 ${
          paused ? 'border-emerald-500 text-emerald-600 hover:bg-emerald-500/10'
                 : 'border-amber-500 text-amber-600 hover:bg-amber-500/10'}`}>
        {paused ? '▶ Immediate Re-enter' : '⏸ Immediate Temp Exit'}
      </button>
      <div className="flex items-center gap-1">
        <input type="number" step="any" value={price} onChange={(e) => setPrice(e.target.value)}
          placeholder={paused ? 'price to re-enter' : 'price to temp-exit'}
          className="w-full bg-gray-50 border border-gray-300 rounded px-1.5 py-0.5 text-right font-mono text-[11px] focus:outline-none focus:border-sky-500" />
        <button disabled={busy} onClick={() => fire('limit')}
          className="px-1.5 py-0.5 rounded text-[11px] border border-gray-300 text-gray-700 hover:border-sky-500 hover:text-sky-700 disabled:opacity-40">
          Set
        </button>
      </div>
      {armed > 0 && (
        <span className="text-[10px] text-amber-600 flex items-center gap-1">
          armed: {noun.toLowerCase()} ≤ {fmtPx(armed)}
          <button onClick={() => fire('cancel')} className="underline hover:text-red-600">cancel</button>
        </span>
      )}
    </div>
  )
}

/** Click-to-edit target for an OPEN rung. Editing the target only moves WHERE
 * this rung exits — P&L stays exact (always (exit−entry)×qty) and later rungs
 * anchor on entry prices, not targets, so the ladder math never drifts. */
function InlineTarget({ lot, onSaved, onError }: {
  lot: LotRow; onSaved: () => void; onError: (m: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState(String(lot.target_price || ''))
  const [saving, setSaving] = useState(false)
  // Enter calls save() and unmounts the input, whose blur then calls save()
  // again — two edits, two cancel/re-place cycles at the broker for one keypress.
  const inflight = useRef(false)

  useEffect(() => {
    if (!editing && !saving) setVal(String(lot.target_price || ''))
  }, [lot.target_price, editing, saving])

  const save = async () => {
    if (inflight.current) return
    const n = Number(val)
    if (val === '' || isNaN(n) || n <= 0 || n === lot.target_price) { setEditing(false); return }
    inflight.current = true
    setSaving(true)
    setEditing(false)
    try { await api.editLot(lot.id, { target_price: n }); onSaved() }
    catch (e) {
      onError(e instanceof Error ? e.message : 'target update failed')
      setVal(String(lot.target_price || ''))     // the edit did not take — show what is actually armed
    }
    finally { inflight.current = false; setSaving(false) }
  }

  // Saving is NOT instant and must not look like a frozen screen. The request
  // waits on the broker: the old resting sell has to be cancelled AND the cancel
  // confirmed before the new one is placed, because placing first would leave
  // two live sell orders on one rung. That is seconds of real work, and showing
  // nothing while it happened read as the UI having hung.
  if (saving) {
    return (
      <span
        title="Moving the resting sell order at the broker: cancelling the old target and waiting for the broker to confirm before placing the new one. Two live sells on one rung is not a risk worth taking to save a second."
        className="inline-flex items-center gap-1.5 font-mono tabular-nums text-emerald-600">
        <span aria-hidden className="h-3 w-3 rounded-full border-2 border-current border-t-transparent animate-spin" />
        {fmtPx(Number(val))}
        <span className="text-[10px] text-gray-500 font-sans">saving…</span>
      </span>
    )
  }

  if (!editing) {
    return (
      <button onClick={() => setEditing(true)} title="click to edit target"
        className="font-mono tabular-nums text-emerald-600 hover:text-emerald-700">
        {fmtPx(lot.target_price)} <span className="text-gray-500 text-[10px]">✎</span>
      </button>
    )
  }
  return (
    <input autoFocus type="number" step="any" value={val}
      onChange={(e) => setVal(e.target.value)}
      onBlur={save}
      onKeyDown={(e) => { if (e.key === 'Enter') save(); if (e.key === 'Escape') setEditing(false) }}
      className="w-24 bg-gray-50 border border-emerald-600 rounded px-1.5 py-0.5 text-right font-mono text-[12px] focus:outline-none" />
  )
}

function EditLotModal({ lot, onClose, onSaved }: { lot: LotRow; onClose: () => void; onSaved: () => void }) {
  const [tgt, setTgt] = useState(String(lot.target_price || ''))
  const [sl, setSl] = useState(String(lot.sl_price || ''))
  const [err, setErr] = useState('')
  const [saving, setSaving] = useState(false)

  const save = async () => {
    setSaving(true)
    setErr('')
    try {
      await api.editLot(lot.id, {
        target_price: tgt === '' ? undefined : Number(tgt),
        sl_price: sl === '' ? undefined : Number(sl),
      })
      onSaved()
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'save failed')
    } finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 z-50 bg-gray-900/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white border border-gray-300 rounded-2xl w-full max-w-sm p-5" onClick={(e) => e.stopPropagation()}>
        <div className="font-semibold text-gray-900 mb-1">Edit {lot.label} — {lot.contract_tsym}</div>
        <div className="text-[11px] text-gray-500 mb-4">entry {fmtPx(lot.entry_price)} · {lot.lots} lot(s)</div>
        <label className="text-[11px] uppercase text-gray-500">Target price</label>
        <input type="number" step="any" value={tgt} onChange={(e) => setTgt(e.target.value)}
          className="w-full bg-gray-50 border border-gray-300 rounded-md px-2 py-1.5 text-sm mb-3 mt-1 focus:outline-none focus:border-sky-500" />
        <label className="text-[11px] uppercase text-gray-500">Stop-loss price</label>
        <input type="number" step="any" value={sl} onChange={(e) => setSl(e.target.value)}
          className="w-full bg-gray-50 border border-gray-300 rounded-md px-2 py-1.5 text-sm mt-1 focus:outline-none focus:border-sky-500" />
        {err && <div className="text-red-600 text-xs mt-2">{err}</div>}
        {/* Saving a target moves a real order at the broker and waits for the
            cancel to be confirmed before re-placing. Say that, rather than
            leaving a dead-looking dialog for the seconds it takes. */}
        {saving && (
          <div className="mt-3 text-[11px] text-gray-600 flex items-center gap-2">
            <span aria-hidden className="h-3 w-3 rounded-full border-2 border-emerald-600 border-t-transparent animate-spin" />
            Moving the resting order at the broker — waiting for the cancel to be confirmed…
          </div>
        )}
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} disabled={saving}
            className="px-3 py-1.5 rounded-lg border border-gray-300 text-gray-700 text-sm disabled:opacity-50">Cancel</button>
          <button onClick={save} disabled={saving}
            className="px-4 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-semibold disabled:opacity-50">
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Intent dialog for a pending order. Two clearly-separated actions so a
 * "cancel" click can NEVER silently start a lower buy:
 *   • Cancel & stop buying → cancels this order and PAUSES the ladder.
 *   • Skip to next level    → cancels this order and buys one level lower. */
function CancelPendingModal({ lot, nextLevelPrice, pendingPrice, onClose, onDone }: {
  lot: LotRow; nextLevelPrice: number | null; pendingPrice: number
  onClose: () => void; onDone: () => Promise<void> | void
}) {
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState<'' | 'stop' | 'skip'>('')

  const run = async (which: 'stop' | 'skip') => {
    setBusy(which)
    setErr('')
    try {
      if (which === 'stop') await api.cancelPendingLot(lot.id)
      else await api.skipLot(lot.id)
      await onDone()
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'action failed')
    } finally { setBusy('') }
  }

  return (
    <div className="fixed inset-0 z-50 bg-gray-900/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white border border-gray-300 rounded-2xl w-full max-w-sm p-5" onClick={(e) => e.stopPropagation()}>
        <div className="font-semibold text-gray-900 mb-1">Pending buy at <span className="font-mono tabular-nums">₹{fmtPx(pendingPrice)}</span></div>
        <div className="text-[11px] text-gray-500 mb-4">
          {lot.label} — {lot.contract_tsym} · what do you want to do?
        </div>

        <button onClick={() => run('stop')} disabled={busy !== ''}
          className="w-full text-left rounded-lg border border-red-500 hover:bg-red-500/10 px-3 py-2.5 mb-2 disabled:opacity-50">
          <div className="text-sm font-semibold text-red-600">{busy === 'stop' ? '…' : 'Cancel & stop buying'}</div>
          <div className="text-[11px] text-gray-600 mt-0.5">
            Cancels this order and <b>pauses the ladder</b> — no lower buy is placed. Targets &amp; stop-losses
            on open trades stay active. Re-arm the ladder to resume.
          </div>
        </button>

        <button onClick={() => run('skip')} disabled={busy !== '' || nextLevelPrice === null}
          className="w-full text-left rounded-lg border border-amber-500 hover:bg-amber-500/10 px-3 py-2.5 disabled:opacity-40">
          <div className="text-sm font-semibold text-amber-600">
            {busy === 'skip' ? '…' : nextLevelPrice !== null
              ? <>Skip to next level (₹{fmtPx(nextLevelPrice)})</>
              : 'Skip to next level (none below)'}
          </div>
          <div className="text-[11px] text-gray-600 mt-0.5">
            Cancels this order and places the buy <b>one level lower</b>. The ladder keeps buying.
          </div>
        </button>

        {err && <div className="text-red-600 text-xs mt-2">{err}</div>}
        <div className="flex justify-end mt-4">
          <button onClick={onClose} disabled={busy !== ''}
            className="px-3 py-1.5 rounded-lg border border-gray-300 text-gray-700 text-sm disabled:opacity-50">Keep order</button>
        </div>
      </div>
    </div>
  )
}
