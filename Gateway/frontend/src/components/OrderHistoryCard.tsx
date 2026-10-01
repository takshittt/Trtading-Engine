import { useMemo, useState } from 'react'
import type { OrderLot } from '../types'
import SkeletonLine from './SkeletonLine'
import { formatINR, formatPnl, serviceBadge } from '../utils/format'
import { usePersistedState } from '../hooks/usePersistedState'

interface OrderHistoryCardProps {
  history: OrderLot[]
  loading: boolean
  hasMore: boolean
  onLoadMore: () => void
  onRefresh: () => void
  onClearCancelled: () => Promise<void>
  onDeleteLot: (lotId: number) => Promise<void>
}

function formatDateTime(iso: string) {
  if (!iso) return null
  const d = new Date(iso + 'Z')
  const date = d.toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: '2-digit' })
  const time = d.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })
  return { date, time }
}

export default function OrderHistoryCard({ history, loading, hasMore, onLoadMore, onRefresh, onClearCancelled, onDeleteLot }: OrderHistoryCardProps) {
  const [collapsed, setCollapsed] = usePersistedState('orderHistory.collapsed', true)
  const [symbolQuery, setSymbolQuery] = usePersistedState('orderHistory.symbolQuery', '')
  const [exchFilter, setExchFilter] = usePersistedState<string | null>('orderHistory.exchFilter', null)
  const [sideFilter, setSideFilter] = usePersistedState<'B' | 'S' | null>('orderHistory.sideFilter', null)
  const [statusFilter, setStatusFilter] = usePersistedState<string | null>('orderHistory.statusFilter', null)
  const [hideOpen, setHideOpen] = usePersistedState('orderHistory.hideOpen', false)
  const [clearing, setClearing] = useState(false)
  const [deletingId, setDeletingId] = useState<number | null>(null)

  const exchanges = useMemo(() => Array.from(new Set(history.map(l => l.exch))).sort(), [history])
  const statuses = useMemo(() => Array.from(new Set(history.map(l => l.status))).sort(), [history])
  const cancelledCount = useMemo(() => history.filter(l => l.status === 'CANCELLED').length, [history])

  const handleClearCancelled = async () => {
    if (!window.confirm(`Permanently delete ${cancelledCount} cancelled order(s) from history?`)) return
    setClearing(true)
    try {
      await onClearCancelled()
    } finally {
      setClearing(false)
    }
  }

  const filteredHistory = useMemo(() => {
    const q = symbolQuery.trim().toUpperCase()
    return history
      .filter(l => !q || l.tsym.toUpperCase().includes(q))
      .filter(l => !exchFilter || l.exch === exchFilter)
      .filter(l => !sideFilter || l.side === sideFilter)
      .filter(l => !statusFilter || l.status === statusFilter)
      .filter(l => !hideOpen || l.status !== 'OPEN')
  }, [history, symbolQuery, exchFilter, sideFilter, statusFilter, hideOpen])

  const totalPnl = useMemo(
    () => filteredHistory.reduce((sum, l) => sum + l.realized_pnl, 0),
    [filteredHistory]
  )

  const handleDeleteLot = async (lot: OrderLot) => {
    if (!window.confirm(`Permanently delete this record from history?\n\n${lot.side === 'B' ? 'BUY' : 'SELL'} ${lot.tsym} (${lot.status})`)) return
    setDeletingId(lot.id)
    try {
      await onDeleteLot(lot.id)
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-5">
      <div className="flex items-center justify-between">
        <button
          onClick={() => setCollapsed(c => !c)}
          className="flex items-center gap-2 text-xs font-semibold text-gray-500 uppercase tracking-widest hover:text-gray-300 transition-colors cursor-pointer"
        >
          <span className="text-gray-600 text-[10px]">{collapsed ? '▶' : '▼'}</span>
          Order History (all time)
        </button>
        <div className="flex items-center gap-2">
          {cancelledCount > 0 && (
            <button
              onClick={handleClearCancelled}
              disabled={clearing}
              className="text-xs px-2 py-1 rounded border border-red-900 text-red-400 hover:border-red-700 hover:text-red-300 disabled:opacity-50 transition-colors cursor-pointer"
            >
              {clearing ? 'Clearing…' : `Clear cancelled (${cancelledCount})`}
            </button>
          )}
          <button
            onClick={onRefresh}
            disabled={loading}
            className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 disabled:opacity-50 transition-colors cursor-pointer"
          >
            ↻
          </button>
        </div>
      </div>

      {!collapsed && loading && !history.length ? (
        <div className="space-y-3">
          {[1, 2, 3].map(i => (
            <SkeletonLine key={i} width={300} />
          ))}
        </div>
      ) : !collapsed && history.length > 0 ? (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={symbolQuery}
              onChange={e => setSymbolQuery(e.target.value)}
              placeholder="Search symbol…"
              className="text-xs px-2 py-1 rounded border border-gray-700 bg-gray-800/60 text-gray-300 placeholder-gray-600 focus:outline-none focus:border-gray-500 w-40"
            />

            {exchanges.length > 1 && (
              <div className="flex items-center gap-1.5">
                <span className="text-xs text-gray-600">Exch</span>
                <button
                  onClick={() => setExchFilter(null)}
                  className={`text-xs px-2 py-0.5 rounded border transition-colors cursor-pointer ${
                    exchFilter === null
                      ? 'border-gray-500 bg-gray-700 text-gray-200'
                      : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-400'
                  }`}
                >
                  All
                </button>
                {exchanges.map(exch => (
                  <button
                    key={exch}
                    onClick={() => setExchFilter(exchFilter === exch ? null : exch)}
                    className={`text-xs px-2 py-0.5 rounded border transition-colors cursor-pointer font-mono ${
                      exchFilter === exch
                        ? 'border-blue-500 bg-blue-600/20 text-blue-300'
                        : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-400'
                    }`}
                  >
                    {exch}
                  </button>
                ))}
              </div>
            )}

            <div className="flex items-center gap-1.5">
              <span className="text-xs text-gray-600">Side</span>
              {([null, 'B', 'S'] as const).map(side => (
                <button
                  key={side ?? 'all'}
                  onClick={() => setSideFilter(side)}
                  className={`text-xs px-2 py-0.5 rounded border transition-colors cursor-pointer ${
                    sideFilter === side
                      ? 'border-gray-500 bg-gray-700 text-gray-200'
                      : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-400'
                  }`}
                >
                  {side === null ? 'All' : side === 'B' ? 'Buy' : 'Sell'}
                </button>
              ))}
            </div>

            <div className="flex items-center gap-1.5">
              <span className="text-xs text-gray-600">Status</span>
              <button
                onClick={() => setStatusFilter(null)}
                className={`text-xs px-2 py-0.5 rounded border transition-colors cursor-pointer ${
                  statusFilter === null
                    ? 'border-gray-500 bg-gray-700 text-gray-200'
                    : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-400'
                }`}
              >
                All
              </button>
              {statuses.map(status => (
                <button
                  key={status}
                  onClick={() => setStatusFilter(statusFilter === status ? null : status)}
                  className={`text-xs px-2 py-0.5 rounded border transition-colors cursor-pointer ${
                    statusFilter === status
                      ? 'border-blue-500 bg-blue-600/20 text-blue-300'
                      : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-400'
                  }`}
                >
                  {status}
                </button>
              ))}
            </div>

            <label className="flex items-center gap-1.5 text-xs text-gray-500 cursor-pointer">
              <input
                type="checkbox"
                checked={hideOpen}
                onChange={e => setHideOpen(e.target.checked)}
                className="cursor-pointer"
              />
              Hide open
            </label>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-base">
              <thead>
                <tr className="text-left text-sm text-gray-600 border-b border-gray-800">
                  <th className="pb-2 pr-4 font-medium">Symbol</th>
                  <th className="pb-2 pr-4 font-medium">Exch</th>
                  <th className="pb-2 pr-4 font-medium">Side</th>
                  <th className="pb-2 pr-4 font-medium text-right">Qty</th>
                  <th className="pb-2 pr-4 font-medium text-right">Entry</th>
                  <th className="pb-2 pr-4 font-medium text-right">Exit</th>
                  <th className="pb-2 pr-4 font-medium text-right">P&amp;L</th>
                  <th className="pb-2 pr-4 font-medium">Status</th>
                  <th className="pb-2 pr-4 font-medium">Opened</th>
                  <th className="pb-2 pr-4 font-medium">Closed</th>
                  <th className="pb-2 font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {filteredHistory.length === 0 && (
                  <tr>
                    <td colSpan={11} className="py-4 text-center text-gray-600">
                      No orders match these filters.
                    </td>
                  </tr>
                )}
                {filteredHistory.map(lot => {
                  const opened = formatDateTime(lot.opened_at)
                  const closed = formatDateTime(lot.closed_at)
                  return (
                    <tr key={lot.id} className="border-b border-gray-800/40">
                      <td className="py-2 pr-4">
                        <div className="flex items-center gap-1.5">
                          <span className="font-mono text-sm text-gray-300">{lot.tsym}</span>
                          {lot.is_external && (
                            <span className="text-xs px-1.5 py-0.5 rounded border bg-amber-950 text-amber-300 border-amber-800 font-mono">
                              EXT
                            </span>
                          )}
                          {lot.is_persistent && (
                            <span className="text-xs px-1.5 py-0.5 rounded border bg-violet-950 text-violet-300 border-violet-800 font-mono">
                              GTT
                            </span>
                          )}
                          {serviceBadge(lot.source_service) && (
                            <span className="text-xs px-1.5 py-0.5 rounded border bg-sky-950 text-sky-300 border-sky-800 font-mono" title={`Placed by the ${lot.source_service} service`}>
                              {serviceBadge(lot.source_service)}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="py-2 pr-4 font-mono text-xs text-gray-500">{lot.exch}</td>
                      <td className="py-2 pr-4">
                        <span className={`text-xs px-2 py-0.5 rounded ${lot.side === 'B' ? 'bg-blue-950 text-blue-300' : 'bg-red-950 text-red-300'}`}>
                          {lot.side === 'B' ? 'BUY' : 'SELL'}
                        </span>
                      </td>
                      <td className="py-2 pr-4 text-right font-mono text-gray-300">{lot.entry_qty}</td>
                      <td className="py-2 pr-4 text-right font-mono text-gray-400">
                        ₹{formatINR(lot.avg_entry_price)}
                      </td>
                      <td className="py-2 pr-4 text-right font-mono text-gray-400">
                        {lot.avg_exit_price && lot.avg_exit_price > 0 ? (
                          <span>₹{formatINR(lot.avg_exit_price)}</span>
                        ) : (
                          <span className="text-gray-600">—</span>
                        )}
                      </td>
                      <td className="py-2 pr-4 text-right font-mono font-semibold">
                        <span className={lot.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                          {formatPnl(lot.realized_pnl)}
                        </span>
                      </td>
                      <td className="py-2 pr-4">
                        <span className={`text-xs px-2 py-0.5 rounded ${
                          lot.status === 'CLOSED' ? 'bg-gray-800 text-gray-400' :
                          lot.status === 'OPEN' ? 'bg-green-950 text-green-300' :
                          lot.status === 'PARTIAL' ? 'bg-yellow-950 text-yellow-300' :
                          lot.status === 'PENDING' ? 'bg-blue-950 text-blue-300' :
                          'bg-red-950 text-red-300'
                        }`}>
                          {lot.status}
                        </span>
                      </td>
                      <td className="py-2 pr-4 font-mono text-xs text-gray-500">
                        {opened ? (
                          <div>
                            <div className="text-gray-600">{opened.date}</div>
                            <div>{opened.time}</div>
                          </div>
                        ) : ''}
                      </td>
                      <td className="py-2 pr-4 font-mono text-xs text-gray-500">
                        {closed ? (
                          <div>
                            <div className="text-gray-600">{closed.date}</div>
                            <div>{closed.time}</div>
                          </div>
                        ) : <span className="text-gray-700">—</span>}
                      </td>
                      <td className="py-2 text-right">
                        <button
                          onClick={() => handleDeleteLot(lot)}
                          disabled={deletingId === lot.id}
                          title="Delete this record"
                          className="text-xs px-1.5 py-0.5 rounded border border-gray-700 text-gray-500 hover:border-red-700 hover:text-red-400 disabled:opacity-50 transition-colors cursor-pointer"
                        >
                          {deletingId === lot.id ? '…' : '✕'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
              {filteredHistory.length > 0 && (
                <tfoot>
                  <tr className="border-t border-gray-700">
                    <td colSpan={6} className="pt-2 pr-4 text-right text-xs text-gray-500 font-medium">
                      Total P&amp;L{filteredHistory.length !== history.length ? ' (filtered)' : ''}
                    </td>
                    <td className="pt-2 pr-4 text-right font-mono font-semibold">
                      <span className={totalPnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                        {formatPnl(totalPnl)}
                      </span>
                    </td>
                    <td colSpan={4} />
                  </tr>
                </tfoot>
              )}
            </table>
          </div>
          {hasMore && (
            <div className="flex justify-center">
              <button
                onClick={onLoadMore}
                disabled={loading}
                className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 disabled:opacity-50 transition-colors cursor-pointer"
              >
                {loading ? 'Loading…' : 'Load more'}
              </button>
            </div>
          )}
        </>
      ) : !collapsed ? (
        <p className="text-sm text-gray-600">No order history yet.</p>
      ) : null}
    </div>
  )
}
