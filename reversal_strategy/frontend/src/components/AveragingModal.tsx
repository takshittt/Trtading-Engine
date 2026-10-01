import { useEffect, useState } from 'react'
import type { PositionLegs } from '../types'
import { api } from '../api'
import { inr2, latency, pnlColor, stampShort } from '../util'
import { Badge, Modal } from './ui'

/**
 * Leg-by-leg breakdown of one position — what the ×N averaging badge opens.
 *
 * `avg_price` only ever shows where a position ended up. When it is sitting on
 * a loss the question is where each buy went in and what it did to the cost
 * basis, which is exactly what this lists: time, fill, the book it crossed, and
 * the average that leg produced.
 */
export function AveragingModal({ positionId, symbol, onClose }: {
  positionId: number; symbol: string; onClose: () => void
}) {
  const [data, setData] = useState<PositionLegs | null>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    let alive = true
    api.legs(positionId)
      .then(d => { if (alive) (d.ok ? setData(d) : setErr(d.error || 'Could not load legs')) })
      .catch(() => { if (alive) setErr('Could not reach the backend.') })
    return () => { alive = false }
  }, [positionId])

  const p = data?.position
  const buys = (data?.legs ?? []).filter(l => l.side === 'BUY')

  return (
    <Modal title={`⊕ Averaging breakdown — ${symbol}`} onClose={onClose} wide>
      {err && <div className="py-6 text-center text-xs text-rose-600">{err}</div>}
      {!data && !err && <div className="py-6 text-center text-xs text-slate-400">Loading legs…</div>}

      {data && p && (
        <>
          <div className="mb-3 grid grid-cols-4 gap-2">
            {[
              ['Entry fill', inr2(p.entry_price), 'text-slate-800'],
              ['Averaged', `×${p.averaging_count}`, 'text-amber-600'],
              ['Avg price now', inr2(p.avg_price), 'text-slate-800'],
              ['LTP', inr2(data.ltp), pnlColor(data.ltp - p.avg_price)],
            ].map(([k, v, cls]) => (
              <div key={k} className="rounded-xl border border-slate-100 bg-slate-50 p-2 text-center">
                <div className="text-[10px] text-slate-400">{k}</div>
                <div className={`text-sm font-bold tabular-nums ${cls}`}>{v}</div>
              </div>
            ))}
          </div>

          <p className="mb-2 text-[11px] text-slate-400">
            Signal fired at <b className="text-slate-600">{inr2(p.signal_price)}</b> · entry filled
            at <b className="text-slate-600">{inr2(p.entry_price)}</b> ({p.entry_slippage >= 0 ? '+' : ''}
            {p.entry_slippage.toFixed(2)} slippage). The average moved
            to <b className="text-slate-600">{inr2(p.avg_price)}</b> because
            of {p.averaging_count} averaging buy{p.averaging_count === 1 ? '' : 's'} below —
            that drift is not slippage.
          </p>

          <div className="overflow-x-auto rounded-xl border border-slate-100">
            <table className="w-full text-left text-[11px]">
              <thead className="bg-slate-50 text-[9px] uppercase tracking-wide text-slate-400">
                <tr className="border-b border-slate-100">
                  {['Leg', 'Time', 'Qty', 'Fill', 'Limit', 'Bid / Ask', 'LTP then',
                    'Spread paid', 'Avg after', 'Δ Avg', 'Lat.'].map(h => (
                    <th key={h} className="whitespace-nowrap px-1.5 py-1">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.legs.map(l => (
                  <tr key={l.order_id} className={`border-b border-slate-50 ${
                    l.side === 'SELL' ? 'bg-sky-50/50' : l.n > 1 ? 'bg-amber-50/50' : ''}`}>
                    <td className="whitespace-nowrap px-1.5 py-1.5">
                      <Badge className={l.side === 'SELL'
                        ? 'border-sky-200 bg-sky-50 text-sky-700'
                        : l.n > 1 ? 'border-amber-200 bg-amber-50 text-amber-700'
                          : 'border-emerald-200 bg-emerald-50 text-emerald-700'}>{l.label}</Badge>
                      <div className="mt-0.5 text-[9px] text-slate-400">{l.intent}</div>
                    </td>
                    <td className="whitespace-nowrap px-1.5 py-1.5 text-slate-500">{stampShort(l.at)}</td>
                    <td className="whitespace-nowrap px-1.5 py-1.5 tabular-nums text-slate-500">
                      {l.qty}<span className="text-[9px] text-slate-400"> · {l.lots} lot</span>
                    </td>
                    <td className="px-1.5 py-1.5 tabular-nums font-semibold text-slate-800">{inr2(l.price)}</td>
                    <td className="px-1.5 py-1.5 tabular-nums text-slate-400">
                      {l.limit_price ? inr2(l.limit_price) : '—'}
                    </td>
                    <td className="whitespace-nowrap px-1.5 py-1.5 tabular-nums text-slate-400">
                      {l.bid ? `${l.bid.toFixed(2)} / ${l.ask.toFixed(2)}` : '—'}
                    </td>
                    <td className="px-1.5 py-1.5 tabular-nums text-slate-500">
                      {l.ltp_at_order ? inr2(l.ltp_at_order) : '—'}
                    </td>
                    <td className={`px-1.5 py-1.5 tabular-nums ${pnlColor(-l.spread_paid)}`}>
                      {l.ltp_at_order ? l.spread_paid.toFixed(2) : '—'}
                    </td>
                    <td className="px-1.5 py-1.5 tabular-nums font-semibold text-slate-800">
                      {l.qty_after ? inr2(l.avg_after) : '—'}
                    </td>
                    <td className={`px-1.5 py-1.5 tabular-nums ${pnlColor(-l.avg_delta)}`}>
                      {l.avg_delta ? `${l.avg_delta > 0 ? '+' : ''}${l.avg_delta.toFixed(2)}` : '—'}
                    </td>
                    <td className="px-1.5 py-1.5 tabular-nums text-indigo-600">{latency(l.latency_ms)}</td>
                  </tr>
                ))}
                {data.legs.length === 0 && (
                  <tr><td colSpan={11} className="px-3 py-8 text-center text-slate-400">
                    No order legs recorded for this position.
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>

          {buys.length > 1 && (
            <p className="mt-2 text-[10px] text-slate-400">
              Each averaging buy pulls the cost basis toward its own fill —
              ₹{Math.abs(p.avg_price - p.entry_price).toFixed(2)} in total
              from the entry. Break-even is the <b>Avg after</b> of the last row,
              not the entry price.
            </p>
          )}
        </>
      )}
    </Modal>
  )
}
