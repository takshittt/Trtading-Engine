import { useEffect, useMemo, useState } from 'react'
import type { Position, RollTarget } from '../types'
import { api } from '../api'
import { inr2 } from '../util'
import { Modal, inputCls, btnGhost } from './ui'

const fmtSigned = (n: number, dp = 2) =>
  `${n >= 0 ? '+' : '−'}₹${Math.abs(n).toLocaleString('en-IN', { minimumFractionDigits: dp, maximumFractionDigits: dp })}`

/**
 * Two-leg rollover: close the near contract, open a later one.
 *
 * The far contract is picked from the expiries the exchange actually lists for
 * this underlying — nearest first — rather than typed as a date, because a roll
 * always lands on a listed contract and a free-text date can name one that does
 * not exist. Both legs are real orders, so the dialog shows the spread being
 * crossed and what the roll costs before anything is sent.
 */
export function RolloverModal({ position, onClose, onDone }: {
  position: Position; onClose: () => void; onDone: (msg: string) => void
}) {
  const [targets, setTargets] = useState<RollTarget[]>([])
  const [loading, setLoading] = useState(true)
  const [tsym, setTsym] = useState('')
  const [qty, setQty] = useState(position.qty)
  const [priceType, setPriceType] = useState<'LMT' | 'MKT'>('LMT')
  const [exitPrice, setExitPrice] = useState(position.ltp || position.avg_price)
  const [entryPrice, setEntryPrice] = useState(0)
  const [carryPnl, setCarryPnl] = useState(true)
  const [carryTarget, setCarryTarget] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    let alive = true
    api.rollTargets(position.id)
      .then(rows => {
        if (!alive) return
        setTargets(rows)
        setTsym(rows[0]?.tsym ?? '')
      })
      .catch(() => alive && setTargets([]))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [position.id])

  const far = useMemo(() => targets.find(t => t.tsym === tsym), [targets, tsym])

  // Buy the far leg on the ask; fall back to its last price.
  useEffect(() => {
    if (far) setEntryPrice(far.ask || far.ltp || 0)
  }, [far])

  const nearPx = priceType === 'LMT' ? exitPrice : (position.ltp || position.avg_price)
  const farPx = priceType === 'LMT' ? entryPrice : (far?.ltp ?? 0)
  const spread = farPx - nearPx
  // Long-only book: carrying forward means buying the far leg and selling the
  // near one, so a dearer far month is money out.
  const rollCost = spread * qty
  const legPnl = (nearPx - position.avg_price) * qty
  const noTargets = !loading && targets.length === 0

  const submit = async () => {
    if (!tsym) { setErr('Pick the contract to roll into.'); return }
    setBusy(true); setErr('')
    try {
      const r = await api.roll(position.id, {
        target_tsym: tsym, target_expiry: far?.expiry ?? '', qty,
        price_type: priceType,
        exit_price: priceType === 'LMT' ? exitPrice : 0,
        entry_price: priceType === 'LMT' ? entryPrice : 0,
        carry_pnl: carryPnl, carry_target: carryTarget,
      })
      if (!r.ok) { setErr(r.error || 'Rollover failed'); setBusy(false); return }
      onDone(`${position.symbol} rolled to ${tsym} · basis ${r.basis >= 0 ? '+' : ''}${r.basis}`)
      onClose()
    } catch (e: any) { setErr(String(e)); setBusy(false) }
  }

  return (
    <Modal title={`Rollover · ${position.symbol}`} onClose={onClose}
      footer={
        <div className="flex items-center justify-end gap-2">
          <button className={btnGhost} onClick={onClose}>Cancel</button>
          <button disabled={busy || !tsym || loading}
            className="rounded-lg bg-amber-500 px-4 py-2 text-sm font-semibold text-white hover:bg-amber-600 disabled:opacity-50"
            onClick={submit}>
            {busy ? 'Rolling…' : `Roll ${qty} → ${tsym || '…'}`}
          </button>
        </div>
      }>

      {/* Near → far */}
      <div className="flex items-stretch gap-3">
        <div className="flex-1 rounded-xl border border-slate-200 bg-slate-50 p-2.5">
          <div className="text-[10px] uppercase tracking-wide text-slate-400">Close (near)</div>
          <div className="mt-0.5 font-mono text-sm font-semibold text-slate-800">{position.symbol}</div>
          <div className="text-[11px] text-slate-400">{position.expiry || '—'}</div>
          <div className="mt-1 text-[11px] text-slate-500">LTP <span className="font-mono text-slate-800">{inr2(position.ltp)}</span></div>
        </div>
        <div className="flex items-center text-lg text-amber-500">→</div>
        <div className="flex-1 rounded-xl border border-amber-300 bg-amber-50 p-2.5">
          <div className="text-[10px] uppercase tracking-wide text-slate-400">Open (far)</div>
          {loading ? (
            <div className="mt-1 text-xs text-slate-400">Loading expiries…</div>
          ) : noTargets ? (
            <div className="mt-1 text-xs text-amber-700">No later expiry listed</div>
          ) : (
            <select value={tsym} onChange={e => setTsym(e.target.value)}
              className={`${inputCls} mt-0.5 font-mono`}>
              {targets.map(t => <option key={t.tsym} value={t.tsym}>{t.tsym}</option>)}
            </select>
          )}
          <div className="text-[11px] text-slate-400">{far?.expiry || '—'}</div>
          <div className="mt-1 text-[11px] text-slate-500">LTP <span className="font-mono text-slate-800">{far ? inr2(far.ltp) : '…'}</span></div>
        </div>
      </div>

      {!noTargets && (
        <>
          <div className="mt-3 flex items-center justify-between rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
            <span className="text-slate-500">Roll spread (far − near)</span>
            <span className="font-mono text-slate-700">{fmtSigned(spread)}</span>
          </div>
          <div className="mt-1 flex items-center justify-between px-3 text-xs">
            <span className="text-slate-500">Est. roll cost · {qty} qty</span>
            <span className={`font-mono font-semibold ${rollCost > 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
              {fmtSigned(rollCost, 0)} {rollCost > 0 ? 'debit' : 'credit'}
            </span>
          </div>
        </>
      )}

      <div className="mt-3 grid grid-cols-2 gap-3">
        <div>
          <label className="text-[11px] font-semibold text-slate-500">Side</label>
          <div className="mt-1 rounded-lg bg-sky-50 px-3 py-2 text-center text-sm font-semibold text-sky-700">BUY (long)</div>
        </div>
        <div>
          <label className="text-[11px] font-semibold text-slate-500">Quantity (open {position.qty})</label>
          <input type="number" min={1} max={position.qty} value={qty}
            onChange={e => setQty(Math.max(1, Math.min(position.qty, +e.target.value || 1)))}
            className={`${inputCls} mt-1 font-mono`} />
        </div>
      </div>

      <div className="mt-3">
        <div className="mb-1.5 flex items-center justify-between">
          <label className="text-[11px] font-semibold text-slate-500">Pricing</label>
          <div className="inline-flex overflow-hidden rounded-lg border border-slate-200 text-xs">
            {(['LMT', 'MKT'] as const).map(pt => (
              <button key={pt} onClick={() => setPriceType(pt)}
                className={`px-3 py-1 font-semibold ${priceType === pt ? 'bg-amber-500 text-white' : 'text-slate-500 hover:text-slate-700'}`}>
                {pt === 'LMT' ? 'Limit' : 'Market'}
              </button>
            ))}
          </div>
        </div>
        {priceType === 'LMT' ? (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-[11px] text-slate-500">Exit price (near)</label>
              <input type="number" step={0.05} min={0} value={exitPrice}
                onChange={e => setExitPrice(Math.max(0, +e.target.value || 0))}
                className={`${inputCls} mt-1 font-mono`} />
            </div>
            <div>
              <label className="text-[11px] text-slate-500">Entry price (far)</label>
              <input type="number" step={0.05} min={0} value={entryPrice}
                onChange={e => setEntryPrice(Math.max(0, +e.target.value || 0))}
                className={`${inputCls} mt-1 font-mono`} />
            </div>
          </div>
        ) : (
          <p className="text-[11px] text-slate-500">
            Both legs fire at market — instant and guaranteed. You accept the spread as it stands.
          </p>
        )}
        <p className="mt-1 text-[11px] text-slate-400">
          The near leg is closed first, then {tsym || 'the far contract'} is bought.
          {priceType === 'LMT' && ' A limit that does not fill leaves the roll incomplete — use Market to guarantee it.'}
        </p>
      </div>

      <label className="mt-3 flex cursor-pointer items-start gap-2 text-xs text-slate-600">
        <input type="checkbox" checked={carryPnl} onChange={e => setCarryPnl(e.target.checked)} className="mt-0.5" />
        <span>
          <b className="text-slate-700">Carry P&amp;L forward</b> — {tsym || 'the far contract'} continues from this
          position's result ({fmtSigned(legPnl, 0)} so far) instead of booking it and starting clean.
        </span>
      </label>
      <label className="mt-2 flex cursor-pointer items-start gap-2 text-xs text-slate-600">
        <input type="checkbox" checked={carryTarget} onChange={e => setCarryTarget(e.target.checked)} className="mt-0.5" />
        <span>
          <b className="text-slate-700">Carry Target &amp; SL</b> — both shift by the roll spread, keeping their
          rupee distance ({inr2(position.target)} / {inr2(position.stop_loss)} today).
        </span>
      </label>

      {err && <div className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">{err}</div>}
    </Modal>
  )
}
