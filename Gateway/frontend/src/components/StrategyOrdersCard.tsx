import { useState } from 'react'
import type { OrderLot } from '../types'
import SkeletonLine from './SkeletonLine'
import { formatPnl as fmtPnl, formatINR, serviceBadge } from '../utils/format'
import { usePersistedState } from '../hooks/usePersistedState'

const _EXPIRY_MONTHS: Record<string, number> = {
  JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5,
  JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11,
}

// Scripmaster expiry format is "31-JUL-2026"; returns whole days from today
// (0 = expires today), or null for non-derivative lots (no expiry).
function daysToExpiry(expiry: string | undefined): number | null {
  if (!expiry) return null
  const m = expiry.match(/^(\d{1,2})-([A-Za-z]{3})-(\d{4})$/)
  if (!m) return null
  const month = _EXPIRY_MONTHS[m[2].toUpperCase()]
  if (month === undefined) return null
  const expiryDate = new Date(Number(m[3]), month, Number(m[1]))
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  return Math.round((expiryDate.getTime() - startOfToday.getTime()) / 86400000)
}

type LotSortKey = 'symbol' | 'strategy' | 'expiry' | 'qty' | 'entry' | 'ltp' | 'pnl' | 'mtm' | 'status' | 'opened'

function lotSortValue(lot: OrderLot, key: LotSortKey): string | number {
  switch (key) {
    case 'symbol': return lot.tsym
    case 'strategy': return lot.source_service || lot.strategy_name || ''
    case 'expiry': { const d = daysToExpiry(lot.expd); return d === null ? Number.POSITIVE_INFINITY : d }
    case 'qty': return lot.open_qty
    case 'entry': return lot.avg_entry_price
    case 'ltp': return lot.ltp || 0
    case 'pnl': return lot.live_pnl
    case 'mtm': return lot.mtm_pnl || 0
    case 'status': return lot.status
    case 'opened': return lot.opened_at
  }
}

function SortTh({ label, sortKey, active, dir, onSort, align = 'left' }: {
  label: string
  sortKey: LotSortKey
  active: LotSortKey | null
  dir: 'asc' | 'desc'
  onSort: (key: LotSortKey) => void
  align?: 'left' | 'right'
}) {
  return (
    <th className={`pb-2 pr-4 font-medium ${align === 'right' ? 'text-right' : ''}`}>
      <button
        onClick={() => onSort(sortKey)}
        className={`flex items-center gap-1 hover:text-gray-300 transition-colors cursor-pointer ${align === 'right' ? 'ml-auto' : ''}`}
      >
        {label}
        <span className="w-3 inline-block text-center">
          {active === sortKey ? (dir === 'desc' ? '↓' : '↑') : <span className="text-gray-700">↕</span>}
        </span>
      </button>
    </th>
  )
}

interface StrategyOrdersCardProps {
  lots: OrderLot[]
  loadingOrders: boolean
  onFetchOrders: () => void
  savingLotStrategy: number | null
  onUpdateLotStrategy: (lotId: number, strategyName: string) => void
  strategyNames: string[]
}

// Read-only monitor for lots placed by strategy services (GR / RV / …) and
// for manually-placed lots the user has tagged with a strategy name — trading
// controls (Exit / Re-entry / Rollover / target) live only on the Orders
// card. The strategy-name tag itself stays editable here so a manually
// tagged lot can be renamed or untagged (which moves it back to Orders).
export default function StrategyOrdersCard({ lots, loadingOrders, onFetchOrders, savingLotStrategy, onUpdateLotStrategy, strategyNames }: StrategyOrdersCardProps) {
  const [strategyModal, setStrategyModal] = useState<{ lot: OrderLot; value: string } | null>(null)
  const [collapsed, setCollapsed] = usePersistedState('strategyOrders.collapsed', false)
  const [hideClosedLots, setHideClosedLots] = usePersistedState('strategyOrders.hideClosedLots', true)
  const [symbolQuery, setSymbolQuery] = usePersistedState('strategyOrders.symbolQuery', '')
  const [sourceFilter, setSourceFilter] = usePersistedState<string | null>('strategyOrders.sourceFilter', null)
  const [sortKey, setSortKey] = usePersistedState<LotSortKey | null>('strategyOrders.sortKey', null)
  const [sortDir, setSortDir] = usePersistedState<'asc' | 'desc'>('strategyOrders.sortDir', 'desc')

  const handleSort = (key: LotSortKey) => {
    if (sortKey !== key) { setSortKey(key); setSortDir('desc'); return }
    if (sortDir === 'desc') { setSortDir('asc'); return }
    setSortKey(null)
  }

  const sources = Array.from(new Set(lots.map(l => l.source_service).filter(Boolean) as string[])).sort()

  const filteredLots = lots
    .filter(l => !symbolQuery.trim() || l.tsym.toUpperCase().includes(symbolQuery.trim().toUpperCase()))
    .filter(l => !hideClosedLots || l.status !== 'CLOSED')
    .filter(l => !sourceFilter || l.source_service === sourceFilter)
    .sort((a, b) => {
      if (sortKey === null) return 0
      const av = lotSortValue(a, sortKey)
      const bv = lotSortValue(b, sortKey)
      const cmp = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv))
      return sortDir === 'desc' ? -cmp : cmp
    })

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button
            onClick={() => setCollapsed(v => !v)}
            className="flex items-center gap-2 text-xs font-semibold text-gray-500 uppercase tracking-widest hover:text-gray-300 transition-colors cursor-pointer"
            aria-expanded={!collapsed}
          >
            <span className="text-gray-600 text-[10px]">{collapsed ? '▶' : '▼'}</span>
            Orders from Strategies
          </button>
          <span className="text-xs px-1.5 py-0.5 rounded border bg-sky-950 text-sky-300 border-sky-800" title="Placed by strategy services — read-only here; manage them from their engine.">
            read-only
          </span>
        </div>
        <div className="flex items-center gap-2">
          {!collapsed && (
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-gray-500">Hide closed</span>
            <button
              role="switch"
              aria-checked={hideClosedLots}
              onClick={() => setHideClosedLots(v => !v)}
              className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors cursor-pointer focus:outline-none ${
                hideClosedLots ? 'bg-amber-500' : 'bg-gray-700'
              }`}
            >
              <span className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform ${hideClosedLots ? 'translate-x-4' : 'translate-x-0.5'}`} />
            </button>
          </div>
          )}
          <button
            onClick={onFetchOrders}
            disabled={loadingOrders}
            className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 disabled:opacity-50 transition-colors cursor-pointer"
          >
            ↻
          </button>
        </div>
      </div>

      {!collapsed && lots.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={symbolQuery}
            onChange={e => setSymbolQuery(e.target.value)}
            placeholder="Search symbol…"
            className="text-xs px-2 py-1 rounded border border-gray-700 bg-gray-800/60 text-gray-300 placeholder-gray-600 focus:outline-none focus:border-gray-500 w-40"
          />

          {sources.length > 1 && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-gray-600">Strategy</span>
              <button
                onClick={() => setSourceFilter(null)}
                className={`text-xs px-2 py-0.5 rounded border transition-colors cursor-pointer ${
                  sourceFilter === null
                    ? 'border-gray-500 bg-gray-700 text-gray-200'
                    : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-400'
                }`}
              >
                All
              </button>
              {sources.map(src => (
                <button
                  key={src}
                  onClick={() => setSourceFilter(sourceFilter === src ? null : src)}
                  className={`text-xs px-2 py-0.5 rounded border transition-colors cursor-pointer font-mono ${
                    sourceFilter === src
                      ? 'border-sky-500 bg-sky-600/20 text-sky-300'
                      : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-400'
                  }`}
                >
                  {serviceBadge(src)}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {collapsed ? null : loadingOrders && !lots.length ? (
        <div className="space-y-3">
          {[1, 2].map(i => (
            <SkeletonLine key={i} width={300} />
          ))}
        </div>
      ) : lots.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-base">
            <thead>
              <tr className="text-left text-sm text-gray-600 border-b border-gray-800">
                <SortTh label="Symbol" sortKey="symbol" active={sortKey} dir={sortDir} onSort={handleSort} />
                <SortTh label="Strategy" sortKey="strategy" active={sortKey} dir={sortDir} onSort={handleSort} />
                <SortTh label="Expiry" sortKey="expiry" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="Qty (open/entry)" sortKey="qty" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="Entry" sortKey="entry" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="LTP" sortKey="ltp" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="MTM P&L" sortKey="mtm" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="Live P&L" sortKey="pnl" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="Status" sortKey="status" active={sortKey} dir={sortDir} onSort={handleSort} />
                <SortTh label="Opened" sortKey="opened" active={sortKey} dir={sortDir} onSort={handleSort} />
              </tr>
            </thead>
            <tbody>
              {filteredLots.length > 0 ? filteredLots.map((lot) => {
                const isPending = lot.status === 'PENDING'
                return (
                  <tr key={lot.id} className="border-b border-gray-800/40">
                    <td className="py-2 pr-4">
                      <div className="flex items-center gap-1.5">
                        <span className="font-mono text-sm text-gray-300">{lot.tsym}</span>
                      </div>
                      {lot.description && (
                        <div className="text-xs text-gray-500 mt-0.5 max-w-[240px] truncate" title={lot.description}>
                          {lot.description}
                        </div>
                      )}
                    </td>
                    <td className="py-2 pr-4">
                      <div className="flex items-center gap-1.5">
                        {serviceBadge(lot.source_service) ? (
                          <span className="text-xs px-1.5 py-0.5 rounded border bg-sky-950 text-sky-300 border-sky-800 font-mono" title={`Placed by the ${lot.source_service} service`}>
                            {serviceBadge(lot.source_service)}
                          </span>
                        ) : lot.strategy_name ? (
                          <span className="text-xs text-gray-300 max-w-[120px] truncate" title={lot.strategy_name}>
                            {lot.strategy_name}
                          </span>
                        ) : (
                          <span className="text-xs text-gray-600">—</span>
                        )}
                        {!lot.source_service && (
                          <button
                            onClick={() => setStrategyModal({ lot, value: lot.strategy_name || '' })}
                            disabled={savingLotStrategy === lot.id}
                            className="text-gray-500 hover:text-gray-200 disabled:opacity-40 transition-colors cursor-pointer p-0.5"
                            title="Edit strategy name"
                            aria-label="Edit strategy name"
                          >
                            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                          </button>
                        )}
                      </div>
                    </td>
                    <td className="py-2 pr-4 text-right font-mono">
                      {(() => {
                        const dte = daysToExpiry(lot.expd)
                        if (dte === null) return <span className="text-gray-600">—</span>
                        return (
                          <span
                            title={lot.expd}
                            className={`font-semibold ${dte <= 2 ? 'text-red-400' : dte <= 7 ? 'text-amber-400' : 'text-gray-300'}`}
                          >
                            {dte}d
                          </span>
                        )
                      })()}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono">
                      <div className="inline-flex items-center gap-1.5 justify-end">
                        <span
                          className={`text-[10px] font-sans font-semibold px-1 py-0.5 rounded ${
                            lot.side === 'B'
                              ? 'bg-green-900/50 text-green-400'
                              : 'bg-red-900/50 text-red-400'
                          }`}
                        >
                          {lot.side === 'B' ? 'LONG' : 'SHORT'}
                        </span>
                        <span className={lot.side === 'B' ? 'text-green-400' : 'text-red-400'}>
                          {lot.open_qty}/{lot.entry_qty}
                        </span>
                      </div>
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-gray-400">
                      ₹{formatINR(lot.avg_entry_price)}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-gray-400">
                      {lot.ltp ? `₹${formatINR(lot.ltp)}` : <span className="text-gray-600">—</span>}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono">
                      {(() => {
                        // Day MTM = (LTP - prev-day close) x qty, side- & prcftr-adjusted.
                        // Blank for PENDING lots or before a close tick has arrived.
                        if (isPending || !lot.prev_close || !lot.mtm_pnl) {
                          return <span className="text-gray-600">—</span>
                        }
                        return (
                          <span
                            className={lot.mtm_pnl >= 0 ? 'text-green-400' : 'text-red-400'}
                            title={`vs prev close ₹${formatINR(lot.prev_close)}`}
                          >
                            {fmtPnl(lot.mtm_pnl)}
                          </span>
                        )
                      })()}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono">
                      {(() => {
                        // live_pnl already includes carried_pnl once (backend
                        // _lot_live_pnl + the WS-tick recompute) — don't re-add it.
                        const displayPnl = lot.live_pnl
                        return (
                          <span className={displayPnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                            {fmtPnl(displayPnl)}
                          </span>
                        )
                      })()}
                      {!!lot.carried_pnl && (
                        <div className="text-[10px] text-gray-500" title="Includes P&L carried forward onto this contract">
                          incl. {fmtPnl(lot.carried_pnl)} carried
                        </div>
                      )}
                    </td>
                    <td className="py-2 pr-4">
                      <span className={`text-xs px-2 py-0.5 rounded ${
                        lot.status === 'OPEN' ? 'bg-green-950 text-green-300' :
                        lot.status === 'PARTIAL' ? 'bg-yellow-950 text-yellow-300' :
                        lot.status === 'PENDING' ? 'bg-blue-950 text-blue-300' :
                        lot.status === 'CLOSED' ? 'bg-gray-800 text-gray-400' :
                        'bg-red-950 text-red-300'
                      }`}>
                        {lot.status}
                      </span>
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs text-gray-500">
                      {lot.opened_at ? (() => {
                        const d = new Date(lot.opened_at + 'Z')
                        const date = d.toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short' })
                        const time = d.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })
                        return (
                          <div>
                            <div className="text-gray-600">{date}</div>
                            <div>{time}</div>
                          </div>
                        )
                      })() : ''}
                    </td>
                  </tr>
                )
              }) : (
                <tr>
                  <td colSpan={10} className="py-4 text-center text-sm text-gray-500">No strategy lots match the current filter</td>
                </tr>
              )}
            </tbody>
            {filteredLots.length > 0 && (
              <tfoot>
                {(() => {
                  const totalLive = filteredLots.reduce((s, l) => s + (l.live_pnl || 0), 0)
                  const totalMtm = filteredLots.reduce((s, l) => s + (l.mtm_pnl || 0), 0)
                  return (
                    <tr className="border-t-2 border-gray-700 bg-gray-800/30">
                      <td className="pt-2 pr-4 text-xs text-gray-500 font-semibold">Total</td>
                      <td colSpan={5} />
                      <td className={`pt-2 pr-4 text-right font-mono font-semibold text-sm ${totalMtm >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {fmtPnl(totalMtm)}
                      </td>
                      <td className={`pt-2 pr-4 text-right font-mono font-semibold text-sm ${totalLive >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {fmtPnl(totalLive)}
                      </td>
                      <td colSpan={2} />
                    </tr>
                  )
                })()}
              </tfoot>
            )}
          </table>
        </div>
      ) : (
        <p className="text-sm text-gray-500 text-center py-4">No strategy orders</p>
      )}

      {strategyModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setStrategyModal(null)}>
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 w-96 space-y-4" onClick={e => e.stopPropagation()}>
            <h3 className="text-sm font-semibold text-gray-200">
              Strategy name · {strategyModal.lot.tsym}
            </h3>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Strategy</label>
              <input
                type="text"
                list="strategy-name-suggestions-so"
                autoFocus
                value={strategyModal.value}
                onChange={e => setStrategyModal(m => m ? { ...m, value: e.target.value } : m)}
                className="w-full px-3 py-2 bg-gray-950 border border-gray-800 rounded text-sm text-gray-200 focus:border-gray-600 outline-none"
                placeholder="e.g. Iron Condor"
              />
              <datalist id="strategy-name-suggestions-so">
                {strategyNames.map(name => <option key={name} value={name} />)}
              </datalist>
              <p className="text-xs text-gray-500 mt-1.5">
                Clearing the name moves this order back to the Orders card.
              </p>
            </div>
            <div className="flex items-center justify-end gap-2 pt-2">
              <button
                onClick={() => {
                  onUpdateLotStrategy(strategyModal.lot.id, '')
                  setStrategyModal(null)
                }}
                disabled={savingLotStrategy === strategyModal.lot.id}
                className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:border-red-700 hover:text-red-300 disabled:opacity-50 transition-colors cursor-pointer mr-auto"
              >
                Clear
              </button>
              <button
                onClick={() => setStrategyModal(null)}
                className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  onUpdateLotStrategy(strategyModal.lot.id, strategyModal.value.trim())
                  setStrategyModal(null)
                }}
                disabled={savingLotStrategy === strategyModal.lot.id}
                className="text-xs px-3 py-1.5 rounded bg-blue-900 border border-blue-700 text-blue-200 hover:bg-blue-800 disabled:opacity-50 transition-colors cursor-pointer"
              >
                Save
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
