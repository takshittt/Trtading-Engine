export interface PriceState {
  lp: number
  bid: number
  ask: number
  ts: string
  age: number
  source: string
}

export interface InstrumentConfig {
  target_mode: 'points' | 'percent'
  target_value: number
  target_chain_pct: number       // chained rung target = prev buy entry + this % of the offset (100 = full)
  sl_mode: 'points' | 'percent'
  sl_value: number
  sl_enabled: boolean            // false = the user disabled stop-losses for this instrument
  tick_size: number              // exchange tick grid — limit prices must be a multiple
  sizing_mode: 'vol_target' | 'fixed'
  vol_formula: 'notional' | 'atr_rupee'
  risk_per_rung: number
  fixed_lots: number
  atr_period: number
  min_lots: number
  max_lots_per_rung: number
  max_rungs: number
  min_gap_points: number
  max_spread_points: number
  marketable_ticks: number       // marketable-limit buffer: auto BUY @ ask + N ticks, AMI_SELL @ bid − N ticks (0 = strict at touch)
  sell_signal_mode: 'exit_all' | 'ignore'
  rollover_days_before: number
  rollover_date_override: string   // '' = automated (system-suggested) | ISO date = manual
  fallback_symbol: string          // yfinance/http symbol for feed-down reference price (e.g. "NG=F")
  product_type: 'M' | 'I'          // "M" = NRML/delivery (carry) | "I" = MIS/intraday
  buy_order_type: 'LMT' | 'MKT'    // entries: LMT (default, no slippage buffer) | MKT (marketable)
  sell_order_type: 'LMT' | 'MKT'   // TARGET exits: LMT (default) | MKT. Stop-loss is ALWAYS market.
  ladder_rearm: boolean            // re-arm a triggered ladder level once price recovers above it
}

export type LadderBasis = 'fixed' | 'support'

export interface LadderLevelRow {
  id: number
  level_no: number
  price: number
  lots_override: number | null
  target_override: number | null
  target_live: number | null      // price this rung actually exits at (lot target, or override)
  sl_override: number | null
  sl_auto: number               // auto SL math for this level's price (0/absent when SL disabled)
  status: 'PENDING' | 'PLACED' | 'FILLED' | 'SKIPPED' | 'AWAIT_RECOVERY' | 'TRIGGERED' | 'CANCELLED'
  source: string
  note: string
  lot_id: number | null
  fire_count: number        // times this level has executed (re-arm on recovery increments)
  triggered_at: string
}

export interface LadderInfo {
  next_buy_price?: number | null   // backend truth: highest price where a buy actually happens next
  basis: LadderBasis
  anchor_price: number
  interval_points: number
  num_levels: number
  sr_lookback_days: number
  armed: boolean
  rearm: boolean            // re-arm triggered levels on price recovery (range-bound re-buy)
  levels: LadderLevelRow[]
}

export interface RolloverMeta {
  class: string
  days_before: number
  expiry: string
  mode: 'auto' | 'manual'
  rollover_date: string
  suggested_date: string
  state: string      // idle | due | queued | scanning | rolling
  due: boolean
  window_open: boolean
  window_reason: string
  windows_text: string
  tooltip: string
}

export interface InstrumentSnap {
  id: number
  sym: string
  exch: string
  tsym: string
  token: string
  lot_size: number
  expiry: string
  enabled: boolean
  mode: 'auto' | 'ladder'
  instr_type: 'FUT' | 'OPT'
  opt_type: string          // '' | 'CE' | 'PE'
  strike: number            // option strike (0 for futures)
  underlying: string        // root underlying for options
  ladder: LadderInfo | null
  price: PriceState
  open_rungs: number
  pending_rungs: number
  resting_rungs?: number    // healthy resting BUY LIMIT orders at the broker (LMT ladder flow)
  open_pnl: number
  realized_today: number
  cb: { enabled: boolean; threshold: number; tripped: boolean; reason: string }
  config: InstrumentConfig
  rollover: RolloverMeta
  unmanaged_qty: number
}

export interface Snapshot {
  ts: string
  engine_on: boolean
  broker_connected: boolean
  feed: { state: 'live' | 'stale' | 'down' | 'closed'; seconds_since_rx: number; reconnects: number }
  global_cb: { threshold: number; tripped: boolean; reason?: string }
  total_open_pnl: number
  funds?: { cash: number; margin_used: number; remaining: number; have: boolean }
  instruments: InstrumentSnap[]
  booted_at: string
  degraded?: { active: boolean; since_s: number; reason: string; fallback_provider: string }
}

export interface AlertSettings {
  whatsapp_enabled: boolean
  whatsapp_number: string
  callmebot_apikey_set: boolean
  alert_on_feed_down: boolean
}

export interface LotRow {
  id: number
  instrument_id: number
  sym: string
  seq: number
  label: string
  exch: string
  contract_tsym: string
  contract_token: string
  lots: number
  qty: number
  lot_size: number
  entry_price: number
  raw_entry_price: number
  entry_time: string
  target_price: number
  sl_price: number
  status: string
  exit_reason: string
  exit_price: number
  exit_time: string
  exit_pending: boolean
  realized_pnl: number
  live_pnl: number | null
  temp_exit_price: number
  temp_exit_loss: number
  temp_exit_time: string
  temp_exit_trigger_price: number
  reenter_trigger_price: number
  carry_recovery: number
  roll_count: number
  total_basis: number
  atr_at_entry: number
  timeframe_min: number
  source: 'automated' | 'ladder'
  notes: string
  level_no?: number | null          // ladder level backing a RESTING lot (B{n} badge)
  level_price?: number | null       // that level's price (the real limit of a legacy UNCONFIRMED order)
  pending_order_at?: number | null  // authoritative price of the broker's pending order
  has_resting_target?: boolean      // a target sell order is resting at the broker
}

export interface LogRow {
  id?: number
  ts: string
  level: string
  category: string
  sym: string
  message: string
  data: Record<string, unknown>
}

export interface EconEventRow {
  id: number
  dt: string
  title: string
  impact: string
  region: string
  note: string
  auto: boolean
}

export interface SearchResult {
  sym: string
  exch: string
  tsym: string
  token: string
  instrumenttype: string   // 'FUT' | 'OPT'
  opttype: string          // '' | 'CE' | 'PE'
  strikeprice: number      // 0 for futures
  expd: string
  lotsize: string
}

export interface PnlResponse {
  engine: {
    total_open_pnl: number
    realized_today: number
    realized_all_time: number
    net_today: number
    open_rungs: number
    per_instrument: {
      sym: string; exch: string; tsym: string; mode: string; instr_type: string
      open_pnl: number; realized_today: number; open_rungs: number
    }[]
  }
  broker: {
    connected: boolean
    error: string
    age_seconds?: number | null   // how old the broker figures are, from the server

    positions: {
      total_pnl: number
      symbol_groups: {
        symbol: string; exchange: string; symbol_pnl: number
        positions: {
          tsym: string; exch: string; prd: string; netqty: string
          rpnl: number; urmtom: number; total_pnl: number
          buyavgprc: string; sellavgprc: string; lp: string; upldprc: number
          lotsize: string; exit_price: number
        }[]
      }[]
    }
    funds: { cash: number; margin_used: number; payin: number; collateral: number } | null
    account: {
      uid: string; actid: string; email: string; brkname: string
      m_num: string; exarr: string[]; prarr: string[]
    } | null
  }
}

export interface ChartMarker {
  time: number
  position: 'aboveBar' | 'belowBar'
  shape: 'arrowUp' | 'arrowDown'
  color: string
  text: string
  price: number
  lot_id: number
  kind: 'entry' | 'exit'
}

export interface ChartPayload {
  tsym: string
  tf: number
  candles: { ts: number; open: number; high: number; low: number; close: number; volume: number }[]
  markers: ChartMarker[]
  open_levels: { lot_id: number; seq: number; entry: number; target: number; sl: number }[]
}

export type SortMode = 'latest' | 'symbol' | 'pnl' | 'entry_price'

// ---- rollover drawer (per-rung roll plan) ----
export interface RollTarget {
  tsym: string
  token: string
  expiry: string
  lot_size: number
  ltp: number
  bid: number
  ask: number
}

export interface RollLotPlan {
  lot_id: number
  seq: number
  contract: string
  qty: number
  lots: number
  entry: number
  target: number
  sl: number
  ltp: number | null
  leg_pnl: number | null      // P&L booked on the old leg if rolled now
  basis: number | null        // est. roll cost per unit (far − near)
  roll_cost: number | null    // basis × qty
  entry_after: number | null
  target_after: number | null
  sl_after: number | null
  exit_pending: boolean
}

export interface RolloverPlan {
  ok: boolean
  sym: string
  current: { tsym: string; expiry: string; ltp: number; bid: number }
  chosen: RollTarget
  targets: RollTarget[]
  basis: number | null
  rollover: RolloverMeta
  lots: RollLotPlan[]
}
