import type { Position } from '../types'
import { duration, inr2, latency, pct, pnlColor, stampShort, tfColor } from '../util'
import { Badge } from './ui'
import type { ColumnDef } from './ColumnPicker'

export interface JournalCtx {
  onStock: (symbol: string) => void
  onAvg: (t: Position) => void
}

const dash = <span className="text-slate-300">—</span>
const num = 'tabular-nums whitespace-nowrap'

/**
 * Closed-trade columns.
 *
 * The full set is a forensic record of the round trip — both signal→fill legs,
 * the book crossed, excursions — which is far more than fits on screen. The
 * P&L-and-timing core is on by default; the rest is a tick away.
 */
export const JOURNAL_COLUMNS: ColumnDef<Position, JournalCtx>[] = [
  {
    key: 'symbol', label: 'Symbol', sortValue: t => t.symbol, locked: true,
    cell: (t, ctx) => (
      <button className="font-bold text-slate-800 hover:text-sky-600"
        onClick={() => ctx.onStock(t.symbol)}>{t.symbol}</button>
    ),
  },
  {
    key: 'book', label: 'Book', sortValue: t => t.is_paper ? 1 : 0,
    cell: t => (
      <Badge className={t.is_paper
        ? 'border-indigo-200 bg-indigo-50 text-indigo-700'
        : 'border-emerald-200 bg-emerald-50 text-emerald-700'}>{t.is_paper ? 'PAPER' : 'LIVE'}</Badge>
    ),
  },
  {
    key: 'tf', label: 'TF', sortValue: t => ({ '1H': 1, '4H': 4, '1D': 24 }[t.timeframe] ?? 0), optional: true,
    cell: t => (t.timeframe ? <Badge className={tfColor[t.timeframe]}>{t.timeframe}</Badge> : dash),
  },
  {
    key: 'signal_price', label: 'Signal @', sortValue: t => t.signal_price || null, align: 'right', optional: true, cellClass: num,
    cell: t => (t.signal_price ? inr2(t.signal_price) : dash),
  },
  {
    key: 'entry', label: 'Entry', sortValue: t => t.avg_price, align: 'right', cellClass: num,
    title: 'Weighted average paid. An averaged trade shows its first fill underneath.',
    cell: t => (
      <>
        {inr2(t.avg_price)}
        {t.averaging_count > 0 && !!t.entry_price && (
          <div className="text-[9px] font-normal text-slate-400"
            title="First entry leg — the average moved from here as the trade was averaged">
            ent {inr2(t.entry_price)}
          </div>
        )}
      </>
    ),
  },
  {
    key: 'slip', label: 'Slip', sortValue: t => t.entry_slippage, align: 'right', optional: true,
    title: 'Entry fill minus the price when the signal fired — the entry leg only, '
      + 'never the averaged-up cost basis',
    cellClass: (t) => `${num} ${pnlColor(-t.entry_slippage)}`,
    cell: t => (t.signal_price
      ? <span title={t.entry_ltp
          ? `market moved ${t.slip_market >= 0 ? '+' : ''}${t.slip_market.toFixed(2)} before the order, `
            + `spread cost ${t.slip_spread >= 0 ? '+' : ''}${t.slip_spread.toFixed(2)}`
          : undefined}>{t.entry_slippage.toFixed(2)}</span>
      : dash),
  },
  {
    key: 'entry_book', label: 'Entry Bid/Ask', align: 'right', optional: true,
    cellClass: `${num} text-slate-400`,
    cell: t => (t.entry_bid ? `${t.entry_bid.toFixed(2)} / ${t.entry_ask.toFixed(2)}` : dash),
  },
  {
    key: 'signal_time', label: 'Signal Time', sortValue: t => t.signal_received_at ? new Date(t.signal_received_at).getTime() : null, optional: true, cellClass: 'whitespace-nowrap text-slate-500',
    cell: t => stampShort(t.signal_received_at),
  },
  {
    key: 'exec_time', label: 'Executed', sortValue: t => t.executed_at ? new Date(t.executed_at).getTime() : null, cellClass: 'whitespace-nowrap text-slate-500',
    cell: t => stampShort(t.executed_at),
  },
  {
    key: 'entry_lat', label: 'Entry Lat.', sortValue: t => t.exec_latency_ms, align: 'right', cellClass: `${num} text-indigo-600`,
    title: 'Signal received → entry filled',
    cell: t => latency(t.exec_latency_ms),
  },
  {
    key: 'exit_signal', label: 'Exit Signal', sortValue: t => t.exit_signal_at ? new Date(t.exit_signal_at).getTime() : null, optional: true, cellClass: 'whitespace-nowrap text-slate-500',
    cell: t => stampShort(t.exit_signal_at),
  },
  {
    key: 'exit_exec', label: 'Exit Exec', sortValue: t => t.exit_executed_at ? new Date(t.exit_executed_at).getTime() : null, optional: true, cellClass: 'whitespace-nowrap text-slate-500',
    cell: t => stampShort(t.exit_executed_at),
  },
  {
    key: 'exit_lat', label: 'Exit Lat.', sortValue: t => t.exit_latency_ms, align: 'right', optional: true,
    cellClass: `${num} text-indigo-600`,
    cell: t => latency(t.exit_latency_ms),
  },
  { key: 'exit', label: 'Exit', sortValue: t => t.exit_price, align: 'right', cellClass: num, cell: t => inr2(t.exit_price) },
  {
    key: 'exit_book', label: 'Exit Bid/Ask', align: 'right', optional: true,
    cellClass: `${num} text-slate-400`,
    cell: t => (t.exit_bid ? `${t.exit_bid.toFixed(2)} / ${t.exit_ask.toFixed(2)}` : dash),
  },
  { key: 'qty', label: 'Qty', sortValue: t => t.exit_qty || t.qty, align: 'right', optional: true, cellClass: num, cell: t => t.exit_qty || t.qty },
  {
    key: 'avg', label: 'Avg×', sortValue: t => t.averaging_count, align: 'center', cellClass: num,
    title: 'How many times the stock was averaged — click for each buy: time, price, '
      + 'and the average it produced',
    cell: (t, ctx) => (t.averaging_count > 0
      ? <button onClick={() => ctx.onAvg(t)} title="Show the averaging breakdown"
          className="rounded-md border border-amber-200 bg-amber-50 px-1 py-0.5 text-[10px] font-semibold text-amber-700 hover:bg-amber-100 hover:underline">
          ×{t.averaging_count}
        </button>
      : dash),
  },
  { key: 'held', label: 'Held', sortValue: t => t.held_seconds ?? null, align: 'right', cellClass: num, cell: t => duration(t.held_seconds) },
  {
    key: 'mfe', label: 'MFE', sortValue: t => t.mfe, align: 'right', optional: true, cellClass: `${num} text-emerald-600`,
    title: 'Max favourable excursion — the best this trade ever was',
    cell: t => inr2(t.mfe),
  },
  {
    key: 'mae', label: 'MAE', sortValue: t => t.mae, align: 'right', optional: true, cellClass: `${num} text-rose-600`,
    title: 'Max adverse excursion — the worst it ever was',
    cell: t => inr2(t.mae),
  },
  {
    key: 'reason', label: 'Reason', sortValue: t => t.exit_reason || null,
    cell: t => <Badge className="border-slate-200 bg-slate-50 text-slate-500">{t.exit_reason || '—'}</Badge>,
  },
  {
    key: 'pnl', label: 'P&L', sortValue: t => t.realized_pnl, align: 'right', locked: true,
    cellClass: (t) => `${num} font-bold ${pnlColor(t.realized_pnl)}`,
    cell: t => inr2(t.realized_pnl),
  },
  {
    key: 'pnl_pct', label: 'P&L %', sortValue: t => t.realized_pct, align: 'right',
    cellClass: (t) => `${num} ${pnlColor(t.realized_pct)}`,
    cell: t => pct(t.realized_pct),
  },
]
