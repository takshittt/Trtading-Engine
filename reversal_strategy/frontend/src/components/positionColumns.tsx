import type { Position } from '../types'
import { duration, inr, inr2, isExpired, latency, pct, pnlColor, stampShort } from '../util'
import { Badge } from './ui'
import type { ColumnDef } from './ColumnPicker'

export interface PositionCtx {
  onStock: (symbol: string) => void
  onEdit: (p: Position) => void
  onRoll: (p: Position) => void
  onAvg: (p: Position) => void
  actions: (p: Position) => React.ReactNode
}

const dash = <span className="text-slate-300">—</span>

export const POSITION_COLUMNS: ColumnDef<Position, PositionCtx>[] = [
  {
    key: 'symbol', label: 'Symbol', sortValue: p => p.symbol, locked: true,
    cell: (p, ctx) => (
      <>
        <button onClick={() => ctx.onStock(p.symbol)}
          className="text-[11px] font-bold text-slate-800 hover:text-sky-600">{p.symbol}</button>
        {/* Real money is stated, not inferred from the absence of a PAPER tag.
            The Journal already tagged both sides; the live board did not, so the
            one table watched during the session was the one where a real
            position looked like an unlabelled row. AUTO is separate because
            is_paper says which BOOK, opened_by says WHO — and "the machine
            bought this with real money" is the row you most need to spot. */}
        {p.is_paper
          ? <Badge className="ml-1.5 border-indigo-200 bg-indigo-50 text-indigo-700"
              title="Simulated fill — no broker order was sent">PAPER</Badge>
          : <Badge className="ml-1.5 border-emerald-300 bg-emerald-50 text-emerald-700"
              title="Real money — a broker order was placed for this">LIVE</Badge>}
        {p.opened_by === 'auto' && !p.is_paper && (
          <Badge className="ml-1 border-rose-300 bg-rose-50 text-rose-700"
            title="Opened by automated execution, not by you">AUTO</Badge>)}
        {p.trailing_active && <Badge className="ml-1.5 border-emerald-200 bg-emerald-50 text-emerald-700">TRAILING</Badge>}
        {p.used_reserve && <Badge className="ml-1.5 border-amber-200 bg-amber-50 text-amber-700">RESERVE</Badge>}
        {!!p.timeframe && <Badge className="ml-1.5 border-slate-200 bg-slate-50 text-slate-500">{p.timeframe}</Badge>}
        {isExpired(p.expiry) && <Badge className="ml-1.5 border-rose-300 bg-rose-100 text-rose-700"
          title={`Contract expired ${p.expiry} — roll it or square off`}>EXPIRED</Badge>}
      </>
    ),
  },
  {
    // Average first — it is the break-even the position actually trades against.
    // The sub-line carries the entry leg, because slippage is a statement about
    // that leg and averaging is what moved the average away from it.
    key: 'entry', label: 'Avg / Entry', sortValue: p => p.avg_price, align: 'right', cellClass: 'tabular-nums text-slate-600',
    cell: p => (
      <>
        {inr2(p.avg_price)}
        {!!p.signal_price && (
          <div className="text-[9px] text-slate-400"
            title={`Signal ${inr2(p.signal_price)} → entry filled ${inr2(p.entry_price)}\n`
              + `slippage ${p.entry_slippage >= 0 ? '+' : ''}${p.entry_slippage.toFixed(2)}`
              + (p.entry_ltp ? ` (market moved ${p.slip_market >= 0 ? '+' : ''}${p.slip_market.toFixed(2)}, `
                + `spread ${p.slip_spread >= 0 ? '+' : ''}${p.slip_spread.toFixed(2)})` : '')
              + (p.averaging_count
                ? `\naveraging ×${p.averaging_count} moved the average by `
                  + `${(p.avg_price - p.entry_price >= 0 ? '+' : '')}${(p.avg_price - p.entry_price).toFixed(2)}`
                : '')}>
            sig {inr2(p.signal_price)} · ent {inr2(p.entry_price)}
            <span className={p.entry_slippage > 0 ? 'text-rose-500' : 'text-emerald-600'}>
              {' '}{p.entry_slippage >= 0 ? '+' : ''}{p.entry_slippage.toFixed(2)}
            </span>
          </div>
        )}
      </>
    ),
  },
  {
    key: 'ltp', label: 'LTP', sortValue: p => p.ltp, align: 'right', cellClass: 'tabular-nums font-medium text-slate-800',
    cell: p => inr2(p.ltp),
  },
  {
    key: 'lots', label: 'Lot Info', sortValue: p => p.lots, align: 'center',
    cell: p => (
      <span className="rounded bg-slate-100 px-1 py-px text-[9px] font-semibold text-slate-600">
        {p.lots} × {p.lot_size} = {p.qty}
      </span>
    ),
  },
  {
    key: 'target', label: 'Target', sortValue: p => p.target, align: 'right', cellClass: 'tabular-nums text-emerald-600',
    cell: p => <>{inr2(p.target)}{p.target_hit && <span className="ml-1 text-[9px] text-emerald-500">hit</span>}</>,
  },
  {
    key: 'sl', label: 'SL', sortValue: p => p.trailing_active ? p.trailing_sl : p.stop_loss, align: 'right', cellClass: 'tabular-nums text-rose-600',
    cell: p => (p.trailing_active
      ? <span title="Trailing stop">{inr2(p.trailing_sl)} <span className="text-[9px] text-slate-400">trail</span></span>
      : inr2(p.stop_loss)),
  },
  {
    key: 'pnl', label: 'Unreal. P&L', sortValue: p => p.unrealized_pnl, align: 'right',
    cellClass: (p) => `tabular-nums font-bold ${pnlColor(p.unrealized_pnl)}`,
    cell: p => <>{inr2(p.unrealized_pnl)}<div className="text-[9px] font-normal leading-tight">{pct(p.unrealized_pct)}</div></>,
  },
  // The two "what happens if it gets there" numbers, mirroring the signals feed.
  // Measured off avg_price and the whole held quantity, NOT off the LTP and one
  // lot as on the signals board: a signal is a prospective entry, so LTP × lot
  // size is what it would cost you; an open position is already on the books, so
  // the money it makes or loses is measured against the cost basis it actually
  // carries, across every lot averaging has added.
  {
    key: 'profit_at_target', label: 'Profit @ Target',
    sortValue: p => (p.target ? (p.target - p.avg_price) * p.qty : null),
    align: 'right', cellClass: 'tabular-nums',
    title: 'What this position books if the target is reached — from the average price, on the full quantity',
    cell: p => {
      if (!p.target) return dash
      const amt = Math.round((p.target - p.avg_price) * p.qty)
      return (
        <span className={`font-semibold ${pnlColor(amt)}`}
          title={`Target ${inr2(p.target)} − avg ${inr2(p.avg_price)} = ${(p.target - p.avg_price).toFixed(2)} × ${p.qty}`}>
          {amt >= 0 ? '+' : '−'}{inr(Math.abs(amt))}
        </span>
      )
    },
  },
  {
    key: 'loss_at_sl', label: 'Loss @ SL',
    // Follows the trailing stop once it is armed, exactly as the SL column does —
    // that is the level this position would actually exit on.
    sortValue: p => {
      const stop = p.trailing_active ? p.trailing_sl : p.stop_loss
      return stop ? (stop - p.avg_price) * p.qty : null
    },
    align: 'right', cellClass: 'tabular-nums',
    title: 'What this position books if the stop is hit — the trailing stop once it is armed. '
      + 'Positive means the stop has trailed above the average and now locks in a profit.',
    cell: p => {
      const stop = p.trailing_active ? p.trailing_sl : p.stop_loss
      if (!stop) return dash
      // Deliberately signed rather than clamped at zero. Once the trail ratchets
      // past the average the stop guarantees a gain, and that is the single most
      // useful thing this column can say — showing it as "−₹0" would bury it.
      const amt = Math.round((stop - p.avg_price) * p.qty)
      return (
        <span className={`font-semibold ${pnlColor(amt)}`}
          title={`${p.trailing_active ? 'Trailing stop' : 'Stop'} ${inr2(stop)} − avg ${inr2(p.avg_price)} `
            + `= ${(stop - p.avg_price).toFixed(2)} × ${p.qty}`
            + (amt > 0 ? '\nStop is above the average — this exit is locked in as a profit.' : '')}>
          {amt >= 0 ? '+' : '−'}{inr(Math.abs(amt))}
        </span>
      )
    },
  },
  {
    key: 'avg', label: 'Avg', sortValue: p => p.averaging_count, align: 'center',
    title: 'How many times this stock was averaged — click for the leg-by-leg breakdown',
    cell: (p, ctx) => (p.averaging_count > 0
      ? <button onClick={() => ctx.onAvg(p)} title="Show each averaging buy — time, price, and the average it produced"
          className="rounded-md border border-amber-200 bg-amber-50 px-1 py-0.5 text-[10px] font-semibold text-amber-700 hover:bg-amber-100 hover:underline">
          ×{p.averaging_count}
        </button>
      : dash),
  },
  {
    key: 'signal_time', label: 'Signal Time', sortValue: p => p.signal_received_at ? new Date(p.signal_received_at).getTime() : null, cellClass: 'whitespace-nowrap text-slate-500',
    title: "When the signal reached the server",
    cell: p => <span title={p.signal_time ? `Signal candle: ${stampShort(p.signal_time)}` : 'Not signal-driven'}>
      {stampShort(p.signal_received_at)}
    </span>,
  },
  {
    key: 'executed', label: 'Executed', sortValue: p => p.executed_at ? new Date(p.executed_at).getTime() : null, cellClass: 'whitespace-nowrap text-slate-500',
    title: 'When the fill was stamped, and how long that took',
    cell: p => (
      <>
        {stampShort(p.executed_at)}
        {!!p.exec_latency_ms && <div className="text-[9px] leading-tight text-indigo-500">+{latency(p.exec_latency_ms)}</div>}
      </>
    ),
  },
  {
    key: 'bidask', label: 'Entry Bid/Ask', align: 'right', optional: true,
    cellClass: 'tabular-nums text-slate-400',
    title: 'Top of book crossed on entry',
    cell: p => (p.entry_bid ? `${p.entry_bid.toFixed(2)} / ${p.entry_ask.toFixed(2)}` : dash),
  },
  {
    key: 'mfe', label: 'MFE', sortValue: p => p.mfe, align: 'right', optional: true, cellClass: 'tabular-nums text-emerald-600',
    title: 'Max favourable excursion — the best this trade has been',
    cell: p => inr2(p.mfe),
  },
  {
    key: 'mae', label: 'MAE', sortValue: p => p.mae, align: 'right', optional: true, cellClass: 'tabular-nums text-rose-600',
    title: 'Max adverse excursion — the worst it has been',
    cell: p => inr2(p.mae),
  },
  {
    key: 'margin', label: 'Margin', sortValue: p => p.margin_used || null, align: 'right', optional: true,
    cellClass: 'tabular-nums text-slate-500',
    cell: p => (p.margin_used ? inr2(p.margin_used) : dash),
  },
  {
    key: 'held', label: 'Held', sortValue: p => p.held_seconds ?? null, align: 'right', optional: true, cellClass: 'text-slate-500',
    cell: p => duration(p.held_seconds),
  },
  { key: 'days', label: 'Days', sortValue: p => p.days_held, align: 'right', cellClass: 'text-slate-500', cell: p => `${p.days_held}d` },
  { key: 'expiry', label: 'Expiry', sortValue: p => p.expiry || null, cellClass: 'text-slate-500', cell: p => p.expiry?.slice(0, 9) || dash },
  {
    key: 'actions', label: 'Actions', align: 'right', locked: true,
    cell: (p, ctx) => ctx.actions(p),
  },
]
