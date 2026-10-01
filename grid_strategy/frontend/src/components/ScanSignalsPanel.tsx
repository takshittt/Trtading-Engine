import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { ScanSignalRow, ScanSignalsResponse } from '../types'
import { Badge, fmtTime } from './ui'

/** minutes → compact label: 5→5m, 15→15m, 60→1h, 240→4h */
const tfLabel = (m: number) => (m >= 60 && m % 60 === 0 ? `${m / 60}h` : `${m}m`)

function agoText(iso: string | null, nowMs: number): string {
  if (!iso) return 'never'
  const s = Math.max(0, Math.round((nowMs - new Date(iso).getTime()) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s ago`
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`
}

/** green if a scan arrived < 6 min ago, amber < 12 min, red otherwise/never.
 * The 5-min scan should keep this GREEN — that's the pipeline heartbeat. */
function freshTone(iso: string | null, nowMs: number): 'green' | 'amber' | 'red' {
  if (!iso) return 'red'
  const s = (nowMs - new Date(iso).getTime()) / 1000
  return s <= 6 * 60 ? 'green' : s <= 12 * 60 ? 'amber' : 'red'
}

function SignalCell({ row }: { row?: ScanSignalRow }) {
  if (!row || row.action === 'NEUTRAL') {
    return <span className="text-gray-300 text-[11px] select-none">—</span>
  }
  const tone = row.action === 'BUY' ? 'green' : 'red'
  const title = `${row.action} @ ${row.price || '—'} · fired ${fmtTime(row.bar_time ?? '')}`
  return <span title={title}><Badge tone={tone}>{row.action}</Badge></span>
}

/** "Check Signals" — the AmiBroker market-scan grid. Display-only: latest sticky
 * BUY/SELL per F&O stock × timeframe, NEUTRAL where nothing has fired. Refreshes
 * every 20s; the header banner shows whether the 5-min scan pipeline is alive. */
export function ScanSignalsModal({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<ScanSignalsResponse | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [activeOnly, setActiveOnly] = useState(false)
  const [nowMs, setNowMs] = useState(() => Date.now())

  const load = () =>
    api.scanSignals()
      .then((d) => { setData(d); setErr('') })
      .catch((e) => setErr(e instanceof Error ? e.message : 'failed to load'))
      .finally(() => setLoading(false))

  useEffect(() => {
    load()
    const poll = setInterval(load, 20_000)       // pull fresh scan data
    const tick = setInterval(() => setNowMs(Date.now()), 1_000)  // live "Ns ago"
    return () => { clearInterval(poll); clearInterval(tick) }
  }, [])

  const timeframes = data?.timeframes?.length ? data.timeframes : [5, 15, 60, 240]

  // pivot flat rows → symbol → (timeframe → row)
  const bySym = useMemo(() => {
    const m = new Map<string, Map<number, ScanSignalRow>>()
    for (const r of data?.signals ?? []) {
      if (!m.has(r.sym)) m.set(r.sym, new Map())
      m.get(r.sym)!.set(r.timeframe_min, r)
    }
    return m
  }, [data])

  const rows = useMemo(() => {
    const needle = q.trim().toUpperCase()
    let syms = [...bySym.keys()]
    if (needle) syms = syms.filter((s) => s.includes(needle))
    if (activeOnly) {
      syms = syms.filter((s) => [...bySym.get(s)!.values()].some((r) => r.action !== 'NEUTRAL'))
    }
    return syms.sort()
  }, [bySym, q, activeOnly])

  const tone = freshTone(data?.last_scan_at ?? null, nowMs)
  const banner = tone === 'green' ? 'bg-emerald-500/10 border-emerald-500 text-emerald-700'
    : tone === 'amber' ? 'bg-amber-500/10 border-amber-500 text-amber-700'
    : 'bg-red-500/10 border-red-400 text-red-600'
  const dot = tone === 'green' ? 'bg-emerald-500' : tone === 'amber' ? 'bg-amber-500' : 'bg-red-500'
  const c = data?.counts

  return (
    <div className="fixed inset-0 z-50 bg-gray-900/50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white border border-gray-300 rounded-2xl w-full max-w-5xl h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}>
        <header className="flex items-center justify-between px-5 py-3 border-b border-gray-200">
          <div className="font-semibold text-gray-900">
            F&amp;O Scan Signals
            {data && <span className="text-gray-500 font-normal text-sm"> · {data.symbols} stocks</span>}
          </div>
          <div className="flex items-center gap-2">
            <input placeholder="filter symbol…" value={q} onChange={(e) => setQ(e.target.value.toUpperCase())}
              className="bg-gray-50 border border-gray-300 rounded-md px-2 py-1 text-xs text-gray-700 w-32" />
            <label className="flex items-center gap-1 text-xs text-gray-600 select-none cursor-pointer">
              <input type="checkbox" checked={activeOnly} onChange={(e) => setActiveOnly(e.target.checked)} />
              signals only
            </label>
            <button onClick={onClose} className="text-gray-500 hover:text-gray-800 text-2xl leading-none px-1">×</button>
          </div>
        </header>

        {/* pipeline heartbeat — the whole point of this phase: is the 5-min scan arriving? */}
        <div className={`mx-5 mt-3 px-3 py-2 rounded-lg border text-xs flex items-center gap-2 ${banner}`}>
          <span className={`w-2.5 h-2.5 rounded-full ${dot} shrink-0`} />
          {data?.last_scan_at
            ? <span>Last scan received <b>{fmtTime(data.last_scan_at)}</b> ({agoText(data.last_scan_at, nowMs)}).
              {tone === 'green' ? ' Pipeline is live — updates ~every 5 min.'
                : tone === 'amber' ? ' Overdue — expected a scan within 5 min.'
                  : ' No recent scan — the AmiBroker scanner or bridge may be down.'}</span>
            : <span>No scan received yet. Waiting for the AmiBroker 5-min scan to POST to <code>/webhook/scan</code>…</span>}
          {c && <span className="ml-auto text-gray-500">
            <b className="text-emerald-600">{c.BUY}</b> buy · <b className="text-red-500">{c.SELL}</b> sell · {c.NEUTRAL} neutral
          </span>}
        </div>

        <div className="flex-1 overflow-auto px-5 py-3">
          {err && <div className="text-red-600 text-sm p-2">{err}</div>}
          {loading && !data && <div className="text-gray-500 text-sm p-3">loading…</div>}
          {data && rows.length === 0 && (
            <div className="text-gray-500 text-sm p-4">
              {data.symbols === 0 ? 'No scan data yet — nothing has been received from AmiBroker.' : 'No symbols match.'}
            </div>
          )}
          {rows.length > 0 && (
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-white">
                <tr className="text-gray-500 text-xs border-b border-gray-200">
                  <th className="text-left font-medium px-2 py-1.5">Symbol</th>
                  {timeframes.map((tf) => (
                    <th key={tf} className="text-center font-medium px-2 py-1.5">{tfLabel(tf)}</th>
                  ))}
                  <th className="text-right font-medium px-2 py-1.5">Last update</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((sym) => {
                  const cells = bySym.get(sym)!
                  const latest = [...cells.values()].reduce<string | null>(
                    (acc, r) => (r.last_scan && (!acc || r.last_scan > acc) ? r.last_scan : acc), null)
                  return (
                    <tr key={sym} className="border-b border-gray-100 hover:bg-gray-50">
                      <td className="px-2 py-1.5 font-medium text-gray-900">{sym}</td>
                      {timeframes.map((tf) => (
                        <td key={tf} className="px-2 py-1.5 text-center"><SignalCell row={cells.get(tf)} /></td>
                      ))}
                      <td className="px-2 py-1.5 text-right text-[11px] text-gray-500 font-mono tabular-nums">
                        {fmtTime(latest ?? '')}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>

        <footer className="px-5 py-2.5 border-t border-gray-200 text-[11px] text-gray-500">
          Display only — these signals never place orders. Sticky: a BUY/SELL stays until a newer signal replaces it.
          AmiBroker → POST /webhook/scan.
        </footer>
      </div>
    </div>
  )
}
