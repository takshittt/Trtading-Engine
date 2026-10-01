import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import type { WatchlistItem, ScripSearchResult } from '../types'
import { formatINR } from '../utils/format'
import { refreshScripmaster } from '../api'
import { usePersistedState } from '../hooks/usePersistedState'

interface MarketWatchProps {
  watchlist: WatchlistItem[]
  watchSearch: string
  watchSuggestions: ScripSearchResult[]
  showWatchSuggestions: boolean
  watchLoading: boolean
  wsRef: RefObject<WebSocket | null>
  isCollapsed: boolean
  onCollapsedChange: (collapsed: boolean) => void
  onSearchChange: (val: string) => void
  onShowSuggestions: (show: boolean) => void
  onAddToWatch: (result: ScripSearchResult) => void
  onRefreshLtp: () => void
  onOpenChainModal: (item: WatchlistItem) => void
  onOpenOrderModal: (item: WatchlistItem, side: 'B' | 'S') => void
  onRemoveFromWatch: (item: WatchlistItem) => void
}

export default function MarketWatch({
  watchlist,
  watchSearch,
  watchSuggestions,
  showWatchSuggestions,
  watchLoading,
  wsRef,
  isCollapsed,
  onCollapsedChange,
  onSearchChange,
  onShowSuggestions,
  onAddToWatch,
  onRefreshLtp,
  onOpenChainModal,
  onOpenOrderModal,
  onRemoveFromWatch,
}: MarketWatchProps) {
  const [filterExch, setFilterExch] = usePersistedState('marketWatch.filterExch', 'ALL')
  const [smBusy, setSmBusy] = useState(false)
  const [smMsg, setSmMsg] = useState<string | null>(null)
  const searchBox = useRef<HTMLDivElement>(null)

  // Close the suggestions on any click outside the search box. More robust
  // than an input onBlur timeout, which races with clicks on the dropdown.
  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (searchBox.current && !searchBox.current.contains(e.target as Node))
        onShowSuggestions(false)
    }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [onShowSuggestions])

  const handleRefreshScripmaster = async () => {
    setSmBusy(true)
    setSmMsg(null)
    try {
      const r = await refreshScripmaster()
      setSmMsg(`✓ ${r.symbols_loaded.toLocaleString('en-IN')} contracts`)
    } catch (e) {
      setSmMsg(`✕ ${e instanceof Error ? e.message : 'Update failed'}`)
    } finally {
      setSmBusy(false)
      setTimeout(() => setSmMsg(null), 5000)
    }
  }

  const exchanges = ['ALL', ...Array.from(new Set(watchlist.map(i => i.exch))).sort()]
  const filteredList = filterExch === 'ALL' ? watchlist : watchlist.filter(i => i.exch === filterExch)

  if (isCollapsed) {
    return (
      <div className="h-full bg-gray-900 border border-gray-800 rounded-xl p-2 flex flex-col items-center gap-3">
        <button
          onClick={() => onCollapsedChange(false)}
          className="text-xs px-1.5 py-1 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer shrink-0"
          title="Expand"
        >
          ▶
        </button>
        <span
          className="text-xs font-semibold text-gray-500 uppercase tracking-widest select-none"
          style={{ writingMode: 'vertical-rl' }}
        >
          Market Watch
        </span>
      </div>
    )
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest">Market Watch</h2>
        <div className="flex items-center gap-2">
          {smMsg && (
            <span className={`text-xs ${smMsg.startsWith('✓') ? 'text-emerald-400' : 'text-red-400'}`}>
              {smMsg}
            </span>
          )}
          <button
            onClick={handleRefreshScripmaster}
            disabled={smBusy}
            className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            title="Update contracts list (scripmaster) — pulls today's expiries from the broker"
          >
            {smBusy ? '⟳ Updating…' : '⭳ Contracts'}
          </button>
          {watchlist.length > 0 && (
            <button
              onClick={onRefreshLtp}
              className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer"
              title="Refresh prices"
            >
              ↻
            </button>
          )}
          <button
            onClick={() => onCollapsedChange(true)}
            className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer"
            title="Collapse"
          >
            ◀
          </button>
        </div>
      </div>

      <div className="relative" ref={searchBox}>
            <input
          type="text"
          placeholder="Search symbol..."
          value={watchSearch}
          onChange={(e) => { onSearchChange(e.target.value.toUpperCase()); onShowSuggestions(true) }}
          onFocus={() => watchSearch.length >= 2 && onShowSuggestions(true)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500 font-mono"
            />
            {showWatchSuggestions && watchSearch.length >= 2 && (
              <div className="absolute z-50 left-0 right-0 mt-1 bg-gray-800 border border-gray-600 rounded-lg shadow-2xl max-h-64 overflow-y-auto">
                {watchLoading && (
                  <div className="px-3 py-2.5 text-sm text-gray-500">Searching…</div>
                )}
                {!watchLoading && watchSuggestions.length === 0 && (
                  <div className="px-3 py-2.5 text-sm text-gray-500">
                    No matches. Try a root name (RELIANCE, NIFTY, BANKNIFTY).
                  </div>
                )}
                {watchSuggestions.map(s => (
                  <button
                    key={`${s.exch}|${s.token}`}
                    type="button"
                    onMouseDown={() => onAddToWatch(s)}
                    className="w-full text-left px-3 py-2.5 hover:bg-gray-700 flex items-center justify-between gap-2"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="font-mono text-sm text-white truncate">{s.tsym}</span>
                      {s.sym && s.sym !== s.tsym && (
                        <span className="text-xs text-gray-500 truncate">{s.sym}</span>
                      )}
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      {s.expd && <span className="text-xs text-gray-400">{s.expd}</span>}
                      <span className="text-xs font-medium px-1.5 py-0.5 rounded bg-gray-700 text-gray-300">{s.exch}</span>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>

          {watchlist.length > 0 && exchanges.length > 2 && (
            <div className="flex gap-1.5 flex-wrap">
              {exchanges.map(exch => (
                <button
                  key={exch}
                  onClick={() => setFilterExch(exch)}
                  className={`px-2.5 py-0.5 text-xs rounded-full border transition-colors cursor-pointer ${
                    filterExch === exch
                      ? 'bg-blue-600 border-blue-500 text-white'
                      : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200'
                  }`}
                >
                  {exch}
                </button>
              ))}
            </div>
          )}

          {watchlist.length === 0 ? (
            <p className="text-sm text-gray-600 text-center py-3">Search and add symbols to watch</p>
          ) : filteredList.length === 0 ? (
            <p className="text-sm text-gray-600 text-center py-3">No symbols for {filterExch}</p>
          ) : (
            <div className="divide-y divide-gray-800/60">
              {filteredList.map(item => (
                <div key={item.token} className="flex items-center gap-3 py-3">
                  <div className="flex-1 min-w-0">
                    <div className="font-mono text-base text-white">{item.tsym}</div>
                    <div className="text-sm text-gray-500 mt-0.5">{item.exch} · {item.instrumenttype}</div>
                  </div>
                  <div className="flex items-center gap-4 mr-2">
                    <div className="text-center min-w-[56px]">
                      <div className={`font-mono text-base font-semibold ${item.bid ? 'text-emerald-400' : 'text-gray-600'}`}>
                        {item.bid ? `₹${formatINR(parseFloat(item.bid))}` : '—'}
                      </div>
                      <div className="text-sm text-gray-600">Bid</div>
                    </div>
                    <div className="text-center min-w-[56px]">
                      <div className={`font-mono text-base font-semibold ${item.ltp ? 'text-white' : 'text-gray-600'}`}>
                        {item.ltp ? `₹${formatINR(parseFloat(item.ltp))}` : '—'}
                      </div>
                      <div className="text-sm text-gray-600">LTP</div>
                    </div>
                    <div className="text-center min-w-[56px]">
                      <div className={`font-mono text-base font-semibold ${item.ask ? 'text-red-400' : 'text-gray-600'}`}>
                        {item.ask ? `₹${formatINR(parseFloat(item.ask))}` : '—'}
                      </div>
                      <div className="text-sm text-gray-600">Ask</div>
                    </div>
                    {(() => {
                      const ltp = parseFloat(item.ltp)
                      const prevClose = parseFloat(item.close)
                      if (!ltp || !prevClose) return (
                        <div className="text-center min-w-[56px]">
                          <div className="font-mono text-base font-semibold text-gray-600">—</div>
                          <div className="text-sm text-gray-600">Chg</div>
                        </div>
                      )
                      const chg = ltp - prevClose
                      const isPos = chg >= 0
                      return (
                        <div className="text-center min-w-[56px]">
                          <div className={`font-mono text-base font-semibold ${isPos ? 'text-emerald-400' : 'text-red-400'}`}>
                            {isPos ? '+' : ''}{chg.toFixed(2)}
                          </div>
                          <div className="text-sm text-gray-600">Chg</div>
                        </div>
                      )
                    })()}
                  </div>
                  <button
                    onClick={() => onOpenChainModal(item)}
                    className="px-3 py-1 text-xs font-bold rounded bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors cursor-pointer"
                    title="Option Chain"
                  >
                    D
                  </button>
                  <button
                    onClick={() => onOpenOrderModal(item, 'B')}
                    className="px-3 py-1 text-xs font-bold rounded bg-green-700 hover:bg-green-600 text-white transition-colors cursor-pointer"
                  >
                    B
                  </button>
                  <button
                    onClick={() => onOpenOrderModal(item, 'S')}
                    className="px-3 py-1 text-xs font-bold rounded bg-red-700 hover:bg-red-600 text-white transition-colors cursor-pointer"
                  >
                    S
                  </button>
                  <button
                    onClick={() => {
                      if (wsRef.current?.readyState === WebSocket.OPEN)
                        wsRef.current.send(JSON.stringify({ action: 'unsubscribe', symbols: [`${item.exch}|${item.token}`] }))
                      onRemoveFromWatch(item)
                    }}
                    className="text-gray-600 hover:text-gray-400 text-sm leading-none px-1 cursor-pointer"
                    title="Remove"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
    </div>
  )
}
