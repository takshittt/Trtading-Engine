import { useState, useEffect, useCallback, useRef } from 'react'
import type {
  UserInfo, PositionItem, PositionsSummary,
  OrderItem, OrderBookResponse, OrderLot, ScripSearchResult, ScripSearchResponse,
  QuoteResponse, WatchlistItem, OptionChainData, SymbolTarget, ExchangeTarget, FundsInfo, RolloverPayload, RolloverFailure,
} from './types'
import { apiFetch, getToken, wsUrl, getExchangeTargets, updateExchangeTarget, getSymbolTargets, updateSymbolTarget, updateLotTarget, updateLotStrategy, getStrategyNames, setLotTempExit, createPersistentOrder, getLotHistory, clearCancelledHistory, getRollTargets, rolloverLot, getQuote, getFailedRollovers, ackRollover } from './api'
import { useAuth } from './auth/AuthContext'
import { openedTodayIST } from './utils/format'
import { usePersistedState } from './hooks/usePersistedState'
import Header from './components/Header'
import MarketWatch from './components/MarketWatch'
import PositionsCard from './components/PositionsCard'
import OrdersCard from './components/OrdersCard'
import StrategyOrdersCard from './components/StrategyOrdersCard'
import BrokerOrdersCard from './components/BrokerOrdersCard'
import OrderHistoryCard from './components/OrderHistoryCard'
import OrderModal from './components/OrderModal'
import OptionChainModal from './components/OptionChainModal'
import SettingsModal from './components/SettingsModal'
import SubscribedTokensModal from './components/SubscribedTokensModal'

export default function App() {
  const { user: authUser, signout } = useAuth()
  const [connected, setConnected] = useState<boolean | null>(null)
  const [subscribedCount, setSubscribedCount] = useState<number | null>(null)

  const [connecting, setConnecting] = useState(false)
  const [user, setUser] = useState<UserInfo | null>(null)
  const [loadingData, setLoadingData] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // Set when the gateway reports its broker session expired (auto-reconnect can't
  // fix that — a human must re-Connect). Cleared once price ticks resume.
  const [feedExpired, setFeedExpired] = useState<string | null>(null)
  const [failedRollovers, setFailedRollovers] = useState<RolloverFailure[]>([])
  const [positionsSummary, setPositionsSummary] = useState<PositionsSummary | null>(null)
  const [loadingPositions, setLoadingPositions] = useState(false)
  const [orders, setOrders] = useState<OrderItem[]>([])
  const [lots, setLots] = useState<OrderLot[]>([])
  const [exitingLot, setExitingLot] = useState<number | null>(null)
  const [rollingLot, setRollingLot] = useState<number | null>(null)
  const [loadingOrders, setLoadingOrders] = useState(false)
  const [orderSymbolFilter, setOrderSymbolFilter] = useState<string | null>(null)
  const [orderHistory, setOrderHistory] = useState<OrderLot[]>([])
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [historyHasMore, setHistoryHasMore] = useState(true)
  const HISTORY_PAGE_SIZE = 200
  // Market watch
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([])
  const [watchSearch, setWatchSearch] = useState('')
  const [watchSuggestions, setWatchSuggestions] = useState<ScripSearchResult[]>([])
  const [showWatchSuggestions, setShowWatchSuggestions] = useState(false)
  const [watchLoading, setWatchLoading] = useState(false)
  const [niftyData, setNiftyData] = useState<{ ltp: string; close: string } | null>(null)
  // Any WS tick, keyed "EXCH|TOKEN" — lets a component (e.g. the rollover
  // preview column) read a live price for a symbol it asked to subscribe,
  // without polling REST once the feed is already pushing it.
  const [tickMap, setTickMap] = useState<Record<string, { lp: string; bp1: string; sp1: string }>>({})
  const wsRef = useRef<WebSocket | null>(null)
  const [wsLive, setWsLive] = useState(false)
  const [wsGeneration, setWsGeneration] = useState(0)
  const wsRetryDelayRef = useRef(1_000)
  const watchlistRef = useRef<WatchlistItem[]>([])
  const positionTokensRef = useRef<string[]>([])
  // Header user dropdown
  const [showUserDropdown, setShowUserDropdown] = useState(false)
  // Settings (market hours) modal
  const [showSettings, setShowSettings] = useState(false)
  // Order modal
  const [showOrderModal, setShowOrderModal] = useState(false)
  const [orderItem, setOrderItem] = useState<WatchlistItem | null>(null)
  const [orderSide, setOrderSide] = useState<'B' | 'S'>('B')
  const [orderQty, setOrderQty] = useState(1)
  const [orderLots, setOrderLots] = useState(1)
  const [orderPrice, setOrderPrice] = useState(0)
  const [orderType, setOrderType] = useState<'LMT' | 'MKT'>('LMT')
  const [orderProduct, setOrderProduct] = useState<'I' | 'M'>('M')
  const [orderHoldForever, setOrderHoldForever] = useState(false)
  const [orderTpEnabled, setOrderTpEnabled] = useState(false)
  const [orderTpValue, setOrderTpValue] = useState(0)
  const [orderDescription, setOrderDescription] = useState('')
  const [reentrySourceLotId, setReentrySourceLotId] = useState<number | null>(null)
  const [placingOrder, setPlacingOrder] = useState(false)
  const [exitingPosition, setExitingPosition] = useState<string | null>(null)
  // Subscribed-tokens modal
  const [showSubscribedModal, setShowSubscribedModal] = useState(false)
  // Option chain modal
  const [showChainModal, setShowChainModal] = useState(false)
  const [chainItem, setChainItem] = useState<WatchlistItem | null>(null)
  const [chainData, setChainData] = useState<OptionChainData | null>(null)
  const [chainLoading, setChainLoading] = useState(false)
  const [chainError, setChainError] = useState<string | null>(null)
  const [chainExpiry, setChainExpiry] = useState('')
  // Global total P&L target
  const [exchangeTargets, setExchangeTargets] = useState<Map<string, ExchangeTarget>>(new Map())
  const [savingExchangeTarget, setSavingExchangeTarget] = useState<string | null>(null)
  // Per-symbol target P&L config — keyed by "EXCH|TSYM"
  const [symbolTargets, setSymbolTargets] = useState<Map<string, SymbolTarget>>(new Map())
  const [savingTarget, setSavingTarget] = useState<string | null>(null)
  const [savingLotTarget, setSavingLotTarget] = useState<number | null>(null)
  const [savingLotStrategy, setSavingLotStrategy] = useState<number | null>(null)
  const [strategyNames, setStrategyNames] = useState<string[]>([])
  // Funds / margin
  const [funds, setFunds] = useState<FundsInfo | null>(null)

  const [positionOrderFilter, setPositionOrderFilter] = useState<string | null>(null)
  const [marketWatchCollapsed, setMarketWatchCollapsed] = usePersistedState('marketWatch.collapsed', false)

  // Watchlist ref so WS reconnects can resubscribe without re-running the WS effect
  useEffect(() => { watchlistRef.current = watchlist }, [watchlist])

  // WebSocket lifecycle — open when connected, close on disconnect.
  // On an unexpected drop, onclose bumps wsGeneration to re-run this effect
  // (reconnect) with exponential backoff; wsLive drives the stale-feed badge.
  useEffect(() => {
    if (!connected) {
      wsRef.current?.close()
      wsRef.current = null
      setWsLive(false)
      setWsGeneration(0)
      return
    }
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    // The ticker WS authenticates via ?token= (browsers can't set WS headers).
    const wsToken = getToken()
    const ws = new WebSocket(wsUrl(`/api/ws/ticker?token=${encodeURIComponent(wsToken ?? '')}`))
    wsRef.current = ws

    ws.onopen = () => {
      wsRetryDelayRef.current = 1_000
      setWsLive(true)
      const symbols = [
        'NSE|26000',
        ...watchlistRef.current.map(item => `${item.exch}|${item.token}`),
        ...positionTokensRef.current,
      ].filter(Boolean)
      ws.send(JSON.stringify({ action: 'subscribe', symbols }))
      if (wsGeneration > 0) {
        // Reconnected after a drop — order_update pushes were missed meanwhile.
        fetchLots()
        fetchOrders()
        fetchPositions()
      }
    }

    // Fetch initial Nifty quote
    apiFetch<QuoteResponse>('/quote?exchange=NSE&token=26000')
      .then(q => setNiftyData({ ltp: q.lp || '', close: q.c || '' }))
      .catch(() => {})

    ws.onmessage = (event) => {
      const tick = JSON.parse(event.data)
      if (tick.type === 'alert') {
        if (tick.level === 'error') {
          // Pull the persisted failure so it shows in the dismissible banner.
          fetchFailedRollovers()
        } else {
          setNotice(tick.message)
          setTimeout(() => setNotice(null), 8000)
        }
        return
      }
      if (tick.type === 'feed_status') {
        // Live-feed health from the gateway. "session_expired" means the broker
        // session died and the gateway can't self-heal — the human must Connect.
        setFeedExpired(tick.state === 'session_expired' ? (tick.message || 'Broker session expired — please Connect.') : null)
        return
      }
      if (tick.type === 'order_update') {
        // Broker pushed a fill/state-change — refresh lots & orders immediately
        // (backend already reconciled the lot table by the time this arrives).
        fetchLots()
        fetchOrders()
        fetchPositions()
        return
      }
      // Any price frame means the feed is alive again — clear a stale warning.
      // Functional updater: no-op when already null, and dodges stale closure.
      if (tick.tk) setFeedExpired(prev => (prev ? null : prev))
      if (tick.e && tick.tk) {
        // A "tf" tick only carries the fields that changed — merge onto the
        // last-known entry instead of replacing it, or an incremental
        // bid-only update would wipe out the ask/lp we already had (showing
        // up downstream as a spurious ₹0).
        const key = `${tick.e}|${tick.tk}`
        setTickMap(prev => {
          const cur = prev[key] || { lp: '', bp1: '', sp1: '' }
          return { ...prev, [key]: { lp: tick.lp || cur.lp, bp1: tick.bp1 || cur.bp1, sp1: tick.sp1 || cur.sp1 } }
        })
      }
      if (tick.e === 'NSE' && tick.tk === '26000') {
        setNiftyData(prev => ({
          ltp: tick.lp || prev?.ltp || '',
          close: tick.c || prev?.close || '',
        }))
        return
      }
      setWatchlist(prev =>
        prev.map(item =>
          item.exch === tick.e && item.token === tick.tk
            ? { ...item, ltp: tick.lp || item.ltp, bid: tick.bp1 || item.bid, ask: tick.sp1 || item.ask, open: tick.o || item.open, close: tick.c || item.close }
            : item
        )
      )
      if (!tick.lp && !tick.bp1 && !tick.sp1) return
      setPositionsSummary(prev => {
        if (!prev) return prev
        let changed = false
        const updatedGroups = prev.symbol_groups.map(group => {
          const updatedPositions = group.positions.map(pos => {
            if (pos.exch !== tick.e || pos.token !== tick.tk) return pos
            changed = true
            const netqty = parseInt(pos.netqty)
            const updatedLp = tick.lp || pos.lp
            // Flat position: only keep LTP fresh, never touch P&L fields.
            if (netqty === 0) return { ...pos, lp: updatedLp }
            // Positions card values P&L at LTP (lots card keeps bid/ask).
            const rawExitPrice = tick.lp
            if (!rawExitPrice) return { ...pos, lp: updatedLp }
            const newExitPrice = parseFloat(rawExitPrice)
            const oldExitPrice = pos.exit_price ?? parseFloat(pos.lp)
            if (!oldExitPrice || oldExitPrice === newExitPrice) {
              return { ...pos, lp: updatedLp, exit_price: newExitPrice }
            }
            const delta = newExitPrice - oldExitPrice
            const prcftr = pos.prcftr || 1
            const urmtom = pos.urmtom + delta * netqty * prcftr
            const total_pnl = pos.total_pnl + delta * netqty * prcftr
            return { ...pos, lp: updatedLp, exit_price: newExitPrice, urmtom, total_pnl }
          })
          const symbol_pnl = updatedPositions.reduce((s, p) => s + p.total_pnl, 0)
          return { ...group, positions: updatedPositions, symbol_pnl }
        })
        if (!changed) return prev
        const total_pnl = updatedGroups.reduce((s, g) => s + g.symbol_pnl, 0)
        return { ...prev, symbol_groups: updatedGroups, total_pnl }
      })
      setLots(prev => {
        let changed = false
        const next = prev.map(lot => {
          if (lot.exch !== tick.e || lot.token !== tick.tk) return lot
          if (lot.open_qty <= 0 || (lot.status !== 'OPEN' && lot.status !== 'PARTIAL')) return lot
          // Long lots exit on the bid; short on the ask. Falls back to LTP.
          const rawExitPrice = lot.side === 'B' ? (tick.bp1 || tick.lp) : (tick.sp1 || tick.lp)
          if (!rawExitPrice) return lot
          const newExitPrice = parseFloat(rawExitPrice)
          if (lot.exit_price === newExitPrice) return lot
          const sideSign = lot.side === 'B' ? 1 : -1
          const prcftr = lot.prcftr || 1
          // live_pnl folds rollover carry in exactly once — mirror the backend
          // (_lot_live_pnl) so a WS-tick recompute matches the REST value.
          const live = lot.realized_pnl + (lot.carried_pnl || 0) + (newExitPrice - lot.avg_entry_price) * lot.open_qty * sideSign * prcftr
          // Day MTM anchored to prev-day close (tick "c") — mirror _lot_mtm_pnl.
          // LTP-based (not bid/ask); keep the last known close if this tick omits it.
          // A lot opened today was never held at yesterday's close, so day-MTM is
          // 0 — mirror the backend's _opened_today guard, else a WS tick would
          // flash a bogus MTM on top of the REST value's 0.
          const prevClose = parseFloat(tick.c) || lot.prev_close || 0
          const tickLtp = parseFloat(tick.lp) || newExitPrice
          const mtm = openedTodayIST(lot.opened_at)
            ? 0
            : prevClose > 0 && tickLtp > 0
              ? (tickLtp - prevClose) * lot.open_qty * sideSign * prcftr
              : (lot.mtm_pnl || 0)
          changed = true
          return { ...lot, exit_price: newExitPrice, live_pnl: live, mtm_pnl: mtm, prev_close: prevClose || lot.prev_close }
        })
        return changed ? next : prev
      })
    }

    ws.onerror = (e) => console.error('Ticker WS error:', e)

    ws.onclose = () => {
      setWsLive(false)
      const delay = wsRetryDelayRef.current
      wsRetryDelayRef.current = Math.min(delay * 2, 15_000)
      retryTimer = setTimeout(() => setWsGeneration(g => g + 1), delay)
    }

    return () => {
      if (retryTimer) clearTimeout(retryTimer)
      ws.onclose = null // deliberate close — don't schedule a reconnect
      ws.close()
      wsRef.current = null
    }
  }, [connected, wsGeneration]) // intentionally excludes watchlist — avoids reconnecting on every tick

  const loadData = useCallback(async () => {
    setLoadingData(true)
    try {
      const u = await apiFetch<UserInfo>('/user')
      setUser(u)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoadingData(false)
    }
  }, [])

  const fetchPositions = useCallback(async () => {
    setLoadingPositions(true)
    try {
      const data = await apiFetch<PositionsSummary>('/positions')
      setPositionsSummary(data)
      const tokens = Array.from(new Set(
        data.symbol_groups.flatMap(g => g.positions.map(p => `${p.exch}|${p.token}`).filter(k => !k.endsWith('|')))
      ))
      positionTokensRef.current = tokens
      if (tokens.length > 0 && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ action: 'subscribe', symbols: tokens }))
      }
    } catch (e) {
      console.error('Failed to fetch positions:', e)
    } finally {
      setLoadingPositions(false)
    }
  }, [])

  const fetchOrders = useCallback(async () => {
    setLoadingOrders(true)
    try {
      const data = await apiFetch<OrderBookResponse>('/orders')
      setOrders(data.orders)
    } catch (e) {
      console.error('Failed to fetch orders:', e)
      setError(`Orders fetch failed: ${(e as Error).message}`)
    } finally {
      setLoadingOrders(false)
    }
  }, [])

  const fetchLots = useCallback(async () => {
    try {
      const data = await apiFetch<OrderLot[]>('/lots')
      setLots(data)
      const tokens = Array.from(new Set(data.map(l => `${l.exch}|${l.token}`).filter(k => !k.endsWith('|'))))
      if (tokens.length > 0 && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ action: 'subscribe', symbols: tokens }))
      }
    } catch (e) {
      console.error('Failed to fetch lots:', e)
    }
  }, [])

  const fetchOrderHistory = useCallback(async (reset = true) => {
    setLoadingHistory(true)
    try {
      const offset = reset ? 0 : orderHistory.length
      const data = await getLotHistory(HISTORY_PAGE_SIZE, offset)
      setOrderHistory(prev => reset ? data : [...prev, ...data])
      setHistoryHasMore(data.length === HISTORY_PAGE_SIZE)
    } catch (e) {
      console.error('Failed to fetch order history:', e)
    } finally {
      setLoadingHistory(false)
    }
  }, [orderHistory.length])

  const handleModifyOrder = useCallback(async (orderId: string, exchange: string, tradingsymbol: string, qty: number, newPrice: number) => {
    try {
      await apiFetch(`/orders/${orderId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          exchange,
          tradingsymbol,
          newquantity: String(qty),
          newprice_type: 'LMT',
          newprice: String(newPrice),
        }),
      })
      await fetchOrders()
    } catch (e) {
      console.error('Modify order failed:', e)
    }
  }, [fetchOrders])

  const handleCleanupExternal = useCallback(async () => {
    try {
      await apiFetch('/lots/cleanup-external', { method: 'POST' })
      await fetchLots()
    } catch (e) {
      console.error('Cleanup failed:', e)
    }
  }, [fetchLots])

  const handleSyncPositions = useCallback(async () => {
    try {
      await apiFetch('/sync-positions', { method: 'POST' })
      await fetchLots()
    } catch (e) {
      console.error('Sync positions failed:', e)
    }
  }, [fetchLots])

  const handleDeleteLot = useCallback(async (lotId: number) => {
    try {
      await apiFetch(`/lots/${lotId}`, { method: 'DELETE' })
      await fetchLots()
    } catch (e) {
      console.error('Delete lot failed:', e)
      setError(`Delete failed: ${(e as Error).message}`)
    }
  }, [fetchLots])

  const handleClearCancelledHistory = useCallback(async () => {
    try {
      await clearCancelledHistory()
      await fetchOrderHistory(true)
    } catch (e) {
      console.error('Clear cancelled history failed:', e)
      setError(`Clear failed: ${(e as Error).message}`)
    }
  }, [fetchOrderHistory])

  const handleDeleteHistoryLot = useCallback(async (lotId: number) => {
    try {
      await apiFetch(`/lots/${lotId}`, { method: 'DELETE' })
      await fetchOrderHistory(true)
    } catch (e) {
      console.error('Delete history record failed:', e)
      setError(`Delete failed: ${(e as Error).message}`)
    }
  }, [fetchOrderHistory])

  const handleExitLot = async (lot: OrderLot, qty: number, price: number, tempExit = false) => {
    setExitingLot(lot.id)
    try {
      await apiFetch(`/lots/${lot.id}/exit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ qty, price_type: 'LMT', price, temp_exit: tempExit }),
      })
      setError(null)
      await Promise.all([fetchLots(), fetchOrders(), fetchPositions()])
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setExitingLot(null)
    }
  }

  const handleSetTempExit = useCallback(async (lotId: number, enabled: boolean) => {
    try {
      await setLotTempExit(lotId, enabled)
      await fetchLots()
    } catch (e) {
      setError((e as Error).message)
    }
  }, [fetchLots])

  const handleRolloverLot = async (lot: OrderLot, payload: RolloverPayload) => {
    setRollingLot(lot.id)
    try {
      const res = await rolloverLot(lot.id, payload)
      // The exit is placed; the far leg auto-fires when it fills. Confirm via
      // the green notice banner (auto-dismisses).
      setError(null)
      setNotice(res.message || `Rolling ${res.near.tsym} → ${res.far.tsym}…`)
      setTimeout(() => setNotice(null), 8000)
      await Promise.all([fetchLots(), fetchOrders(), fetchPositions(), fetchSymbolTargets()])
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setRollingLot(null)
    }
  }

  const fetchFailedRollovers = useCallback(async () => {
    try {
      setFailedRollovers(await getFailedRollovers())
    } catch {
      // non-fatal — failures just won't surface until the next fetch
    }
  }, [])

  const dismissRollover = async (id: number) => {
    setFailedRollovers(prev => prev.filter(f => f.id !== id))
    try {
      await ackRollover(id)
    } catch {
      // if the ack fails it'll reappear on the next fetch — acceptable
    }
  }

  const fetchExchangeTargets = useCallback(async () => {
    try {
      const list = await getExchangeTargets()
      setExchangeTargets(new Map(list.map(t => [t.exch, t])))
    } catch (e) {
      console.error('Failed to fetch exchange targets:', e)
    }
  }, [])

  const handleUpdateExchangeTarget = async (exch: string, patch: { enabled?: boolean; target_value?: number }) => {
    setSavingExchangeTarget(exch)
    try {
      const updated = await updateExchangeTarget(exch, patch)
      setExchangeTargets(prev => new Map(prev).set(exch, updated))
    } catch (e) {
      console.error('Failed to update exchange target:', e)
    } finally {
      setSavingExchangeTarget(null)
    }
  }

  const fetchSymbolTargets = useCallback(async () => {
    try {
      const list = await getSymbolTargets()
      setSymbolTargets(new Map(list.map(t => [`${t.exch}|${t.tsym}`, t])))
    } catch (e) {
      console.error('Failed to fetch symbol targets:', e)
    }
  }, [])

  const fetchFunds = useCallback(async () => {
    try {
      const data = await apiFetch<FundsInfo>('/funds')
      setFunds(data)
    } catch (e) {
      console.error('Failed to fetch funds:', e)
    }
  }, [])

  const fetchWatchlist = useCallback(async () => {
    try {
      type WatchlistDB = Omit<WatchlistItem, 'ltp' | 'bid' | 'ask' | 'open' | 'close'>
      const items = await apiFetch<WatchlistDB[]>('/watchlist')
      const withPrices: WatchlistItem[] = items.map(i => ({ ...i, ltp: '', bid: '', ask: '', open: '', close: '' }))
      setWatchlist(withPrices)
      const symbols = withPrices.map(i => `${i.exch}|${i.token}`).filter(s => !s.endsWith('|'))
      if (symbols.length > 0 && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ action: 'subscribe', symbols }))
      }
      const updated = await Promise.all(
        withPrices.map(async item => ({ ...item, ...await fetchSymbolQuote(item) }))
      )
      setWatchlist(updated)
    } catch (e) {
      console.error('Failed to fetch watchlist:', e)
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const handleUpdateSymbolTarget = async (exch: string, tsym: string, patch: { enabled?: boolean; target_value?: number }) => {
    const key = `${exch}|${tsym}`
    setSavingTarget(key)
    try {
      const updated = await updateSymbolTarget(exch, tsym, patch)
      setSymbolTargets(prev => new Map(prev).set(key, updated))
    } catch (e) {
      console.error('Failed to update symbol target:', e)
    } finally {
      setSavingTarget(null)
    }
  }

  // Per-order (per-lot) target: rides with a single lot, independent of the
  // symbol/exchange targets on the Positions card.
  const handleUpdateLotTarget = async (lotId: number, patch: { enabled?: boolean; target_value?: number }) => {
    setSavingLotTarget(lotId)
    try {
      const updated = await updateLotTarget(lotId, patch)
      setLots(prev => prev.map(l => (l.id === lotId ? { ...l, ...updated } : l)))
    } catch (e) {
      console.error('Failed to update order target:', e)
    } finally {
      setSavingLotTarget(null)
    }
  }

  const fetchStrategyNames = useCallback(async () => {
    try {
      setStrategyNames(await getStrategyNames())
    } catch (e) {
      console.error('Failed to fetch strategy names:', e)
    }
  }, [])

  const handleUpdateLotStrategy = async (lotId: number, strategyName: string) => {
    setSavingLotStrategy(lotId)
    try {
      const updated = await updateLotStrategy(lotId, strategyName)
      setLots(prev => prev.map(l => (l.id === lotId ? { ...l, ...updated } : l)))
      if (updated.strategy_name && !strategyNames.includes(updated.strategy_name)) {
        setStrategyNames(prev => [...prev, updated.strategy_name as string].sort())
      }
    } catch (e) {
      console.error('Failed to update strategy name:', e)
    } finally {
      setSavingLotStrategy(null)
    }
  }

  const fetchSymbolQuote = async (item: WatchlistItem): Promise<{ ltp: string; bid: string; ask: string; open: string; close: string }> => {
    if (!item.token) return { ltp: '', bid: '', ask: '', open: '', close: '' }
    try {
      const q = await apiFetch<QuoteResponse>(`/quote?exchange=${item.exch}&token=${item.token}`)
      return { ltp: q.lp || '', bid: q.bp1 || '', ask: q.sp1 || '', open: q.o || '', close: q.c || '' }
    } catch { return { ltp: '', bid: '', ask: '', open: '', close: '' } }
  }

  const handleAddToWatch = async (result: ScripSearchResult) => {
    setWatchSearch('')
    setShowWatchSuggestions(false)
    if (watchlist.some(w => w.token === result.token)) return
    const item: WatchlistItem = {
      tsym: result.tsym,
      exch: result.exch,
      token: result.token,
      ltp: '',
      bid: '',
      ask: '',
      open: '',
      close: '',
      lotsize: result.lotsize,
      instrumenttype: result.instrumenttype,
      expd: result.expd,
      sym: result.sym,
    }
    setWatchlist(w => [...w, item])
    apiFetch('/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tsym: item.tsym, exch: item.exch, token: item.token, lotsize: item.lotsize, instrumenttype: item.instrumenttype, expd: item.expd, sym: item.sym }),
    }).catch(e => console.error('Failed to save watchlist item:', e))
    const quote = await fetchSymbolQuote(item)
    setWatchlist(w => w.map(x => x.token === item.token ? { ...x, ...quote } : x))
    if (wsRef.current?.readyState === WebSocket.OPEN)
      wsRef.current.send(JSON.stringify({ action: 'subscribe', symbols: [`${item.exch}|${item.token}`] }))
  }

  const refreshWatchlistLtp = async () => {
    const updated = await Promise.all(
      watchlist.map(async (item) => {
        const quote = await fetchSymbolQuote(item)
        return { ...item, ...quote }
      })
    )
    setWatchlist(updated)
  }

  const openOrderModal = (item: WatchlistItem, side: 'B' | 'S') => {
    setOrderItem(item)
    setOrderSide(side)
    const ls = parseInt(item.lotsize) || 1
    setOrderQty(ls)
    setOrderLots(1)
    setOrderPrice(parseFloat(side === 'B' ? (item.ask || item.ltp) : (item.bid || item.ltp)) || 0)
    setOrderType('LMT')
    setOrderProduct('M')
    setOrderHoldForever(false)
    setOrderTpEnabled(false)
    setOrderTpValue(0)
    setOrderDescription('')
    setReentrySourceLotId(null)
    setShowOrderModal(true)
  }

  const openReentryFromLot = async (lot: OrderLot) => {
    const ls = lot.lotsize || 1
    const existing = watchlist.find(w => w.exch === lot.exch && w.tsym === lot.tsym)
    const item: WatchlistItem = existing ?? {
      tsym: lot.tsym,
      exch: lot.exch,
      token: lot.token,
      ltp: String(lot.avg_entry_price),
      bid: '',
      ask: '',
      open: '',
      close: '',
      lotsize: String(ls),
      instrumenttype: '',
      expd: '',
      sym: lot.tsym,
    }
    // Price at the current best bid/ask (buy at ask, sell at bid) instead of the old avg entry price.
    // Prefer the watchlist's live WS-streamed quote — a one-off REST snapshot can lag on thin books
    // (e.g. MCX options), and is only used as a fallback when the symbol isn't already in the watchlist.
    const liveBid = parseFloat(existing?.bid || '')
    const liveAsk = parseFloat(existing?.ask || '')
    const livePrice = lot.side === 'B' ? liveAsk : liveBid

    setOrderItem(item)
    setOrderSide(lot.side)
    setOrderQty(lot.entry_qty)
    setOrderLots(Math.round((lot.entry_qty / ls) * 100) / 100)
    setOrderPrice(livePrice > 0 ? livePrice : lot.avg_entry_price)
    setOrderType('LMT')
    setOrderProduct((lot.product_type === 'I' ? 'I' : 'M') as 'I' | 'M')
    setOrderHoldForever(false)
    setOrderTpEnabled(false)
    setOrderTpValue(0)
    setOrderDescription('')
    setReentrySourceLotId(lot.id)
    setShowOrderModal(true)

    if (livePrice > 0) return

    const quote = await fetchSymbolQuote(item)
    const bestPrice = parseFloat(lot.side === 'B' ? quote.ask : quote.bid)
    if (bestPrice > 0) setOrderPrice(bestPrice)
  }

  const handlePlaceOrder = async () => {
    if (!orderItem) return
    setPlacingOrder(true)
    try {
      if (orderHoldForever) {
        // Route through the persistent-order pipeline. Backend places today's
        // DAY order immediately if the venue is open, or defers to the sweeper.
        await createPersistentOrder({
          buy_or_sell: orderSide,
          product_type: orderProduct,
          exchange: orderItem.exch,
          tradingsymbol: orderItem.tsym,
          quantity: orderQty,
          price_type: orderType,
          price: orderType === 'LMT' ? orderPrice : 0,
          trigger_price: 0,
          target_enabled: orderTpEnabled && orderTpValue > 0,
          target_value: orderTpEnabled ? orderTpValue : 0,
          description: orderDescription.trim(),
        })
      } else {
        // TP on a plain DAY order arms THIS order's own per-order target: it
        // rides with the lot this order creates and fires on that lot's own
        // live P&L — it never touches other lots of the same symbol.
        await apiFetch('/orders', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            buy_or_sell: orderSide,
            product_type: orderProduct,
            exchange: orderItem.exch,
            tradingsymbol: orderItem.tsym,
            quantity: orderQty,
            discloseqty: 0,
            price_type: orderType,
            price: orderType === 'LMT' ? orderPrice : 0,
            trigger_price: 0,
            retention: 'DAY',
            remarks: '',
            description: orderDescription.trim(),
            target_enabled: orderTpEnabled && orderTpValue > 0,
            target_value: orderTpEnabled ? orderTpValue : 0,
            reentry_source_lot_id: reentrySourceLotId,
          }),
        })
      }
      setShowOrderModal(false)
      setReentrySourceLotId(null)
      setError(null)
      await Promise.all([fetchOrders(), fetchLots()])
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setPlacingOrder(false)
    }
  }

  const handleExitPosition = async (pos: PositionItem) => {
    const netQty = parseInt(pos.netqty)
    if (netQty === 0) return
    const key = `${pos.tsym}_${pos.prd}`
    setExitingPosition(key)
    try {
      await apiFetch('/orders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          buy_or_sell: netQty > 0 ? 'S' : 'B',
          product_type: pos.prd === 'I' ? 'I' : 'M',
          exchange: pos.exch,
          tradingsymbol: pos.tsym,
          quantity: Math.abs(netQty),
          discloseqty: 0,
          price_type: 'LMT',
          price: pos.exit_price ?? parseFloat(pos.lp),
          trigger_price: 0,
          retention: 'DAY',
          remarks: 'exit',
        }),
      })
      setError(null)
      await Promise.all([fetchPositions(), fetchOrders()])
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setExitingPosition(null)
    }
  }

  const fetchOptionChain = async (item: WatchlistItem, expiry = '') => {
    setChainLoading(true)
    setChainError(null)
    try {
      const data = await apiFetch<OptionChainData>('/option-chain', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: item.sym || item.tsym,
          exchange: item.exch,
          expiry,
          atm: parseFloat(item.ltp) || 0,
          count: 10,
        }),
      })
      setChainData(data)
      if (data.expiry) setChainExpiry(data.expiry)
      if (data.error) setChainError(data.error)
    } catch (e) {
      setChainError((e as Error).message)
    } finally {
      setChainLoading(false)
    }
  }

  const openChainModal = (item: WatchlistItem) => {
    setChainItem(item)
    setChainData(null)
    setChainExpiry('')
    setChainError(null)
    setShowChainModal(true)
    fetchOptionChain(item)
  }

  const handleCancelOrder = async (orderId: string) => {
    if (!window.confirm('Cancel this order?')) return
    try {
      await apiFetch(`/orders/${orderId}`, { method: 'DELETE' })
      await fetchOrders()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const handleCancelExitOrder = async (lot: OrderLot) => {
    if (!window.confirm('Cancel the exit order for this lot?')) return
    try {
      await apiFetch(`/lots/${lot.id}/exit`, { method: 'DELETE' })
      setError(null)
      await Promise.all([fetchLots(), fetchOrders()])
    } catch (e) {
      setError((e as Error).message)
    }
  }

  // Debounced market watch search
  useEffect(() => {
    if (watchSearch.length < 2) {
      setWatchSuggestions([])
      setShowWatchSuggestions(false)
      setWatchLoading(false)
      return
    }
    setWatchLoading(true)
    const timer = setTimeout(async () => {
      try {
        const data = await apiFetch<ScripSearchResponse>(
          `/search?q=${encodeURIComponent(watchSearch)}`
        )
        setWatchSuggestions(data.results)
        // Open even on an empty result so the "no matches" hint can show.
        setShowWatchSuggestions(true)
      } catch {
        setWatchSuggestions([])
      } finally {
        setWatchLoading(false)
      }
    }, 300)
    return () => clearTimeout(timer)
  }, [watchSearch])

  useEffect(() => {
    apiFetch<{ connected: boolean; subscribed_count: number }>('/status')
      .then(({ connected: c, subscribed_count }) => {
        setConnected(c)
        setSubscribedCount(subscribed_count ?? null)
        if (c) {
          loadData()
          fetchPositions()
          fetchOrders()
          fetchLots()
          fetchExchangeTargets()
          fetchSymbolTargets()
          fetchWatchlist()
          fetchFunds()
          fetchOrderHistory()
          fetchFailedRollovers()
          fetchStrategyNames()
        }
      })
      .catch(() => setConnected(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadData, fetchPositions, fetchOrders, fetchLots, fetchExchangeTargets, fetchSymbolTargets, fetchWatchlist, fetchFunds, fetchStrategyNames])

  useEffect(() => {
    if (!connected) return
    const interval = setInterval(() => {
      fetchPositions()
      fetchOrders()
      fetchLots()
      fetchFunds()
      fetchFailedRollovers()
    }, 30_000)
    return () => clearInterval(interval)
  }, [connected, fetchPositions, fetchOrders, fetchLots, fetchFunds, fetchFailedRollovers])

  useEffect(() => {
    if (!connected) return
    const interval = setInterval(() => {
      apiFetch<{ subscribed_count: number }>('/status')
        .then(({ subscribed_count }) => setSubscribedCount(subscribed_count ?? null))
        .catch(() => {})
    }, 5_000)
    return () => clearInterval(interval)
  }, [connected])

  const handleConnect = async () => {
    setConnecting(true)
    setError(null)
    try {
      await apiFetch('/connect', { method: 'POST' })
      setConnected(true)
      await loadData()
      await fetchPositions()
      await fetchOrders()
      await fetchLots()
      await fetchExchangeTargets()
      await fetchSymbolTargets()
      await fetchWatchlist()
      await fetchFunds()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setConnecting(false)
    }
  }

  const handleDisconnect = async () => {
    await apiFetch('/disconnect', { method: 'POST' }).catch(() => {})
    setConnected(false)
    setUser(null)
  }

  // Split lots by origin: strategy-service lots (GR/RV/…) and any lot the
  // user has tagged with a strategy name both land in the read-only
  // "Orders from Strategies" card; the manual Orders card shows the rest.
  const strategyLots = lots.filter(l => l.source_service || l.strategy_name)
  const manualLots = lots.filter(l => !l.source_service && !l.strategy_name)

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      <Header
        connected={connected}
        connecting={connecting}
        user={user}
        funds={funds}
        niftyData={niftyData}
        wsLive={wsLive}
        subscribedCount={subscribedCount}
        loadingData={loadingData}
        showUserDropdown={showUserDropdown}
        onOpenSubscribed={() => setShowSubscribedModal(true)}
        onConnect={handleConnect}
        onDisconnect={handleDisconnect}
        onToggleUserDropdown={() => setShowUserDropdown(v => !v)}
        onCloseUserDropdown={() => setShowUserDropdown(false)}
        onOpenSettings={() => setShowSettings(true)}
        accountEmail={authUser?.email ?? null}
        onSignOut={signout}
      />

      <SettingsModal show={showSettings} onClose={() => setShowSettings(false)} />
      <SubscribedTokensModal show={showSubscribedModal} onClose={() => setShowSubscribedModal(false)} />

      <main className="px-6 py-8 space-y-6">
        {error && (
          <div className="bg-red-950/60 border border-red-800 text-red-300 text-sm px-4 py-3 rounded-lg">
            {error}
          </div>
        )}
        {feedExpired && (
          <div className="bg-red-950/60 border border-red-800 text-red-300 text-sm px-4 py-3 rounded-lg flex items-start justify-between gap-3">
            <span>
              <span className="font-semibold">Live feed stopped:</span> {feedExpired}
            </span>
            <button
              onClick={() => setFeedExpired(null)}
              className="text-red-500 hover:text-red-300 cursor-pointer shrink-0"
              aria-label="Dismiss"
            >
              ×
            </button>
          </div>
        )}
        {notice && (
          <div className="bg-emerald-950/60 border border-emerald-800 text-emerald-300 text-sm px-4 py-3 rounded-lg flex items-center justify-between gap-3">
            <span>{notice}</span>
            <button onClick={() => setNotice(null)} className="text-emerald-500 hover:text-emerald-300 cursor-pointer">×</button>
          </div>
        )}
        {failedRollovers.map(f => (
          <div key={f.id} className="bg-red-950/60 border border-red-800 text-red-300 text-sm px-4 py-3 rounded-lg flex items-start justify-between gap-3">
            <span>
              <span className="font-semibold">Rollover failed:</span> {f.near_tsym} closed, but{' '}
              <span className="font-mono">{f.far_tsym}</span> could not be placed{f.error ? ` — ${f.error}` : ''}.{' '}
              You are flat on {f.near_tsym}; place <span className="font-mono">{f.far_tsym}</span> manually.
            </span>
            <button
              onClick={() => dismissRollover(f.id)}
              className="text-red-500 hover:text-red-300 cursor-pointer shrink-0"
              aria-label="Dismiss"
            >
              ×
            </button>
          </div>
        ))}

        {connected === false && (
          <div className="flex flex-col items-center justify-center py-24 text-center space-y-3">
            <div className="w-16 h-16 rounded-full border-2 border-gray-800 flex items-center justify-center mb-2">
              <svg className="w-8 h-8 text-gray-700" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M13.19 8.688a4.5 4.5 0 0 1 1.242 7.244l-4.5 4.5a4.5 4.5 0 0 1-6.364-6.364l1.757-1.757m13.35-.622 1.757-1.757a4.5 4.5 0 0 0-6.364-6.364l-4.5 4.5a4.5 4.5 0 0 0 1.242 7.244" />
              </svg>
            </div>
            <p className="text-lg font-medium text-gray-400">Not connected to Shoonya broker</p>
            <p className="text-sm text-gray-600 max-w-sm">
              Click <span className="text-blue-400">Connect</span> above to authenticate using credentials from your <code className="text-gray-500">.env</code> file.
            </p>
          </div>
        )}

        {connected && (
          <div className="space-y-6">
            <div className={`grid gap-6 ${marketWatchCollapsed ? 'grid-cols-[auto_1fr] items-stretch' : 'grid-cols-5 items-start'}`}>
              <div className={`${marketWatchCollapsed ? '' : 'col-span-2 space-y-4'}`}>
                <MarketWatch
                  watchlist={watchlist}
                  watchSearch={watchSearch}
                  watchSuggestions={watchSuggestions}
                  showWatchSuggestions={showWatchSuggestions}
                  watchLoading={watchLoading}
                  wsRef={wsRef}
                  isCollapsed={marketWatchCollapsed}
                  onCollapsedChange={setMarketWatchCollapsed}
                  onSearchChange={setWatchSearch}
                  onShowSuggestions={setShowWatchSuggestions}
                  onAddToWatch={handleAddToWatch}
                  onRefreshLtp={refreshWatchlistLtp}
                  onOpenChainModal={openChainModal}
                  onOpenOrderModal={openOrderModal}
                  onRemoveFromWatch={(item) => {
                    setWatchlist(w => w.filter(x => x.token !== item.token))
                    apiFetch(`/watchlist/${item.exch}/${item.token}`, { method: 'DELETE' })
                      .catch(e => console.error('Failed to remove watchlist item:', e))
                  }}
                />
              </div>

              <div className={marketWatchCollapsed ? '' : 'col-span-3'}>
                <PositionsCard
                  positionsSummary={positionsSummary}
                  loadingPositions={loadingPositions}
                  exchangeTargets={exchangeTargets}
                  savingExchangeTarget={savingExchangeTarget}
                  symbolTargets={symbolTargets}
                  savingTarget={savingTarget}
                  exitingPosition={exitingPosition}
                  onFetchPositions={fetchPositions}
                  onExitPosition={handleExitPosition}
                  onUpdateExchangeTarget={handleUpdateExchangeTarget}
                  onUpdateSymbolTarget={handleUpdateSymbolTarget}
                />
              </div>
            </div>

            <OrdersCard
              orders={orders}
              lots={manualLots}
              loadingOrders={loadingOrders}
              orderSymbolFilter={orderSymbolFilter}
              positionOrderFilter={positionOrderFilter}
              exitingLot={exitingLot}
              onFetchOrders={() => { fetchOrders(); fetchLots() }}
              onSetOrderSymbolFilter={setOrderSymbolFilter}
              onClearPositionOrderFilter={() => setPositionOrderFilter(null)}
              onCancelOrder={handleCancelOrder}
              onExitLot={handleExitLot}
              onCancelExitOrder={handleCancelExitOrder}
              onReentryLot={openReentryFromLot}
              rollingLot={rollingLot}
              onRolloverLot={handleRolloverLot}
              onFetchRollTargets={getRollTargets}
              onFetchQuote={getQuote}
              tickMap={tickMap}
              onSubscribeSymbol={(exch, token) => wsRef.current?.send(JSON.stringify({ action: 'subscribe', symbols: [`${exch}|${token}`] }))}
              savingLotTarget={savingLotTarget}
              onUpdateLotTarget={handleUpdateLotTarget}
              savingLotStrategy={savingLotStrategy}
              onUpdateLotStrategy={handleUpdateLotStrategy}
              strategyNames={strategyNames}
              onSetTempExit={handleSetTempExit}
              onSyncPositions={handleSyncPositions}
              onCleanupExternal={handleCleanupExternal}
              onModifyOrder={handleModifyOrder}
              onDeleteLot={handleDeleteLot}
            />

            <StrategyOrdersCard
              lots={strategyLots}
              loadingOrders={loadingOrders}
              onFetchOrders={() => { fetchOrders(); fetchLots() }}
              savingLotStrategy={savingLotStrategy}
              onUpdateLotStrategy={handleUpdateLotStrategy}
              strategyNames={strategyNames}
            />

            <BrokerOrdersCard
              orders={orders}
              loading={loadingOrders}
              onRefresh={fetchOrders}
            />

            <OrderHistoryCard
              history={orderHistory}
              loading={loadingHistory}
              hasMore={historyHasMore}
              onLoadMore={() => fetchOrderHistory(false)}
              onRefresh={() => fetchOrderHistory(true)}
              onClearCancelled={handleClearCancelledHistory}
              onDeleteLot={handleDeleteHistoryLot}
            />
          </div>
        )}
      </main>

      <OptionChainModal
        show={showChainModal}
        chainItem={chainItem}
        chainData={chainData}
        chainLoading={chainLoading}
        chainError={chainError}
        chainExpiry={chainExpiry}
        onClose={() => setShowChainModal(false)}
        onFetchChain={fetchOptionChain}
        onExpiryChange={setChainExpiry}
      />

      <OrderModal
        show={showOrderModal}
        orderItem={orderItem}
        orderSide={orderSide}
        orderQty={orderQty}
        orderLots={orderLots}
        orderPrice={orderPrice}
        orderType={orderType}
        orderProduct={orderProduct}
        placingOrder={placingOrder}
        holdForever={orderHoldForever}
        tpEnabled={orderTpEnabled}
        tpValue={orderTpValue}
        description={orderDescription}
        onClose={() => { setShowOrderModal(false); setReentrySourceLotId(null) }}
        onPlaceOrder={handlePlaceOrder}
        onQtyChange={setOrderQty}
        onLotsChange={setOrderLots}
        onPriceChange={setOrderPrice}
        onTypeChange={setOrderType}
        onProductChange={setOrderProduct}
        onHoldForeverChange={setOrderHoldForever}
        onTpEnabledChange={setOrderTpEnabled}
        onTpValueChange={setOrderTpValue}
        onDescriptionChange={setOrderDescription}
      />

    </div>
  )
}
