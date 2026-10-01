import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { AlertSettings, PnlResponse } from '../types'
import { Badge, inr, pnlClass } from './ui'

/** Full account P&L panel. The header shows a simple summary; this modal shows
 * "all the things": the engine's independent-lot ladder ledger AND Shoonya's own
 * settlement-aware P&L (per symbol + total), funds/margin and account profile. */
/** Last payload, kept at module scope so it outlives the modal.
 *
 * The panel used to start empty on every open and sit there reading
 * "disconnected" for as long as the broker round trip took — several seconds —
 * while perfectly good numbers from moments earlier were being thrown away on
 * close. Now the previous figures appear immediately and the poll refreshes them
 * underneath, with their age on screen so nobody mistakes them for live. */
let cachedPnl: PnlResponse | null = null
let cachedAtMs = 0

export function PnlPanel({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<PnlResponse | null>(cachedPnl)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(cachedPnl === null)
  const [ageMs, setAgeMs] = useState(cachedPnl ? Date.now() - cachedAtMs : 0)

  const load = useCallback(async () => {
    try {
      const fresh = await api.pnl()
      cachedPnl = fresh
      cachedAtMs = Date.now()
      setData(fresh); setAgeMs(0); setErr('')
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed to load P&L')
    } finally { setLoading(false) }
  }, [])

  useEffect(() => {
    load()
    // The endpoint now answers from a server-side cache, so polling costs
    // almost nothing and the panel can track the rest of the dashboard.
    const t = setInterval(load, 1500)
    // Tick the age separately so a slow or failing refresh visibly grows stale
    // rather than quietly presenting old numbers as current.
    const a = setInterval(() => setAgeMs(cachedAtMs ? Date.now() - cachedAtMs : 0), 1000)
    return () => { clearInterval(t); clearInterval(a) }
  }, [load])

  const eng = data?.engine
  const brk = data?.broker

  return (
    <div className="fixed inset-0 z-50 bg-gray-900/50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white border border-gray-300 rounded-2xl w-full max-w-5xl h-[88vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}>
        <header className="flex items-center justify-between px-5 py-3 border-b border-gray-200">
          <div className="font-semibold text-gray-900 flex items-center gap-2">
            Account P&amp;L
            <span className="text-[11px] text-gray-500 font-normal">
              engine ledger + broker (Shoonya) ·{' '}
              {/* The broker figures come from a server-side snapshot refreshed
                  behind the request, so its age — not the time since our own
                  fetch — is what actually tells you how current they are. */}
              {loading && !data ? 'loading…'
                : (() => {
                    const age = data?.broker.age_seconds
                    const secs = age != null ? age : ageMs / 1000
                    return secs < 5 ? 'live' : `broker data ${Math.round(secs)}s old`
                  })()}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {err && <span className="text-red-600 text-xs">{err}</span>}
            <button onClick={load} className="px-2 py-1 rounded border border-gray-300 text-gray-700 hover:border-sky-500 hover:text-sky-700 text-[11px]">↻ Refresh</button>
            <button onClick={onClose} className="text-gray-500 hover:text-gray-800 text-2xl leading-none px-1">×</button>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto p-5 space-y-5">
          {loading && !data && <div className="text-gray-500 text-sm">loading…</div>}

          <AlertsSettings />


          {/* ---- headline numbers (engine ledger) ---- */}
          {eng && (
            <section>
              <div className="text-[11px] uppercase tracking-wide text-sky-600 mb-2">Grid engine — independent-lot ledger</div>
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
                <Stat label="Net P&L (today)" value={eng.net_today} big />
                <Stat label="Open P&L" value={eng.total_open_pnl} />
                <Stat label="Realized today" value={eng.realized_today} />
                <Stat label="Realized all-time" value={eng.realized_all_time} />
                <Stat label="Open rungs" value={eng.open_rungs} plain />
              </div>
            </section>
          )}

          {/* ---- broker account (Shoonya) ---- */}
          <section>
            <div className="text-[11px] uppercase tracking-wide text-emerald-600 mb-2 flex items-center gap-2">
              Broker account (Shoonya)
              {/* Before the first reply we do not KNOW the state, and saying
                  "disconnected" while merely waiting sent people off to fix a
                  connection that was fine. */}
              {!data
                ? <Badge tone="gray">checking…</Badge>
                : <Badge tone={brk?.connected ? 'green' : 'red'}>{brk?.connected ? 'connected' : 'disconnected'}</Badge>}
            </div>
            {!data ? (
              <div className="text-gray-500 text-sm bg-gray-50 border border-gray-200 rounded-lg px-3 py-3">
                Checking the broker session…
              </div>
            ) : !brk?.connected ? (
              <div className="text-gray-500 text-sm bg-gray-50 border border-gray-200 rounded-lg px-3 py-3">
                Broker session is down at the gateway — funds, positions and settlement P&amp;L are unavailable.
                Connect Shoonya from the Gateway UI (port 5173).
              </div>
            ) : (
              <>
                {/* funds / margin */}
                {brk.funds && (
                  <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 mb-4">
                    <Stat label="Remaining margin" value={brk.funds.cash - brk.funds.margin_used} big />
                    <Stat label="Cash / balance" value={brk.funds.cash} plainInr />
                    <Stat label="Margin used" value={brk.funds.margin_used} plainInr />
                    <Stat label="Collateral" value={brk.funds.collateral} plainInr />
                    <Stat label="Pay-in" value={brk.funds.payin} plainInr />
                  </div>
                )}

                {/* broker positions with settlement-aware P&L */}
                <div className="bg-gray-50 border border-gray-200 rounded-lg overflow-hidden">
                  <div className="flex items-center justify-between px-3 py-2 border-b border-gray-200">
                    <span className="text-xs font-semibold text-gray-700">Broker positions (settlement-aware P&amp;L)</span>
                    <span className={`text-sm font-mono font-semibold ${pnlClass(brk.positions?.total_pnl ?? 0)}`}>
                      total {inr(brk.positions?.total_pnl ?? 0)}
                    </span>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-[12px]">
                      <thead>
                        <tr className="text-gray-500 text-[10px] uppercase tracking-wide">
                          <th className="text-left px-3 py-1.5">Symbol</th>
                          <th className="text-left px-2">Exch</th>
                          <th className="text-right px-2">Net qty</th>
                          <th className="text-right px-2">Buy avg</th>
                          <th className="text-right px-2">Sell avg</th>
                          <th className="text-right px-2">LTP</th>
                          <th className="text-right px-3">P&amp;L</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(brk.positions?.symbol_groups ?? []).flatMap((g) => g.positions).length === 0 && (
                          <tr><td colSpan={7} className="text-center text-gray-500 py-4">No broker positions.</td></tr>
                        )}
                        {(brk.positions?.symbol_groups ?? []).flatMap((g) => g.positions).map((p, idx) => (
                          <tr key={`${p.exch}-${p.tsym}-${idx}`} className="border-t border-gray-200">
                            <td className="px-3 py-1.5 text-gray-800">{p.tsym}</td>
                            <td className="px-2 text-gray-500">{p.exch}</td>
                            <td className="px-2 text-right font-mono text-gray-700">{p.netqty}</td>
                            <td className="px-2 text-right font-mono text-gray-600">{p.buyavgprc}</td>
                            <td className="px-2 text-right font-mono text-gray-600">{p.sellavgprc}</td>
                            <td className="px-2 text-right font-mono text-gray-700">{p.lp}</td>
                            <td className={`px-3 text-right font-mono font-semibold ${pnlClass(p.total_pnl)}`}>{inr(p.total_pnl)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>

                {/* account profile */}
                {brk.account && (
                  <div className="mt-3 text-[11px] text-gray-500 flex flex-wrap gap-x-4 gap-y-1">
                    <span>Acct <span className="text-gray-700">{brk.account.actid || brk.account.uid || '—'}</span></span>
                    {brk.account.brkname && <span>Broker <span className="text-gray-700">{brk.account.brkname}</span></span>}
                    {brk.account.email && <span>Email <span className="text-gray-700">{brk.account.email}</span></span>}
                    {brk.account.exarr?.length ? <span>Segments <span className="text-gray-700">{brk.account.exarr.join(', ')}</span></span> : null}
                  </div>
                )}
                {brk.error && <div className="mt-2 text-amber-600 text-[11px]">note: {brk.error}</div>}
              </>
            )}
          </section>

          {/* ---- engine per-instrument breakdown ---- */}
          {eng && eng.per_instrument.length > 0 && (
            <section>
              <div className="text-[11px] uppercase tracking-wide text-sky-600 mb-2">Per-instrument (engine)</div>
              <div className="bg-gray-50 border border-gray-200 rounded-lg overflow-x-auto">
                <table className="w-full text-[12px]">
                  <thead>
                    <tr className="text-gray-500 text-[10px] uppercase tracking-wide">
                      <th className="text-left px-3 py-1.5">Instrument</th>
                      <th className="text-left px-2">Type</th>
                      <th className="text-center px-2">Open rungs</th>
                      <th className="text-right px-2">Open P&amp;L</th>
                      <th className="text-right px-3">Realized today</th>
                    </tr>
                  </thead>
                  <tbody>
                    {eng.per_instrument.map((i) => (
                      <tr key={`${i.exch}-${i.sym}`} className="border-t border-gray-200">
                        <td className="px-3 py-1.5 text-gray-800">{i.sym} <span className="text-gray-500">{i.exch}</span></td>
                        <td className="px-2">
                          <Badge tone={i.mode === 'ladder' ? 'purple' : 'blue'}>{i.mode === 'ladder' ? 'LADDER' : 'AUTO'}</Badge>
                          {i.instr_type === 'OPT' && <Badge tone="green">OPT</Badge>}
                        </td>
                        <td className="px-2 text-center text-gray-700">{i.open_rungs}</td>
                        <td className={`px-2 text-right font-mono ${pnlClass(i.open_pnl)}`}>{inr(i.open_pnl)}</td>
                        <td className={`px-3 text-right font-mono ${pnlClass(i.realized_today)}`}>{inr(i.realized_today)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  )
}

function AlertsSettings() {
  const [s, setS] = useState<AlertSettings | null>(null)
  const [number, setNumber] = useState('')
  const [apikey, setApikey] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [onDown, setOnDown] = useState(true)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    api.getSettings().then((d) => {
      setS(d); setNumber(d.whatsapp_number); setEnabled(d.whatsapp_enabled); setOnDown(d.alert_on_feed_down)
    }).catch(() => { /* engine may be down */ })
  }, [])

  const save = async () => {
    setSaving(true); setMsg('')
    try {
      const body: Record<string, unknown> = {
        whatsapp_enabled: enabled, whatsapp_number: number.trim(), alert_on_feed_down: onDown,
      }
      if (apikey.trim()) body.callmebot_apikey = apikey.trim()
      const d = await api.saveSettings(body)
      setS(d); setApikey(''); setMsg('saved ✓')
    } catch (e) { setMsg(e instanceof Error ? e.message : 'save failed') }
    finally { setSaving(false) }
  }

  return (
    <section className="bg-amber-50 border border-amber-200 rounded-lg p-4">
      <div className="text-[11px] uppercase tracking-wide text-amber-700 mb-2 flex items-center gap-2">
        WhatsApp alerts (feed-down manual-exit)
        <Badge tone={s?.whatsapp_enabled ? 'green' : 'gray'}>{s?.whatsapp_enabled ? 'on' : 'off'}</Badge>
      </div>
      <p className="text-[11px] text-gray-600 mb-3">
        If the Shoonya feed dies while you hold positions, the engine can no longer auto-exit. It will send a
        WhatsApp message telling you exactly what to sell (per independent rung). Delivery uses CallMeBot —
        message their bot once to get a free API key, then paste it here.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="flex items-center gap-2 text-sm text-gray-700">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)}
            className="w-4 h-4 accent-emerald-500" />
          Enable WhatsApp alerts
        </label>
        <label className="flex items-center gap-2 text-sm text-gray-700">
          <input type="checkbox" checked={onDown} onChange={(e) => setOnDown(e.target.checked)}
            className="w-4 h-4 accent-emerald-500" />
          Alert when feed dies with open positions
        </label>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-500">WhatsApp number (E.164)</div>
          <input type="text" value={number} onChange={(e) => setNumber(e.target.value)}
            placeholder="+9198XXXXXXXX"
            className="w-full bg-white border border-gray-300 rounded-md px-2 py-1.5 text-sm mt-1 focus:outline-none focus:border-amber-500" />
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-500">
            CallMeBot API key {s?.callmebot_apikey_set && <span className="text-emerald-600">(set — blank keeps it)</span>}
          </div>
          <input type="password" value={apikey} onChange={(e) => setApikey(e.target.value)}
            placeholder={s?.callmebot_apikey_set ? '•••••• (unchanged)' : 'paste CallMeBot key'}
            className="w-full bg-white border border-gray-300 rounded-md px-2 py-1.5 text-sm mt-1 focus:outline-none focus:border-amber-500" />
        </div>
      </div>
      <div className="flex items-center gap-3 mt-3">
        <button onClick={save} disabled={saving}
          className="px-4 py-1.5 rounded-lg bg-amber-600 hover:bg-amber-500 text-white text-sm font-semibold disabled:opacity-50">
          {saving ? 'Saving…' : 'Save alert settings'}
        </button>
        {msg && <span className={`text-xs ${msg.includes('✓') ? 'text-emerald-600' : 'text-red-600'}`}>{msg}</span>}
      </div>
    </section>
  )
}

function Stat({ label, value, big, plain, plainInr }: {
  label: string; value: number; big?: boolean; plain?: boolean; plainInr?: boolean
}) {
  const cls = plain ? 'text-gray-900' : plainInr ? 'text-gray-900' : pnlClass(value)
  const text = plain ? String(value) : inr(value)
  return (
    <div className="bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`font-mono font-semibold tabular-nums ${big ? 'text-xl' : 'text-base'} ${cls}`}>{text}</div>
    </div>
  )
}
