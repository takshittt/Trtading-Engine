import { useEffect, useState } from 'react'
import type { BrokerInfo, Summary } from '../types'
import { api } from '../api'
import { logout } from '../auth'
import { inr, inr2, pnlColor } from '../util'
import { Dot } from './ui'
import { PaperControl } from './PaperControl'

/** Which account is trading, and the control to hand the session back.
 *
 *  The broker login lives in a gateway shared with another strategy, so "which
 *  account am I actually connected as" is not answerable from this dashboard
 *  without asking — and it is the first thing you want to know before pressing
 *  anything that spends money.
 *
 *  Connect is deliberately not just "check the status again". The gateway
 *  reuses any session token issued the same day, so a session the broker killed
 *  is not replaced by re-reading its state — only by a forced re-login, which
 *  is what this button triggers. It drives a headless browser login and can
 *  take the better part of a minute, hence the explicit pending state. */
function BrokerAccount({ connected, onChanged }: {
  connected: boolean
  onChanged: (msg: string) => void
}) {
  const [info, setInfo] = useState<BrokerInfo | null>(null)
  const [busy, setBusy] = useState<'' | 'connect' | 'disconnect'>('')
  const [confirming, setConfirming] = useState(false)

  const load = () => api.broker().then(setInfo).catch(() => setInfo(null))
  useEffect(() => { load() }, [connected])

  const doDisconnect = async () => {
    setBusy('disconnect'); setConfirming(false)
    try {
      const r = await api.brokerDisconnect()
      onChanged(r.warning || 'Broker disconnected.')
    } catch { onChanged('Disconnect failed — see logs.') }
    finally { setBusy(''); load() }
  }

  const doConnect = async () => {
    setBusy('connect')
    try {
      const r = await api.brokerConnect()
      onChanged(r.connected
        // The gateway skips a redundant re-login when the session is already
        // live — say so plainly rather than implying a fresh connect happened.
        ? (r.already_connected
            ? 'Broker already connected.'
            : `Broker connected${r.uid ? ` as ${r.uid}` : ''}.`)
        : `Connect failed: ${r.detail || 'no detail'}`)
    } catch { onChanged('Connect failed — see logs.') }
    finally { setBusy(''); load() }
  }

  const label = info?.name || info?.uid || (connected ? '—' : 'Not connected')
  const sub = info?.uid && info?.name ? info.uid : (info?.broker || 'Shoonya')

  return (
    <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-2 py-1">
      <div className="leading-tight">
        <div className="flex items-center gap-1.5 text-[11px] font-bold text-slate-700">
          {/* Amber when the gateway says connected but will not name the account —
              a session that answers its own status check and rejects real calls. */}
          <Dot on={connected} warn={connected && info !== null && !info.identified} />
          <span className="max-w-[132px] truncate" title={info?.email || label}>{label}</span>
        </div>
        <div className="text-[9px] uppercase tracking-wide text-slate-400">{sub}</div>
      </div>

      {connected ? (
        confirming ? (
          <div className="flex items-center gap-1">
            <button onClick={doDisconnect} disabled={busy !== ''}
              className="rounded-md bg-rose-600 px-2 py-1 text-[10px] font-bold text-white hover:bg-rose-700 disabled:opacity-50">
              Confirm
            </button>
            <button onClick={() => setConfirming(false)}
              className="rounded-md border border-slate-200 px-2 py-1 text-[10px] font-semibold text-slate-500 hover:bg-slate-50">
              No
            </button>
          </div>
        ) : (
          <button onClick={() => setConfirming(true)} disabled={busy !== ''}
            title="Log the shared gateway out of the broker. Prices stop and stops are not monitored while disconnected."
            className="rounded-md border border-slate-200 px-2 py-1 text-[10px] font-semibold text-slate-500 hover:bg-rose-50 hover:text-rose-600 disabled:opacity-50">
            {busy === 'disconnect' ? '…' : 'Disconnect'}
          </button>
        )
      ) : (
        <button onClick={doConnect} disabled={busy !== ''}
          title="Force a fresh broker login. Takes up to a minute — a headless browser sign-in runs behind it."
          className="rounded-md bg-emerald-600 px-2 py-1 text-[10px] font-bold text-white hover:bg-emerald-700 disabled:opacity-60">
          {busy === 'connect' ? 'Connecting…' : 'Connect'}
        </button>
      )}
    </div>
  )
}

function StatBox({ label, value, sub, valueClass = 'text-slate-800', accent }: {
  label: string; value: string; sub?: string; valueClass?: string; accent?: string
}) {
  return (
    <div className={`min-w-[128px] rounded-xl border bg-white px-3.5 py-2 ${accent ?? 'border-slate-200'}`}>
      <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">{label}</div>
      <div className={`text-base font-bold tabular-nums ${valueClass}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-400">{sub}</div>}
    </div>
  )
}

/** Labeled Manual/Automated segmented toggle. `soon` marks the disabled side. */
function ModeToggle({ label, value, onChange, soon }: {
  label: string; value: string; onChange: (v: string) => void; soon?: string
}) {
  const opts: [string, string][] = [['manual', 'Manual'], ['automated', 'Auto']]
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</span>
      <div className="inline-flex rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-[11px] font-semibold">
        {opts.map(([v, lbl]) => (
          <button key={v} onClick={() => onChange(v)}
            title={soon && v === 'automated' ? soon : ''}
            className={`rounded-md px-2 py-0.5 ${value === v ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-400 hover:text-slate-600'}`}>
            {lbl}{soon && v === 'automated' ? ' •' : ''}
          </button>
        ))}
      </div>
    </div>
  )
}

/**
 * Execution mode, with the truth about whether it is actually running.
 *
 * The plain ModeToggle would sit on "Auto" whenever the config says automated —
 * but automated execution stands down while Stoploss is Manual or signals are
 * halted, and a switch that reads Auto while nothing trades is the single most
 * dangerous thing this bar could display. So the armed state comes from the
 * backend (`summary.auto_exec.armed`), not from the mode string, and the reason
 * it is held back is shown next to it rather than buried in a tooltip.
 */
function ExecutionToggle({ summary, onMode }: {
  summary: Summary | null
  onMode: (k: string, v: string) => void
}) {
  const mode = summary?.execution_mode ?? 'manual'
  const auto = summary?.auto_exec
  const isAuto = mode === 'automated'
  const armed = !!auto?.armed
  const blocked = isAuto && !armed

  const armedTitle = auto
    ? `Automated execution is LIVE — risking ${auto.risk_pct}% of budget per trade, `
      + `max ${auto.max_lots} lots, max ${auto.max_positions} positions, `
      + `averaging ${auto.averaging_mode}.`
    : 'Automated execution'

  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Execution</span>
      <div className={`inline-flex rounded-lg border p-0.5 text-[11px] font-semibold ${
        blocked ? 'border-amber-300 bg-amber-50'
          : isAuto ? 'border-rose-300 bg-rose-50' : 'border-slate-200 bg-slate-50'}`}>
        <button onClick={() => onMode('execution_mode', 'manual')}
          title="You place every entry yourself from the dashboard"
          className={`rounded-md px-2 py-0.5 ${!isAuto ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-400 hover:text-slate-600'}`}>
          Manual
        </button>
        <button onClick={() => onMode('execution_mode', 'automated')}
          title={blocked ? `Set to Auto but NOT trading — ${auto?.blocked_by}` : armedTitle}
          className={`rounded-md px-2 py-0.5 ${
            isAuto ? (blocked ? 'bg-white text-amber-700 shadow-sm' : 'bg-rose-600 text-white shadow-sm')
              : 'text-slate-400 hover:text-slate-600'}`}>
          Auto
        </button>
      </div>
      {blocked && (
        <span title={auto?.blocked_by}
          className="max-w-[190px] truncate rounded-md border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] font-bold text-amber-800">
          not trading — {auto?.blocked_by}
        </span>
      )}
    </div>
  )
}

export function TopBar({ summary, onConfig, onKill, onMode, onPaperChanged }: {
  summary: Summary | null
  onConfig: () => void
  onKill: () => void
  onMode: (key: string, value: string) => void
  onPaperChanged: (msg: string) => void
}) {
  const b = summary?.budget
  const capBadge =
    b?.cap_state === 'HARD' ? 'border-rose-200 bg-rose-50 text-rose-700'
    : b?.cap_state === 'SOFT' ? 'border-amber-200 bg-amber-50 text-amber-700'
    : 'border-emerald-200 bg-emerald-50 text-emerald-700'
  // Prefix-matched, not equality: the status carries a qualifier — which day the
  // holiday is, and whether an OPEN is real or forced by the override.
  const mkt = summary?.market_status ?? ''
  const mktCls =
    mkt.startsWith('OPEN (') ? 'text-rose-600'        // forced — the exchange is actually shut
    : mkt === 'OPEN' ? 'text-emerald-600'
    : mkt.startsWith('HOLIDAY') || mkt === 'PRE-MARKET' ? 'text-amber-600'
    : 'text-slate-400'

  // Hover text for the Shoonya dot. The connection flag alone cannot explain a
  // frozen board, so say how old the newest price is and what the warmer last
  // tripped over — the two facts that actually locate the fault.
  const pf = summary?.price_feed
  const age = pf?.price_age_seconds
  const feedTitle = !summary?.connection.broker
    ? 'Broker gateway is not answering /api/status — prices are frozen at their last value'
    : !pf || pf.stalled
      ? `No price update in ${age == null ? 'this session' : `${Math.round(age)}s`}`
        + ` · ${pf?.quotes_empty ?? 0} empty quotes · ticker ${pf?.ticker_connected ? 'up' : 'DOWN'}`
        + (pf?.warmer_last_error ? ` · warmer: ${pf.warmer_last_error.slice(0, 120)}` : '')
      : `Live · newest price ${Math.round(age ?? 0)}s ago · ${pf.symbols_priced} symbols priced`

  return (
    <header className="sticky top-0 z-40 border-b border-slate-200 bg-white">
      <div className="flex flex-wrap items-center gap-3 px-4 py-2.5">
        {/* Brand */}
        <div className="flex items-center gap-2.5 pr-1">
          <div className="grid h-8 w-8 place-items-center rounded-lg bg-sky-600 text-sm font-bold text-white select-none">S</div>
          <div>
            <div className="text-[15px] font-bold leading-none tracking-tight text-slate-800">Reversal Strategy</div>
            <div className="text-[10px] font-medium text-slate-400">Reversal Strategy</div>
          </div>
        </div>

        {/* Stat boxes (your top-menu sketch) */}
        <StatBox label="Total Money (Shoonya)" value={inr(summary?.shoonya_cash ?? 0)} sub="broker cash" />
        {/* `available_for_new`, not `available`. The latter is total minus used and
            so includes the averaging reserve, which a new entry cannot spend —
            showing it as "free" overstated the headroom by the whole reserve. */}
        <StatBox label="Budget · This Strategy" value={inr(b?.total_budget ?? 0)}
                 sub={`${b?.utilisation_pct?.toFixed(0) ?? 0}% used · ${inr(b?.available_for_new ?? 0)} for new`} />
        <StatBox label="Reserve · Averaging" value={inr(b?.reserve ?? 0)}
                 sub={`${b?.reserve_pct?.toFixed(0) ?? 0}% locked${b?.reserve_in_use ? ` · ${inr(b.reserve_in_use)} in use` : ''}`} />
        <StatBox label="Today's P&L" value={inr2(summary?.today_pnl ?? 0)} valueClass={pnlColor(summary?.today_pnl ?? 0)} />
        <StatBox label="Overall P&L" value={inr2(summary?.overall_pnl ?? 0)} valueClass={pnlColor(summary?.overall_pnl ?? 0)} />
        {(summary?.paper?.enabled || !!summary?.paper?.trades || !!summary?.paper?.open_positions) && (
          <StatBox label="Paper P&L" value={inr2(summary.paper.overall_pnl)}
            valueClass={pnlColor(summary.paper.overall_pnl)}
            accent="border-indigo-300"
            sub={`${summary.paper.open_positions} open · ${summary.paper.trades} closed · ${summary.paper.win_rate}% win`} />
        )}

        <div className="ml-auto flex items-center gap-2.5">
          <span className={`rounded-lg border px-2 py-1 text-[10px] font-bold ${capBadge}`}>
            {b?.cap_state === 'HARD' ? 'BUDGET EXHAUSTED' : b?.cap_state === 'SOFT' ? 'SOFT CAP 80%' : 'BUDGET OK'}
          </span>
          <span className={`flex items-center gap-1.5 text-[11px] font-bold ${mktCls}`}>
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-current" />
            {mkt || '—'}
          </span>
          <div className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-500">
            {/* Amber when the broker handshake is fine but no price has landed
                in two minutes — the state where positions sit frozen at their
                entry price while the connection still reports healthy. */}
            <span title={feedTitle}>
              <Dot on={!!summary?.connection.broker} warn={!!summary?.price_feed?.stalled} /> Shoonya
            </span>
          </div>

          <BrokerAccount connected={!!summary?.connection.broker} onChanged={onPaperChanged} />

          {/* Stock Selection + Execution mode toggles */}
          <ModeToggle label="Selection" value={summary?.stock_selection_mode ?? 'automated'}
            onChange={v => onMode('stock_selection_mode', v)} />
          <ExecutionToggle summary={summary} onMode={onMode} />

          {/* PAPER TRADING — start/stop the test run */}
          <PaperControl paper={summary?.paper} onChanged={onPaperChanged} />

          {/* CONFIG — beside the P&L / info, per spec */}
          <button onClick={onConfig} title="Strategy configuration"
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50">
            Config
          </button>

          {/* KILL SWITCH */}
          <button onClick={onKill}
            className="rounded-lg bg-rose-600 px-3 py-1.5 text-xs font-bold text-white hover:bg-rose-700">
            KILL SWITCH
          </button>

          <button onClick={logout} title="Sign out of the Reversal dashboard"
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-50">
            Sign out
          </button>
        </div>
      </div>
    </header>
  )
}
