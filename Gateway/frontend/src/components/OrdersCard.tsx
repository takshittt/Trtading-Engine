import { useEffect, useState } from 'react'
import type { OrderItem, OrderLot, ScripSearchResult, RolloverPayload, QuoteResponse } from '../types'
import SkeletonLine from './SkeletonLine'
import { formatINR, formatPnl as fmtPnl, serviceBadge } from '../utils/format'
import { usePersistedState } from '../hooks/usePersistedState'

// Signed rupee/price display: "+₹1,234.50" / "−₹1,234.50".
function fmtSigned(v: number, digits = 2): string {
  const sign = v < 0 ? '−' : '+'
  return `${sign}₹${formatINR(Math.abs(v), digits)}`
}

// Per-lot rollover preview for the optional "Rollover" column. Mirrors the
// Rollover modal's defaults: nearest later expiry, roll cost on the full open
// qty. rollCost > 0 = debit (money out); < 0 = credit.
type RollPreview =
  | { status: 'loading' }
  | { status: 'none' } // not rollable / no later expiry / quote unavailable
  // exch/token carry the far leg's identity so the 5s refresh can re-quote it.
  | { status: 'ready'; tsym: string; expd?: string; exch: string; token: string; farLtp: number; rollCost: number }

// Roll cost from a fresh far-leg quote + the (live-updating) near lot. Buy the
// far leg on the ask (long) / sell on the bid (short), LTP fallback. Roll cost
// per unit: long pays far-near, short the reverse. rollCost > 0 = debit.
// Only the fields a live WS tick and a REST QuoteResponse share.
type PriceQuote = { lp: string; bp1: string; sp1: string }

function computeRollCost(lot: OrderLot, q: PriceQuote): { farLtp: number; rollCost: number } {
  const lp = parseFloat(q.lp) || 0
  const bid = parseFloat(q.bp1) || 0
  const ask = parseFloat(q.sp1) || 0
  const farEntry = lot.side === 'B' ? (ask || lp) : (bid || lp)
  const nearPx = lot.exit_price ?? lot.ltp ?? lot.avg_entry_price ?? 0
  const perUnit = lot.side === 'B' ? (farEntry - nearPx) : (nearPx - farEntry)
  const rollCost = perUnit * lot.open_qty * (lot.prcftr || 1)
  return { farLtp: lp, rollCost }
}

// Mirrors the backend per-order target check in targets.py: live P&L =
// realized + (price - avg_entry) * open_qty * sign * prcftr, sign = +1 for
// BUY (long), -1 for SELL (short). Solving for price gives the trigger.
// carried_pnl counts once, matching targets.py's "realized" and the Live P&L display.
function lotTargetTriggerPrice(lot: OrderLot, targetValue: number): number | null {
  if (!Number.isFinite(targetValue) || targetValue <= 0) return null
  const sign = lot.side === 'B' ? 1 : -1
  const denom = lot.open_qty * sign * (lot.prcftr || 1)
  if (!denom) return null
  const realized = (lot.realized_pnl || 0) + (lot.carried_pnl || 0)
  return lot.avg_entry_price + (targetValue - realized) / denom
}

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

type LotSortKey = 'symbol' | 'lots' | 'expiry' | 'qty' | 'entry' | 'exit' | 'ltp' | 'amount' | 'pnl' | 'mtm' | 'status' | 'opened'

function lotSortValue(lot: OrderLot, key: LotSortKey): string | number {
  switch (key) {
    case 'symbol': return lot.tsym
    case 'lots': return lot.lotsize > 1 ? lot.open_qty / lot.lotsize : lot.open_qty
    case 'expiry': { const d = daysToExpiry(lot.expd); return d === null ? Number.POSITIVE_INFINITY : d }
    case 'qty': return lot.open_qty
    case 'entry': return lot.avg_entry_price
    case 'exit': return lot.avg_exit_price || 0
    case 'ltp': return lot.ltp || 0
    // Amount = notional deployed on the open leg: open qty × entry price.
    case 'amount': return lot.open_qty * lot.avg_entry_price
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

interface OrdersCardProps {
  orders: OrderItem[]
  lots: OrderLot[]
  loadingOrders: boolean
  orderSymbolFilter: string | null
  positionOrderFilter: string | null
  exitingLot: number | null
  onFetchOrders: () => void
  onSetOrderSymbolFilter: (filter: string | null) => void
  onClearPositionOrderFilter: () => void
  onCancelOrder: (orderId: string) => void
  onExitLot: (lot: OrderLot, qty: number, price: number, tempExit: boolean) => void
  onCancelExitOrder: (lot: OrderLot) => void
  onReentryLot: (lot: OrderLot) => void
  rollingLot: number | null
  onRolloverLot: (lot: OrderLot, payload: RolloverPayload) => void
  onFetchRollTargets: (lotId: number) => Promise<ScripSearchResult[]>
  onFetchQuote: (exch: string, token: string) => Promise<QuoteResponse>
  tickMap: Record<string, PriceQuote>
  onSubscribeSymbol: (exch: string, token: string) => void
  savingLotTarget: number | null
  onUpdateLotTarget: (lotId: number, patch: { enabled?: boolean; target_value?: number }) => void
  savingLotStrategy: number | null
  onUpdateLotStrategy: (lotId: number, strategyName: string) => void
  strategyNames: string[]
  onSetTempExit: (lotId: number, enabled: boolean) => void
  onSyncPositions: () => void
  onCleanupExternal: () => void
  onModifyOrder: (orderId: string, exchange: string, tradingsymbol: string, qty: number, newPrice: number) => void
  onDeleteLot: (lotId: number) => void
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
        className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform ${
          enabled ? 'translate-x-4' : 'translate-x-0.5'
        }`}
      />
    </button>
  )
}

export default function OrdersCard({
  orders,
  lots,
  loadingOrders,
  orderSymbolFilter,
  positionOrderFilter,
  exitingLot,
  onFetchOrders,
  onSetOrderSymbolFilter,
  onClearPositionOrderFilter,
  onCancelOrder,
  onExitLot,
  onCancelExitOrder,
  onReentryLot,
  rollingLot,
  onRolloverLot,
  onFetchRollTargets,
  onFetchQuote,
  tickMap,
  onSubscribeSymbol,
  savingLotTarget,
  onUpdateLotTarget,
  savingLotStrategy,
  onUpdateLotStrategy,
  strategyNames,
  onSetTempExit,
  onSyncPositions,
  onCleanupExternal,
  onModifyOrder,
  onDeleteLot,
}: OrdersCardProps) {
  const [modalLot, setModalLot] = useState<OrderLot | null>(null)
  const [deleteLot, setDeleteLot] = useState<OrderLot | null>(null)
  const [modalQty, setModalQty] = useState(1)
  const [modalPrice, setModalPrice] = useState(0)
  const [modalTempExit, setModalTempExit] = useState(false)
  const [modifyModal, setModifyModal] = useState<{ lot: OrderLot; openOrder: OrderItem; price: string } | null>(null)
  const [targetModal, setTargetModal] = useState<{ lot: OrderLot; value: string; editing: boolean } | null>(null)
  const [strategyModal, setStrategyModal] = useState<{ lot: OrderLot; value: string } | null>(null)
  const [hideClosedLots, setHideClosedLots] = usePersistedState('orders.hideClosedLots', true)
  const [showRollover, setShowRollover] = usePersistedState('orders.showRollover', false)
  const [rollPreviews, setRollPreviews] = useState<Record<number, RollPreview>>({})
  const [exchFilter, setExchFilter] = usePersistedState<string | null>('orders.exchFilter', null)
  const [symbolQuery, setSymbolQuery] = usePersistedState('orders.symbolQuery', '')
  const [sideFilter, setSideFilter] = usePersistedState<'B' | 'S' | null>('orders.sideFilter', null)
  const [statusFilter, setStatusFilter] = usePersistedState<string | null>('orders.statusFilter', null)
  const [sortKey, setSortKey] = usePersistedState<LotSortKey | null>('orders.sortKey', null)
  const [sortDir, setSortDir] = usePersistedState<'asc' | 'desc'>('orders.sortDir', 'desc')

  const handleSort = (key: LotSortKey) => {
    if (sortKey !== key) { setSortKey(key); setSortDir('desc'); return }
    if (sortDir === 'desc') { setSortDir('asc'); return }
    setSortKey(null)
  }

  // Rollover modal state
  const [rolloverLot, setRolloverLot] = useState<OrderLot | null>(null)
  const [rollTargets, setRollTargets] = useState<ScripSearchResult[]>([])
  const [rollTargetTsym, setRollTargetTsym] = useState('')
  const [rollQty, setRollQty] = useState(1)
  const [rollPriceType, setRollPriceType] = useState<'LMT' | 'MKT'>('LMT')
  const [rollExitPrice, setRollExitPrice] = useState(0)
  const [rollEntryPrice, setRollEntryPrice] = useState(0)
  const [rollCarry, setRollCarry] = useState(true)
  const [rollCarryPnl, setRollCarryPnl] = useState(true)
  const [rollNearPrice, setRollNearPrice] = useState(0)
  const [rollFarQuote, setRollFarQuote] = useState<{ lp: number; bid: number; ask: number } | null>(null)
  const [rollLoadingTargets, setRollLoadingTargets] = useState(false)
  // Once the user hand-edits a leg's order price, stop overwriting it with
  // live ticks — an order actually gets placed off these, so a manual price
  // must stick, unlike the read-only table preview.
  const [nearPriceDirty, setNearPriceDirty] = useState(false)
  const [farPriceDirty, setFarPriceDirty] = useState(false)

  useEffect(() => {
    if (modalLot) {
      setModalQty(modalLot.open_qty)
      // Prefer the live touch (bid for long / ask for short, maintained from
      // ticks in App.tsx), then the LTP snapshot, then entry price as a floor.
      setModalPrice(modalLot.exit_price ?? modalLot.ltp ?? modalLot.avg_entry_price ?? 0)
    }
  }, [modalLot])

  // When the rollover modal opens, seed defaults and load roll targets.
  useEffect(() => {
    if (!rolloverLot) return
    const lot = rolloverLot
    setRollQty(lot.open_qty)
    setRollPriceType('LMT')
    const nearLtp = lot.exit_price ?? lot.ltp ?? lot.avg_entry_price ?? 0
    setRollNearPrice(nearLtp)
    setRollExitPrice(nearLtp)
    setRollEntryPrice(0)
    setRollFarQuote(null)
    setRollCarry(!!lot.target_enabled)
    setRollCarryPnl(true)
    setNearPriceDirty(false)
    setFarPriceDirty(false)
    setRollLoadingTargets(true)
    onFetchRollTargets(lot.id)
      .then(rows => {
        setRollTargets(rows)
        setRollTargetTsym(rows[0]?.tsym ?? '')
      })
      .catch(() => { setRollTargets([]); setRollTargetTsym('') })
      .finally(() => setRollLoadingTargets(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rolloverLot])

  // Pull a live quote for the selected far contract to prefill its entry price
  // and show the roll spread, then subscribe it on the WS so the effect below
  // keeps both live from here on instead of this being the only price you get.
  useEffect(() => {
    if (!rolloverLot || !rollTargetTsym) { setRollFarQuote(null); return }
    const tgt = rollTargets.find(t => t.tsym === rollTargetTsym)
    if (!tgt) return
    onSubscribeSymbol(tgt.exch, tgt.token)
    let cancelled = false
    onFetchQuote(tgt.exch, tgt.token)
      .then(q => {
        if (cancelled) return
        const lp = parseFloat(q.lp) || 0
        const bid = parseFloat(q.bp1) || 0
        const ask = parseFloat(q.sp1) || 0
        setRollFarQuote({ lp, bid, ask })
        // Buy the far leg on the ask, sell on the bid; fall back to LTP.
        if (!farPriceDirty) setRollEntryPrice(rolloverLot.side === 'B' ? (ask || lp) : (bid || lp))
      })
      .catch(() => { if (!cancelled) setRollFarQuote(null) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rolloverLot, rollTargetTsym, rollTargets])

  // Keep the far leg's price live off the WS feed (subscribed above) instead of
  // the one-time REST snapshot going stale while the modal sits open — an
  // order actually gets placed off rollEntryPrice, so a frozen number here
  // risks a fill or rejection at a price nowhere near the current market.
  useEffect(() => {
    if (!rolloverLot || !rollTargetTsym) return
    const tgt = rollTargets.find(t => t.tsym === rollTargetTsym)
    if (!tgt) return
    const q = tickMap[`${tgt.exch}|${tgt.token}`]
    if (!q) return
    const lp = parseFloat(q.lp) || 0
    const bid = parseFloat(q.bp1) || 0
    const ask = parseFloat(q.sp1) || 0
    setRollFarQuote({ lp, bid, ask })
    if (!farPriceDirty) setRollEntryPrice(rolloverLot.side === 'B' ? (ask || lp) : (bid || lp))
  }, [tickMap, rolloverLot, rollTargetTsym, rollTargets, farPriceDirty])

  // Same for the near (closing) leg — it's already WS-subscribed as an open
  // position, so this just reads the live exit_price App.tsx keeps updating.
  useEffect(() => {
    if (!rolloverLot) return
    const liveLot = lots.find(l => l.id === rolloverLot.id)
    if (!liveLot) return
    const nearLtp = liveLot.exit_price ?? liveLot.ltp ?? liveLot.avg_entry_price ?? 0
    setRollNearPrice(nearLtp)
    if (!nearPriceDirty) setRollExitPrice(nearLtp)
  }, [lots, rolloverLot, nearPriceDirty])

  // Stable signature of the rollable lot set. WS ticks hand `lots` a brand-new
  // array reference every price change; keying the fetch effect on that cancelled
  // every in-flight request mid-flight (permanent "…"). This only changes when a
  // lot actually enters or leaves OPEN/PARTIAL, so ticks no longer thrash it.
  const rollableKey = lots
    .filter(l => l.status === 'OPEN' || l.status === 'PARTIAL')
    .map(l => l.id)
    .sort((a, b) => a - b)
    .join(',')

  // When the Rollover column is on, lazily resolve each rollable lot's nearest
  // later expiry + a live quote and compute its roll cost — the same two calls
  // (roll-targets, quote) and math the Rollover modal uses. Cached per lot id; a
  // resolved cell is a snapshot, refreshed by toggling the column off then on.
  useEffect(() => {
    if (!showRollover) return
    // Only OPEN/PARTIAL lots can roll — mirror the Rollover button's gate.
    const rollable = lots.filter(l => l.status === 'OPEN' || l.status === 'PARTIAL')
    for (const lot of rollable) {
      if (rollPreviews[lot.id]) continue // already loading / resolved
      ;(async () => {
        setRollPreviews(prev => (prev[lot.id] ? prev : { ...prev, [lot.id]: { status: 'loading' } }))
        try {
          const targets = await onFetchRollTargets(lot.id)
          if (targets.length === 0) {
            setRollPreviews(prev => ({ ...prev, [lot.id]: { status: 'none' } }))
            return
          }
          const far = targets[0]
          // The far leg is a different contract than any open lot, so nothing
          // has it on the WS feed yet — ask for it once, then ticks keep the
          // cell live (see the tick-driven effect below) instead of polling.
          onSubscribeSymbol(far.exch, far.token)
          const q = await onFetchQuote(far.exch, far.token)
          const { farLtp, rollCost } = computeRollCost(lot, q)
          setRollPreviews(prev => ({
            ...prev,
            [lot.id]: { status: 'ready', tsym: far.tsym, expd: far.expd, exch: far.exch, token: far.token, farLtp, rollCost },
          }))
        } catch {
          setRollPreviews(prev => ({ ...prev, [lot.id]: { status: 'none' } }))
        }
      })()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showRollover, rollableKey])

  // Keep resolved cells live off the WS feed: whenever a subscribed far leg's
  // tick arrives, recompute its roll cost against the (also WS-updated) near
  // lot. Replaces polling /api/quote every 5s — the far leg is subscribed once
  // in the effect above and pushes ticks like any other symbol from then on.
  useEffect(() => {
    if (!showRollover) return
    setRollPreviews(prev => {
      let changed = false
      const next = { ...prev }
      for (const [lotIdStr, pv] of Object.entries(prev)) {
        if (pv.status !== 'ready') continue
        const q = tickMap[`${pv.exch}|${pv.token}`]
        if (!q) continue
        const lotId = Number(lotIdStr)
        const lot = lots.find(l => l.id === lotId)
        if (!lot || (lot.status !== 'OPEN' && lot.status !== 'PARTIAL')) continue
        const { farLtp, rollCost } = computeRollCost(lot, q)
        if (farLtp === pv.farLtp && rollCost === pv.rollCost) continue
        changed = true
        next[lotId] = { ...pv, farLtp, rollCost }
      }
      return changed ? next : prev
    })
  }, [showRollover, tickMap, lots])

  const filteredLots = lots
    .filter(l => !positionOrderFilter || l.tsym.toUpperCase().startsWith(positionOrderFilter.toUpperCase()))
    .filter(l => !orderSymbolFilter || l.tsym === orderSymbolFilter)
    .filter(l => !symbolQuery.trim() || l.tsym.toUpperCase().includes(symbolQuery.trim().toUpperCase()))
    // TE-tagged lots are kept visible even with "Hide closed" on — staying
    // reachable for re-entry/roll is the whole point of tagging them.
    .filter(l => !hideClosedLots || l.status !== 'CLOSED' || l.is_temp_exit)
    .filter(l => !exchFilter || l.exch === exchFilter)
    .filter(l => !sideFilter || l.side === sideFilter)
    .filter(l => !statusFilter || l.status === statusFilter)
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
          <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest">Orders (from database)</h2>
          {positionOrderFilter && (
            <button
              onClick={onClearPositionOrderFilter}
              className="flex items-center gap-1 text-xs px-2 py-0.5 rounded border border-violet-700 bg-violet-950 text-violet-300 hover:border-violet-500 transition-colors cursor-pointer"
            >
              {positionOrderFilter}
              <span className="text-violet-500">×</span>
            </button>
          )}
          {orderSymbolFilter && (
            <button
              onClick={() => onSetOrderSymbolFilter(null)}
              className="flex items-center gap-1 text-xs px-2 py-0.5 rounded border border-blue-700 bg-blue-950 text-blue-300 hover:border-blue-500 transition-colors cursor-pointer"
            >
              {orderSymbolFilter}
              <span className="text-blue-500">×</span>
            </button>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onSyncPositions}
            className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:border-amber-600 hover:text-amber-300 transition-colors cursor-pointer"
            title="Sync open positions from broker (imports lots placed outside Gateway)"
          >
            Sync Positions
          </button>
          <button
            onClick={onCleanupExternal}
            className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:border-red-700 hover:text-red-400 transition-colors cursor-pointer"
            title="Cancel EXT lots that don't match an actual broker position"
          >
            Cleanup
          </button>
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-gray-500">Hide closed</span>
            <Toggle enabled={hideClosedLots} disabled={false} onChange={() => setHideClosedLots(v => !v)} />
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-gray-500">Show rollover</span>
            <Toggle
              enabled={showRollover}
              disabled={false}
              onChange={() => {
                // Drop cached previews on turn-off so re-enabling pulls fresh quotes.
                if (showRollover) setRollPreviews({})
                setShowRollover(v => !v)
              }}
            />
          </div>
          <button
            onClick={onFetchOrders}
            disabled={loadingOrders}
            className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 disabled:opacity-50 transition-colors cursor-pointer"
          >
            ↻
          </button>
        </div>
      </div>

      {lots.length > 0 && (() => {
        const exchanges = Array.from(new Set(lots.map(l => l.exch))).sort()
        const statuses = Array.from(new Set(lots.map(l => l.status))).sort()
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

            {statuses.length > 1 && (
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
                    onClick={() => {
                      setStatusFilter(statusFilter === status ? null : status)
                      if (status === 'CLOSED') setHideClosedLots(false)
                    }}
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
            )}
          </div>
        )
      })()}

      {loadingOrders && !lots.length ? (
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
                <SortTh label="Expiry" sortKey="expiry" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="Lots" sortKey="lots" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="Qty (open/entry)" sortKey="qty" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="Entry" sortKey="entry" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="Exit" sortKey="exit" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="LTP" sortKey="ltp" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="Amount" sortKey="amount" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="MTM P&L" sortKey="mtm" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="Live P&L" sortKey="pnl" active={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                <SortTh label="Status" sortKey="status" active={sortKey} dir={sortDir} onSort={handleSort} />
                <SortTh label="Opened" sortKey="opened" active={sortKey} dir={sortDir} onSort={handleSort} />
                <th className="pb-2 pr-4 font-medium">Strategy</th>
                <th className="pb-2 pr-4 font-medium">Order Target</th>
                {showRollover && <th className="pb-2 pr-4 font-medium text-right">Rollover</th>}
                <th className="pb-2 font-medium text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {filteredLots.length > 0 ? filteredLots.map((lot) => {
                const openOrder = orders.find(o => o.norenordno === lot.broker_entry_orderid)
                const canExit = lot.status === 'OPEN' || lot.status === 'PARTIAL'
                const isPending = lot.status === 'PENDING'
                const hasPendingExit = !!lot.pending_exit_orderid
                return (
                  <tr key={lot.id} className="border-b border-gray-800/40">
                    <td className="py-2 pr-4">
                      <div className="flex items-center gap-1.5">
                        <button
                          onClick={() => onSetOrderSymbolFilter(orderSymbolFilter === lot.tsym ? null : lot.tsym)}
                          className={`font-mono text-sm cursor-pointer hover:text-white transition-colors ${orderSymbolFilter === lot.tsym ? 'text-blue-300' : 'text-gray-300'}`}
                        >
                          {lot.tsym}
                        </button>
                        {lot.is_external && (
                          <span className="text-xs px-1.5 py-0.5 rounded border bg-amber-950 text-amber-300 border-amber-800 font-mono">
                            EXT
                          </span>
                        )}
                        {lot.is_rollover && (
                          <span className="text-xs px-1.5 py-0.5 rounded border bg-teal-950 text-teal-300 border-teal-800 font-mono" title="Auto-placed by a futures rollover from the previous contract">
                            ROLL
                          </span>
                        )}
                        {lot.is_reentry && (
                          <span className="text-xs px-1.5 py-0.5 rounded border bg-indigo-950 text-indigo-300 border-indigo-800 font-mono" title="Placed via Re-entry — carries the prior lot's P&L forward">
                            RE
                          </span>
                        )}
                        {lot.is_temp_exit && (
                          <span className="text-xs px-1.5 py-0.5 rounded border bg-amber-950 text-amber-300 border-amber-800 font-mono" title="Temporary Exit — this closed lot is kept on the list across days so you can re-enter / roll it next session">
                            TE
                          </span>
                        )}
                        {lot.is_persistent && (
                          <span className="text-xs px-1.5 py-0.5 rounded border bg-violet-950 text-violet-300 border-violet-800 font-mono" title="Good Till Triggered — re-submitted daily until filled">
                            GTT
                          </span>
                        )}
                        {serviceBadge(lot.source_service) && (
                          <span className="text-xs px-1.5 py-0.5 rounded border bg-sky-950 text-sky-300 border-sky-800 font-mono" title={`Placed by the ${lot.source_service} service`}>
                            {serviceBadge(lot.source_service)}
                          </span>
                        )}
                      </div>
                      {lot.description && (
                        <div className="text-xs text-gray-500 mt-0.5 max-w-[240px] truncate" title={lot.description}>
                          {lot.description}
                        </div>
                      )}
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
                    <td className="py-2 pr-4 text-right font-mono text-gray-300">
                      {lot.lotsize > 1
                        ? (lot.open_qty / lot.lotsize).toFixed(lot.open_qty % lot.lotsize === 0 ? 0 : 2)
                        : <span className="text-gray-600">—</span>}
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
                      {lot.avg_exit_price && lot.avg_exit_price > 0 ? (
                        <span>
                          ₹{formatINR(lot.avg_exit_price)}
                          {lot.status === 'PARTIAL' && lot.exit_filled_qty ? (
                            <span className="text-xs text-gray-600 ml-1">({lot.exit_filled_qty})</span>
                          ) : null}
                        </span>
                      ) : (
                        <span className="text-gray-600">—</span>
                      )}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-gray-400">
                      {lot.ltp ? `₹${formatINR(lot.ltp)}` : <span className="text-gray-600">—</span>}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-gray-300">
                      ₹{formatINR(lot.open_qty * lot.avg_entry_price)}
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
                        <div
                          className="text-[10px] text-gray-500"
                          title={lot.is_reentry
                            ? 'Includes P&L carried forward from the prior closed order (re-entry)'
                            : 'Includes P&L carried from the rolled-over contract'}
                        >
                          incl. {fmtPnl(lot.carried_pnl)} {lot.is_reentry ? 'carried' : 'rolled'}
                        </div>
                      )}
                    </td>
                    <td className="py-2 pr-4">
                      {hasPendingExit ? (
                        <span className="text-xs px-2 py-0.5 rounded bg-orange-950 text-orange-300">
                          EXIT PENDING
                        </span>
                      ) : (
                        <span className={`text-xs px-2 py-0.5 rounded ${
                          lot.status === 'OPEN' ? 'bg-green-950 text-green-300' :
                          lot.status === 'PARTIAL' ? 'bg-yellow-950 text-yellow-300' :
                          lot.status === 'PENDING' ? 'bg-blue-950 text-blue-300' :
                          lot.status === 'CLOSED' ? 'bg-gray-800 text-gray-400' :
                          'bg-red-950 text-red-300'
                        }`}>
                          {lot.status}
                        </span>
                      )}
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
                    <td className="py-2 pr-4">
                      <div className="flex items-center gap-1.5">
                        {lot.strategy_name ? (
                          <span className="text-xs text-gray-300 max-w-[120px] truncate" title={lot.strategy_name}>
                            {lot.strategy_name}
                          </span>
                        ) : (
                          <span className="text-xs text-gray-600">—</span>
                        )}
                        <button
                          onClick={() => setStrategyModal({ lot, value: lot.strategy_name || '' })}
                          disabled={savingLotStrategy === lot.id}
                          className="text-gray-500 hover:text-gray-200 disabled:opacity-40 transition-colors cursor-pointer p-0.5"
                          title="Set strategy name"
                          aria-label="Set strategy name"
                        >
                          <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                        </button>
                      </div>
                    </td>
                    <td className="py-2 pr-4">
                      {canExit || isPending ? (() => {
                        const targetOn = !!lot.target_enabled
                        const saving = savingLotTarget === lot.id
                        return (
                          <div className="flex items-center gap-2">
                            {targetOn && (
                              <>
                                <div className="flex flex-col items-end leading-tight">
                                  <span className="font-mono text-xs text-amber-400">
                                    ₹{formatINR(lot.target_value || 0, 0)}
                                  </span>
                                  {(() => {
                                    const tp = lotTargetTriggerPrice(lot, lot.target_value || 0)
                                    return tp !== null ? (
                                      <span className="text-[10px] text-white font-mono">@ ₹{formatINR(tp)}</span>
                                    ) : null
                                  })()}
                                </div>
                                <button
                                  onClick={() => setTargetModal({ lot, value: String(lot.target_value || 1000), editing: true })}
                                  disabled={saving}
                                  className="text-xs text-gray-400 hover:text-gray-200 disabled:opacity-40 transition-colors cursor-pointer"
                                  title="Edit order target"
                                >
                                  <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                                </button>
                              </>
                            )}
                            <Toggle
                              enabled={targetOn}
                              disabled={saving}
                              onChange={() => {
                                if (targetOn) {
                                  onUpdateLotTarget(lot.id, { enabled: false })
                                } else {
                                  setTargetModal({ lot, value: String(lot.target_value || 1000), editing: false })
                                }
                              }}
                            />
                          </div>
                        )
                      })() : (
                        <span className="text-xs text-gray-600">—</span>
                      )}
                    </td>
                    {showRollover && (
                      <td className="py-2 pr-4 text-right font-mono align-top">
                        {(() => {
                          if (!canExit) return <span className="text-gray-600">—</span>
                          const pv = rollPreviews[lot.id]
                          if (!pv || pv.status === 'loading') return <span className="text-gray-600">…</span>
                          if (pv.status === 'none') return <span className="text-gray-600">—</span>
                          return (
                            <div className="flex flex-col items-end leading-tight gap-0.5" title={pv.expd ? `Roll into ${pv.tsym} · ${pv.expd}` : `Roll into ${pv.tsym}`}>
                              <span className="text-xs text-gray-300">{pv.tsym}</span>
                              <span className={pv.rollCost > 0 ? 'text-xs text-red-400' : 'text-xs text-green-400'}>
                                {fmtSigned(pv.rollCost, 0)} {pv.rollCost > 0 ? 'debit' : 'credit'}
                              </span>
                              <span className="text-[10px] text-gray-500">₹{formatINR(pv.farLtp)}</span>
                            </div>
                          )
                        })()}
                      </td>
                    )}
                    <td className="py-2 text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        {hasPendingExit && (
                          <button
                            onClick={() => onCancelExitOrder(lot)}
                            className="text-xs px-2 py-1 rounded border border-orange-700 text-orange-400 hover:border-orange-600 hover:text-orange-300 transition-colors cursor-pointer"
                          >
                            Cancel Exit
                          </button>
                        )}
                        {canExit && !hasPendingExit && (
                          <>
                            <button
                              onClick={() => onReentryLot(lot)}
                              className="text-xs px-2 py-1 rounded border border-emerald-700 text-emerald-400 hover:border-emerald-600 hover:text-emerald-300 transition-colors cursor-pointer"
                              title="Place a new order with the same side, qty, and price"
                            >
                              Re-entry
                            </button>
                            <button
                              onClick={() => { setModalTempExit(false); setModalLot(lot) }}
                              disabled={exitingLot === lot.id}
                              className="text-xs px-2 py-1 rounded border border-red-700 text-red-400 hover:border-red-600 hover:text-red-300 disabled:opacity-50 transition-colors cursor-pointer"
                            >
                              {exitingLot === lot.id ? '…' : 'Exit'}
                            </button>
                            <button
                              onClick={() => setRolloverLot(lot)}
                              disabled={rollingLot === lot.id}
                              className="text-xs px-2 py-1 rounded border border-amber-700 text-amber-400 hover:border-amber-600 hover:text-amber-300 disabled:opacity-50 transition-colors cursor-pointer"
                              title="Roll to the next expiry: close this contract and re-open the same position in the far month"
                            >
                              {rollingLot === lot.id ? '…' : 'Rollover'}
                            </button>
                          </>
                        )}
                        {lot.status === 'CLOSED' && (
                          <>
                            <button
                              onClick={() => onSetTempExit(lot.id, !lot.is_temp_exit)}
                              className={`text-xs px-2 py-1 rounded border transition-colors cursor-pointer ${
                                lot.is_temp_exit
                                  ? 'border-amber-600 bg-amber-950 text-amber-300 hover:border-amber-500'
                                  : 'border-gray-700 text-gray-400 hover:border-amber-700 hover:text-amber-300'
                              }`}
                              title={lot.is_temp_exit
                                ? 'Temporary Exit is on — this closed lot stays on the list across days. Click to clear.'
                                : 'Mark as Temporary Exit — keep this closed lot on the list so you can re-enter / roll it next session.'}
                            >
                              {lot.is_temp_exit ? 'TE ✓' : 'TE'}
                            </button>
                            <button
                              onClick={() => onReentryLot(lot)}
                              className="text-xs px-2 py-1 rounded border border-emerald-700 text-emerald-400 hover:border-emerald-600 hover:text-emerald-300 transition-colors cursor-pointer"
                              title="Place a new order with the same side, qty, and price"
                            >
                              Re-entry
                            </button>
                          </>
                        )}
                        {isPending && openOrder && (
                          <>
                            <button
                              onClick={() => setModifyModal({ lot, openOrder, price: openOrder.price })}
                              className="text-xs px-2 py-1 rounded border border-blue-700 text-blue-400 hover:border-blue-500 hover:text-blue-300 transition-colors cursor-pointer"
                            >
                              Edit
                            </button>
                            <button
                              onClick={() => onCancelOrder(openOrder.norenordno)}
                              className="text-xs px-2 py-1 rounded border border-red-700 text-red-400 hover:border-red-600 hover:text-red-300 transition-colors cursor-pointer"
                            >
                              Cancel
                            </button>
                          </>
                        )}
                        <button
                          onClick={() => setDeleteLot(lot)}
                          className="text-gray-600 hover:text-red-400 transition-colors cursor-pointer p-1"
                          title="Delete this lot from the database (does not touch the broker)"
                          aria-label="Delete lot"
                        >
                          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              }) : (
                <tr>
                  <td colSpan={showRollover ? 16 : 15} className="py-4 text-center text-sm text-gray-500">No lots match the current filter</td>
                </tr>
              )}
            </tbody>
            {filteredLots.length > 0 && (
              <tfoot>
                {(() => {
                  const totalLive = filteredLots.reduce((s, l) => s + (l.live_pnl || 0), 0)
                  const totalMtm = filteredLots.reduce((s, l) => s + (l.mtm_pnl || 0), 0)
                  const totalAmount = filteredLots.reduce((s, l) => s + l.open_qty * l.avg_entry_price, 0)
                  return (
                    <tr className="border-t-2 border-gray-700 bg-gray-800/30">
                      <td className="pt-2 pr-4 text-xs text-gray-500 font-semibold">Total</td>
                      <td colSpan={6} />
                      <td className="pt-2 pr-4 text-right font-mono font-semibold text-sm text-gray-200">
                        ₹{formatINR(totalAmount)}
                      </td>
                      <td className={`pt-2 pr-4 text-right font-mono font-semibold text-sm ${totalMtm >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {fmtPnl(totalMtm)}
                      </td>
                      <td className={`pt-2 pr-4 text-right font-mono font-semibold text-sm ${totalLive >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {fmtPnl(totalLive)}
                      </td>
                      <td colSpan={3} />
                    </tr>
                  )
                })()}
              </tfoot>
            )}
          </table>
        </div>
      ) : (
        <p className="text-sm text-gray-500 text-center py-4">No tracked lots</p>
      )}

      {modalLot && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setModalLot(null)}>
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 w-96 space-y-4" onClick={e => e.stopPropagation()}>
            <h3 className="text-sm font-semibold text-gray-200">Exit lot · {modalLot.tsym}</h3>
            <div className="text-xs text-gray-400 space-y-1">
              <div>Side: <span className={modalLot.side === 'B' ? 'text-blue-300' : 'text-red-300'}>{modalLot.side === 'B' ? 'BUY (long)' : 'SELL (short)'}</span></div>
              <div>Open qty: <span className="text-gray-200 font-mono">{modalLot.open_qty}</span> (entry {modalLot.entry_qty})</div>
              <div>Entry price: <span className="text-gray-200 font-mono">₹{formatINR(modalLot.avg_entry_price)}</span></div>
              <div>Live P&amp;L: <span className={modalLot.live_pnl >= 0 ? 'text-green-400' : 'text-red-400'}>{fmtPnl(modalLot.live_pnl)}</span></div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-gray-500 mb-1">Quantity</label>
                <input
                  type="number"
                  min={1}
                  max={modalLot.open_qty}
                  value={modalQty}
                  onChange={e => setModalQty(Math.max(1, Math.min(modalLot.open_qty, parseInt(e.target.value) || 1)))}
                  className="w-full px-3 py-2 bg-gray-950 border border-gray-800 rounded text-sm text-gray-200 font-mono focus:border-gray-600 outline-none"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">Limit price</label>
                <input
                  type="number"
                  step="0.05"
                  min={0}
                  value={modalPrice}
                  onChange={e => setModalPrice(Math.max(0, parseFloat(e.target.value) || 0))}
                  className="w-full px-3 py-2 bg-gray-950 border border-gray-800 rounded text-sm text-gray-200 font-mono focus:border-gray-600 outline-none"
                />
              </div>
            </div>
            <label className="flex items-start gap-2 text-xs text-gray-400 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={modalTempExit}
                onChange={e => setModalTempExit(e.target.checked)}
                className="mt-0.5 accent-amber-500 cursor-pointer"
              />
              <span>
                <span className="text-amber-300 font-mono">Temporary exit</span> — keep this
                row on the list after it closes so you can re-enter / roll it next session.
              </span>
            </label>
            <div className="flex items-center justify-end gap-2 pt-2">
              <button
                onClick={() => setModalLot(null)}
                className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={() => { onExitLot(modalLot, modalQty, modalPrice, modalTempExit); setModalLot(null) }}
                disabled={exitingLot === modalLot.id}
                className="text-xs px-3 py-1.5 rounded bg-red-900 border border-red-700 text-red-200 hover:bg-red-800 disabled:opacity-50 transition-colors cursor-pointer"
              >
                Exit {modalQty} @ ₹{formatINR(modalPrice)}
              </button>
            </div>
          </div>
        </div>
      )}

      {rolloverLot && (() => {
        // Resolve the live lot (open_qty/side/target_enabled can move while the
        // modal is open) — fall back to the snapshot if it's been fully closed
        // out from under the modal.
        const lot = lots.find(l => l.id === rolloverLot.id) ?? rolloverLot
        const nearPx = rollPriceType === 'LMT' ? rollExitPrice : rollNearPrice
        const farPx = rollPriceType === 'LMT' ? rollEntryPrice : (rollFarQuote?.lp ?? 0)
        // Roll cost per unit is what leaves your pocket to carry forward:
        // long → pay far, receive near; short → the reverse. Positive = debit.
        const rollCostPerUnit = lot.side === 'B' ? (farPx - nearPx) : (nearPx - farPx)
        const rollCostRupees = rollCostPerUnit * rollQty * (lot.prcftr || 1)
        const nearTargetEnabled = !!lot.target_enabled
        const noTargets = !rollLoadingTargets && rollTargets.length === 0
        // A limit roll with no far-leg price would go out at ₹0 and be rejected by
        // the broker — but only AFTER the near exit fills, leaving you flat with an
        // unplaced far leg. Block it here so the roll can't start in that state.
        const farPriceMissing = rollPriceType === 'LMT' && rollEntryPrice <= 0
        const confirmDisabled = rollingLot === lot.id || !rollTargetTsym || rollLoadingTargets || farPriceMissing
        return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setRolloverLot(null)}>
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 w-[26rem] space-y-4" onClick={e => e.stopPropagation()}>
            <h3 className="text-sm font-semibold text-gray-200">Rollover · {lot.tsym}</h3>

            {/* Near → Far summary */}
            <div className="flex items-center justify-between gap-3 text-xs">
              <div className="flex-1 rounded border border-gray-800 bg-gray-950 p-2.5">
                <div className="text-gray-500 mb-1">Close (near)</div>
                <div className="font-mono text-sm text-gray-200">{lot.tsym}</div>
                <div className="text-gray-500 mt-0.5">{lot.expd || '—'}</div>
                <div className="mt-1 text-gray-400">LTP <span className="font-mono text-gray-200">₹{formatINR(rollNearPrice)}</span></div>
              </div>
              <div className="text-amber-400 text-lg">→</div>
              <div className="flex-1 rounded border border-amber-900/60 bg-amber-950/20 p-2.5">
                <div className="text-gray-500 mb-1">Open (far)</div>
                {noTargets ? (
                  <div className="text-amber-300 text-xs">No later expiry listed</div>
                ) : (
                  <select
                    value={rollTargetTsym}
                    onChange={e => { setRollTargetTsym(e.target.value); setFarPriceDirty(false) }}
                    disabled={rollLoadingTargets}
                    className="w-full px-2 py-1 bg-gray-950 border border-gray-800 rounded text-sm text-gray-200 font-mono focus:border-amber-700 outline-none cursor-pointer"
                  >
                    {rollLoadingTargets && <option>Loading…</option>}
                    {rollTargets.map(t => (
                      <option key={t.tsym} value={t.tsym}>{t.tsym}</option>
                    ))}
                  </select>
                )}
                <div className="text-gray-500 mt-0.5">{rollTargets.find(t => t.tsym === rollTargetTsym)?.expd || '—'}</div>
                <div className="mt-1 text-gray-400">LTP <span className="font-mono text-gray-200">{rollFarQuote ? `₹${formatINR(rollFarQuote.lp)}` : '…'}</span></div>
              </div>
            </div>

            {/* Roll cost */}
            {!noTargets && (
              <div className="flex items-center justify-between text-xs rounded border border-gray-800 bg-gray-950 px-3 py-2">
                <span className="text-gray-500">Roll spread (far − near)</span>
                <span className="font-mono text-gray-300">{fmtSigned(farPx - nearPx)}</span>
              </div>
            )}
            {!noTargets && (
              <div className="flex items-center justify-between text-xs px-3">
                <span className="text-gray-500">Est. roll cost · {rollQty} × ₹{formatINR(lot.prcftr || 1, 0)}/pt</span>
                <span className={`font-mono ${rollCostRupees > 0 ? 'text-red-400' : 'text-green-400'}`}>
                  {fmtSigned(rollCostRupees, 0)} {rollCostRupees > 0 ? 'debit' : 'credit'}
                </span>
              </div>
            )}

            {/* Side + qty */}
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-gray-500 mb-1">Side</label>
                <div className={`px-3 py-2 rounded text-sm text-center ${lot.side === 'B' ? 'bg-blue-950 text-blue-300' : 'bg-red-950 text-red-300'}`}>
                  {lot.side === 'B' ? 'BUY (long)' : 'SELL (short)'}
                </div>
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">Quantity (open {lot.open_qty})</label>
                <input
                  type="number"
                  min={1}
                  max={lot.open_qty}
                  value={rollQty}
                  onChange={e => setRollQty(Math.max(1, Math.min(lot.open_qty, parseInt(e.target.value) || 1)))}
                  className="w-full px-3 py-2 bg-gray-950 border border-gray-800 rounded text-sm text-gray-200 font-mono focus:border-gray-600 outline-none"
                />
              </div>
            </div>

            {/* Pricing */}
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <label className="text-xs text-gray-500">Pricing</label>
                <div className="flex rounded border border-gray-800 overflow-hidden text-xs">
                  {(['LMT', 'MKT'] as const).map(pt => (
                    <button
                      key={pt}
                      onClick={() => setRollPriceType(pt)}
                      className={`px-3 py-1 transition-colors cursor-pointer ${rollPriceType === pt ? 'bg-amber-900/60 text-amber-200' : 'text-gray-500 hover:text-gray-300'}`}
                    >
                      {pt === 'LMT' ? 'Limit' : 'Market'}
                    </button>
                  ))}
                </div>
              </div>
              {rollPriceType === 'LMT' ? (
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="flex items-center justify-between mb-1">
                      <span className="text-xs text-gray-500">Exit price (near)</span>
                      {nearPriceDirty ? (
                        <button type="button" onClick={() => setNearPriceDirty(false)}
                          className="text-[10px] text-amber-400 hover:text-amber-300 cursor-pointer">Use live</button>
                      ) : (
                        <span className="text-[10px] text-green-500">live</span>
                      )}
                    </label>
                    <input
                      type="number"
                      step="0.05"
                      min={0}
                      value={rollExitPrice}
                      onChange={e => { setRollExitPrice(Math.max(0, parseFloat(e.target.value) || 0)); setNearPriceDirty(true) }}
                      className="w-full px-3 py-2 bg-gray-950 border border-gray-800 rounded text-sm text-gray-200 font-mono focus:border-gray-600 outline-none"
                    />
                  </div>
                  <div>
                    <label className="flex items-center justify-between mb-1">
                      <span className="text-xs text-gray-500">Entry price (far)</span>
                      {farPriceDirty ? (
                        <button type="button" onClick={() => setFarPriceDirty(false)}
                          className="text-[10px] text-amber-400 hover:text-amber-300 cursor-pointer">Use live</button>
                      ) : (
                        <span className="text-[10px] text-green-500">live</span>
                      )}
                    </label>
                    <input
                      type="number"
                      step="0.05"
                      min={0}
                      value={rollEntryPrice}
                      onChange={e => { setRollEntryPrice(Math.max(0, parseFloat(e.target.value) || 0)); setFarPriceDirty(true) }}
                      className="w-full px-3 py-2 bg-gray-950 border border-gray-800 rounded text-sm text-gray-200 font-mono focus:border-gray-600 outline-none"
                    />
                  </div>
                </div>
              ) : (
                <p className="text-xs text-gray-500">Both legs fire at market — instant, guaranteed roll. You accept the spread at market.</p>
              )}
              <p className="text-xs text-gray-600 mt-1">
                {rollTargetTsym || 'The far contract'} is placed <span className="text-gray-500">automatically</span> once {lot.tsym} fills — however long that takes.
                {rollPriceType === 'LMT' && ' A limit exit that never fills holds the roll — use Market to guarantee the close.'}
              </p>
            </div>

            {/* Carry running P&L onto the far contract */}
            <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer">
              <input
                type="checkbox"
                checked={rollCarryPnl}
                onChange={e => setRollCarryPnl(e.target.checked)}
                className="accent-amber-500 cursor-pointer"
              />
              Carry P&amp;L forward — {rollTargetTsym || 'far contract'} continues from this position's result (and counts toward its target)
            </label>

            {/* Carry target */}
            {nearTargetEnabled && (
              <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer">
                <input
                  type="checkbox"
                  checked={rollCarry}
                  onChange={e => setRollCarry(e.target.checked)}
                  className="accent-amber-500 cursor-pointer"
                />
                Carry order target to {rollTargetTsym || 'far contract'}
              </label>
            )}

            {/* Why the roll is blocked: a limit far leg with no price would be
                rejected at ₹0 after the near exit already closed. */}
            {farPriceMissing && !noTargets && (
              <p className="text-xs text-amber-300 bg-amber-950/30 border border-amber-900/50 rounded px-3 py-2">
                No live price for {rollTargetTsym || 'the far contract'} yet — the far month isn't quoting.
                Enter a far-leg entry price, or switch to <span className="font-semibold">Market</span>.
                A limit roll at ₹0 would be rejected and leave you flat on {lot.tsym}.
              </p>
            )}

            <div className="flex items-center justify-end gap-2 pt-1">
              <button
                onClick={() => setRolloverLot(null)}
                className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  onRolloverLot(lot, {
                    target_tsym: rollTargetTsym,
                    qty: rollQty,
                    price_type: rollPriceType,
                    exit_price: rollPriceType === 'LMT' ? rollExitPrice : 0,
                    entry_price: rollPriceType === 'LMT' ? rollEntryPrice : 0,
                    carry_target: rollCarry,
                    carry_pnl: rollCarryPnl,
                  })
                  setRolloverLot(null)
                }}
                disabled={confirmDisabled}
                className="text-xs px-3 py-1.5 rounded bg-amber-900 border border-amber-700 text-amber-200 hover:bg-amber-800 disabled:opacity-50 transition-colors cursor-pointer"
              >
                Roll {rollQty} → {rollTargetTsym || '…'}
              </button>
            </div>
          </div>
        </div>
        )
      })()}

      {deleteLot && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setDeleteLot(null)}>
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 w-96 space-y-4" onClick={e => e.stopPropagation()}>
            <h3 className="text-sm font-semibold text-gray-200">Delete lot · {deleteLot.tsym}</h3>
            <div className="text-xs text-gray-400 space-y-1">
              <div>Side: <span className={deleteLot.side === 'B' ? 'text-blue-300' : 'text-red-300'}>{deleteLot.side === 'B' ? 'BUY (long)' : 'SELL (short)'}</span></div>
              <div>Qty: <span className="text-gray-200 font-mono">{deleteLot.open_qty}/{deleteLot.entry_qty}</span></div>
              <div>Entry: <span className="text-gray-200 font-mono">₹{formatINR(deleteLot.avg_entry_price)}</span></div>
              <div>Status: <span className="text-gray-200 font-mono">{deleteLot.status}</span>{deleteLot.is_external && <span className="ml-1 text-amber-300">· EXT</span>}{deleteLot.is_rollover && <span className="ml-1 text-teal-300">· ROLL</span>}{deleteLot.is_reentry && <span className="ml-1 text-indigo-300">· RE</span>}</div>
            </div>
            <p className="text-xs text-gray-500">
              Removes this lot from the Gateway database only. It does <span className="text-gray-300">not</span> touch your broker.
              If this is a real open position, the next <span className="text-gray-300">Sync Positions</span> will re-import it with the broker's correct values.
            </p>
            <div className="flex items-center justify-end gap-2 pt-2">
              <button
                onClick={() => setDeleteLot(null)}
                className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={() => { onDeleteLot(deleteLot.id); setDeleteLot(null) }}
                className="text-xs px-3 py-1.5 rounded bg-red-900 border border-red-700 text-red-200 hover:bg-red-800 transition-colors cursor-pointer"
              >
                Delete lot
              </button>
            </div>
          </div>
        </div>
      )}

      {targetModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setTargetModal(null)}>
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 w-96 space-y-4" onClick={e => e.stopPropagation()}>
            <h3 className="text-sm font-semibold text-gray-200">
              {targetModal.editing ? 'Update Order Target' : 'Set Order Target'} · {targetModal.lot.tsym}
            </h3>
            <div className="text-xs text-gray-400 space-y-1">
              <div>Exchange: <span className="text-gray-200 font-mono">{targetModal.lot.exch}</span></div>
              <div>This order: <span className={targetModal.lot.side === 'B' ? 'text-blue-300' : 'text-red-300'}>{targetModal.lot.side === 'B' ? 'BUY (long)' : 'SELL (short)'}</span> qty <span className="text-gray-200 font-mono">{targetModal.lot.open_qty}</span> @ ₹{formatINR(targetModal.lot.avg_entry_price)}</div>
              <div>This order's live P&amp;L: <span className={targetModal.lot.live_pnl >= 0 ? 'text-green-400' : 'text-red-400'}>{fmtPnl(targetModal.lot.live_pnl)}</span></div>
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">This order's P&amp;L target (₹)</label>
              <input
                type="number"
                step="1"
                autoFocus
                value={targetModal.value}
                onChange={e => setTargetModal(m => m ? { ...m, value: e.target.value } : m)}
                className="w-full px-3 py-2 bg-gray-950 border border-gray-800 rounded text-sm text-gray-200 font-mono focus:border-gray-600 outline-none"
              />
              {(() => {
                const v = parseFloat(targetModal.value)
                const tp = Number.isFinite(v) && v > 0 ? lotTargetTriggerPrice(targetModal.lot, v) : null
                return tp !== null ? (
                  <p className="text-[11px] text-amber-400/80 mt-1.5">
                    Fires when price reaches ≈ ₹{formatINR(tp)}
                  </p>
                ) : null
              })()}
              <p className="text-xs text-gray-500 mt-1">
                Exits <span className="text-gray-300">only this order</span> ({targetModal.lot.open_qty} qty) when
                its own live P&amp;L reaches the target. Independent of other {targetModal.lot.tsym} orders
                and of the Positions-card target.
              </p>
            </div>
            <div className="flex items-center justify-end gap-2 pt-2">
              <button
                onClick={() => setTargetModal(null)}
                className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  const v = parseFloat(targetModal.value)
                  if (!Number.isFinite(v) || v <= 0) return
                  onUpdateLotTarget(targetModal.lot.id, { enabled: true, target_value: v })
                  setTargetModal(null)
                }}
                disabled={savingLotTarget === targetModal.lot.id}
                className="text-xs px-3 py-1.5 rounded bg-amber-900 border border-amber-700 text-amber-200 hover:bg-amber-800 disabled:opacity-50 transition-colors cursor-pointer"
              >
                {targetModal.editing ? 'Update Target' : 'Enable Target'}
              </button>
            </div>
          </div>
        </div>
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
                list="strategy-name-suggestions"
                autoFocus
                value={strategyModal.value}
                onChange={e => setStrategyModal(m => m ? { ...m, value: e.target.value } : m)}
                className="w-full px-3 py-2 bg-gray-950 border border-gray-800 rounded text-sm text-gray-200 focus:border-gray-600 outline-none"
                placeholder="e.g. Iron Condor"
              />
              <datalist id="strategy-name-suggestions">
                {strategyNames.map(name => <option key={name} value={name} />)}
              </datalist>
            </div>
            <div className="flex items-center justify-end gap-2 pt-2">
              {strategyModal.lot.strategy_name && (
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
              )}
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

      {modifyModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setModifyModal(null)}>
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 w-96 space-y-4" onClick={e => e.stopPropagation()}>
            <h3 className="text-sm font-semibold text-gray-200">Modify order · {modifyModal.lot.tsym}</h3>
            <div className="text-xs text-gray-400 space-y-1">
              <div>Side: <span className={modifyModal.lot.side === 'B' ? 'text-blue-300' : 'text-red-300'}>{modifyModal.lot.side === 'B' ? 'BUY' : 'SELL'}</span></div>
              <div>Qty: <span className="text-gray-200 font-mono">{modifyModal.openOrder.qty}</span></div>
              <div>Current price: <span className="text-gray-200 font-mono">₹{formatINR(parseFloat(modifyModal.openOrder.price))}</span></div>
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">New limit price (₹)</label>
              <input
                type="number"
                step="0.05"
                min={0}
                autoFocus
                value={modifyModal.price}
                onChange={e => setModifyModal(m => m ? { ...m, price: e.target.value } : m)}
                className="w-full px-3 py-2 bg-gray-950 border border-gray-800 rounded text-sm text-gray-200 font-mono focus:border-gray-600 outline-none"
              />
            </div>
            <div className="flex items-center justify-end gap-2 pt-2">
              <button
                onClick={() => setModifyModal(null)}
                className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  const newPrice = parseFloat(modifyModal.price)
                  if (!Number.isFinite(newPrice) || newPrice <= 0) return
                  onModifyOrder(
                    modifyModal.openOrder.norenordno,
                    modifyModal.openOrder.exch,
                    modifyModal.openOrder.tsym,
                    parseInt(modifyModal.openOrder.qty),
                    newPrice,
                  )
                  setModifyModal(null)
                }}
                className="text-xs px-3 py-1.5 rounded bg-blue-900 border border-blue-700 text-blue-200 hover:bg-blue-800 transition-colors cursor-pointer"
              >
                Modify @ ₹{formatINR(parseFloat(modifyModal.price || '0'))}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
