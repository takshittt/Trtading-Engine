import { useEffect, useRef, useState } from 'react'
import {
  createChart, ColorType, CrosshairMode,
  type SeriesMarker, type Time, type UTCTimestamp,
} from 'lightweight-charts'
import { API_BASE } from '../../api'
import { Badge, Spinner, fmtPx, inr, pnlClass } from '../ui'
import type { BtPreset, BtRequest, BtResult } from './backtestTypes'

const IST_SHIFT = 19800

/** Backtest lab — yfinance history + RSI(30↑) dip signals through the SAME
 * ladder math as live (sizing, chained targets, per-lot SL, CB, rollover).
 * Pure sandbox: nothing here touches the live engine or its DB. */
export function BacktestModal({ onClose }: { onClose: () => void }) {
  const [presets, setPresets] = useState<BtPreset[]>([])
  const [intervals, setIntervals] = useState<string[]>(['1d'])
  const [req, setReq] = useState<BtRequest>({
    preset: 'NATURALGAS', ticker: '', interval: '1d', days: 365,
    rsi_period: 14, rsi_level: 30,
    target_mode: 'percent', target_value: 5, target_chain_pct: 100, sl_mode: 'percent', sl_value: 12,
    sizing_mode: 'vol_target', vol_formula: 'atr_rupee', risk_per_rung: 100000,
    fixed_lots: 1, atr_period: 14, min_lots: 1, max_lots_per_rung: 5, max_rungs: 6,
    min_gap_points: 0, slippage_points: 0, cb_threshold: 0,
    roll_enabled: true, start_capital: 0,
  })
  const [running, setRunning] = useState(false)
  const [err, setErr] = useState('')
  const [res, setRes] = useState<BtResult | null>(null)
  const [tab, setTab] = useState<'chart' | 'trades' | 'math'>('chart')

  useEffect(() => {
    fetch(`${API_BASE}/api/backtest/presets`)
      .then((r) => r.json())
      .then((d) => { setPresets(d.presets); setIntervals(d.intervals) })
      .catch(() => setErr('backtest module not available — run: poetry install --with backtest'))
  }, [])

  const set = (k: keyof BtRequest, v: unknown) => setReq((f) => ({ ...f, [k]: v }))
  const num = (v: string) => (v === '' ? 0 : Number(v))

  const run = async () => {
    setRunning(true)
    setErr('')
    try {
      const r = await fetch(`${API_BASE}/api/backtest/run`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(req),
      })
      if (!r.ok) {
        const j = await r.json().catch(() => ({}))
        throw new Error(j.detail ?? r.statusText)
      }
      setRes(await r.json())
      setTab('chart')
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'run failed')
    } finally {
      setRunning(false)
    }
  }

  const inputCls = 'w-full bg-gray-50 border border-gray-300 rounded-md px-2 py-1 text-[13px] focus:outline-none focus:border-sky-500'
  const L = ({ children }: { children: React.ReactNode }) =>
    <label className="text-[10px] uppercase tracking-wide text-gray-500 block mt-2">{children}</label>

  const s = res?.stats

  return (
    <div className="fixed inset-0 z-50 bg-gray-900/50 flex items-center justify-center p-3" onClick={onClose}>
      <div className="bg-white border border-gray-300 rounded-2xl w-full max-w-7xl h-[92vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}>
        <header className="flex items-center justify-between px-5 py-3 border-b border-gray-200 shrink-0">
          <div>
            <div className="font-semibold text-gray-900">Backtest Lab</div>
            <div className="text-[11px] text-gray-500">
              yfinance history + RSI-crosses-{req.rsi_level}↑ dip signals → the exact live ladder math. Sandbox only — live engine untouched.
            </div>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-800 text-2xl leading-none">×</button>
        </header>

        <div className="flex flex-1 min-h-0">
          {/* ============ config column ============ */}
          <div className="w-72 shrink-0 border-r border-gray-200 overflow-y-auto p-4">
            <div className="text-[11px] font-semibold text-sky-600">DATA (yfinance)</div>
            <L>Instrument</L>
            <select value={req.preset} onChange={(e) => set('preset', e.target.value)} className={inputCls}>
              {presets.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
              <option value="CUSTOM">Custom ticker…</option>
            </select>
            {req.preset === 'CUSTOM' && (<>
              <L>yfinance ticker</L>
              <input value={req.ticker} onChange={(e) => set('ticker', e.target.value)} placeholder="NG=F" className={inputCls} />
              <L>Lot size (₹ per point per lot)</L>
              <input type="number" value={req.lot_size ?? 1} onChange={(e) => set('lot_size', num(e.target.value))} className={inputCls} />
            </>)}
            <div className="grid grid-cols-2 gap-2">
              <div><L>Interval</L>
                <select value={req.interval} onChange={(e) => set('interval', e.target.value)} className={inputCls}>
                  {intervals.map((i) => <option key={i}>{i}</option>)}
                </select>
              </div>
              <div><L>Days back</L>
                <input type="number" value={req.days} onChange={(e) => set('days', num(e.target.value))} className={inputCls} />
              </div>
            </div>
            <label className="flex items-center gap-2 mt-3 text-[12px] text-gray-700">
              <input type="checkbox" checked={req.roll_enabled} onChange={(e) => set('roll_enabled', e.target.checked)}
                className="accent-sky-500" disabled={req.interval !== '1d'} />
              Simulate futures rollover (1d only)
            </label>

            <div className="text-[11px] font-semibold text-sky-600 mt-4">SIGNAL (live-signal stand-in)</div>
            <div className="grid grid-cols-2 gap-2">
              <div><L>RSI period</L>
                <input type="number" value={req.rsi_period} onChange={(e) => set('rsi_period', num(e.target.value))} className={inputCls} /></div>
              <div><L>Oversold level ↑</L>
                <input type="number" value={req.rsi_level} onChange={(e) => set('rsi_level', num(e.target.value))} className={inputCls} /></div>
            </div>
            <div className="text-[10px] text-gray-500 mt-1">BUY when RSI closes back above the level from below. Daily RSI&lt;30 is rare — raise to 35–40 for more trades.</div>

            <div className="text-[11px] font-semibold text-sky-600 mt-4">LADDER CONFIG (same as live)</div>
            <div className="grid grid-cols-2 gap-2">
              <div><L>Target</L>
                <input type="number" step="any" value={req.target_value} onChange={(e) => set('target_value', num(e.target.value))} className={inputCls} /></div>
              <div><L>mode</L>
                <select value={req.target_mode} onChange={(e) => set('target_mode', e.target.value)} className={inputCls}>
                  <option value="points">points</option><option value="percent">%</option></select></div>
              <div><L>Stop-loss</L>
                <input type="number" step="any" value={req.sl_value} onChange={(e) => set('sl_value', num(e.target.value))} className={inputCls} /></div>
              <div><L>mode</L>
                <select value={req.sl_mode} onChange={(e) => set('sl_mode', e.target.value)} className={inputCls}>
                  <option value="points">points</option><option value="percent">%</option></select></div>
              <div><L>Chain % (lower-rung target)</L>
                <input type="number" step="any" min="0" value={req.target_chain_pct} onChange={(e) => set('target_chain_pct', num(e.target.value))} className={inputCls} /></div>
              <div className="self-end text-[10px] text-gray-500 pb-1">
                chained target = prev buy + this % of the offset (100 = full, same as live)</div>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div><L>Sizing</L>
                <select value={req.sizing_mode} onChange={(e) => set('sizing_mode', e.target.value)} className={inputCls}>
                  <option value="vol_target">vol target</option><option value="fixed">fixed</option></select></div>
              <div><L>Formula</L>
                <select value={req.vol_formula} onChange={(e) => set('vol_formula', e.target.value)} className={inputCls}>
                  <option value="notional">risk/(P×ATR)</option><option value="atr_rupee">risk/(ATR×lot)</option></select></div>
              <div><L>Risk per rung ₹</L>
                <input type="number" value={req.risk_per_rung} onChange={(e) => set('risk_per_rung', num(e.target.value))} className={inputCls} /></div>
              <div><L>ATR period</L>
                <input type="number" value={req.atr_period} onChange={(e) => set('atr_period', num(e.target.value))} className={inputCls} /></div>
              <div><L>Max rungs</L>
                <input type="number" value={req.max_rungs} onChange={(e) => set('max_rungs', num(e.target.value))} className={inputCls} /></div>
              <div><L>Max lots/rung</L>
                <input type="number" value={req.max_lots_per_rung} onChange={(e) => set('max_lots_per_rung', num(e.target.value))} className={inputCls} /></div>
              <div><L>Min gap pts</L>
                <input type="number" step="any" value={req.min_gap_points} onChange={(e) => set('min_gap_points', num(e.target.value))} className={inputCls} /></div>
              <div><L>Slippage pts</L>
                <input type="number" step="any" value={req.slippage_points} onChange={(e) => set('slippage_points', num(e.target.value))} className={inputCls} /></div>
              <div><L>CB loss ₹ (0=off)</L>
                <input type="number" value={req.cb_threshold} onChange={(e) => set('cb_threshold', num(e.target.value))} className={inputCls} /></div>
              <div><L>Start capital ₹</L>
                <input type="number" value={req.start_capital} onChange={(e) => set('start_capital', num(e.target.value))} className={inputCls} /></div>
            </div>

            <button onClick={run} disabled={running}
              className="w-full mt-4 px-4 py-2 rounded-xl bg-sky-600 hover:bg-sky-500 text-white text-sm font-bold disabled:opacity-50">
              {running ? 'Running…' : '▶ RUN BACKTEST'}
            </button>
            {err && <div className="text-red-600 text-xs mt-2 whitespace-pre-wrap">{err}</div>}
            {res && <div className="text-[10px] text-gray-500 mt-3 whitespace-pre-wrap">{res.meta.note}</div>}
          </div>

          {/* ============ results ============ */}
          <div className="flex-1 min-w-0 flex flex-col">
            {!res && !running && (
              <div className="flex-1 flex items-center justify-center text-gray-500 text-sm">
                Configure on the left and hit RUN — same math, historical data.
              </div>
            )}
            {running && (
              <div className="flex-1 flex items-center justify-center gap-2 text-gray-500 text-sm">
                <Spinner /> downloading data & simulating…
              </div>
            )}
            {res && s && !running && (<>
              {/* stats strip */}
              <div className="grid grid-cols-4 lg:grid-cols-8 gap-2 p-3 border-b border-gray-200 shrink-0">
                <Stat label="Net P&L" value={inr(s.net_pnl)} cls={pnlClass(s.net_pnl)} />
                <Stat label="Trades" value={`${s.trades_closed}${s.open_at_end ? ` +${s.open_at_end} open` : ''}`} />
                <Stat label="Win rate" value={`${s.win_rate}%`} />
                <Stat label="Profit factor" value={s.profit_factor === null ? '∞' : String(s.profit_factor)} />
                <Stat label="Max DD" value={inr(s.max_drawdown)} cls="text-amber-600" />
                <Stat label="Target / SL" value={`${s.target_exits} / ${s.sl_exits}`} />
                <Stat label="Rollovers" value={String(s.rolls)} cls="text-sky-600" />
                <Stat label="CB trips" value={String(s.cb_trips)} />
              </div>

              <div className="flex gap-1 px-3 pt-2 shrink-0">
                {(['chart', 'trades', 'math'] as const).map((t) => (
                  <button key={t} onClick={() => setTab(t)}
                    className={`px-3 py-1 rounded-t-lg text-xs font-semibold ${tab === t ? 'bg-gray-100 text-sky-700' : 'text-gray-500 hover:text-gray-700'}`}>
                    {t === 'chart' ? 'Chart + Equity' : t === 'trades' ? `Trades (${res.trades.length})` : 'Math log'}
                  </button>
                ))}
              </div>

              <div className="flex-1 min-h-0 overflow-hidden px-3 pb-3">
                {tab === 'chart' && <BtCharts res={res} />}
                {tab === 'trades' && <BtTrades res={res} />}
                {tab === 'math' && (
                  <div className="h-full overflow-y-auto bg-gray-50 border border-gray-200 rounded-lg p-3 font-mono text-[11px] leading-relaxed text-gray-700 space-y-0.5">
                    {res.math_log.map((line, i) => (
                      <div key={i} className={line.includes("Don't execute") ? 'text-amber-600'
                        : line.includes('ROLLOVER') ? 'text-sky-600'
                        : line.includes('CIRCUIT') ? 'text-red-600'
                        : line.includes('Buy') ? 'text-emerald-600' : ''}>{line}</div>
                    ))}
                  </div>
                )}
              </div>
            </>)}
          </div>
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, cls = 'text-gray-900' }: { label: string; value: string; cls?: string }) {
  return (
    <div className="bg-gray-50 border border-gray-200 rounded-lg px-2.5 py-1.5">
      <div className="text-[9px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`text-sm font-semibold font-mono ${cls}`}>{value}</div>
    </div>
  )
}

function BtCharts({ res }: { res: BtResult }) {
  const priceRef = useRef<HTMLDivElement | null>(null)
  const eqRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!priceRef.current || !eqRef.current) return
    const priceEl = priceRef.current
    const eqEl = eqRef.current
    priceEl.innerHTML = ''
    eqEl.innerHTML = ''

    const common = {
      layout: { background: { type: ColorType.Solid, color: '#ffffff' }, textColor: '#4b5563' },
      grid: { vertLines: { color: '#eef1f5' }, horzLines: { color: '#eef1f5' } },
      timeScale: { timeVisible: res.meta.interval !== '1d', borderColor: '#d1d5db' },
      rightPriceScale: { borderColor: '#d1d5db' },
      autoSize: true,
    } as const

    const priceChart = createChart(priceEl, { ...common, crosshair: { mode: CrosshairMode.Normal } })
    const candles = priceChart.addCandlestickSeries({
      upColor: '#10b981', downColor: '#ef4444', wickUpColor: '#10b981', wickDownColor: '#ef4444',
      borderVisible: false,
    })
    candles.setData(res.candles.map((c) => ({
      time: (c.ts + IST_SHIFT) as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close,
    })))
    const markers: SeriesMarker<Time>[] = res.markers.map((m) => ({
      time: (m.time + IST_SHIFT) as UTCTimestamp,
      position: m.position, shape: m.shape, color: m.color, text: m.text,
      size: m.kind === 'roll' ? 2 : 1,
    }))
    candles.setMarkers(markers)

    const eqChart = createChart(eqEl, common)
    const eq = eqChart.addAreaSeries({
      lineColor: '#38bdf8', topColor: 'rgba(56,189,248,0.25)', bottomColor: 'rgba(56,189,248,0.02)',
      lineWidth: 2,
    })
    eq.setData(res.equity.map((p) => ({ time: (p.ts + IST_SHIFT) as UTCTimestamp, value: p.value })))
    const rsiSeries = eqChart.addLineSeries({ color: '#7c3aed', lineWidth: 1, priceScaleId: 'left' })
    rsiSeries.setData(res.rsi.map((p) => ({ time: (p.ts + IST_SHIFT) as UTCTimestamp, value: p.value })))
    eqChart.applyOptions({ leftPriceScale: { visible: true, borderColor: '#d1d5db' } })
    rsiSeries.createPriceLine({ price: 30, color: '#7c3aed', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: 'RSI 30' })

    // keep the two time scales in sync
    const sync = (from: ReturnType<typeof createChart>, to: ReturnType<typeof createChart>) =>
      from.timeScale().subscribeVisibleLogicalRangeChange((r) => { if (r) to.timeScale().setVisibleLogicalRange(r) })
    sync(priceChart, eqChart)
    sync(eqChart, priceChart)

    priceChart.timeScale().fitContent()
    eqChart.timeScale().fitContent()
    return () => { priceChart.remove(); eqChart.remove() }
  }, [res])

  return (
    <div className="h-full flex flex-col gap-2">
      <div className="text-[10px] text-gray-500 shrink-0">
        ▲ buys · ▼ target/SL exits (linked to their rung) · <span className="text-sky-600">↻ rollovers with basis</span> ·
        bottom pane: <span className="text-sky-600">equity ₹</span> + <span className="text-purple-600">RSI (left scale)</span>
      </div>
      <div ref={priceRef} className="flex-[3] min-h-0 border border-gray-200 rounded-lg overflow-hidden" />
      <div ref={eqRef} className="flex-[1.2] min-h-0 border border-gray-200 rounded-lg overflow-hidden" />
    </div>
  )
}

function BtTrades({ res }: { res: BtResult }) {
  const fmtT = (ts: number | null) => ts ? new Date((ts + IST_SHIFT) * 1000).toISOString().slice(0, 16).replace('T', ' ') : '—'
  return (
    <div className="h-full overflow-y-auto border border-gray-200 rounded-lg">
      <table className="w-full text-[12px]">
        <thead className="sticky top-0 bg-white">
          <tr className="text-gray-500 text-[10px] uppercase">
            <th className="text-left px-2 py-1.5">#</th>
            <th className="text-right px-2">Lots</th>
            <th className="text-right px-2">Buy at</th>
            <th className="text-right px-2">Target</th>
            <th className="text-right px-2">Stop</th>
            <th className="text-right px-2">Exit</th>
            <th className="text-left px-2">Reason</th>
            <th className="text-right px-2">P&L</th>
            <th className="text-right px-2">RSI@sig</th>
            <th className="text-right px-2">ATR@entry</th>
            <th className="text-left px-2">Entry time</th>
            <th className="text-left px-2">Rolls</th>
          </tr>
        </thead>
        <tbody>
          {res.trades.map((t) => {
            const pnl = t.status === 'CLOSED' ? t.realized_pnl : t.live_pnl
            return (
              <tr key={t.seq} className="border-t border-gray-200 hover:bg-gray-100">
                <td className="px-2 py-1 font-semibold text-gray-900">{t.label}
                  {t.status === 'OPEN' && <Badge tone="green">open</Badge>}</td>
                <td className="px-2 text-right font-mono">{t.lots}</td>
                <td className="px-2 text-right font-mono">{fmtPx(t.entry_price, 3)}
                  {t.roll_count > 0 && <div className="text-[9px] text-gray-500">raw {fmtPx(t.raw_entry_price, 3)}</div>}</td>
                <td className="px-2 text-right font-mono text-emerald-600">{fmtPx(t.target_price, 3)}</td>
                <td className="px-2 text-right font-mono text-red-600">{fmtPx(t.sl_price, 3)}</td>
                <td className="px-2 text-right font-mono">{t.exit_price ? fmtPx(t.exit_price, 3) : '—'}</td>
                <td className="px-2">{t.exit_reason || '—'}</td>
                <td className={`px-2 text-right font-mono font-semibold ${pnlClass(pnl)}`}>{pnl === null ? '—' : inr(pnl)}</td>
                <td className="px-2 text-right font-mono text-purple-700">{t.rsi_at_signal}</td>
                <td className="px-2 text-right font-mono text-gray-600">{t.atr_at_entry}</td>
                <td className="px-2 text-gray-600">{fmtT(t.entry_time)}</td>
                <td className="px-2 text-sky-600">{t.roll_count > 0 ? `↻${t.roll_count} (${t.total_basis >= 0 ? '+' : ''}${t.total_basis})` : '—'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
