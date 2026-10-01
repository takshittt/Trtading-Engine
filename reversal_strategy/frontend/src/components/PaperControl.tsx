import { useState } from 'react'
import type { PaperSummary } from '../types'
import { api } from '../api'
import { inr2, pnlColor } from '../util'
import { Modal, btnGhost, btnDanger, inputCls } from './ui'

/**
 * Start / stop control for a paper-trading run.
 *
 * A run has a beginning and an end, so it gets a button rather than a setting
 * buried in a config dialog. Stopping is the part that needs care: switching
 * off only stops NEW signals from executing — anything already open keeps
 * running under the exit engine, exactly as a real position would. So the stop
 * dialog makes that an explicit choice instead of quietly picking one.
 */
export function PaperControl({ paper, onChanged }: {
  paper: PaperSummary | undefined
  onChanged: (msg: string) => void
}) {
  const [dialog, setDialog] = useState<'start' | 'stop' | 'reset' | null>(null)
  const [lots, setLots] = useState(paper?.lots || 1)
  const [squareOff, setSquareOff] = useState(true)
  const [busy, setBusy] = useState(false)

  const running = !!paper?.enabled
  const openCount = paper?.open_positions ?? 0

  const run = async (fn: () => Promise<string>) => {
    setBusy(true)
    try { onChanged(await fn()) } finally { setBusy(false); setDialog(null) }
  }

  return (
    <>
      <button
        onClick={() => setDialog(running ? 'stop' : 'start')}
        title={running
          ? 'Paper trading is running — click to stop'
          : 'Start paper trading: every signal auto-executes at the live bid/ask, no real orders'}
        className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-bold transition-colors ${
          running
            ? 'bg-indigo-600 text-white hover:bg-indigo-700'
            : 'border border-indigo-300 bg-white text-indigo-700 hover:bg-indigo-50'}`}>
        {running ? (
          <>
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-white opacity-70" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-white" />
            </span>
            Paper: Running
          </>
        ) : 'Start Paper'}
      </button>

      {dialog === 'start' && (
        <Modal title="Start Paper Trading" onClose={() => setDialog(null)}>
          <p className="text-sm text-slate-600">
            Every incoming signal will be executed automatically against the live Shoonya
            order book — buys lift the <b>ask</b>, sells hit the <b>bid</b> — so a round trip
            pays the same spread a real market order would.
          </p>
          <ul className="mt-3 space-y-1 text-xs text-slate-500">
            <li>• <b className="text-slate-700">No broker order is ever sent.</b> Nothing reaches your real account.</li>
            <li>• Paper positions are tagged <b>PAPER</b>, lock no margin, and are excluded from your live budget, portfolio SL and reconciliation.</li>
            <li>• A repeat BUY averages the position; a SELL squares it off completely.</li>
            <li>• Signal→fill latency and the bid/ask crossed are recorded for every leg.</li>
          </ul>
          <div className="mt-4 w-44">
            <label className="text-[11px] font-semibold text-slate-500">Lots per simulated buy
              <input type="number" min={1} value={lots} onChange={e => setLots(Math.max(1, +e.target.value))}
                className={`${inputCls} mt-1`} />
            </label>
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <button className={btnGhost} onClick={() => setDialog(null)}>Cancel</button>
            <button disabled={busy}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
              onClick={() => run(async () => {
                await api.paperStart(lots)
                return `Paper trading started — ${lots} lot(s) per signal.`
              })}>
              {busy ? 'Starting…' : 'Start Paper Trading'}
            </button>
          </div>
        </Modal>
      )}

      {dialog === 'stop' && (
        <Modal title="Stop Paper Trading" onClose={() => setDialog(null)}>
          <p className="text-sm text-slate-600">
            New signals will stop being executed. You have <b>{openCount}</b> open paper
            position{openCount === 1 ? '' : 's'}.
          </p>
          <label className="mt-3 flex cursor-pointer items-start gap-2 rounded-xl border border-slate-200 p-3">
            <input type="checkbox" checked={squareOff} onChange={e => setSquareOff(e.target.checked)}
              className="mt-0.5" />
            <span className="text-xs text-slate-600">
              <b className="text-slate-800">Square off all open paper positions now</b>
              <span className="block text-slate-400">
                Seals the run — every trade lands in the journal with a realized P&amp;L.
                Unchecked, they keep running under their Target/SL so the test can finish
                on its own.
              </span>
            </span>
          </label>
          <div className="mt-5 flex items-center justify-between">
            <button className="text-[11px] font-medium text-rose-500 hover:text-rose-700 hover:underline"
              onClick={() => setDialog('reset')}>Clear all paper data…</button>
            <div className="flex gap-2">
              <button className={btnGhost} onClick={() => setDialog(null)}>Cancel</button>
              <button disabled={busy}
                className="rounded-lg bg-slate-800 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-900 disabled:opacity-50"
                onClick={() => run(async () => {
                  const r = await api.paperStop(squareOff)
                  return squareOff
                    ? `Paper trading stopped — ${r.squared_off} position(s) squared off.`
                    : 'Paper trading stopped — open positions left running.'
                })}>
                {busy ? 'Stopping…' : 'Stop Paper Trading'}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {dialog === 'reset' && (
        <Modal title="Clear all paper data?" onClose={() => setDialog(null)}>
          <p className="text-sm text-slate-600">
            Permanently deletes <b>every</b> paper position and order, and resets the signals
            they consumed back to actionable. Use this to start a clean test run.
          </p>
          <p className="mt-2 text-xs text-slate-400">
            Your live book, its orders and its journal are not touched — only rows flagged
            as paper are in range. This cannot be undone.
          </p>
          {paper && paper.trades > 0 && (
            <p className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
              You will lose {paper.trades} closed paper trade(s) and their P&amp;L of{' '}
              <b className={pnlColor(paper.realized_pnl)}>{inr2(paper.realized_pnl)}</b>.
            </p>
          )}
          <div className="mt-5 flex justify-end gap-2">
            <button className={btnGhost} onClick={() => setDialog('stop')}>Back</button>
            <button disabled={busy} className={btnDanger}
              onClick={() => run(async () => {
                const r = await api.paperReset()
                return `Paper data cleared — ${r.positions_removed} trade(s) removed, ${r.signals_reset} signal(s) reset.`
              })}>
              {busy ? 'Clearing…' : 'Yes, clear everything'}
            </button>
          </div>
        </Modal>
      )}
    </>
  )
}
