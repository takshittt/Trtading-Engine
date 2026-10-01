import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import { logout } from './auth'
import { useEngine } from './hooks/useEngine'
import type { InstrumentSnap } from './types'
import { ChartModal } from './components/ChartModal'
import { ConfigModal } from './components/ConfigModal'
import { EventsModal } from './components/EventsPanel'
import { LadderModal } from './components/LadderModal'
import { LogsModal } from './components/LogsPanel'
import { PnlPanel } from './components/PnlPanel'
import { SearchPanel } from './components/SearchPanel'
import { TradesTable } from './components/TradesTable'
import { WatchlistTable } from './components/WatchlistTable'
import { inr, pnlClass } from './components/ui'

export default function App() {
  const { snapshot, wsUp } = useEngine()
  const [configFor, setConfigFor] = useState<InstrumentSnap | null>(null)
  const [ladderFor, setLadderFor] = useState<InstrumentSnap | null>(null)
  const [chartFor, setChartFor] = useState<{ inst: InstrumentSnap; focusLot?: number } | null>(null)
  const [showPnl, setShowPnl] = useState(false)
  const [showLogs, setShowLogs] = useState(false)
  const [showEvents, setShowEvents] = useState(false)
  const [switching, setSwitching] = useState(false)
  const [brokerBusy, setBrokerBusy] = useState(false)
  const [confirmingDisconnect, setConfirmingDisconnect] = useState(false)
  const [connectMsg, setConnectMsg] = useState('')
  const [searchCollapsed, setSearchCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem('grid.searchCollapsed') === '1' } catch { return false }
  })
  const setCollapsed = (v: boolean) => {
    setSearchCollapsed(v)
    try { localStorage.setItem('grid.searchCollapsed', v ? '1' : '0') } catch { /* ignore */ }
  }

  // instrument list refresh is implicit — snapshots stream every second
  const refresh = useCallback(() => {}, [])

  const toggleEngine = async () => {
    if (!snapshot) return
    if (snapshot.engine_on) {
      if (!confirm('Stop applying the strategy? Open rungs keep their target/SL protection; only NEW entries stop.')) return
    }
    setSwitching(true)
    try {
      if (snapshot.engine_on) await api.engineStop()
      else await api.engineStart()
    } finally {
      setSwitching(false)
    }
  }

  // Grid holds no broker credentials — the Gateway does — so this asks the
  // shared Gateway to log in (or no-ops when it's already connected). Success
  // clears itself: the streaming snapshot flips broker_connected and the banner
  // disappears. A login rejection (wrong password/TOTP) or a missing `connect`
  // scope comes back in `detail`, so surface it rather than a bare failure.
  const doConnect = async () => {
    setBrokerBusy(true)
    setConnectMsg('')
    try {
      const r = await api.brokerConnect()
      if (r.already_connected) setConnectMsg('Broker already connected.')
      else if (r.connected) setConnectMsg('Broker connected.')
      else setConnectMsg(`Connect failed: ${r.detail || 'no detail'}`)
    } catch (e) {
      setConnectMsg(`Connect failed: ${e instanceof Error ? e.message : 'see logs'}`)
    } finally {
      setBrokerBusy(false)
    }
  }

  // Disconnect tears down the SHARED Gateway broker session — the same one Swing
  // rides — so it is gated behind an inline Confirm/No step (set in the navbar).
  // Prices and stop-loss monitoring stop for everyone until someone reconnects.
  // Success is reflected by the streaming snapshot.
  const doDisconnect = async () => {
    setBrokerBusy(true)
    setConfirmingDisconnect(false)
    setConnectMsg('')
    try {
      const r = await api.brokerDisconnect()
      setConnectMsg(r.connected ? `Disconnect failed: ${r.detail || 'still connected'}` : 'Broker disconnected.')
    } catch (e) {
      setConnectMsg(`Disconnect failed: ${e instanceof Error ? e.message : 'see logs'}`)
    } finally {
      setBrokerBusy(false)
    }
  }

  // keep the config/ladder modals' instrument fresh as snapshots stream in
  useEffect(() => {
    if (configFor && snapshot && !snapshot.instruments.find((i) => i.id === configFor.id)) setConfigFor(null)
    if (ladderFor && snapshot && !snapshot.instruments.find((i) => i.id === ladderFor.id)) setLadderFor(null)
  }, [snapshot, configFor, ladderFor])

  const feed = snapshot?.feed.state ?? 'down'
  const engineOn = snapshot?.engine_on ?? false
  const allInstruments = snapshot?.instruments ?? []

  // simple headline P&L (details live in the P&L panel)
  const openPnl = snapshot?.total_open_pnl ?? 0
  const realizedToday = allInstruments.reduce((s, i) => s + (i.realized_today || 0), 0)
  const netToday = openPnl + realizedToday

  return (
    <div className="min-h-screen flex flex-col">
      {/* ============ header ============ */}
      <header className="border-b border-gray-200 px-5 py-3 flex items-center justify-between gap-4 sticky top-0 bg-white/90 backdrop-blur z-30">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center font-bold text-sm text-white select-none">
            S
          </div>
          <div>
            <h1 className="text-base font-bold text-gray-900 leading-tight">Grid Trading Dashboard</h1>
            <div className="text-[11px] text-gray-500">Signals · ladder exits · Shoonya via Gateway</div>
          </div>
        </div>

        {/* simple P&L summary — full breakdown in the P&L panel */}
        <button
          onClick={() => setShowPnl(true)}
          title="Open the full account P&L panel"
          className="hidden md:flex items-center gap-4 px-4 py-1.5 rounded-xl border border-gray-200 bg-gray-100 hover:border-sky-500">
          <PnlStat label="Net today" value={netToday} strong />
          <span className="w-px h-7 bg-gray-100" />
          <PnlStat label="Open" value={openPnl} />
          <span className="w-px h-7 bg-gray-100" />
          <PnlStat label="Realized today" value={realizedToday} />
          {snapshot?.funds?.have && (
            <>
              <span className="w-px h-7 bg-gray-100" />
              <PnlStat label="Margin left" value={snapshot.funds.remaining} plain />
            </>
          )}
        </button>

        <div className="flex items-center gap-3">
          {/* compact status lights (label + green/red/amber dot) */}
          <div className="flex items-center gap-3 pr-1">
            <StatusDot label="broker" tone={snapshot?.broker_connected ? 'green' : 'red'}
              note={snapshot?.broker_connected ? 'connected' : 'down'} />
            <StatusDot label="feed"
              tone={feed === 'live' ? 'green' : feed === 'closed' ? 'gray' : feed === 'stale' ? 'amber' : 'red'}
              note={feed === 'live' ? 'live' : `${feed}${snapshot ? ` ${Math.round(snapshot.feed.seconds_since_rx)}s` : ''}`} />
            <StatusDot label="ui" tone={wsUp ? 'green' : 'red'} note={wsUp ? 'live' : 'poll'} />
          </div>
          {/* Broker Connect/Disconnect lives here (not the Gateway UI): Grid
              holds no broker creds, so these drive the SHARED Gateway broker
              session. Permanent pair — Connect when down, Disconnect when up. */}
          <div className="flex items-center gap-2">
            {snapshot?.broker_connected ? (
              confirmingDisconnect ? (
                <div className="flex items-center gap-1">
                  <button
                    onClick={doDisconnect}
                    disabled={brokerBusy}
                    title="Confirm: log the shared broker session out for every strategy on the Gateway."
                    className="px-2.5 py-1.5 rounded-lg text-[13px] font-semibold bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 whitespace-nowrap">
                    {brokerBusy ? '…' : 'Confirm'}
                  </button>
                  <button
                    onClick={() => setConfirmingDisconnect(false)}
                    className="px-2.5 py-1.5 rounded-lg text-[13px] font-semibold border border-gray-300 text-gray-500 hover:bg-gray-100 whitespace-nowrap">
                    No
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setConfirmingDisconnect(true)}
                  disabled={brokerBusy}
                  title="Log the shared Gateway out of the broker. Prices stop and stop-losses are not monitored while disconnected — this affects every strategy on the Gateway."
                  className="px-2.5 py-1.5 rounded-lg text-[13px] font-semibold border border-red-500 text-red-600 hover:bg-red-500/10 disabled:opacity-50 whitespace-nowrap">
                  {brokerBusy ? '…' : 'Disconnect'}
                </button>
              )
            ) : (
              <button
                onClick={doConnect}
                disabled={brokerBusy}
                title="Ask the shared Gateway to log in to Shoonya. Takes up to a minute — a headless browser sign-in runs behind it."
                className="px-2.5 py-1.5 rounded-lg text-[13px] font-semibold border border-emerald-600 text-emerald-700 hover:bg-emerald-500/10 disabled:opacity-50 whitespace-nowrap">
                {brokerBusy ? 'Connecting…' : 'Connect'}
              </button>
            )}
            {connectMsg && (
              <span className="text-[11px] text-gray-500 max-w-[200px] truncate" title={connectMsg}>{connectMsg}</span>
            )}
          </div>
          <button
            onClick={() => setShowPnl(true)}
            className="px-2.5 py-1.5 rounded-lg text-[13px] font-semibold border border-emerald-600 text-emerald-700 hover:bg-emerald-500/10">
            P&amp;L
          </button>
          <button
            onClick={toggleEngine}
            disabled={switching || !snapshot}
            title={engineOn ? 'Stop applying the strategy' : 'Start applying the strategy & mathematics'}
            className={`px-3 py-1.5 rounded-lg text-[13px] font-semibold transition-colors whitespace-nowrap
              ${engineOn
                ? 'bg-red-500/15 border border-red-600 text-red-600 hover:bg-red-500/25'
                : 'bg-emerald-600 hover:bg-emerald-500 text-white'}`}>
            {engineOn ? 'Stop Strategy' : 'Start Strategy'}
          </button>
          <button
            onClick={logout}
            title="Sign out of the grid dashboard"
            className="px-2.5 py-1.5 rounded-lg text-[13px] font-semibold border border-gray-300 text-gray-600 hover:bg-gray-100 whitespace-nowrap">
            Sign out
          </button>
        </div>
      </header>

      {/* DEGRADED: feed died with open positions — manual exit required */}
      {snapshot?.degraded?.active && (
        <div className="bg-red-600 text-white text-sm px-5 py-2 font-semibold flex items-center gap-2">
          DEGRADED — {snapshot.degraded.reason}. Auto-exit is OFF. Check WhatsApp for the manual-exit list
          (fallback: {snapshot.degraded.fallback_provider}). Down {Math.round(snapshot.degraded.since_s)}s.
        </div>
      )}

      {/* Feed banner. A shut exchange sends nothing, so silence outside market
          hours is normal and says so plainly — warning about it all night is how
          a real warning stops being read. */}
      {snapshot && feed === 'closed' && (
        <div className="bg-gray-500/10 border-b border-gray-400 text-gray-500 text-xs px-5 py-1.5">
          Market closed — the price feed is idle. Trading resumes at the next session open.
        </div>
      )}
      {snapshot && feed !== 'live' && feed !== 'closed' && (
        <div className="bg-amber-500/10 border-b border-amber-500 text-amber-600 text-xs px-5 py-1.5">
          Price feed is {feed.toUpperCase()} (no WebSocket data for {Math.round(snapshot.feed.seconds_since_rx)}s).
          New entries are paused; prices fall back to 1-second REST polling; exits stay armed on polled prices.
        </div>
      )}
      {/* GLOBAL circuit breaker — blocks every buy across every instrument.
          It had no UI at all and no reset: a trip (real or from a stale price)
          silently froze all trading until someone restarted the process. */}
      {snapshot?.global_cb?.tripped && (
        <div className="bg-red-600 text-white text-sm px-5 py-2 font-semibold flex items-center gap-3">
          <span>
            GLOBAL CIRCUIT BREAKER TRIPPED — {snapshot.global_cb.reason || 'total loss threshold breached'}.
            ALL new buys are blocked and resting entry orders were pulled. Open positions still exit normally.
          </span>
          <button
            onClick={async () => {
              if (!confirm('Reset the GLOBAL circuit breaker?\n\nThis re-enables buying across every '
                         + 'instrument and re-places resting ladder entry orders. Confirm the drawdown '
                         + 'that tripped it is understood first.')) return
              try { await api.resetGlobalCb(); refresh() } catch { /* banner stays up */ }
            }}
            className="ml-auto shrink-0 px-3 py-1 rounded border border-white/70 hover:bg-white/15 text-xs font-semibold">
            Reset breaker
          </button>
        </div>
      )}

      {snapshot && !snapshot.broker_connected && (
        <div className="bg-red-500/10 border-b border-red-400 text-red-600 text-xs px-5 py-1.5">
          Broker session is down at the Gateway — use <span className="font-semibold">Connect</span> in the top bar.
          The engine keeps reconciling and will resume automatically.
        </div>
      )}

      {/* ============ main grid ============ */}
      <main className="flex-1 p-4 grid grid-cols-12 gap-4 items-start">
        {/* left — discovery (collapsible: minimize to give the watchlist more room) */}
        {searchCollapsed ? (
          <div className="col-span-12 lg:col-span-1">
            <button onClick={() => setCollapsed(false)} title="expand search"
              className="w-full flex lg:flex-col items-center justify-center gap-2 rounded-2xl border border-gray-300 bg-white hover:border-sky-500 text-gray-600 hover:text-sky-700 py-3 lg:min-h-[140px]">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.7} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
              </svg>
              <span className="text-xs font-semibold lg:[writing-mode:vertical-rl] lg:rotate-180">Search</span>
            </button>
          </div>
        ) : (
          <div className="col-span-12 lg:col-span-3">
            <SearchPanel onAdded={refresh} instruments={allInstruments} onCollapse={() => setCollapsed(true)} />
          </div>
        )}

        {/* right — one shared watchlist; expands when search is collapsed */}
        <div className={`col-span-12 ${searchCollapsed ? 'lg:col-span-11' : 'lg:col-span-9'}`}>
          <WatchlistTable
            instruments={allInstruments}
            onConfigure={setConfigFor}
            onOpenLadder={setLadderFor}
            onChanged={refresh}
            onOpenChart={(inst) => setChartFor({ inst })}
          />
        </div>

        {/* trades — full width */}
        <div className="col-span-12">
          <TradesTable
            snapshot={snapshot}
            onOpenChart={(inst, focusLot) => setChartFor({ inst, focusLot })}
          />
        </div>

        {/* bottom — Regime (left corner) & Logs (right corner) highlighted buttons, per the sketch */}
        <div className="col-span-12 flex items-center justify-between gap-3 pt-1">
          <button
            onClick={() => setShowEvents(true)}
            className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold border border-sky-500 bg-sky-500/10 text-sky-700 hover:bg-sky-500/20">
            Events / Market Regime
          </button>
          <span className="hidden md:block text-[11px] text-gray-500 text-center">
            Regime: EIA Thursdays, expiries, roll days · Logs: every decision with its math (1-month retention)
          </span>
          <button
            onClick={() => setShowLogs(true)}
            className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold border border-violet-600 bg-violet-500/10 text-violet-700 hover:bg-violet-500/20">
            System Logs
          </button>
        </div>
      </main>

      <footer className="px-5 py-2 text-[10px] text-gray-500 border-t border-gray-200">
        Engine {snapshot ? `up since ${snapshot.booted_at}` : 'offline'} · reconciles with broker every 60s ·
        heartbeat 10s · prices refresh every second
      </footer>

      {configFor && snapshot && (
        <ConfigModal
          inst={snapshot.instruments.find((i) => i.id === configFor.id) ?? configFor}
          onClose={() => setConfigFor(null)}
          onSaved={refresh}
        />
      )}
      {ladderFor && snapshot && (
        <LadderModal
          inst={snapshot.instruments.find((i) => i.id === ladderFor.id) ?? ladderFor}
          onClose={() => setLadderFor(null)}
          onSaved={refresh}
        />
      )}
      {chartFor && (
        <ChartModal inst={chartFor.inst} focusLot={chartFor.focusLot} onClose={() => setChartFor(null)} />
      )}
      {showPnl && <PnlPanel onClose={() => setShowPnl(false)} />}
      {showLogs && <LogsModal onClose={() => setShowLogs(false)} />}
      {showEvents && <EventsModal onClose={() => setShowEvents(false)} />}
    </div>
  )
}

/** Compact status light — a small label with a green/amber/red dot beneath it
 * (broker · feed · ui), replacing the wordier status pills. */
function StatusDot({ label, tone, note }: { label: string; tone: 'green' | 'amber' | 'red' | 'gray'; note?: string }) {
  // gray = nothing is expected right now (a shut market), as opposed to red,
  // which means something that should be working isn't.
  const color = tone === 'green' ? 'bg-emerald-500' : tone === 'amber' ? 'bg-amber-500'
    : tone === 'gray' ? 'bg-gray-400' : 'bg-red-500'
  const glow = tone === 'green' ? 'shadow-[0_0_6px_1px_rgba(16,185,129,0.55)]'
    : tone === 'amber' ? 'shadow-[0_0_6px_1px_rgba(245,158,11,0.55)]'
    : tone === 'gray' ? ''
    : 'shadow-[0_0_6px_1px_rgba(239,68,68,0.55)]'
  return (
    <div className="flex flex-col items-center gap-1 select-none" title={`${label}: ${note ?? ''}`}>
      <span className="text-[9px] uppercase tracking-wide text-gray-500 leading-none">{label}</span>
      <span className={`w-2.5 h-2.5 rounded-full ${color} ${glow}`} />
    </div>
  )
}

function PnlStat({ label, value, strong, plain }: { label: string; value: number; strong?: boolean; plain?: boolean }) {
  return (
    <div className="text-left">
      <div className="text-[9px] uppercase tracking-wide text-gray-500 leading-none">{label}</div>
      <div className={`font-mono font-semibold tabular-nums ${strong ? 'text-sm' : 'text-[13px]'} ${plain ? 'text-gray-800' : pnlClass(value)}`}>
        {inr(value)}
      </div>
    </div>
  )
}
