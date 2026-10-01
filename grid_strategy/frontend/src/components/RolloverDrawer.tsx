import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { InstrumentSnap, RollLotPlan, RolloverPlan } from '../types'
import { Badge, fmtPx, inr, pnlClass } from './ui'

/** Expandable rollover drawer under a watchlist row (▼ on the Rollover cell).
 *
 * Shows EVERY open rung with the full money math of rolling it now into the
 * chosen contract — current price, basis (far − near), P&L booked on the old
 * leg, roll cost, and the entry/target/SL each rung would carry after the roll
 * (all basis-shifted, so the economics are unchanged: new entry = entry +
 * basis, target' = target + basis, sl' = sl + basis).
 *
 * Manual mode adds a contract picker over the LISTED later futures (a roll
 * always lands on a listed contract — never a typed date). Quick roll per rung
 * opens a confirm card with the numbers, then rolls that ONE rung: sell leg
 * verified, then buy leg verified, nothing else touched. */
export function RolloverDrawer({ inst, onChanged }: {
  inst: InstrumentSnap
  onChanged: () => void
}) {
  const [plan, setPlan] = useState<RolloverPlan | null>(null)
  const [target, setTarget] = useState('')          // token of the chosen contract ('' = nearest)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [busyLot, setBusyLot] = useState(0)
  const [confirmLot, setConfirmLot] = useState<RollLotPlan | null>(null)
  const [confirmAll, setConfirmAll] = useState(false)
  const manual = inst.rollover.mode === 'manual'

  const load = useCallback(async (tok: string) => {
    setLoading(true)
    setErr('')
    try {
      setPlan(await api.rolloverPlan(inst.id, tok))
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'could not load the rollover plan')
      setPlan(null)
    } finally {
      setLoading(false)
    }
  }, [inst.id])

  useEffect(() => { load(target) }, [load, target])

  const quickRoll = async (lot: RollLotPlan) => {
    setBusyLot(lot.lot_id)
    setErr('')
    try {
      const t = plan && target ? { target_tsym: plan.chosen.tsym, target_token: plan.chosen.token } : undefined
      await api.rollLot(lot.lot_id, t)
      setConfirmLot(null)
      onChanged()
      await load(target)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'quick roll failed')
    } finally {
      setBusyLot(0)
    }
  }

  const rollAll = async () => {
    setBusyLot(-1)
    setErr('')
    try {
      const t = plan && target ? { target_tsym: plan.chosen.tsym, target_token: plan.chosen.token } : undefined
      await api.rolloverNow(inst.id, t)
      setConfirmAll(false)
      onChanged()
      await load(target)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'rollover failed')
    } finally {
      setBusyLot(0)
    }
  }

  const px = (v: number | null | undefined) => (v == null ? '—' : fmtPx(v))

  return (
    <div className="bg-violet-50/60 border-t border-violet-200 px-3 py-2" onClick={(e) => e.stopPropagation()}>
      {loading && <div className="text-xs text-gray-500 py-2">loading rollover plan…</div>}
      {!loading && err && !plan && <div className="text-xs text-red-600 py-2">{err}</div>}
      {!loading && plan && (
        <>
          {/* header: near → far, roll date + why */}
          <div className="flex items-center gap-3 flex-wrap text-[11px] text-gray-700 pb-1.5">
            <span className="font-semibold text-gray-900">{plan.current.tsym}</span>
            <span className="text-gray-400">→</span>
            {manual ? (
              <select value={target || plan.chosen.token}
                onChange={(e) => setTarget(e.target.value)}
                className="bg-white border border-gray-300 rounded px-1.5 py-0.5 text-[11px]">
                {plan.targets.map((t) => (
                  <option key={t.token} value={t.token}>
                    {t.tsym} · exp {t.expiry} · LTP {t.ltp ? fmtPx(t.ltp) : '—'}
                  </option>
                ))}
              </select>
            ) : (
              <span className="font-semibold text-violet-700">{plan.chosen.tsym}</span>
            )}
            <span>exp {plan.chosen.expiry}</span>
            <span>near LTP <span className="font-mono">{px(plan.current.ltp)}</span></span>
            <span>far LTP <span className="font-mono">{px(plan.chosen.ltp)}</span></span>
            <span>
              basis{' '}
              <span className={`font-mono font-semibold ${plan.basis != null && plan.basis > 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                {plan.basis == null ? '—' : `${plan.basis > 0 ? '+' : ''}${fmtPx(plan.basis)}`}
              </span>
              {plan.basis != null && <span className="text-gray-500"> ({plan.basis > 0 ? 'debit' : 'credit'})</span>}
            </span>
            <span className="text-gray-500">
              roll date <span className="font-mono">{plan.rollover.rollover_date || '—'}</span>
              {plan.rollover.window_reason ? ` · ${plan.rollover.window_reason}` : ''}
            </span>
            <button onClick={() => load(target)}
              className="ml-auto px-1.5 py-0.5 rounded border border-gray-300 text-gray-600 hover:border-violet-500 text-[10px]">
              ⟳ refresh
            </button>
            {manual && plan.lots.length > 0 && (
              <button disabled={busyLot !== 0} onClick={() => setConfirmAll(true)}
                className="px-2 py-0.5 rounded border border-violet-600 text-violet-700 hover:bg-violet-500/10 text-[10px] font-semibold disabled:opacity-40">
                Roll ALL now → {plan.chosen.tsym}
              </button>
            )}
          </div>

          {plan.lots.length === 0 ? (
            <div className="text-xs text-gray-500 py-1">no open rungs — nothing to roll.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-gray-500 text-[10px] uppercase tracking-wide">
                    <th className="text-left px-1.5 py-0.5">#</th>
                    <th className="text-left px-1.5">Contract</th>
                    <th className="text-right px-1.5">Qty</th>
                    <th className="text-right px-1.5">Entry</th>
                    <th className="text-right px-1.5">Current</th>
                    <th className="text-right px-1.5" title="P&L booked on the OLD leg if this rung rolls now">Leg P&L</th>
                    <th className="text-right px-1.5" title="basis × qty — cost (debit) or credit of carrying forward">Roll cost</th>
                    <th className="text-right px-1.5">Entry after</th>
                    <th className="text-right px-1.5">Target after</th>
                    <th className="text-right px-1.5">SL after</th>
                    <th className="text-right px-1.5">Quick roll</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.lots.map((l) => (
                    <tr key={l.lot_id} className="border-t border-violet-200/60">
                      <td className="px-1.5 py-1 font-semibold">#{l.seq}</td>
                      <td className="px-1.5 font-mono">{l.contract}</td>
                      <td className="px-1.5 text-right font-mono">{l.qty} ({l.lots}L)</td>
                      <td className="px-1.5 text-right font-mono">{px(l.entry)}</td>
                      <td className="px-1.5 text-right font-mono">{px(l.ltp)}</td>
                      <td className={`px-1.5 text-right font-mono ${pnlClass(l.leg_pnl ?? 0)}`}>{l.leg_pnl == null ? '—' : inr(l.leg_pnl)}</td>
                      <td className={`px-1.5 text-right font-mono ${l.roll_cost != null && l.roll_cost > 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                        {l.roll_cost == null ? '—' : inr(l.roll_cost)}
                      </td>
                      <td className="px-1.5 text-right font-mono">{px(l.entry_after)}</td>
                      <td className="px-1.5 text-right font-mono">{px(l.target_after)}</td>
                      <td className="px-1.5 text-right font-mono">{l.sl > 0 ? px(l.sl_after) : 'no stop'}</td>
                      <td className="px-1.5 text-right">
                        {l.exit_pending
                          ? <Badge tone="amber">exit busy</Badge>
                          : (
                            <button disabled={busyLot !== 0} onClick={() => setConfirmLot(l)}
                              className="px-2 py-0.5 rounded border border-violet-600 text-violet-700 hover:bg-violet-500/10 text-[10px] font-semibold disabled:opacity-40">
                              {busyLot === l.lot_id ? 'rolling…' : 'Roll →'}
                            </button>
                          )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {err && <div className="text-[11px] text-red-600 pt-1">{err}</div>}

          {/* per-rung confirm card */}
          {confirmLot && (
            <div className="mt-2 bg-white border border-violet-300 rounded-lg px-3 py-2 text-[11px] text-gray-700">
              <div className="font-semibold text-gray-900 pb-1">
                Quick roll rung #{confirmLot.seq}: {confirmLot.contract} → {plan.chosen.tsym}
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-0.5">
                <span>current price <span className="font-mono">{px(confirmLot.ltp)}</span></span>
                <span>rollover price <span className="font-mono">{px(plan.chosen.ltp)}</span></span>
                <span>basis <span className="font-mono">{confirmLot.basis == null ? '—' : `${confirmLot.basis > 0 ? '+' : ''}${fmtPx(confirmLot.basis)}`}</span></span>
                <span>roll cost <span className="font-mono">{confirmLot.roll_cost == null ? '—' : inr(confirmLot.roll_cost)}</span></span>
                <span>P&L after rollover (booked) <span className={`font-mono ${pnlClass(confirmLot.leg_pnl ?? 0)}`}>{confirmLot.leg_pnl == null ? '—' : inr(confirmLot.leg_pnl)}</span></span>
                <span>entry after <span className="font-mono">{px(confirmLot.entry_after)}</span></span>
                <span>target after <span className="font-mono">{px(confirmLot.target_after)}</span></span>
                <span>SL after <span className="font-mono">{confirmLot.sl > 0 ? px(confirmLot.sl_after) : 'no stop'}</span></span>
              </div>
              <div className="text-[10px] text-gray-500 pt-1">
                Sell {confirmLot.qty}u {confirmLot.contract}, verify the fill, then buy {confirmLot.qty}u {plan.chosen.tsym} and
                verify — one rung only; entry/target/SL shift by the ACTUAL fill basis, so P&L continuity is exact.
              </div>
              <div className="flex gap-2 pt-1.5">
                <button disabled={busyLot !== 0} onClick={() => quickRoll(confirmLot)}
                  className="px-3 py-1 rounded bg-violet-600 hover:bg-violet-500 text-white text-[11px] font-semibold disabled:opacity-40">
                  {busyLot === confirmLot.lot_id ? 'rolling…' : `✓ Roll rung #${confirmLot.seq} now`}
                </button>
                <button disabled={busyLot !== 0} onClick={() => setConfirmLot(null)}
                  className="px-3 py-1 rounded border border-gray-300 text-gray-700 text-[11px]">Cancel</button>
              </div>
            </div>
          )}

          {/* roll-all confirm card */}
          {confirmAll && (
            <div className="mt-2 bg-white border border-violet-300 rounded-lg px-3 py-2 text-[11px] text-gray-700">
              <div className="font-semibold text-gray-900 pb-1">
                Roll ALL {plan.lots.length} rung(s) now → {plan.chosen.tsym}?
              </div>
              <div>
                Strictly one at a time: each rung's sell is verified, then its buy is verified, before the next rung
                starts. Est. total roll cost{' '}
                <span className="font-mono">
                  {inr(plan.lots.reduce((a, l) => a + (l.roll_cost ?? 0), 0))}
                </span>. Overrides the roll date and liquidity window.
              </div>
              <div className="flex gap-2 pt-1.5">
                <button disabled={busyLot !== 0} onClick={rollAll}
                  className="px-3 py-1 rounded bg-violet-600 hover:bg-violet-500 text-white text-[11px] font-semibold disabled:opacity-40">
                  {busyLot === -1 ? 'rolling…' : '✓ Roll everything now'}
                </button>
                <button disabled={busyLot !== 0} onClick={() => setConfirmAll(false)}
                  className="px-3 py-1 rounded border border-gray-300 text-gray-700 text-[11px]">Cancel</button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
