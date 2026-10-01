import { useState } from 'react'
import type { PositionItem, PositionsSummary, SymbolTarget, ExchangeTarget } from '../types'
import SkeletonLine from './SkeletonLine'
import { formatINR, formatPnl } from '../utils/format'
import { usePersistedState } from '../hooks/usePersistedState'

interface PositionsCardProps {
  positionsSummary: PositionsSummary | null
  loadingPositions: boolean
  exchangeTargets: Map<string, ExchangeTarget>
  savingExchangeTarget: string | null
  symbolTargets: Map<string, SymbolTarget>
  savingTarget: string | null
  exitingPosition: string | null
  onFetchPositions: () => void
  onExitPosition: (pos: PositionItem) => void
  onUpdateExchangeTarget: (exch: string, patch: { enabled?: boolean; target_value?: number }) => void
  onUpdateSymbolTarget: (exch: string, tsym: string, patch: { enabled?: boolean; target_value?: number }) => void
}

interface ModalState {
  kind: 'exchange' | 'symbol'
  exch?: string
  tsym?: string
  value: string
  editing?: boolean
}

function Toggle({ enabled, disabled, onChange }: { enabled: boolean; disabled: boolean; onChange: () => void }) {
  return (
    <button
      role="switch"
      aria-checked={enabled}
      disabled={disabled}
      onClick={onChange}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors cursor-pointer disabled:opacity-50 focus:outline-none ${
        enabled ? 'bg-amber-500' : 'bg-gray-700'
      }`}
    >
      <span
        className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
          enabled ? 'translate-x-4' : 'translate-x-0.5'
        }`}
      />
    </button>
  )
}

// Mirrors the backend trigger check in targets.py: symbol total P&L = (closed_pnl + netqty*price)*prcftr,
// summed across the symbol's positions (same instrument, same prcftr, so one price applies to all of them).
// Solving for price gives the price at which the symbol's target will fire.
function symbolTargetTriggerPrice(positions: PositionItem[], targetValue: number): number | null {
  if (!positions.length || !Number.isFinite(targetValue) || targetValue <= 0) return null
  const netQty = positions.reduce((s, p) => s + parseInt(p.netqty), 0)
  const prcftr = positions[0].prcftr || 1
  const priced = positions.find(p => parseInt(p.netqty) !== 0)
  const price = priced?.exit_price || parseFloat(positions[0].lp) || 0
  const currentPnl = positions.reduce((s, p) => s + p.total_pnl, 0)
  if (!netQty || !prcftr || !price) return null
  return price + (targetValue - currentPnl) / (netQty * prcftr)
}

const _EXPIRY_MONTHS: Record<string, number> = {
  JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5,
  JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11,
}

// Scripmaster expiry format is "31-JUL-2026"; returns whole days from today
// (0 = expires today), or null for non-derivative positions (no expiry).
function daysToExpiry(expiry: string): number | null {
  const m = expiry.match(/^(\d{1,2})-([A-Za-z]{3})-(\d{4})$/)
  if (!m) return null
  const month = _EXPIRY_MONTHS[m[2].toUpperCase()]
  if (month === undefined) return null
  const expiryDate = new Date(Number(m[3]), month, Number(m[1]))
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  return Math.round((expiryDate.getTime() - startOfToday.getTime()) / 86400000)
}

function Tooltip({ text }: { text: string }) {
  return (
    <span className="relative group inline-flex items-center">
      <span className="text-gray-200 hover:text-white cursor-help text-xs select-none">ⓘ</span>
      <span className="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 hidden group-hover:block w-52 rounded bg-gray-800 border border-gray-700 px-2.5 py-1.5 text-xs text-gray-300 leading-snug z-20 shadow-lg">
        {text}
      </span>
    </span>
  )
}

type PosSortKey =
  | 'symbol' | 'product' | 'expiry' | 'lots' | 'netqty'
  | 'buyavg' | 'sellavg' | 'ltp' | 'rpnl' | 'urmtom' | 'total'

type PosRow = PositionItem & { _symbol: string; _exchange: string }

// Long / short / flat from the signed net quantity.
function posDirection(netqty: string): 'LONG' | 'SHORT' | 'FLAT' {
  const q = parseInt(netqty)
  return q > 0 ? 'LONG' : q < 0 ? 'SHORT' : 'FLAT'
}

// MTM shown in the table. While a leg is open this is the live unrealized mark.
// Once flat, the broker's urmtom is 0, so show the mark frozen at square-off
// (mtm_at_close) — the number the trader was watching when they exited.
function mtmDisplay(p: PositionItem): number {
  return (parseInt(p.netqty) || 0) === 0 ? p.mtm_at_close : p.urmtom
}

function posSortValue(row: PosRow, key: PosSortKey): string | number {
  switch (key) {
    case 'symbol': return row._symbol
    case 'product': return row.s_prdt_ali
    case 'expiry': { const d = daysToExpiry(row.expiry); return d === null ? Number.POSITIVE_INFINITY : d }
    case 'lots': { const ls = parseInt(row.lotsize) || 1; const qty = parseInt(row.netqty) || 0; return ls > 1 ? qty / ls : 0 }
    case 'netqty': return parseInt(row.netqty) || 0
    case 'buyavg': return parseFloat(row.buyavgprc) || 0
    case 'sellavg': return parseFloat(row.sellavgprc) || 0
    case 'ltp': return parseFloat(row.lp) || 0
    case 'rpnl': return row.rpnl
    case 'urmtom': return mtmDisplay(row)
    case 'total': return row.total_pnl
  }
}

function SortTh({ label, sortKey, active, dir, onSort, align = 'left' }: {
  label: string
  sortKey: PosSortKey
  active: PosSortKey | null
  dir: 'asc' | 'desc'
  onSort: (key: PosSortKey) => void
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

export default function PositionsCard({
  positionsSummary,
  loadingPositions,
  exchangeTargets,
  savingExchangeTarget,
  symbolTargets,
  savingTarget,
  exitingPosition,
  onFetchPositions,
  onExitPosition,
  onUpdateExchangeTarget,
  onUpdateSymbolTarget,
}: PositionsCardProps) {
  const [modal, setModal] = useState<ModalState | null>(null)
  const [exitConfirm, setExitConfirm] = useState<PositionItem | null>(null)
  const [exitPriceInput, setExitPriceInput] = useState<string>('')
  const [exchFilter, setExchFilter] = usePersistedState<string | null>('positions.exchFilter', null)
  const [symbolQuery, setSymbolQuery] = usePersistedState('positions.symbolQuery', '')
  const [dirFilter, setDirFilter] = usePersistedState<'LONG' | 'SHORT' | 'FLAT' | null>('positions.dirFilter', null)
  const [prodFilter, setProdFilter] = usePersistedState<string | null>('positions.prodFilter', null)
  const [hideClosed, setHideClosed] = usePersistedState('positions.hideClosed', false)
  const [sortKey, setSortKey] = usePersistedState<PosSortKey | null>('positions.sortKey', null)
  const [sortDir, setSortDir] = usePersistedState<'asc' | 'desc'>('positions.sortDir', 'desc')

  const handleSort = (key: PosSortKey) => {
    if (sortKey !== key) { setSortKey(key); setSortDir('desc'); return }
    if (sortDir === 'desc') { setSortDir('asc'); return }
    setSortKey(null)
  }

  const symbolPositionsMap = new Map(
    (positionsSummary?.symbol_groups ?? []).map(g => [`${g.exchange}|${g.symbol}`, g.positions]),
  )

  const openExchangeModal = (exch: string, editing = false) => {
    const existing = exchangeTargets.get(exch)
    setModal({ kind: 'exchange', exch, value: String(existing?.target_value ?? 50000), editing })
  }

  const openSymbolModal = (exch: string, tsym: string, editing = false) => {
    const existing = symbolTargets.get(`${exch}|${tsym}`)
    setModal({ kind: 'symbol', exch, tsym, value: String(existing?.target_value ?? 10000), editing })
  }

  const confirmModal = () => {
    if (!modal) return
    const v = parseFloat(modal.value)
    if (isNaN(v) || v <= 0) return
    if (modal.kind === 'exchange') {
      onUpdateExchangeTarget(modal.exch!, { enabled: true, target_value: v })
    } else {
      onUpdateSymbolTarget(modal.exch!, modal.tsym!, { enabled: true, target_value: v })
    }
    setModal(null)
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-5">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest shrink-0">Positions & P&L</h2>
        <button
          onClick={onFetchPositions}
          disabled={loadingPositions}
          className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 disabled:opacity-50 transition-colors cursor-pointer shrink-0 ml-auto"
        >
          ↻
        </button>
      </div>

      {loadingPositions && !positionsSummary ? (
        <div className="space-y-4">
          {[1, 2, 3].map(i => (
            <SkeletonLine key={i} width={200 + i * 50} />
          ))}
        </div>
      ) : positionsSummary && positionsSummary.symbol_groups.length > 0 ? (
        <>
          {/* Total P&L (compact), and per-exchange breakdown with per-exchange targets */}
          <div className="flex items-stretch gap-3 flex-wrap">
            {(() => {
              const totalUrmtom = positionsSummary.symbol_groups.reduce(
                (sum, g) => sum + g.positions.reduce((s, p) => s + p.urmtom, 0),
                0,
              )
              return (
                <div className="flex flex-col justify-center shrink-0 px-3 py-2 bg-gray-800 rounded-lg border border-gray-700 w-[170px]">
                  <span className="text-xs text-gray-400">Total P&L</span>
                  <span className={`text-lg font-semibold ${positionsSummary.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {formatPnl(positionsSummary.total_pnl)}
                  </span>
                  <span className={`text-sm font-mono ${totalUrmtom >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    MTM {formatPnl(totalUrmtom, 0)}
                  </span>
                </div>
              )
            })()}

            {/* Per-exchange PnL + unrealised MTM + per-exchange auto-exit target.
                Cards flex-grow to fill the row and wrap as more exchanges appear. */}
            {(() => {
              const exchStats = positionsSummary.symbol_groups.reduce((acc, g) => {
                const entry = acc[g.exchange] ?? { pnl: 0, urmtom: 0 }
                entry.pnl += g.symbol_pnl
                entry.urmtom += g.positions.reduce((sum, p) => sum + p.urmtom, 0)
                acc[g.exchange] = entry
                return acc
              }, {} as Record<string, { pnl: number; urmtom: number }>)
              const exchanges = Object.keys(exchStats).sort()
              return exchanges.map(exch => {
                const { pnl, urmtom } = exchStats[exch]
                const target = exchangeTargets.get(exch)
                const isSaving = savingExchangeTarget === exch
                return (
                  <div key={exch} className="flex items-center justify-between gap-3 flex-1 basis-[220px] px-4 py-2 rounded-lg bg-gray-800 border border-gray-700">
                    <div className="flex flex-col">
                      <span className="text-xs text-gray-200 font-semibold uppercase tracking-wide font-mono">{exch}</span>
                      <span className={`text-sm font-semibold font-mono ${pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {formatPnl(pnl, 0)}
                      </span>
                      <span className={`text-sm font-mono ${urmtom >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        MTM {formatPnl(urmtom, 0)}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      <Tooltip text={`When ${exch}'s total P&L (realized + unrealized) hits this value, all ${exch} positions are auto-exited. Each exchange has its own target.`} />
                      {target?.enabled && (
                        <>
                          <span className="text-[11px] font-semibold text-amber-400 font-mono">₹{formatINR(target.target_value, 0)}</span>
                          <button
                            onClick={() => openExchangeModal(exch, true)}
                            disabled={isSaving}
                            className="text-xs text-gray-300 hover:text-white disabled:opacity-40 transition-colors cursor-pointer"
                            title="Edit target"
                          >
                            <svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                          </button>
                        </>
                      )}
                      <Toggle
                        enabled={!!target?.enabled}
                        disabled={isSaving}
                        onChange={() => {
                          if (target?.enabled) {
                            onUpdateExchangeTarget(exch, { enabled: false })
                          } else {
                            openExchangeModal(exch)
                          }
                        }}
                      />
                    </div>
                  </div>
                )
              })
            })()}
          </div>

          {/* Filters */}
          {(() => {
            const exchanges = Array.from(new Set(positionsSummary.symbol_groups.map(g => g.exchange))).sort()
            const products = Array.from(new Set(
              positionsSummary.symbol_groups.flatMap(g => g.positions.map(p => p.s_prdt_ali)),
            )).sort()
            return (
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
                    <span className="text-xs text-gray-600">Exchange</span>
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
                  {([null, 'LONG', 'SHORT', 'FLAT'] as const).map(dir => (
                    <button
                      key={dir ?? 'all'}
                      onClick={() => setDirFilter(dir)}
                      className={`text-xs px-2 py-0.5 rounded border transition-colors cursor-pointer ${
                        dirFilter === dir
                          ? 'border-gray-500 bg-gray-700 text-gray-200'
                          : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-400'
                      }`}
                    >
                      {dir === null ? 'All' : dir === 'LONG' ? 'Long' : dir === 'SHORT' ? 'Short' : 'Flat'}
                    </button>
                  ))}
                </div>

                {products.length > 1 && (
                  <div className="flex items-center gap-1.5">
                    <span className="text-xs text-gray-600">Product</span>
                    <button
                      onClick={() => setProdFilter(null)}
                      className={`text-xs px-2 py-0.5 rounded border transition-colors cursor-pointer ${
                        prodFilter === null
                          ? 'border-gray-500 bg-gray-700 text-gray-200'
                          : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-400'
                      }`}
                    >
                      All
                    </button>
                    {products.map(prod => (
                      <button
                        key={prod}
                        onClick={() => setProdFilter(prodFilter === prod ? null : prod)}
                        className={`text-xs px-2 py-0.5 rounded border transition-colors cursor-pointer font-mono ${
                          prodFilter === prod
                            ? 'border-blue-500 bg-blue-600/20 text-blue-300'
                            : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-400'
                        }`}
                      >
                        {prod}
                      </button>
                    ))}
                  </div>
                )}

                <label className="flex items-center gap-1.5 ml-auto cursor-pointer select-none">
                  <span className="text-xs text-gray-600">Hide closed</span>
                  <Toggle enabled={hideClosed} disabled={false} onChange={() => setHideClosed(v => !v)} />
                </label>
              </div>
            )
          })()}

          {/* Position table */}
          {(() => {
            const query = symbolQuery.trim().toUpperCase()
            const rows: PosRow[] = positionsSummary.symbol_groups
              .filter(g => !exchFilter || g.exchange === exchFilter)
              .flatMap(g => g.positions.map(p => ({ ...p, _symbol: g.symbol, _exchange: g.exchange })))
              .filter(r => !query || r._symbol.toUpperCase().includes(query) || r.tsym.toUpperCase().includes(query))
              .filter(r => !dirFilter || posDirection(r.netqty) === dirFilter)
              .filter(r => !prodFilter || r.s_prdt_ali === prodFilter)
              .filter(r => !hideClosed || parseInt(r.netqty) !== 0)
              .sort((a, b) => {
                if (sortKey === null) return 0
                const av = posSortValue(a, sortKey)
                const bv = posSortValue(b, sortKey)
                const cmp = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv))
                return sortDir === 'desc' ? -cmp : cmp
              })
            return (
              <div className="overflow-x-auto">
                <table className="w-full text-base">
                  <thead>
                    <tr className="text-left text-sm text-gray-600 border-b border-gray-800">
                      <SortTh label="Symbol" sortKey="symbol" active={sortKey} dir={sortDir} onSort={handleSort} />
                      <SortTh label="Product" sortKey="product" active={sortKey} dir={sortDir} onSort={handleSort} />
                      <SortTh label="Expiry" sortKey="expiry" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                      <SortTh label="Lots" sortKey="lots" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                      <SortTh label="Net Qty" sortKey="netqty" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                      <SortTh label="Buy Avg" sortKey="buyavg" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                      <SortTh label="Sell Avg" sortKey="sellavg" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                      <SortTh label="LTP" sortKey="ltp" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                      <SortTh label="Realized P&L" sortKey="rpnl" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                      <SortTh label="Unrealized MTM" sortKey="urmtom" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                      <SortTh label="Total P&L" sortKey="total" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                      <th className="pb-2 pr-4 font-medium text-center">
                        <span className="inline-flex items-center gap-1">
                          Target
                          <Tooltip text="Total P&L (realized + unrealized), valued at best bid (long) / best ask (short). Not scaled by lot count." />
                        </span>
                      </th>
                      <th className="pb-2 font-medium text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.length > 0 ? rows.map((pos, idx) => {
                      const posKey = `${pos.tsym}_${pos.prd}`
                      const isExiting = exitingPosition === posKey
                      const netQty = parseInt(pos.netqty)
                      const targetKey = `${pos._exchange}|${pos._symbol}`
                      const target = symbolTargets.get(targetKey)
                      const isSaving = savingTarget === targetKey
                      const triggerPrice = target?.enabled
                        ? symbolTargetTriggerPrice(symbolPositionsMap.get(targetKey) ?? [pos], target.target_value)
                        : null
                      return (
                        <tr key={idx} className="border-b border-gray-800/40">
                          <td className="py-2 pr-4 text-gray-400 text-xs font-mono">{pos._symbol}</td>
                          <td className="py-2 pr-4">{pos.s_prdt_ali}</td>
                          <td className="py-2 pr-4 text-right font-mono">
                            {(() => {
                              const dte = daysToExpiry(pos.expiry)
                              if (dte === null) return <span className="text-gray-600">—</span>
                              return (
                                <span
                                  title={pos.expiry}
                                  className={`font-semibold ${dte <= 2 ? 'text-red-400' : dte <= 7 ? 'text-amber-400' : 'text-gray-300'}`}
                                >
                                  {dte}d
                                </span>
                              )
                            })()}
                          </td>
                          <td className="py-2 pr-4 text-right font-mono text-gray-300">
                            {(() => {
                              const ls = parseInt(pos.lotsize) || 1
                              const qty = parseInt(pos.netqty) || 0
                              return ls > 1 ? (qty / ls).toFixed(qty % ls === 0 ? 0 : 2) : '—'
                            })()}
                          </td>
                          <td className="py-2 pr-4 text-right font-mono">
                            <div className="inline-flex items-center gap-1.5 justify-end">
                              <span
                                className={`text-[10px] font-sans font-semibold px-1 py-0.5 rounded ${
                                  netQty > 0
                                    ? 'bg-green-900/50 text-green-400'
                                    : netQty < 0
                                      ? 'bg-red-900/50 text-red-400'
                                      : 'bg-gray-800 text-gray-500'
                                }`}
                              >
                                {netQty > 0 ? 'LONG' : netQty < 0 ? 'SHORT' : 'FLAT'}
                              </span>
                              <span className={netQty > 0 ? 'text-green-400' : netQty < 0 ? 'text-red-400' : 'text-gray-300'}>
                                {pos.netqty}
                              </span>
                            </div>
                          </td>
                          <td className="py-2 pr-4 text-right font-mono text-gray-400">{formatINR(parseFloat(pos.buyavgprc))}</td>
                          <td className="py-2 pr-4 text-right font-mono text-gray-400">{formatINR(parseFloat(pos.sellavgprc))}</td>
                          <td className="py-2 pr-4 text-right font-mono text-gray-400">{formatINR(parseFloat(pos.lp))}</td>
                          <td className={`py-2 pr-4 text-right font-mono ${pos.rpnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                            {formatPnl(pos.rpnl)}
                          </td>
                          <td className={`py-2 pr-4 text-right font-mono ${mtmDisplay(pos) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                            {formatPnl(mtmDisplay(pos))}
                          </td>
                          <td className={`py-2 pr-4 text-right font-mono font-semibold ${pos.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                            {formatPnl(pos.total_pnl)}
                          </td>
                          <td className="py-2 pr-4 text-center">
                            <div className="flex items-center justify-center gap-1.5">
                              {target?.enabled && (
                                <>
                                  <div className="flex flex-col items-end leading-tight">
                                    <span className="text-xs text-amber-400 font-semibold">₹{target.target_value.toLocaleString('en-IN')}</span>
                                    {triggerPrice !== null && (
                                      <span className="text-[10px] text-white font-mono">@ {formatINR(triggerPrice)}</span>
                                    )}
                                  </div>
                                  <button
                                    onClick={() => openSymbolModal(pos._exchange, pos._symbol, true)}
                                    disabled={isSaving}
                                    className="text-xs text-gray-300 hover:text-white disabled:opacity-40 transition-colors cursor-pointer"
                                    title="Edit target"
                                  >
                                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                                  </button>
                                </>
                              )}
                              <Toggle
                                enabled={!!target?.enabled}
                                disabled={isSaving}
                                onChange={() => {
                                  if (target?.enabled) {
                                    onUpdateSymbolTarget(pos._exchange, pos._symbol, { enabled: false })
                                  } else {
                                    openSymbolModal(pos._exchange, pos._symbol)
                                  }
                                }}
                              />
                            </div>
                          </td>
                          <td className="py-2 text-right">
                            <button
                              onClick={() => {
                                setExitConfirm(pos)
                                setExitPriceInput(String((pos.exit_price ?? parseFloat(pos.lp)).toFixed(2)))
                              }}
                              disabled={isExiting || netQty === 0}
                              className="text-xs px-2 py-1 rounded border border-red-700 bg-red-950/50 text-red-400 hover:border-red-500 hover:text-red-300 disabled:opacity-40 disabled:cursor-not-allowed transition-colors cursor-pointer"
                            >
                              {isExiting ? '...' : 'Exit'}
                            </button>
                          </td>
                        </tr>
                      )
                    }) : (
                      <tr>
                        <td colSpan={13} className="py-4 text-center text-sm text-gray-500">No positions match the current filter</td>
                      </tr>
                    )}
                  </tbody>
                  {rows.length > 0 && (
                  <tfoot>
                    {(() => {
                      const totalRpnl = rows.reduce((s, p) => s + p.rpnl, 0)
                      const totalUrmtom = rows.reduce((s, p) => s + mtmDisplay(p), 0)
                      const totalPnl = rows.reduce((s, p) => s + p.total_pnl, 0)
                      return (
                        <tr className="border-t-2 border-gray-700 bg-gray-800/30">
                          <td className="pt-2 pr-4 text-xs text-gray-500 font-semibold">Total</td>
                          <td colSpan={7} className="pt-2 pr-4 text-xs text-gray-500 font-semibold" />
                          <td className={`pt-2 pr-4 text-right font-mono font-semibold text-sm ${totalRpnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                            {formatPnl(totalRpnl)}
                          </td>
                          <td className={`pt-2 pr-4 text-right font-mono font-semibold text-sm ${totalUrmtom >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                            {formatPnl(totalUrmtom)}
                          </td>
                          <td className={`pt-2 pr-4 text-right font-mono font-semibold text-sm ${totalPnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                            {formatPnl(totalPnl)}
                          </td>
                          <td colSpan={2} />
                        </tr>
                      )
                    })()}
                  </tfoot>
                  )}
                </table>
              </div>
            )
          })()}
        </>
      ) : (
        <p className="text-sm text-gray-500 text-center py-4">No active positions</p>
      )}

      {/* Exit confirmation modal */}
      {exitConfirm && (() => {
        const netQty = parseInt(exitConfirm.netqty)
        const side = netQty > 0 ? 'SELL' : 'BUY'
        const sideColor = side === 'SELL' ? 'text-red-400' : 'text-green-400'
        const parsedPrice = parseFloat(exitPriceInput)
        const priceValid = !isNaN(parsedPrice) && parsedPrice > 0
        return (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
            <div className="bg-gray-900 border border-gray-700 rounded-xl p-6 w-80 space-y-4 shadow-2xl">
              <div className="space-y-0.5">
                <h3 className="text-sm font-semibold text-gray-200">Confirm Exit</h3>
                <p className="text-xs text-gray-500">This will place a market-limit order to close the position.</p>
              </div>
              <div className="bg-gray-800 rounded-lg p-3 space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-gray-500">Symbol</span>
                  <span className="text-gray-200 font-medium">{exitConfirm.tsym}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-500">Side</span>
                  <span className={`font-semibold ${sideColor}`}>{side}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-500">Qty</span>
                  <span className="text-gray-200 font-mono">{Math.abs(netQty)}</span>
                </div>
                <div className="flex items-center justify-between border-t border-gray-700 pt-2 gap-3">
                  <span className="text-gray-500 shrink-0">Price ({side === 'SELL' ? 'Best Bid' : 'Best Ask'})</span>
                  <input
                    type="number"
                    value={exitPriceInput}
                    onChange={e => setExitPriceInput(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && priceValid && (onExitPosition({ ...exitConfirm, exit_price: parsedPrice }), setExitConfirm(null))}
                    className="w-28 text-right text-sm px-2 py-1 rounded border border-gray-700 bg-gray-900 text-gray-100 font-mono font-semibold focus:outline-none focus:border-blue-500"
                  />
                </div>
              </div>
              <div className="flex gap-2 justify-end">
                <button
                  onClick={() => setExitConfirm(null)}
                  className="text-xs px-4 py-2 rounded border border-gray-700 text-gray-400 hover:text-gray-200 transition-colors cursor-pointer"
                >
                  Cancel
                </button>
                <button
                  onClick={() => { onExitPosition({ ...exitConfirm, exit_price: parsedPrice }); setExitConfirm(null) }}
                  disabled={!priceValid}
                  className="text-xs px-4 py-2 rounded border border-red-700 bg-red-950/50 text-red-400 hover:border-red-500 hover:text-red-300 disabled:opacity-40 disabled:cursor-not-allowed transition-colors cursor-pointer"
                >
                  Confirm Exit
                </button>
              </div>
            </div>
          </div>
        )
      })()}

      {/* Shared modal for global and per-symbol target */}
      {modal && (() => {
        const modalTriggerPrice = modal.kind === 'symbol'
          ? symbolTargetTriggerPrice(symbolPositionsMap.get(`${modal.exch}|${modal.tsym}`) ?? [], parseFloat(modal.value))
          : null
        return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="bg-gray-900 border border-gray-700 rounded-xl p-6 w-80 space-y-4 shadow-2xl">
            <div className="space-y-0.5">
              <h3 className="text-sm font-semibold text-gray-200">
                {modal.kind === 'exchange' ? `Set ${modal.exch} P&L Target` : `Set Target — ${modal.tsym}`}
              </h3>
              <p className="text-xs text-gray-500">
                {modal.kind === 'exchange'
                  ? `All ${modal.exch} positions will be auto-exited when that exchange's total P&L hits this value.`
                  : "This symbol's positions will auto-exit when its total P&L (realized + unrealized, valued at best bid/ask) hits this value."}
              </p>
            </div>
            <div>
              <label className="text-xs text-gray-500 block mb-1">
                {modal.kind === 'exchange' ? `${modal.exch} total P&L target (₹)` : `${modal.tsym} total P&L target (₹)`}
              </label>
              <input
                autoFocus
                type="number"
                value={modal.value}
                onChange={e => setModal(m => m ? { ...m, value: e.target.value } : m)}
                onKeyDown={e => e.key === 'Enter' && confirmModal()}
                className="w-full text-sm px-3 py-2 rounded border border-gray-700 bg-gray-800 text-gray-200 font-mono focus:outline-none focus:border-amber-500"
              />
              {modal.kind === 'symbol' && modalTriggerPrice !== null && (
                <p className="text-[11px] text-amber-400/80 mt-1.5">
                  Fires when price reaches ≈ ₹{formatINR(modalTriggerPrice)}
                </p>
              )}
            </div>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setModal(null)}
                className="text-xs px-4 py-2 rounded border border-gray-700 text-gray-400 hover:text-gray-200 transition-colors cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={confirmModal}
                disabled={isNaN(parseFloat(modal.value)) || parseFloat(modal.value) <= 0}
                className="text-xs px-4 py-2 rounded border border-amber-600 bg-amber-600/20 text-amber-300 hover:bg-amber-600/30 disabled:opacity-50 transition-colors cursor-pointer"
              >
                {modal.editing ? 'Update Target' : 'Enable Target'}
              </button>
            </div>
          </div>
        </div>
        )
      })()}
    </div>
  )
}
