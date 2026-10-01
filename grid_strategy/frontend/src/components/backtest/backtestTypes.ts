/** Types for the isolated backtest module — kept out of the live types.ts. */

export interface BtPreset {
  key: string
  ticker: string
  label: string
  lot_size: number
}

export interface BtRequest {
  preset: string
  ticker: string
  interval: string
  days: number
  lot_size?: number
  rsi_period: number
  rsi_level: number
  target_mode: string
  target_value: number
  target_chain_pct: number   // chained rung target = prev entry + this % of offset (100 = full)
  sl_mode: string
  sl_value: number
  sizing_mode: string
  vol_formula: string
  risk_per_rung: number
  fixed_lots: number
  atr_period: number
  min_lots: number
  max_lots_per_rung: number
  max_rungs: number
  min_gap_points: number
  slippage_points: number
  cb_threshold: number
  roll_enabled: boolean
  start_capital: number
}

export interface BtStats {
  bars: number
  from_ts: number
  to_ts: number
  trades_closed: number
  open_at_end: number
  net_pnl: number
  realized_pnl: number
  open_pnl: number
  win_rate: number
  profit_factor: number | null
  avg_win: number
  avg_loss: number
  max_drawdown: number
  rolls: number
  cb_trips: number
  target_exits: number
  sl_exits: number
}

export interface BtTrade {
  seq: number
  label: string
  lots: number
  qty: number
  entry_price: number
  raw_entry_price: number
  entry_time: number
  target_price: number
  sl_price: number
  status: string
  exit_time: number | null
  exit_price: number | null
  exit_reason: string
  realized_pnl: number
  live_pnl: number | null
  roll_count: number
  total_basis: number
  atr_at_entry: number
  rsi_at_signal: number
}

export interface BtMarker {
  time: number
  position: 'aboveBar' | 'belowBar'
  shape: 'arrowUp' | 'arrowDown'
  color: string
  text: string
  price: number
  lot_id: number
  kind: string
}

export interface BtResult {
  stats: BtStats
  candles: { ts: number; open: number; high: number; low: number; close: number; volume: number }[]
  markers: BtMarker[]
  equity: { ts: number; value: number }[]
  trades: BtTrade[]
  rolls: { ts: number; basis: number; lots_rolled: number; note: string }[]
  math_log: string[]
  rsi: { ts: number; value: number }[]
  meta: { ticker: string; interval: string; days_used: number; lot_size: number; roll_enabled: boolean; note: string }
}
