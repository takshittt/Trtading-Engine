export interface Signal {
  id: number
  symbol: string
  signal_type: 'BUY' | 'SELL'
  timeframe: '1H' | '4H' | '1D'
  signal_time: string
  ltp: number
  /** Price the moment the signal fired — frozen, never re-priced. */
  signal_ltp: number
  /** % the price has moved since the signal fired. */
  move_pct: number
  /** Reward:risk at the current price — decays as the stock nears its target. */
  rr: number
  /** Reward:risk at the signal price — the number the min_rr filter judged. */
  signal_rr: number
  atr: number
  /** The signal's resistance above (the Target) and support below (the SL). */
  resistance: number
  support: number
  target: number
  stop_loss: number
  lot_size: number
  expiry: string
  status: string
  manual_add: boolean
  note: string
  /** False when the signal's candle had not finished — it can still repaint. */
  candle_closed: boolean
  /** Automated execution acted on this against real money — the counterpart
   *  of `paper`. */
  auto?: boolean
  /** Backend's ⭐ Essential verdict — the same set automated execution trades.
   *  null on websocket-broadcast rows, which carry no open-book context; the
   *  panel falls back to deciding locally there. */
  essential?: boolean | null
  paper: boolean
  position_id: number | null
  executed_at: string | null
  exec_price: number
  exec_latency_ms: number
  created_at: string
}

export interface Position {
  id: number
  symbol: string
  status: string
  avg_price: number
  qty: number
  lots: number
  lot_size: number
  target: number
  stop_loss: number
  target_method: string
  sl_method: string
  target_hit: boolean
  trailing_active: boolean
  trailing_sl: number
  /** Stop breached while exits were manual — nothing sold, awaiting the user. */
  sl_breached: boolean
  sl_breached_at: string | null
  trail_peak: number
  averaging_count: number
  ltp: number
  unrealized_pnl: number
  unrealized_pct: number
  realized_pnl: number
  realized_pct: number
  days_held: number
  expiry: string
  margin_used: number
  used_reserve: boolean
  exit_price: number
  exit_reason: string
  opened_at: string
  closed_at: string | null

  // paper / provenance / execution timing
  is_paper: boolean
  /** Who opened it: 'manual' (you clicked Buy) or 'auto' (automated execution
   *  or the paper trader). Orthogonal to is_paper, which says which book. */
  opened_by?: string
  signal_id: number | null
  timeframe: string
  signal_price: number
  signal_time: string | null
  signal_received_at: string | null
  executed_at: string | null
  exec_latency_ms: number
  /** Fill of the FIRST entry leg — unlike avg_price it never moves. */
  entry_price: number
  /** LTP when the entry order went out. */
  entry_ltp: number
  entry_bid: number
  entry_ask: number
  entry_spread: number
  exit_signal_at: string | null
  exit_executed_at: string | null
  exit_latency_ms: number
  exit_bid: number
  exit_ask: number
  exit_spread: number
  /** Entry fill − signal price. Measured on the entry leg, not the average. */
  entry_slippage: number
  /** How much of that was the market moving before the order went out. */
  slip_market: number
  /** …and how much was the spread the fill crossed. */
  slip_spread: number
  /** Weighted average − signal price. Averaging drift, not execution quality. */
  avg_drift: number
  peak_price: number
  trough_price: number
  /** Size the trade carried — `qty` is drawn to 0 on close. */
  exit_qty: number
  /** Max favourable excursion in ₹ — the best this trade ever was. */
  mfe: number
  /** Max adverse excursion in ₹ — the worst it ever was. */
  mae: number
  held_seconds: number | null
}

export interface Budget {
  total_budget: number
  utilised: number
  /** Total minus used — INCLUDES the reserve, which a new entry cannot touch. */
  available: number
  /** What a new (non-averaging) entry can actually draw on: hard cap minus used. */
  available_for_new: number
  reserve: number
  reserve_pct: number
  reserve_in_use: number
  utilisation_pct: number
  cap_state: 'OK' | 'SOFT' | 'HARD'
  open_positions: number
}

export interface Summary {
  shoonya_cash: number
  budget: Budget
  today_pnl: number
  overall_pnl: number
  realized_pnl: number
  open_positions: number
  paper: PaperSummary
  market_status: string
  signals_halted: boolean
  stock_selection_mode: string
  execution_mode: string
  /** 'auto' squares off on a stop breach; 'manual' flags it for the user. */
  exit_mode: string
  /** Whether automated execution will actually trade, and what is stopping it.
   *  `mode` alone does not answer that — auto stands down while Stoploss is
   *  manual or signals are halted. */
  auto_exec?: {
    mode: string
    armed: boolean
    blocked_by: string
    risk_pct: number
    max_lots: number
    max_positions: number
    averaging_mode: string
  }
  connection: { broker: boolean }
  /** Heartbeat, as opposed to `connection.broker` which is a boot-time
   *  handshake. `stalled` is true when no price has landed in two minutes —
   *  the state where positions freeze at their entry price and the broker
   *  flag still reads healthy. */
  price_feed?: {
    last_price_at: string | null
    price_age_seconds: number | null
    updates: number
    quotes_ok: number
    quotes_empty: number
    ticker_connected: boolean
    warmer_last_run_at: string | null
    warmer_last_error: string
    stalled: boolean
    symbols_priced: number
  }
  last_reconcile: string | null
  margin_as_of: string
}

export interface Config {
  total_budget: number
  soft_cap_pct: number
  hard_cap_pct: number
  reserve_pct: number
  max_averaging_buys: number
  atr_period: number
  atr_target_mult: number
  atr_sl_mult: number
  target_mode: string
  sl_mode: string
  default_target_pct: number
  default_sl_pct: number
  stock_selection_mode: string
  execution_mode: string
  auto_risk_pct: number            // % of budget risked per automated entry
  auto_max_lots: number            // per-trade lot ceiling
  auto_max_positions: number       // concurrent live automated positions
  /** 'auto' squares off on a stop breach; 'manual' flags it for the user. */
  exit_mode: string
  trailing_buffer_pct: number
  global_sl_pct: number
  averaging_mode: string
  order_retries: number
  slippage_ticks: number
  reconcile_seconds: number
  signals_halted: boolean
  paper_trading: boolean
  paper_lots: number
  auto_rollover: boolean
  auto_rollover_days: number
  min_rr: number                  // reward:risk a signal must clear to be actionable
  market_open_time: string        // "HH:MM" IST
  market_close_time: string       // "HH:MM" IST
  market_holidays: string         // comma/newline separated YYYY-MM-DD
  ignore_market_hours: boolean
  /** Hours actually being bypassed right now — the toggle OR the env var. */
  market_hours_bypassed: boolean
}

export interface StockInfo {
  symbol: string
  ltp: number
  lot_size: number
  margin_per_lot: number
  expiry: string
  sector: string
  blacklisted: boolean
  held: Position | null
}

export interface PreviousGroup { date: string; count: number; signals: Signal[] }

export interface SearchResult {
  symbol: string; tsym: string; token: string; exch: string
  instr_type: 'FUT' | 'OPT'; expiry: string; lot_size: number
  strike: string; opttype: string; ltp: number
}

export interface Preview {
  symbol: string; ltp: number; lot_size: number; expiry: string
  atr_target: number; atr_sl: number; resistance_target: number
}

/** Which broker account the shared gateway is currently logged in as.
 *  `identified` is false when the gateway answers its status check but will not
 *  say who it is — a session alive enough to respond and dead enough to reject
 *  real calls, which reads as "connected" everywhere else. */
export interface BrokerInfo {
  connected: boolean
  uid: string
  account_id: string
  name: string
  email: string
  broker: string
  identified: boolean
}

export interface LogLine { action: string; symbol: string; detail: string; level: string; created_at: string }

export interface JournalStats {
  trades: number; wins: number; losses: number; win_rate: number
  avg_pnl: number; avg_win: number; avg_loss: number; best: number; worst: number
  /** null = undefined (wins but no losses). 0 = no trades. */
  profit_factor: number | null; max_drawdown: number; total_pnl: number
  avg_exec_latency_ms: number; avg_exit_latency_ms: number
  avg_hold_minutes: number; avg_mfe: number; avg_mae: number
  by_exit_reason: Record<string, { trades: number; pnl: number }>
}

/** One filled order of a position — entry, an averaging buy, or an exit. */
export interface PositionLeg {
  order_id: number
  broker_order_id: string
  /** 1-based index among the BUY legs (0 for a sell). */
  n: number
  /** "Entry" · "Avg #1" · "Exit" */
  label: string
  side: 'BUY' | 'SELL'
  intent: string
  qty: number
  lots: number
  price: number
  limit_price: number
  order_type: string
  bid: number
  ask: number
  ltp_at_order: number
  /** Fill − LTP at the time the order went out. */
  spread_paid: number
  value: number
  retries: number
  latency_ms: number
  at: string
  qty_after: number
  avg_before: number
  /** The position's average price after this leg — the point of the popup. */
  avg_after: number
  avg_delta: number
}

export interface PositionLegs {
  ok: boolean
  error?: string
  position: Position
  legs: PositionLeg[]
  averaging_count: number
  ltp: number
}

export interface RollTarget {
  tsym: string; token: string; exch: string; expiry: string
  lot_size: number; ltp: number; bid: number; ask: number
}

export interface RollPayload {
  target_tsym: string; target_expiry: string; qty: number
  price_type: 'LMT' | 'MKT'; exit_price: number; entry_price: number
  carry_pnl: boolean; carry_target: boolean
}

export interface PaperStatus {
  running: boolean
  lots: number
  open_positions: number
  closed_trades: number
  unrealized_pnl: number
  realized_pnl: number
}

export interface PaperSummary {
  enabled: boolean
  lots: number
  open_positions: number
  unrealized_pnl: number
  realized_pnl: number
  overall_pnl: number
  trades: number
  win_rate: number
}
