import { useEffect, useState } from 'react'
import type { StockInfo } from '../types'
import { api } from '../api'
import { inr, inr2, pnlColor, pct } from '../util'
import { Modal, Field, inputCls, btnDanger, btnGhost, btnPrimary } from './ui'

/** Per-stock configuration modal — opens when a stock is clicked. */
export function StockModal({ symbol, onClose, refresh }: {
  symbol: string; onClose: () => void; refresh: () => void
}) {
  const [info, setInfo] = useState<StockInfo | null>(null)
  const [target, setTarget] = useState(0)
  const [sl, setSl] = useState(0)

  const load = () => api.stock(symbol).then(i => {
    setInfo(i)
    if (i.held) { setTarget(i.held.target); setSl(i.held.stop_loss) }
  })
  useEffect(() => { load() }, [symbol])

  if (!info) return <Modal title={symbol} onClose={onClose}><div className="py-6 text-center text-slate-400">Loading…</div></Modal>

  return (
    <Modal title={`Stock — ${info.symbol}`} onClose={onClose} wide>
      <div className="grid grid-cols-4 gap-2 rounded-xl bg-slate-50 p-3 text-center text-xs">
        <div><div className="text-slate-400">LTP</div><div className="font-bold text-slate-800">{inr2(info.ltp)}</div></div>
        <div><div className="text-slate-400">Lot Size</div><div className="font-bold text-slate-800">{info.lot_size}</div></div>
        <div><div className="text-slate-400">Margin / Lot</div><div className="font-bold text-slate-800">{inr(info.margin_per_lot)}</div></div>
        <div><div className="text-slate-400">Expiry</div><div className="font-bold text-slate-800">{info.expiry || '—'}</div></div>
      </div>

      <div className="mt-3 text-xs text-slate-500">Segment: <span className="font-medium text-slate-700">{info.sector}</span></div>

      {info.held ? (
        <div className="mt-4 rounded-xl border border-slate-200 p-3">
          <div className="mb-2 flex items-center justify-between">
            <h4 className="text-xs font-bold text-slate-700">Held Position — override Target / SL</h4>
            <span className={`text-xs font-bold ${pnlColor(info.held.unrealized_pnl)}`}>{inr2(info.held.unrealized_pnl)} ({pct(info.held.unrealized_pct)})</span>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Target"><input type="number" value={target} onChange={e => setTarget(+e.target.value)} className={inputCls} /></Field>
            <Field label="Stop Loss"><input type="number" value={sl} onChange={e => setSl(+e.target.value)} className={inputCls} /></Field>
          </div>
          <div className="mt-3 flex justify-end">
            <button className={btnPrimary} onClick={async () => { await api.editTargets(info.held!.id, { target, stop_loss: sl }); refresh(); onClose() }}>Save Overrides</button>
          </div>
        </div>
      ) : (
        <div className="mt-4 rounded-xl border border-dashed border-slate-200 p-3 text-center text-xs text-slate-400">No open position in this stock.</div>
      )}

      <div className="mt-4 flex items-center justify-between border-t border-slate-100 pt-3">
        <span className="text-xs text-slate-400">
          {info.blacklisted ? 'Blacklisted — signals are hidden.' : 'Blacklisting hides all future signals for this stock.'}
        </span>
        {info.blacklisted ? (
          <button className={btnGhost} onClick={async () => { await api.removeBlacklist(info.symbol); await load(); refresh() }}>Remove from Blacklist</button>
        ) : (
          <button className={btnDanger} onClick={async () => { await api.addBlacklist(info.symbol); await load(); refresh() }}>Add to Blacklist</button>
        )}
      </div>
    </Modal>
  )
}
