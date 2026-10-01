import { useEffect, useMemo, useState } from 'react'
import type { Signal, Position, Preview } from '../types'
import { api } from '../api'
import { inr, inr2 } from '../util'
import { Modal, Field, Toggle, inputCls, btnGreen, btnGhost } from './ui'

type Mode = 'manual' | 'auto'

export function BuyModal({ signal, position, onClose, onDone }: {
  signal: Signal
  /** Open position in this stock, if any — its presence makes this an averaging buy. */
  position?: Position
  onClose: () => void
  onDone: () => void
}) {
  const isAveraging = !!position
  const [lots, setLots] = useState(1)
  const [pv, setPv] = useState<Preview | null>(null)

  // Target
  const [tMode, setTMode] = useState<Mode>('auto')
  const [tMethod, setTMethod] = useState('atr')        // atr | resistance
  const [tManual, setTManual] = useState(signal.target || 0)
  // Stop Loss
  const [sMode, setSMode] = useState<Mode>('auto')
  const [sMethod, setSMethod] = useState('atr')        // atr
  const [sManual, setSManual] = useState(signal.stop_loss || 0)

  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => { api.preview(signal.symbol).then(setPv) }, [signal.symbol])

  const lotSize = pv?.lot_size || signal.lot_size || 1
  const entry = pv?.ltp || signal.ltp
  const qty = lots * lotSize

  // Effective target/SL prices given current toggles (mirrors backend resolve_*)
  const targetPrice = useMemo(() => {
    if (tMode === 'manual') return tManual
    if (!pv) return signal.target
    return tMethod === 'resistance' ? pv.resistance_target : pv.atr_target
  }, [tMode, tMethod, tManual, pv, signal.target])

  const slPrice = useMemo(() => {
    if (sMode === 'manual') return sManual
    if (!pv) return signal.stop_loss
    return pv.atr_sl
  }, [sMode, sManual, pv, signal.stop_loss])

  const projProfit = Math.round((targetPrice - entry) * qty)
  const projLoss = Math.round((entry - slPrice) * qty)

  const submit = async () => {
    setBusy(true); setErr('')
    try {
      const res = position
        ? await api.average(position.id, lots, signal.id)
        : await api.buy({
            symbol: signal.symbol, lots,
            target_mode: tMode, target_method: tMethod, target_value: tMode === 'manual' ? tManual : 0,
            sl_mode: sMode, sl_method: sMethod, sl_value: sMode === 'manual' ? sManual : 0,
            atr: signal.atr, resistance: signal.resistance, support: signal.support,
            signal_id: signal.id,
          })
      if (!res.ok) { setErr(res.error || 'Order failed'); setBusy(false); return }
      onDone(); onClose()
    } catch (e: any) { setErr(String(e)); setBusy(false) }
  }

  return (
    <Modal title={`${isAveraging ? `Averaging Buy #${position!.averaging_count + 1}` : 'Buy Order'} — ${signal.symbol}`} onClose={onClose} wide>
      {/* Why this is not a fresh entry: the stock is already in the open book. */}
      {position && (
        <div className="mb-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          <b>You already hold {signal.symbol}</b> — {position.lots} lot ({position.qty} qty) at
          an average of <b>{inr2(position.avg_price)}</b>
          {position.averaging_count > 0 && <> · {position.averaging_count} averaging buy(s) so far</>}.
          This order is added to that position, not opened as a new one.
        </div>
      )}

      {/* Lot Info + entry */}
      <div className="mb-4 grid grid-cols-4 gap-2 rounded-xl bg-slate-50 p-3 text-center text-xs">
        <div><div className="text-slate-400">LTP (entry)</div><div className="font-bold text-slate-800">{inr2(entry)}</div></div>
        <div><div className="text-slate-400">Lot Size</div><div className="font-bold text-slate-800">{lotSize}</div></div>
        <div><div className="text-slate-400">Qty ({lots} lot)</div><div className="font-bold text-slate-800">{qty}</div></div>
        <div><div className="text-slate-400">Expiry</div><div className="font-bold text-slate-800">{pv?.expiry || signal.expiry || '—'}</div></div>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Field label="Lots (quantity)">
          <input type="number" min={1} value={lots} onChange={e => setLots(Math.max(1, +e.target.value))} className={inputCls} />
        </Field>
        <div />

        {!isAveraging && <>
          {/* TARGET */}
          <div className="rounded-xl border border-slate-200 p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-bold text-slate-700">Target</span>
              <Toggle value={tMode} options={['manual', 'auto']} onChange={v => setTMode(v as Mode)} />
            </div>
            {tMode === 'manual' ? (
              <input type="number" value={tManual} onChange={e => setTManual(+e.target.value)} className={inputCls} placeholder="Target price" />
            ) : (
              <select value={tMethod} onChange={e => setTMethod(e.target.value)} className={inputCls}>
                <option value="atr">ATR-based ({pv ? inr2(pv.atr_target) : '…'})</option>
                <option value="resistance">Nearest Resistance ({pv ? inr2(pv.resistance_target) : '…'})</option>
              </select>
            )}
            <div className="mt-2 flex items-center justify-between text-[11px]">
              <span className="text-slate-400">@ {inr2(targetPrice)}</span>
              <span className="font-bold text-emerald-600">+{inr(Math.max(0, projProfit))} Profit</span>
            </div>
          </div>

          {/* STOP LOSS */}
          <div className="rounded-xl border border-slate-200 p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-bold text-slate-700">Stop Loss</span>
              <Toggle value={sMode} options={['manual', 'auto']} onChange={v => setSMode(v as Mode)} />
            </div>
            {sMode === 'manual' ? (
              <input type="number" value={sManual} onChange={e => setSManual(+e.target.value)} className={inputCls} placeholder="SL price" />
            ) : (
              <select value={sMethod} onChange={e => setSMethod(e.target.value)} className={inputCls}>
                <option value="atr">ATR-based ({pv ? inr2(pv.atr_sl) : '…'})</option>
              </select>
            )}
            <div className="mt-2 flex items-center justify-between text-[11px]">
              <span className="text-slate-400">@ {inr2(slPrice)}</span>
              <span className="font-bold text-rose-600">−{inr(Math.max(0, projLoss))} Loss</span>
            </div>
          </div>
        </>}
      </div>

      {isAveraging && (
        <p className="mt-1 text-xs text-slate-500">Averaging keeps the position's existing Target/SL method and recomputes them off the new average price.</p>
      )}

      {err && <div className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">{err}</div>}

      <div className="mt-5 flex items-center justify-between">
        <div className="text-[11px] text-slate-400">Risk / Reward ≈ <b className="text-slate-600">{projLoss > 0 ? (projProfit / projLoss).toFixed(2) : '—'} : 1</b></div>
        <div className="flex gap-2">
          <button className={btnGhost} onClick={onClose}>Cancel</button>
          <button className={btnGreen} disabled={busy} onClick={submit}>
            {busy ? 'Placing…' : isAveraging ? `Confirm Averaging (${lots} lot)` : `Confirm Buy (${lots} lot)`}
          </button>
        </div>
      </div>
    </Modal>
  )
}
