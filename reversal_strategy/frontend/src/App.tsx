import { useCallback, useEffect, useRef, useState } from 'react'
import type { Position, Signal, Summary } from './types'
import { api } from './api'
import { useWebSocket } from './ws'
import { TopBar } from './components/TopBar'
import { SignalPanel } from './components/SignalPanel'
import { PositionsPanel } from './components/PositionsPanel'
import { SideDrawer } from './components/SideDrawer'
import { BuyModal } from './components/BuyModal'
import { StockModal } from './components/StockModal'
import { ConfigModal } from './components/ConfigModal'
import { Modal, btnDanger, btnGhost } from './components/ui'

interface Alert { id: number; type: string; message: string }

/**
 * Newest position first — the same order the API returns.
 *
 * Both the poll and the websocket write this list, so they have to agree on
 * ordering or rows swap places every time the two disagree. Ties break on id so
 * positions opened within the same second cannot oscillate.
 */
const sortPositions = (rows: Position[]) =>
  [...rows].sort((a, b) =>
    (Date.parse(b.opened_at) - Date.parse(a.opened_at)) || (b.id - a.id))

export default function App() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [signals, setSignals] = useState<Signal[]>([])
  const [positions, setPositions] = useState<Position[]>([])
  const [tick, setTick] = useState(0)
  const [buyFor, setBuyFor] = useState<Signal | null>(null)
  const [sellFor, setSellFor] = useState<Position | null>(null)
  const [stockFor, setStockFor] = useState<string | null>(null)
  const [showConfig, setShowConfig] = useState(false)
  const [confirmKill, setConfirmKill] = useState(false)
  const [alerts, setAlerts] = useState<Alert[]>([])
  const alertSeq = useRef(0)

  const refreshCore = useCallback(() => {
    api.summary().then(setSummary)
    api.todaySignals().then(setSignals)
    api.positions().then(rows => setPositions(sortPositions(rows)))
  }, [])
  const refreshAll = useCallback(() => { refreshCore(); setTick(t => t + 1) }, [refreshCore])

  useEffect(() => {
    refreshCore()
    const iv = setInterval(refreshCore, 5000)
    return () => clearInterval(iv)
  }, [refreshCore])

  const pushAlert = (type: string, message: string) => {
    const id = ++alertSeq.current
    setAlerts(a => [...a, { id, type, message }])
    setTimeout(() => setAlerts(a => a.filter(x => x.id !== id)), 9000)
  }

  useWebSocket(useCallback((msg) => {
    switch (msg.event) {
      case 'signal': {
        const s = msg.data as Signal
        // Today feed = all of today's signals (any status); newest first.
        setSignals(prev => [s, ...prev.filter(x => x.id !== s.id)])
        break
      }
      case 'position': {
        const p = msg.data as Position
        // Update in place. Re-inserting at the front moved a row every time its
        // price ticked, so a group of positions visibly reshuffled on every
        // update until the next poll restored the sorted order.
        setPositions(prev => {
          if (p.status !== 'OPEN') return prev.filter(x => x.id !== p.id)
          const i = prev.findIndex(x => x.id === p.id)
          if (i < 0) return sortPositions([...prev, p])
          const next = [...prev]
          next[i] = p
          return next
        })
        break
      }
      case 'prices':
        setPositions(prev => prev.map(p => ({
          ...p,
          ltp: msg.data[p.symbol] ?? p.ltp,
          unrealized_pnl: +(((msg.data[p.symbol] ?? p.ltp) - p.avg_price) * p.qty).toFixed(2),
        })))
        // Signals tick too. Every row on that table carries a live LTP and a Δ
        // against the price the signal fired at — including rows already acted
        // on, whose frozen record is signal_ltp, not ltp.
        setSignals(prev => prev.map(s => {
          const ltp = msg.data[s.symbol]
          if (!ltp || ltp === s.ltp) return s
          const base = s.signal_ltp || 0
          return { ...s, ltp, move_pct: base ? +((ltp / base - 1) * 100).toFixed(2) : 0 }
        }))
        break
      case 'alert':
        pushAlert(msg.data.type, msg.data.message)
        refreshCore()
        break
      case 'order': case 'config': case 'reconcile':
        setTick(t => t + 1)
        break
    }
  }, [refreshCore]))

  const doKill = async () => {
    setConfirmKill(false)
    const r = await api.killSwitch()
    pushAlert('KILL_SWITCH', `Kill switch executed — ${r.closed} position(s) squared off.`)
    refreshAll()
  }

  // Averaging is decided by whether the stock is currently held, not by the
  // signal's age or status — a previous day's BUY on a held stock averages too.
  const heldPos = buyFor ? positions.find(p => p.symbol === buyFor.symbol && p.status === 'OPEN' && !p.is_paper) : undefined

  const doSell = async () => {
    if (!sellFor) return
    const p = sellFor
    setSellFor(null)
    const r = await api.exit(p.id)
    pushAlert(r.ok ? 'EXIT' : 'EXIT_FAILED',
      r.ok ? `${p.symbol} squared off (${p.lots} lot).` : `Could not exit ${p.symbol}.`)
    refreshAll()
  }

  return (
    <div className="flex min-h-screen flex-col">
      <TopBar summary={summary} onConfig={() => setShowConfig(true)} onKill={() => setConfirmKill(true)}
        onMode={async (k, v) => { await api.saveConfig({ [k]: v } as any); refreshAll() }}
        onPaperChanged={msg => { pushAlert('PAPER', msg); refreshAll() }} />

      {summary?.signals_halted && (
        <div className="flex items-center justify-between bg-rose-50 px-4 py-1.5 text-xs text-rose-700">
          <span>Signal processing is halted (global SL or kill switch).</span>
          <button className="rounded-lg bg-rose-600 px-2.5 py-0.5 text-[11px] font-bold text-white hover:bg-rose-700"
            onClick={async () => { await api.resume(); refreshAll() }}>Resume</button>
        </div>
      )}

      {/* Live trades on top, the signal feed below.
          Open Positions is the money already at risk, so it gets the top of the
          page and renders at its full height — every held stock is visible
          without a scrollbar, because there are rarely many of them.
          The signal feed keeps its own viewport underneath: it is an endless
          stream, and letting it grow would push the positions off the screen
          entirely. Only that feed scrolls inside itself; the page scrolls
          around it. */}
      <main className="flex flex-col gap-2 p-2 pr-10">
        <PositionsPanel positions={positions} summary={summary} onStock={setStockFor} refresh={refreshAll} />
        <div className="h-[52vh] shrink-0">
          <SignalPanel signals={signals} summary={summary} positions={positions} onBuy={setBuyFor} onSell={setSellFor} onStock={setStockFor} refresh={refreshAll} tick={tick} />
        </div>
      </main>

      <SideDrawer onStock={setStockFor} tick={tick} />

      {buyFor && (
        <BuyModal signal={buyFor} position={heldPos}
          onClose={() => setBuyFor(null)} onDone={refreshAll} />
      )}

      {sellFor && (
        <Modal title={`Sell / Exit — ${sellFor.symbol}`} onClose={() => setSellFor(null)}>
          <p className="text-sm text-slate-600">
            Square off the full position — <b>{sellFor.lots} lot ({sellFor.qty} qty)</b> bought
            at <b>₹{sellFor.avg_price}</b> — on this SELL signal?
          </p>
          <p className="mt-1 text-xs text-slate-400">Use the Positions panel above for a partial exit.</p>
          <div className="mt-5 flex justify-end gap-2">
            <button className={btnGhost} onClick={() => setSellFor(null)}>Cancel</button>
            <button className={btnDanger} onClick={doSell}>Confirm Exit</button>
          </div>
        </Modal>
      )}
      {stockFor && <StockModal symbol={stockFor} onClose={() => setStockFor(null)} refresh={refreshAll} />}
      {showConfig && <ConfigModal onClose={() => setShowConfig(false)} onSaved={refreshAll} />}

      {confirmKill && (
        <Modal title="Activate Kill Switch?" onClose={() => setConfirmKill(false)}>
          <p className="text-sm text-slate-600">This will <b className="text-rose-600">square off all open live positions</b> at a limit price and halt all new signal processing.</p>
          <p className="mt-1 text-xs text-slate-400">Paper positions are not squared off — halting signals stops the paper trader from taking anything new. Use Stop Paper to close those.</p>
          <div className="mt-5 flex justify-end gap-2">
            <button className={btnGhost} onClick={() => setConfirmKill(false)}>Cancel</button>
            <button className={btnDanger} onClick={doKill}>Yes, Kill Everything</button>
          </div>
        </Modal>
      )}

      <div className="fixed bottom-4 right-4 z-[60] flex flex-col gap-2">
        {alerts.map(a => (
          <div key={a.id} className="max-w-sm rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs text-amber-800 shadow-lg">
            <div className="font-bold text-amber-700">{a.type.replace('_', ' ')}</div>
            {a.message}
          </div>
        ))}
      </div>
    </div>
  )
}
