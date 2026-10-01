import { Fragment, useMemo, useState } from 'react'
import { api } from '../api'
import type { InstrumentSnap } from '../types'
import { RolloverDrawer } from './RolloverDrawer'
import { Badge, Card, PillSwitch, Tooltip, fmtPx, inr, pnlClass } from './ui'

type ModeFilter = 'all' | 'auto' | 'ladder'

// optional watchlist columns the user can show/hide (persisted). 'Instrument'
// and 'Actions' are always shown; hiding columns lets the table breathe and the
// visible columns widen to fill the space (great with the search panel collapsed).
const WATCH_COLS: { key: string; label: string }[] = [
  { key: 'ltp', label: 'LTP' },
  { key: 'nextbuy', label: 'Next buy' },
  { key: 'pnl', label: 'Open P&L' },
  { key: 'sl', label: 'Stop-loss' },
  { key: 'rollover', label: 'Rollover' },
  { key: 'cb', label: 'CB (₹ limit)' },
  { key: 'armed', label: 'Armed' },
]
const WATCH_COLS_KEY = 'grid.watchCols'

/** ONE watchlist for everything — automated-signal and manual-ladder stocks
 * live together, each row tagged AUTO or LADDER. A filter shows all / only
 * automated / only ladder. Ladder rows expose their next buy level, arm switch
 * and Levels editor inline; automated rows drive on incoming signals. */
export function WatchlistTable({ instruments, onConfigure, onOpenLadder, onChanged, onOpenChart }: {
  instruments: InstrumentSnap[]
  onConfigure: (inst: InstrumentSnap) => void
  onOpenLadder: (inst: InstrumentSnap) => void
  onChanged: () => void
  onOpenChart: (inst: InstrumentSnap) => void
}) {
  const [busyId, setBusyId] = useState<number>(0)
  const [err, setErr] = useState('')
  const [filter, setFilter] = useState<ModeFilter>('all')
  const [rollOpenId, setRollOpenId] = useState<number>(0)   // instrument whose rollover drawer is expanded

  const [visible, setVisible] = useState<Set<string>>(() => {
    try {
      const s = localStorage.getItem(WATCH_COLS_KEY)
      if (s) {
        const set = new Set(JSON.parse(s) as string[])
        if (set.delete('rungs')) set.add('sl')   // the Rungs column became the SL column
        return set
      }
    } catch { /* ignore */ }
    return new Set(WATCH_COLS.map((c) => c.key))
  })
  const show = (k: string) => visible.has(k)
  const toggleCol = (k: string) => {
    const s = new Set(visible)
    s.has(k) ? s.delete(k) : s.add(k)
    setVisible(s)
    try { localStorage.setItem(WATCH_COLS_KEY, JSON.stringify([...s])) } catch { /* ignore */ }
  }

  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem('grid.watchCollapsed') === '1' } catch { return false }
  })
  const toggleCollapsed = () => {
    setCollapsed((c) => {
      const v = !c
      try { localStorage.setItem('grid.watchCollapsed', v ? '1' : '0') } catch { /* ignore */ }
      return v
    })
  }

  const act = async (id: number, fn: () => Promise<unknown>) => {
    setBusyId(id)
    setErr('')
    try { await fn(); onChanged() } catch (e) { setErr(e instanceof Error ? e.message : 'action failed') }
    finally { setBusyId(0) }
  }

  const counts = useMemo(() => ({
    all: instruments.length,
    auto: instruments.filter((i) => (i.mode ?? 'auto') !== 'ladder').length,
    ladder: instruments.filter((i) => i.mode === 'ladder').length,
  }), [instruments])

  const rows = useMemo(() => instruments.filter((i) =>
    filter === 'all' ? true : filter === 'ladder' ? i.mode === 'ladder' : (i.mode ?? 'auto') !== 'ladder'
  ), [instruments, filter])

  const tab = (f: ModeFilter, label: string) => (
    <button onClick={() => setFilter(f)}
      className={`px-2.5 py-1 text-xs font-semibold rounded-md border transition-colors
        ${filter === f
          ? (f === 'ladder' ? 'bg-violet-500/20 border-violet-600 text-violet-700'
            : f === 'auto' ? 'bg-sky-500/20 border-sky-500 text-sky-700'
            : 'bg-gray-200 border-gray-400 text-gray-900')
          : 'border-gray-300 text-gray-600 hover:text-gray-800'}`}>
      {label} <span className="opacity-60">{counts[f]}</span>
    </button>
  )

  return (
    <Card
      title={
        <button onClick={toggleCollapsed} title={collapsed ? 'expand watchlist' : 'collapse watchlist'}
          className="flex items-center gap-2 hover:text-sky-700">
          <span className="text-gray-500">{collapsed ? '▶' : '▼'}</span>
          Watchlist Stocks
          {collapsed && <span className="text-xs font-normal text-gray-500">({instruments.length} — click to expand)</span>}
        </button>
      }
      right={
        <div className="flex items-center gap-2">
          {err && <span className="text-red-600 text-xs">{err}</span>}
          <details className="relative">
            <summary className="cursor-pointer list-none px-2 py-1 rounded-md border border-gray-300 text-xs text-gray-700 hover:border-sky-500">
              Columns
            </summary>
            <div className="absolute right-0 mt-1 bg-white border border-gray-300 rounded-lg shadow-lg p-2 z-20 w-44">
              <div className="text-[10px] uppercase tracking-wide text-gray-400 px-1 pb-1">Show columns</div>
              {WATCH_COLS.map((c) => (
                <label key={c.key} className="flex items-center gap-2 text-xs py-0.5 px-1 rounded hover:bg-gray-50 cursor-pointer">
                  <input type="checkbox" checked={show(c.key)} onChange={() => toggleCol(c.key)}
                    className="w-3.5 h-3.5 accent-sky-500" />
                  {c.label}
                </label>
              ))}
            </div>
          </details>
          <div className="flex gap-1">{tab('all', 'All')}{tab('auto', 'Automated')}{tab('ladder', 'Ladder')}</div>
        </div>
      }>
      {!collapsed && (rows.length === 0 ? (
        <div className="text-gray-500 text-sm px-2 py-6 text-center">
          {instruments.length === 0
            ? 'Watchlist is empty — search on the left and add instruments as Automated or Ladder. The engine only trades what is in this list.'
            : `No ${filter} stocks. Switch the filter above.`}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="text-gray-500 text-[11px] uppercase tracking-wide">
                <th className="text-left px-2 py-1.5">Instrument</th>
                {show('ltp') && <th className="text-right px-2">LTP</th>}
                {show('nextbuy') && <th className="text-right px-2">Next buy</th>}
                {show('pnl') && <th className="text-right px-2">Open P&L</th>}
                {show('sl') && <th className="text-center px-2" title="Stop-loss state for this instrument — set from the Levels view">SL</th>}
                {show('rollover') && <th className="text-left px-2">Rollover</th>}
                {show('cb') && <th className="text-center px-2" title="Circuit breaker: green = trading allowed, red = new buys blocked. ₹ value = loss threshold.">CB (₹ limit)</th>}
                {show('armed') && <th className="text-center px-2" title="Ladder armed: green = levels fire, red = paused (ladder rows only)">Armed</th>}
                <th className="text-right px-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((i) => {
                const isLadder = i.mode === 'ladder'
                const isOpt = i.instr_type === 'OPT'
                const lad = i.ladder
                {/* backend-computed truth: highest price where a buy will actually happen next
                    (live pending orders at the broker + placeable levels) */}
                const nextable = (lad?.levels ?? []).filter((l) => l.status === 'PENDING' || l.status === 'PLACED')
                const nextBuy = lad?.next_buy_price ?? (nextable.length ? Math.max(...nextable.map((l) => l.price)) : null)
                const stalePx = i.price.age > 5
                const colCount = 2 + ['ltp', 'nextbuy', 'pnl', 'sl', 'rollover', 'cb', 'armed'].filter(show).length
                return (
                  <Fragment key={i.id}>
                  <tr
                    className={`border-t border-gray-200 hover:bg-gray-100 ${isLadder ? 'cursor-pointer' : ''}`}
                    onClick={isLadder ? () => onOpenLadder(i) : undefined}>
                    {/* instrument + mode highlight */}
                    <td className="px-2 py-2 min-w-0 max-w-[280px]">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="text-[12px] font-semibold text-gray-900 break-all leading-tight">{i.sym}</span>
                        {isLadder
                          ? <Badge tone="purple">LADDER</Badge>
                          : <Badge tone="blue">AUTO</Badge>}
                        <Badge tone={i.exch === 'MCX' ? 'amber' : 'blue'}>{i.exch}</Badge>
                        {isOpt && <Badge tone="green">{i.opt_type || 'OPT'}{i.strike ? ` ${i.strike}` : ''}</Badge>}
                        {isLadder && <Badge tone="gray">{lad?.basis === 'support' ? '4h support' : 'fixed'}</Badge>}
                        {!i.enabled && <Badge tone="gray">disabled</Badge>}
                        {isLadder && lad && !lad.armed && <Badge tone="gray">not confirmed</Badge>}
                        {i.pending_rungs > 0 && <Badge tone="amber">{i.pending_rungs} pending</Badge>}
                        {(i.resting_rungs ?? 0) > 0 && (
                          <Tooltip text="BUY LIMIT order(s) pending at the broker — see the Pending tab.">
                            <Badge tone="blue">{i.resting_rungs} pending @ broker</Badge>
                          </Tooltip>
                        )}
                        {i.unmanaged_qty > 0 && (
                          <Tooltip text={`Broker holds ${i.unmanaged_qty} units the ladder did not open (manual/external trades). The engine will not touch them.`}>
                            <Badge tone="red">+{i.unmanaged_qty}u external</Badge>
                          </Tooltip>
                        )}
                      </div>
                      <div className="text-[10px] text-gray-500 break-all leading-tight mt-0.5">{i.tsym} · lot {i.lot_size} · exp {i.expiry || '—'}</div>
                    </td>
                    {/* LTP */}
                    {show('ltp') && (
                    <td className={`px-2 text-right font-mono tabular-nums ${stalePx ? 'text-gray-500' : 'text-gray-900'}`}>
                      {fmtPx(i.price.lp)}
                      {/* Name the source honestly. A price carried over from the
                          broker after the feed stopped is worth showing, but
                          labelling it "ws" would claim a live tick that isn't. */}
                      <div className="text-[10px] text-gray-500">
                        {i.price.source === 'broker-last'
                          ? <span title="last price the broker reported — the live feed is not running">last known</span>
                          : <>{i.price.source === 'poll' ? 'poll' : 'ws'} · {i.price.age < 999 ? `${i.price.age.toFixed(0)}s` : '—'}</>}
                      </div>
                    </td>
                    )}
                    {/* next buy (ladder only) */}
                    {show('nextbuy') && (
                    <td className="px-2 text-right font-mono tabular-nums text-violet-700">
                      {isLadder ? (
                        nextBuy !== null ? (
                          <>
                            {fmtPx(nextBuy)}
                            {i.price.lp > 0 && (
                              <div className="text-[10px] text-gray-500">
                                {(i.price.lp - nextBuy) > 0 ? `${(i.price.lp - nextBuy).toFixed(1)} away` : 'at/below'}
                              </div>
                            )}
                          </>
                        ) : <span className="text-gray-500">—</span>
                      ) : <span className="text-gray-400">—</span>}
                    </td>
                    )}
                    {/* open P&L */}
                    {show('pnl') && (
                    <td className={`px-2 text-right font-mono tabular-nums ${pnlClass(i.open_pnl)}`}>
                      {inr(i.open_pnl)}
                      <div className="text-[10px] text-gray-500">today {inr(i.realized_today)}</div>
                    </td>
                    )}
                    {/* stop-loss state (replaces the old Rungs column) */}
                    {show('sl') && (
                    <td className="px-2 text-center">
                      {i.config.sl_enabled === false ? (
                        <Tooltip text="Stop-losses are DISABLED for this instrument — every rung trades WITHOUT a stop. Enable them in the Levels view.">
                          <span className="inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold bg-rose-500/15 text-rose-700 border border-rose-400">
                            SL disabled
                          </span>
                        </Tooltip>
                      ) : (
                        <span className="text-[10px] font-semibold text-emerald-600" title="stop-losses active — per-level SLs are set in the Levels view">SL on</span>
                      )}
                    </td>
                    )}
                    {/* rollover — compact inline controls, synced with Config */}
                    {show('rollover') && (
                    <td className="px-2 align-top" onClick={(e) => e.stopPropagation()}>
                      <RolloverCell inst={i} busy={busyId === i.id} act={act}
                        expanded={rollOpenId === i.id}
                        onToggleExpand={() => setRollOpenId((v) => (v === i.id ? 0 : i.id))} />
                    </td>
                    )}
                    {/* circuit breaker + its ₹ value (feature 4) */}
                    {show('cb') && (
                    <td className="px-2 text-center" onClick={(e) => e.stopPropagation()}>
                      <div className="flex flex-col items-center gap-0.5">
                        <PillSwitch
                          on={!i.cb.tripped}
                          busy={busyId === i.id}
                          title={i.cb.tripped
                            ? `TRIPPED: ${i.cb.reason || 'loss threshold hit'} — click to re-arm buying`
                            : `Trading allowed (trips at a loss of ${inr(-i.cb.threshold)}). Click to block new buys.`}
                          onChange={(on) => act(i.id, () => api.setCircuitBreaker(i.id, on))}
                        />
                        <span className={`text-[10px] font-mono ${i.cb.tripped ? 'text-red-600 font-semibold' : i.cb.enabled ? 'text-red-600' : 'text-gray-500'}`}>
                          {i.cb.tripped ? 'TRIPPED' : i.cb.enabled ? inr(-i.cb.threshold) : 'off'}
                        </span>
                      </div>
                    </td>
                    )}
                    {/* armed (ladder only) */}
                    {show('armed') && (
                    <td className="px-2 text-center" onClick={(e) => e.stopPropagation()}>
                      {isLadder ? (
                        <PillSwitch
                          on={!!lad?.armed}
                          busy={busyId === i.id}
                          title={lad?.armed ? 'Ladder ARMED — levels fire when price reaches them. Click to pause.'
                            : 'Ladder paused — open it and Confirm (or click) to arm.'}
                          // Arming sends REAL buy orders to the broker — levels
                          // at or above the live price fire within ~1s. Every
                          // other order-placing control here (Flatten, Exit,
                          // Roll) confirms first; this one sat one pixel from
                          // the circuit-breaker switch and did not. Only ARMING
                          // asks — pausing is always safe, never obstruct it.
                          onChange={(on) => {
                            if (on) {
                              const pending = (lad?.levels ?? []).filter((l) => l.status === 'PENDING')
                              const live = pending.filter((l) => i.price.lp > 0 && l.price >= i.price.lp)
                              const msg = `ARM the ${i.sym} ladder?\n\n`
                                + `${pending.length} pending level(s) go live and will place REAL buy orders.\n`
                                + (live.length
                                    ? `${live.length} of them sit at or above the current price `
                                      + `${fmtPx(i.price.lp)} and will fire immediately.`
                                    : `Nearest level is below the current price ${fmtPx(i.price.lp)}.`)
                              if (!confirm(msg)) return
                            }
                            act(i.id, () => api.ladderArm(i.id, on))
                          }}
                        />
                      ) : <span className="text-gray-400">—</span>}
                    </td>
                    )}
                    {/* actions */}
                    <td className="px-2" onClick={(e) => e.stopPropagation()}>
                      <div className="flex justify-end gap-1 flex-wrap">
                        {isLadder && (
                          <button onClick={() => onOpenLadder(i)}
                            className="px-2 py-1 rounded border border-violet-600 text-violet-700 hover:bg-violet-500/10 text-[11px] font-semibold">
                            Levels
                          </button>
                        )}
                        <button onClick={() => onOpenChart(i)}
                          className="px-2 py-1 rounded border border-gray-300 text-gray-700 hover:border-sky-500 hover:text-sky-700 text-[11px]">
                          Chart
                        </button>
                        <button onClick={() => onConfigure(i)}
                          className="px-2 py-1 rounded border border-gray-300 text-gray-700 hover:border-sky-500 hover:text-sky-700 text-[11px]">
                          Config
                        </button>
                        <button
                          disabled={busyId === i.id || i.open_rungs === 0}
                          onClick={() => { if (confirm(`Market-exit ALL ${i.open_rungs} open rung(s) of ${i.sym}?`)) act(i.id, () => api.flatten(i.id)) }}
                          className={`px-2 py-1 rounded border text-[11px] ${i.open_rungs === 0 ? 'border-gray-200 text-gray-400' : 'border-red-500 text-red-600 hover:bg-red-500/10'}`}>
                          Flatten
                        </button>
                        <button
                          disabled={busyId === i.id}
                          onClick={() => { if (i.open_rungs === 0 && confirm(`Remove ${i.sym} from watchlist?`)) act(i.id, () => api.deleteInstrument(i.id)) }}
                          className={`px-2 py-1 rounded border text-[11px] ${i.open_rungs > 0 ? 'border-gray-200 text-gray-400 cursor-not-allowed' : 'border-gray-300 text-gray-600 hover:border-red-500 hover:text-red-600'}`}
                          title={i.open_rungs > 0 ? 'flatten first' : 'remove'}>
                          ✕
                        </button>
                      </div>
                    </td>
                  </tr>
                  {rollOpenId === i.id && !isOpt && (
                    <tr>
                      <td colSpan={colCount} className="p-0">
                        <RolloverDrawer inst={i} onChanged={onChanged} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      ))}
    </Card>
  )
}

/** Compact rollover control for the watchlist row — the same Automated/Manual
 * switch, days-before / manual-date and ⚡ Quick-rollover as the Config modal.
 * Both write the SAME backend fields (rollover_days_before / rollover_date_override)
 * and rolloverNow(), so a change in either place shows up in the other on the
 * next 1s snapshot. Only the ROLL TIMING is edited here — the roll basis math
 * (entry/target/SL += new_fill − old_exit) is untouched. */
function RolloverCell({ inst, busy, act, expanded, onToggleExpand }: {
  inst: InstrumentSnap
  busy: boolean
  act: (id: number, fn: () => Promise<unknown>) => void
  expanded: boolean
  onToggleExpand: () => void
}) {
  const roll = inst.rollover
  const cfg = inst.config
  const isOpt = inst.instr_type === 'OPT'
  const manual = roll.mode === 'manual'
  const suggested = roll.suggested_date || ''

  if (isOpt) {
    return (
      <Tooltip text={roll.tooltip}>
        <span className="text-gray-500 text-xs cursor-help">expires {inst.expiry || '—'} ⓘ</span>
      </Tooltip>
    )
  }

  const setMode = (m: 'auto' | 'manual') =>
    act(inst.id, () => api.updateInstrument(inst.id, {
      rollover_date_override: m === 'manual' ? (cfg.rollover_date_override || suggested) : '',
    }))
  const pill = (m: 'auto' | 'manual', label: string) => (
    <button disabled={busy} onClick={() => setMode(m)}
      className={`px-1.5 py-0.5 rounded text-[10px] font-semibold border disabled:opacity-40 ${
        (m === 'manual') === manual
          ? (m === 'manual' ? 'bg-amber-500/20 border-amber-600 text-amber-600' : 'bg-sky-500/20 border-sky-500 text-sky-700')
          : 'border-gray-300 text-gray-500 hover:text-gray-700'}`}>
      {label}
    </button>
  )

  return (
    <div className="flex flex-col gap-1 min-w-[150px]">
      <div className="flex items-center gap-1">
        {pill('auto', 'Auto')}
        {pill('manual', 'Manual')}
        <Tooltip text={roll.tooltip}><span className="text-sky-600 cursor-help text-xs">ⓘ</span></Tooltip>
      </div>

      {(roll.state === 'queued' || roll.state === 'scanning') && (
        <span className={`text-[10px] font-semibold ${roll.state === 'scanning' ? 'text-emerald-600' : 'text-amber-600'}`}>
          {roll.state === 'scanning' ? 'sniper scanning…' : 'queued for window'}
        </span>
      )}

      <div className="flex items-center gap-1.5">
        <span className={`text-[11px] font-mono ${roll.due ? 'text-amber-600 font-semibold' : 'text-gray-700'}`}>
          {roll.rollover_date || '—'}
        </span>
        <button onClick={onToggleExpand}
          title="open the rollover table: every open rung with its roll price, P&L after rollover, roll cost and a per-rung Quick roll"
          className={`px-1 rounded border text-[10px] leading-4 ${expanded
            ? 'border-violet-600 text-violet-700 bg-violet-500/10'
            : 'border-gray-300 text-gray-500 hover:border-violet-500 hover:text-violet-700'}`}>
          {expanded ? '▲' : '▼'}
        </button>
        {manual && (
          <span className="text-[10px] text-amber-600"
            title="Manual mode: no calendar — pick the target CONTRACT and roll from the ▼ table (per-rung Quick roll or Roll ALL). The shown date stays as the safety fallback.">
            roll via ▼
          </span>
        )}
      </div>

      {roll.due && (
        <div className="text-[10px] text-amber-600">
          {roll.window_open ? '▶ in roll window now' : roll.window_reason}
        </div>
      )}
    </div>
  )
}
