export interface Product {
  prd: string
  s_prdt_ali: string
  exch: string[]
}

export interface UserInfo {
  uid: string
  actid: string
  email: string
  m_num: string
  brkname: string
  brnchid: string
  uprev: string
  exarr: string[]
  orarr: string[]
  prarr: Product[]
  request_time: string
}

export type AuthMethod = 'rest' | 'playwright' | 'none'

export interface PositionItem {
  tsym: string
  exch: string
  prd: string
  s_prdt_ali: string
  netqty: string
  rpnl: number
  urmtom: number
  mtm_at_close: number
  total_pnl: number
  buyavgprc: string
  sellavgprc: string
  lp: string
  upldprc: number
  lotsize: string
  prcftr: number
  token: string
  exit_price?: number
  expiry: string
}

export interface SymbolGroup {
  symbol: string
  exchange: string
  symbol_pnl: number
  positions: PositionItem[]
}

export interface PositionsSummary {
  symbol_groups: SymbolGroup[]
  total_pnl: number
}

export interface OrderItem {
  norenordno: string
  tsym: string
  exch: string
  prd: string
  trantype: string
  qty: string
  price: string
  pricetype: string
  status: string
  orderid: string
  pytime: string
  exch_orderid: string
}

export interface OrderBookResponse {
  orders: OrderItem[]
}

export interface OrderLot {
  id: number
  tsym: string
  exch: string
  side: 'B' | 'S'
  entry_qty: number
  open_qty: number
  avg_entry_price: number
  realized_pnl: number
  live_pnl: number
  mtm_pnl?: number      // day MTM on open leg: (ltp - prev_close) * qty, side- & prcftr-adjusted
  prev_close?: number   // previous-day close used for mtm_pnl
  ltp?: number
  status: 'PENDING' | 'OPEN' | 'PARTIAL' | 'CLOSED' | 'CANCELLED'
  opened_at: string
  closed_at: string
  lotsize: number
  prcftr: number
  broker_entry_orderid: string
  product_type: string
  token: string
  exit_price?: number
  pending_exit_orderid?: string
  is_external?: boolean
  is_rollover?: boolean
  is_reentry?: boolean
  is_temp_exit?: boolean   // TE tag: CLOSED lot kept visible across days for re-entry/roll
  is_persistent?: boolean
  source_service?: string | null
  avg_exit_price?: number
  exit_filled_qty?: number
  description?: string
  expd?: string
  sym?: string
  carried_pnl?: number
  target_enabled?: boolean   // per-order auto-exit target armed on this lot
  target_value?: number      // this lot's own live-P&L threshold in ₹
  strategy_name?: string | null
}

export interface RolloverPayload {
  target_tsym: string
  qty: number
  price_type: 'LMT' | 'MKT'
  exit_price: number
  entry_price: number
  carry_target: boolean
  carry_pnl: boolean
}

export interface RolloverResult {
  // 'rolling' = exit placed; far leg auto-fires when it fills.
  status: 'rolling' | 'ok' | 'partial' | 'exit_pending' | 'exit_failed'
  near: { lot_id: number; tsym: string; exit_qty: number }
  far: { tsym: string; expd?: string; order_id?: string; lot_id?: number | null; error?: string }
  message?: string
  target_carried?: boolean
}

export interface RolloverFailure {
  id: number
  near_tsym: string
  far_tsym: string
  error: string
  created_at: string
}

export interface ScripSearchResult {
  tsym: string
  exch: string
  token: string
  instrumenttype: string
  expd: string
  opttype: string
  strikeprice: string
  lotsize: string
  sym: string
}

export interface ScripSearchResponse {
  results: ScripSearchResult[]
}

export interface QuoteResponse {
  lp: string
  bp1: string
  sp1: string
  bq1: string
  sq1: string
  o: string
  h: string
  l: string
  c: string
  v: string
  ti: string
  lot: string
  tsym: string
  exch: string
  token: string
}

export interface WatchlistItem {
  tsym: string
  exch: string
  token: string
  ltp: string
  bid: string
  ask: string
  open: string
  close: string
  lotsize: string
  instrumenttype: string
  expd: string
  sym: string
}

export interface ChainLeg {
  token: string
  tsym: string
  exch: string
  lp: string
  oi: string
  v: string
}

export interface ChainRow {
  strike: number
  CE: ChainLeg | null
  PE: ChainLeg | null
}

export interface OptionChainData {
  symbol: string
  exchange: string
  expiry: string
  expiries: string[]
  chain: ChainRow[]
  error: string
}

export interface SymbolTarget {
  exch: string
  tsym: string
  enabled: boolean
  target_value: number
  carried_pnl?: number
}

export interface ExchangeTarget {
  exch: string
  enabled: boolean
  target_value: number
}

export interface FundsInfo {
  cash: number
  margin_used: number
  payin: number
  collateral: number
}

export interface ExchangeHours {
  exch: string
  open: string          // effective "HH:MM" IST
  close: string
  default_open: string
  default_close: string
  is_custom: boolean
}

export interface HolidayItem {
  date: string          // "YYYY-MM-DD"
  label: string
}

export interface MarketHoursSettings {
  enforced: boolean
  exchanges: ExchangeHours[]
  holidays: HolidayItem[]
}

export interface PersistentOrder {
  id: number
  exch: string
  tsym: string
  side: 'B' | 'S'
  product_type: string
  price_type: string
  price: number
  trigger_price: number
  quantity: number
  target_enabled: boolean
  target_value: number
  status: 'ACTIVE' | 'FILLED' | 'USER_CANCELLED' | 'FAILED'
  last_broker_orderid: string
  last_submitted_at: string
  filled_lot_id: number | null
  created_at: string
}
