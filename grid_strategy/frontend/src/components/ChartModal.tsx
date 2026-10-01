import { useEffect, useRef, useState } from 'react'
import {
  createChart, ColorType, CrosshairMode,
  type IChartApi, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from 'lightweight-charts'
import { api } from '../api'
import type { ChartPayload, InstrumentSnap } from '../types'
import { Spinner } from './ui'

const TIMEFRAMES = [1, 3, 5, 15, 30, 60, 240]
// lightweight-charts renders epoch times as UTC — shift both candles and
// markers by IST offset so the axis reads Indian market time
const IST_SHIFT = 19800

/** Candlestick chart with the engine's decisions plotted on it:
 *  ▲ buy 1 (2 lots) markers at each entry, ▼ target 1 (buy 1 closed) at each
 *  exit, plus live target/SL price lines for open rungs. */
export function ChartModal({ inst, focusLot, onClose }: {
  inst: InstrumentSnap
  focusLot?: number
  onClose: () => void
}) {
  const wrap = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const [tf, setTf] = useState(15)
  const [data, setData] = useState<ChartPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setErr('')
    api.chart(inst.id, tf)
      .then((d) => { if (!cancelled) setData(d) })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : 'chart failed') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [inst.id, tf])

  useEffect(() => {
    if (!wrap.current || !data) return
    const el = wrap.current
    el.innerHTML = ''
    const chart = createChart(el, {
      layout: { background: { type: ColorType.Solid, color: '#ffffff' }, textColor: '#4b5563' },
      grid: { vertLines: { color: '#eef1f5' }, horzLines: { color: '#eef1f5' } },
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#d1d5db' },
      rightPriceScale: { borderColor: '#d1d5db' },
      autoSize: true,
    })
    chartRef.current = chart
    const series = chart.addCandlestickSeries({
      upColor: '#10b981', downColor: '#ef4444',
      wickUpColor: '#10b981', wickDownColor: '#ef4444', borderVisible: false,
    })
    seriesRef.current = series
    series.setData(data.candles.map((c) => ({
      time: (c.ts + IST_SHIFT) as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close,
    })))

    // snap markers onto existing bars so they always render
    const firstBar = data.candles.length ? data.candles[0].ts : 0
    const markers: SeriesMarker<Time>[] = data.markers
      .filter((m) => m.time >= firstBar)
      .map((m) => ({
        time: (m.time + IST_SHIFT) as UTCTimestamp,
        position: m.position, shape: m.shape,
        color: focusLot && m.lot_id === focusLot ? '#38bdf8' : m.color,
        text: m.text,
        size: focusLot && m.lot_id === focusLot ? 2 : 1,
      }))
    series.setMarkers(markers)

    // live target / SL lines for open rungs (dashed)
    for (const lvl of data.open_levels) {
      const hl = focusLot ? lvl.lot_id === focusLot : true
      if (lvl.target > 0) series.createPriceLine({
        price: lvl.target, color: '#f59e0b', lineWidth: hl ? 2 : 1, lineStyle: 2,
        axisLabelVisible: hl, title: `T${lvl.seq}`,
      })
      if (lvl.sl > 0) series.createPriceLine({
        price: lvl.sl, color: '#ef4444', lineWidth: hl ? 2 : 1, lineStyle: 2,
        axisLabelVisible: hl, title: `SL${lvl.seq}`,
      })
      series.createPriceLine({
        price: lvl.entry, color: '#10b981', lineWidth: 1, lineStyle: 3,
        axisLabelVisible: false, title: `B${lvl.seq}`,
      })
    }

    chart.timeScale().fitContent()
    return () => { chart.remove(); chartRef.current = null }
  }, [data, focusLot])

  return (
    <div className="fixed inset-0 z-50 bg-gray-900/50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white border border-gray-300 rounded-2xl w-full max-w-5xl h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}>
        <header className="flex items-center justify-between px-5 py-3 border-b border-gray-200">
          <div>
            <div className="font-semibold text-gray-900">
              {inst.sym} <span className="text-gray-500 font-normal">· {data?.tsym ?? inst.tsym} · strategy markers</span>
            </div>
            <div className="text-[11px] text-gray-500">
              ▲ entries with lot counts · ▼ exits linked to their rung · dashed lines = live targets/stops
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex rounded-lg overflow-hidden border border-gray-300">
              {TIMEFRAMES.map((t) => (
                <button key={t} onClick={() => setTf(t)}
                  className={`px-2 py-1 text-[11px] font-semibold ${tf === t ? 'bg-sky-500/20 text-sky-700' : 'text-gray-600 hover:text-gray-800'}`}>
                  {t < 60 ? `${t}m` : `${t / 60}h`}
                </button>
              ))}
            </div>
            <button onClick={onClose} className="text-gray-500 hover:text-gray-800 text-2xl leading-none px-1">×</button>
          </div>
        </header>
        <div className="flex-1 min-h-0 relative">
          {loading && (
            <div className="absolute inset-0 flex items-center justify-center gap-2 text-gray-500 text-sm">
              <Spinner /> loading candles…
            </div>
          )}
          {err && <div className="absolute inset-0 flex items-center justify-center text-red-600 text-sm">{err}</div>}
          <div ref={wrap} className="absolute inset-0" />
        </div>
      </div>
    </div>
  )
}
