import type { Position, Signal } from '../types'
import { inr, inr2, isExpired, latency, pct, pnlColor, stampShort, tfColor, timeShort } from '../util'
import { Badge } from './ui'
import type { ColumnDef } from './ColumnPicker'

export interface SignalCtx {
  hardCap: boolean
  held: Map<string, Position>
  onBuy: (s: Signal) => void
  onSell: (p: Position) => void
  onStock: (symbol: string) => void
}

const dash = <span className="text-slate-300">—</span>

const statusCls: Record<string, string> = {
  STALE: 'border-slate-200 bg-slate-50 text-slate-500',
  ACTED: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  IGNORED: 'border-slate-200 bg-slate-50 text-slate-400',
  LOW_RR: 'border-amber-200 bg-amber-50 text-amber-700',
  BLACKLISTED: 'border-rose-200 bg-rose-50 text-rose-600',
  EXECUTED: 'border-emerald-300 bg-emerald-100 text-emerald-800',
  AVERAGED: 'border-amber-300 bg-amber-100 text-amber-800',
  SQUARED_OFF: 'border-sky-300 bg-sky-100 text-sky-800',
  EXEC_FAILED: 'border-rose-300 bg-rose-100 text-rose-700',
}

/** A signal the paper trader acted on is tinted across the whole row. */
export const rowTint = (s: Signal) =>
  s.status === 'EXECUTED' ? 'bg-emerald-50/70 hover:bg-emerald-50'
    : s.status === 'AVERAGED' ? 'bg-amber-50/70 hover:bg-amber-50'
      : s.status === 'SQUARED_OFF' ? 'bg-sky-50/60 hover:bg-sky-50'
        : 'hover:bg-slate-50/70'

/**
 * Columns for the signals tables (Today and Previous share one set, so a
 * choice made in one carries to the other).
 *
 * `optional: true` starts hidden — the full set is wider than most screens, and
 * a table nobody can read is worse than one that omits a column until asked.
 */
export const SIGNAL_COLUMNS: ColumnDef<Signal, SignalCtx>[] = [
  {
    key: 'symbol', label: 'Symbol', locked: true,
    sortValue: s => s.symbol,
    cell: (s, ctx) => {
      const pos = ctx.held.get(s.symbol)
      return (
        <>
          <button onClick={() => ctx.onStock(s.symbol)}
            className="text-[11px] font-bold text-slate-800 hover:text-sky-600">{s.symbol}</button>
          {pos && (
            <Badge className="ml-1.5 border-amber-200 bg-amber-50 text-amber-700"
              title={`Already holding ${pos.lots} lot(s) @ ${inr2(pos.avg_price)}`}>
              HELD{pos.averaging_count > 0 ? ` ×${pos.averaging_count + 1}` : ''}
            </Badge>
          )}
          {s.manual_add && <Badge className="ml-1.5 border-violet-200 bg-violet-50 text-violet-700">MANUAL</Badge>}
          {s.paper && <Badge className="ml-1.5 border-indigo-200 bg-indigo-50 text-indigo-700"
            title="The paper trader acted on this — simulated, no broker order">PAPER</Badge>}
          {/* The real-money counterpart. Without it an automated live fill and a
              simulated one were both just "a settled signal", and only the PAPER
              row said which. */}
          {s.auto && <Badge className="ml-1.5 border-rose-300 bg-rose-50 text-rose-700"
            title="Automated execution acted on this against REAL money">AUTO · LIVE</Badge>}
          {!s.candle_closed && (
            <Badge className="ml-1.5 border-amber-300 bg-amber-100 text-amber-800"
              title="Fired on a candle that had not closed yet — the rule can still flip before the bar ends, so this entry may repaint.">
              INTRA-CANDLE
            </Badge>
          )}
          {isExpired(s.expiry) && (
            <Badge className="ml-1.5 border-rose-300 bg-rose-100 text-rose-700"
              title={`Contract expired ${s.expiry} — it no longer ticks, so any price shown is dead`}>EXPIRED</Badge>
          )}
          <div className="text-[9px] leading-tight text-slate-400">
            1 lot = {s.lot_size}
            {s.executed_at && (
              <span className="ml-1.5 text-indigo-500" title={`Filled at ${inr2(s.exec_price)}`}>
                · filled {inr2(s.exec_price)} in {latency(s.exec_latency_ms)}
              </span>
            )}
          </div>
        </>
      )
    },
  },
  {
    key: 'signal', label: 'Signal',
    sortValue: s => s.signal_type,
    cell: s => (
      <span className={s.signal_type === 'BUY' ? 'font-bold text-emerald-600' : 'font-bold text-rose-600'}>
        {s.signal_type}
      </span>
    ),
  },
  {
    key: 'tf', label: 'TF',
    sortValue: s => ({ '1H': 1, '4H': 4, '1D': 24 }[s.timeframe] ?? 0),
    cell: s => <Badge className={tfColor[s.timeframe]}>{s.timeframe}</Badge>,
  },
  {
    // Arrival, not the candle stamp. A 1H setup is only *known* when its bar
    // closes, so the signal is labelled with the bar it fired on — an 11:00 stamp
    // for something that could not exist before 12:00. Showing that as "Signal
    // Time" read as an hour-old signal every single time. The bar it fired on
    // is still one hover (or the Candle column) away.
    key: 'signal_time', label: 'Signal Time',
    sortValue: s => new Date(s.created_at).getTime(),
    cellClass: 'whitespace-nowrap text-slate-500',
    title: 'When the signal reached this dashboard — hover a row for the bar it fired on',
    cell: s => (
      <span title={`${s.timeframe} bar: ${stampShort(s.signal_time)}`
        + ` → arrived ${stampShort(s.created_at)}`}>
        {timeShort(s.created_at)}
      </span>
    ),
  },
  {
    // Key deliberately left as `received` so a saved column preference from
    // before this swap keeps pointing at the same (still-optional) column.
    key: 'received', label: 'Candle', optional: true, cellClass: 'whitespace-nowrap text-slate-500',
    title: 'The bar the rule fired on — one timeframe behind the arrival time',
    cell: s => <span title={`${s.timeframe} bar`}>{stampShort(s.signal_time)}</span>,
  },
  {
    key: 'signal_ltp', label: 'Signal @',
    sortValue: s => s.signal_ltp || null,
    align: 'right',
    title: 'Price when the signal fired — frozen, never re-priced',
    cellClass: 'tabular-nums text-slate-500',
    cell: s => (s.signal_ltp ? inr2(s.signal_ltp) : dash),
  },
  {
    key: 'ltp', label: 'LTP',
    sortValue: s => s.ltp,
    align: 'right', title: 'Current live price',
    cellClass: 'tabular-nums font-medium text-slate-800',
    cell: s => (isExpired(s.expiry)
      ? <span className="text-slate-300" title="Expired contract — no live price">—</span>
      : inr2(s.ltp)),
  },
  {
    key: 'move', label: 'Δ %',
    sortValue: s => s.move_pct,
    align: 'right', title: 'Move since the signal fired',
    cellClass: 'tabular-nums',
    cell: s => (s.signal_ltp
      ? <span className={`font-semibold ${pnlColor(s.move_pct)}`}>{pct(s.move_pct)}</span>
      : dash),
  },
  {
    key: 'target', label: 'Target',
    sortValue: s => s.target || null,
    align: 'right', cellClass: 'tabular-nums text-emerald-600',
    cell: s => (s.target ? inr2(s.target) : dash),
  },
  {
    key: 'sl', label: 'SL',
    sortValue: s => s.stop_loss || null,
    align: 'right', cellClass: 'tabular-nums text-rose-600',
    cell: s => (s.stop_loss ? inr2(s.stop_loss) : dash),
  },
  {
    // Two different numbers, deliberately both shown. The big one is live and
    // decays as the stock walks toward its target; the small one is what the
    // signal was judged on and never changes. Showing only the live figure made
    // a signal accepted at 1.8 look like it should never have passed.
    key: 'rr', label: 'R:R',
    sortValue: s => s.rr || null,
    align: 'right',
    title: 'Reward ÷ risk — live above, and at the signal price below',
    cellClass: 'tabular-nums',
    cell: s => (s.rr || s.signal_rr
      ? <>
          <span className={s.rr >= 1 ? 'font-semibold text-emerald-600' : 'text-amber-600'}>
            {s.rr ? s.rr.toFixed(2) : '—'}
          </span>
          {!!s.signal_rr && (
            <div className="text-[9px] leading-tight text-slate-400"
              title="At the signal price — the value the minimum R:R filter used">
              sig {s.signal_rr.toFixed(2)}
            </div>
          )}
        </>
      : dash),
  },
  {
    key: 'support', label: 'Support',
    sortValue: s => s.support || null,
    align: 'right', optional: true,
    title: 'Support below, sent with the signal — this is where the Stop Loss is placed',
    cellClass: 'tabular-nums text-slate-500',
    cell: s => (s.support ? inr2(s.support) : dash),
  },
  {
    key: 'resistance', label: 'Resistance',
    sortValue: s => s.resistance || null,
    align: 'right', optional: true,
    title: 'Resistance above, sent with the signal — this is where the Target is placed',
    cellClass: 'tabular-nums text-slate-500',
    cell: s => (s.resistance ? inr2(s.resistance) : dash),
  },
  {
    key: 'atr', label: 'ATR',
    sortValue: s => s.atr || null,
    align: 'right', optional: true,
    title: 'ATR sent with the signal',
    cellClass: 'tabular-nums text-slate-500',
    cell: s => (s.atr ? s.atr.toFixed(2) : dash),
  },
  {
    key: 'expiry', label: 'Expiry',
    sortValue: s => s.expiry || null,
    optional: true, cellClass: 'text-slate-500',
    cell: s => s.expiry || dash,
  },
  {
    key: 'profit', label: 'Profit @ Target',
    sortValue: s => s.target ? (s.target - s.ltp) * s.lot_size : null,
    align: 'right', cellClass: 'tabular-nums',
    cell: s => (s.target
      ? <span className="font-semibold text-emerald-600" title={`Target ${inr2(s.target)}`}>
          +{inr(Math.max(0, Math.round((s.target - s.ltp) * s.lot_size)))}
        </span>
      : dash),
  },
  {
    key: 'loss', label: 'Loss @ SL',
    sortValue: s => s.stop_loss ? (s.ltp - s.stop_loss) * s.lot_size : null,
    align: 'right', cellClass: 'tabular-nums',
    cell: s => (s.stop_loss
      ? <span className="font-semibold text-rose-600" title={`SL ${inr2(s.stop_loss)}`}>
          −{inr(Math.max(0, Math.round((s.ltp - s.stop_loss) * s.lot_size)))}
        </span>
      : dash),
  },
  {
    key: 'note', label: 'Note', optional: true, cellClass: 'max-w-[180px] truncate text-slate-400',
    cell: s => <span title={s.note}>{s.note || '—'}</span>,
  },
  {
    key: 'action', label: 'Action', align: 'right', locked: true,
    cell: (s, ctx) => {
      const pos = ctx.held.get(s.symbol)
      const held = !!pos
      const canAct = ['NEW', 'AVERAGING'].includes(s.status)
      if (s.signal_type === 'SELL') {
        return held
          ? <button onClick={() => ctx.onSell(pos!)}
              className="rounded-md bg-rose-600 px-2 py-0.5 text-[10px] font-bold text-white shadow-sm hover:bg-rose-700">Sell / Exit</button>
          : <span className="text-[10px] text-slate-400">exit signal</span>
      }
      if (canAct && s.note.includes('limit')) {
        return <span className="text-[10px] font-medium text-amber-600">Limit reached</span>
      }
      if (canAct && ctx.hardCap && !held) {
        return <span className="rounded-md border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[10px] font-semibold text-rose-600">Budget Exhausted</span>
      }
      if (canAct) {
        // Already in the book ⇒ this is an averaging buy, never a fresh entry.
        return (
          <button onClick={() => ctx.onBuy(s)}
            title={held ? `Averaging buy #${pos!.averaging_count + 1}` : 'Fresh entry'}
            className={`rounded-md px-2 py-0.5 text-[10px] font-bold text-white shadow-sm ${
              held ? 'bg-amber-500 hover:bg-amber-600' : 'bg-emerald-600 hover:bg-emerald-700'}`}>
            {held ? `Average +${pos!.averaging_count + 1}` : 'Buy'}
          </button>
        )
      }
      return (
        <span title={s.note || undefined}>
          <Badge className={statusCls[s.status] || 'border-slate-200 bg-slate-50 text-slate-400'}>
            {s.status.replace('_', ' ')}
          </Badge>
        </span>
      )
    },
  },
]
