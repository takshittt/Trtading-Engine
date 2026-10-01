import { useEffect, useRef, useState } from 'react'
import { getToken, wsUrl, getSubscribedSymbols, unsubscribeAllSymbols, type SubscribedSymbol } from '../api'

interface SubscribedTokensModalProps {
  show: boolean
  onClose: () => void
}

interface TickState {
  lp: string
  bp1: string
  sp1: string
}

export default function SubscribedTokensModal({ show, onClose }: SubscribedTokensModalProps) {
  const [symbols, setSymbols] = useState<SubscribedSymbol[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [ticks, setTicks] = useState<Record<string, TickState>>({})
  const [search, setSearch] = useState('')
  const [confirmingUnsub, setConfirmingUnsub] = useState(false)
  const [unsubscribing, setUnsubscribing] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const ticksBufferRef = useRef<Record<string, TickState>>({})
  const flushTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    if (!show) return

    setLoading(true)
    setError(null)
    setTicks({})
    setSearch('')
    setConfirmingUnsub(false)
    ticksBufferRef.current = {}

    getSubscribedSymbols()
      .then(({ symbols }) => {
        setSymbols(symbols)
        // Seed from the gateway's own cache so rows show a price immediately
        // instead of a blank cell until the first fresh tick arrives.
        const seeded: Record<string, TickState> = {}
        for (const s of symbols) {
          if (s.lp || s.bp1 || s.sp1) seeded[`${s.exch}|${s.token}`] = { lp: s.lp, bp1: s.bp1, sp1: s.sp1 }
        }
        ticksBufferRef.current = { ...ticksBufferRef.current, ...seeded }
        setTicks(prev => ({ ...prev, ...seeded }))
      })
      .catch(e => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))

    const wsToken = getToken()
    const ws = new WebSocket(wsUrl(`/api/ws/ticker?token=${encodeURIComponent(wsToken ?? '')}`))
    wsRef.current = ws

    ws.onmessage = (event) => {
      const tick = JSON.parse(event.data)
      if (!tick.e || !tick.tk) return
      const key = `${tick.e}|${tick.tk}`
      const cur = ticksBufferRef.current[key] || { lp: '', bp1: '', sp1: '' }
      ticksBufferRef.current[key] = {
        lp: tick.lp || cur.lp,
        bp1: tick.bp1 || cur.bp1,
        sp1: tick.sp1 || cur.sp1,
      }
    }

    // Flush the tick buffer into render state once a second — ticks can arrive
    // far faster than that; this is what gives the table its 1s update cadence
    // without re-rendering on every single frame.
    flushTimerRef.current = setInterval(() => {
      setTicks({ ...ticksBufferRef.current })
    }, 1000)

    return () => {
      if (flushTimerRef.current) clearInterval(flushTimerRef.current)
      ws.close()
      wsRef.current = null
    }
  }, [show])

  if (!show) return null

  const handleUnsubscribeAll = async () => {
    setUnsubscribing(true)
    setError(null)
    try {
      await unsubscribeAllSymbols()
      setSymbols([])
      setTicks({})
      ticksBufferRef.current = {}
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setUnsubscribing(false)
      setConfirmingUnsub(false)
    }
  }

  const filteredSymbols = search.trim()
    ? symbols.filter(s => (s.tsym || '').toLowerCase().includes(search.trim().toLowerCase()))
    : symbols

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full max-w-2xl mx-4 rounded-xl overflow-hidden shadow-2xl border border-gray-700 bg-gray-900 max-h-[90vh] flex flex-col">
        <div className="px-5 py-3 border-b border-gray-800 flex items-center justify-between shrink-0">
          <div>
            <span className="font-bold text-white">Subscribed Tokens</span>
            <span className="ml-2 text-xs text-gray-500">
              · {search.trim() ? `${filteredSymbols.length} of ${symbols.length}` : symbols.length} live on the broker feed
            </span>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-xl leading-none cursor-pointer">×</button>
        </div>

        {!loading && !error && symbols.length > 0 && (
          <div className="px-5 py-2 border-b border-gray-800 shrink-0 flex items-center gap-2">
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search by symbol…"
              className="flex-1 px-3 py-1.5 rounded-md bg-gray-800 border border-gray-700 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-gray-500"
              autoFocus
            />
            {confirmingUnsub ? (
              <>
                <span className="text-xs text-gray-400">Unsubscribe all {symbols.length}?</span>
                <button
                  onClick={handleUnsubscribeAll}
                  disabled={unsubscribing}
                  className="px-3 py-1.5 rounded-md bg-red-900/60 hover:bg-red-900 border border-red-800 text-xs text-red-200 disabled:opacity-50 cursor-pointer whitespace-nowrap"
                >
                  {unsubscribing ? 'Unsubscribing…' : 'Confirm'}
                </button>
                <button
                  onClick={() => setConfirmingUnsub(false)}
                  disabled={unsubscribing}
                  className="px-3 py-1.5 rounded-md bg-gray-800 hover:bg-gray-700 border border-gray-700 text-xs text-gray-300 disabled:opacity-50 cursor-pointer"
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                onClick={() => setConfirmingUnsub(true)}
                className="px-3 py-1.5 rounded-md bg-gray-800 hover:bg-red-900/40 border border-gray-700 hover:border-red-800 text-xs text-gray-300 hover:text-red-200 cursor-pointer whitespace-nowrap"
                title="Unsubscribe every token from the broker feed. Anything still in use (open positions, active lots/targets) re-subscribes automatically."
              >
                Unsubscribe All
              </button>
            )}
          </div>
        )}

        <div className="overflow-y-auto flex-1">
          {loading ? (
            <p className="text-sm text-gray-500 text-center py-8">Loading…</p>
          ) : error ? (
            <p className="text-sm text-red-400 text-center py-8">{error}</p>
          ) : symbols.length === 0 ? (
            <p className="text-sm text-gray-500 text-center py-8">No tokens subscribed</p>
          ) : filteredSymbols.length === 0 ? (
            <p className="text-sm text-gray-500 text-center py-8">No symbols match "{search}"</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-gray-900">
                <tr className="text-left text-xs text-gray-600 border-b border-gray-800">
                  <th className="px-5 py-2 font-medium">Symbol</th>
                  <th className="px-2 py-2 font-medium">Exch</th>
                  <th className="px-2 py-2 font-medium text-right">LTP</th>
                  <th className="px-2 py-2 font-medium text-right">Bid</th>
                  <th className="px-5 py-2 font-medium text-right">Ask</th>
                </tr>
              </thead>
              <tbody>
                {filteredSymbols.map(s => {
                  const key = `${s.exch}|${s.token}`
                  const t = ticks[key]
                  return (
                    <tr key={key} className="border-b border-gray-800/40">
                      <td className="px-5 py-1.5 font-mono text-gray-300">{s.tsym || `token ${s.token}`}</td>
                      <td className="px-2 py-1.5 text-xs text-gray-500">{s.exch}</td>
                      <td className="px-2 py-1.5 text-right font-mono text-gray-200">{t?.lp || '—'}</td>
                      <td className="px-2 py-1.5 text-right font-mono text-gray-400">{t?.bp1 || '—'}</td>
                      <td className="px-5 py-1.5 text-right font-mono text-gray-400">{t?.sp1 || '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}
