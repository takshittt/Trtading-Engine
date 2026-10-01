"""Runtime configuration for the Reversal Strategy.

The strategy config is stored in the DB (single-row `StrategyConfig`) so the UI
can edit it live. This module holds process-level constants and defaults only.
"""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# --- Timezone / market ---------------------------------------------------
# The live values are DB-backed (StrategyConfig, editable from the UI); these
# are only the seed for a fresh row and the fallback if that row can't be read.
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = (9, 15)   # 09:15 IST
MARKET_CLOSE = (15, 40)  # 15:40 IST — extended from 15:30 effective 03-Aug-2026 (NSE F&O CAS)

# Seed holiday calendar. Stored on the config row as a comma/newline separated
# list of YYYY-MM-DD so a new year's dates are a UI edit, not a code change.
DEFAULT_MARKET_HOLIDAYS = ",".join([
    "2026-01-26", "2026-03-06", "2026-03-25", "2026-04-01", "2026-04-14",
    "2026-05-01", "2026-08-15", "2026-10-02", "2026-11-09", "2026-12-25",
])

# --- Broker gateway ------------------------------------------------------
# LIVE by default: "shoonya" consumes the isolated Shoonya Gateway (REST+WS) that
# holds the credentials (the Grid gateway). "mock" is an explicit offline
# simulator for development only — it must be requested, never the default.
BROKER_MODE = os.getenv("SWING_BROKER_MODE", "shoonya").lower()

# NSE F&O exchange + product for futures (NRML = positional).
EXCHANGE = os.getenv("SWING_EXCHANGE", "NFO")
PRODUCT_TYPE = os.getenv("SWING_PRODUCT_TYPE", "M")  # M=NRML, I=MIS, C=CNC

# Dev convenience: treat the market as always open. NEVER enable in production.
IGNORE_MARKET_HOURS = os.getenv("SWING_IGNORE_MARKET_HOURS", "false").lower() in ("1", "true", "yes")

# --- Signal source -------------------------------------------------------
# "mock" spins up a fake signal generator (development only, must be requested
# explicitly). Anything else: no automatic source — signals are added manually.
SIGNAL_SOURCE = os.getenv("SWING_SIGNAL_SOURCE", "none").lower()

# --- Defaults for a fresh StrategyConfig row -----------------------------
# Reserve is NOT stored: it is derived as (100 - hard_cap_pct), mathematically
# locked for averaging. Target/SL "mode" is chosen per-buy in the modal, not
# globally. There is no post-SL cooldown.
DEFAULTS = {
    "total_budget": 1_000_000.0,      # ₹ allocated to this strategy (user-defined)
    "soft_cap_pct": 80.0,             # yellow warning
    "hard_cap_pct": 90.0,             # block new buys; reserve = 100 - this = 10%
    "max_averaging_buys": 3,          # per stock
    # Auto Target/SL calculation parameters
    "atr_period": 14,
    "atr_target_mult": 2.0,           # AUTO target = entry + atr*mult (fallback)
    "atr_sl_mult": 1.5,               # AUTO SL     = entry - atr*mult
    # Signal levels come from Amibroker's support & resistance. These two are
    # the last-resort fallback only, for a signal that arrives with no S&R and
    # no ATR — they no longer price signals on their own.
    "default_target_pct": 4.0,
    "default_sl_pct": 2.0,
    # Per-buy Target/SL mode in the Buy modal ("auto" | "manual"), not a global
    # pricing mode for signals.
    "target_mode": "auto",
    "sl_mode": "auto",
    # Reject signals whose reward:risk at the signal price is below this.
    "min_rr": 1.5,
    # Operating modes
    "stock_selection_mode": "automated",   # "automated" (Amibroker + manual) | "manual" (manual only)
    # "manual" (user buys from the dashboard) | "automated" (auto_exec trades the
    # real book off actionable signals). Automated stands down entirely while
    # exit_mode is "manual" — see app/services/auto_exec.py.
    "execution_mode": "manual",
    # Automated execution: risk-based sizing plus the two ceilings that bound it.
    "auto_risk_pct": 1.0,             # % of total_budget risked per auto entry
    "auto_max_lots": 5,               # per-trade lot ceiling
    "auto_max_positions": 5,          # concurrent live auto positions
    # How a stop-loss breach is handled. "auto" squares off immediately;
    # "manual" flags the position and waits for the user to exit or move the stop.
    "exit_mode": "auto",
    # Trailing SL after target is hit
    "trailing_buffer_pct": 1.0,       # trailing SL = peak - buffer% (activated at target)
    # Risk
    "global_sl_pct": 5.0,             # portfolio MTM stop (% of total budget)
    # Execution (LIMIT-only)
    "order_retries": 3,
    "slippage_ticks": 2,              # limit price = LTP ± ticks*tick_size
    # Ops
    "averaging_mode": "manual",       # "auto" | "manual"
    "reconcile_seconds": 60,          # broker reconciliation interval
    "signals_halted": False,          # set by global SL / kill switch
    # Paper trading — auto-executes every signal against the live Shoonya
    # bid/ask without ever sending a broker order. For strategy/latency testing.
    "paper_trading": False,
    "paper_lots": 1,                  # lots per simulated entry & averaging buy
    # Auto-rollover — roll an open position to the next expiry this many
    # trading-calendar days before the held contract expires.
    "auto_rollover": False,
    "auto_rollover_days": 2,
    # Market session (IST). Editable from the UI because the exchange moves
    # these — the 15:30 → 15:40 F&O extension was a code change and a redeploy.
    "market_open_time": "%02d:%02d" % MARKET_OPEN,
    "market_close_time": "%02d:%02d" % MARKET_CLOSE,
    "market_holidays": DEFAULT_MARKET_HOLIDAYS,
    # Treat the market as always open. For Saturday mock sessions and demos —
    # ticks are accepted and acted on with the exchange shut, so this is unsafe
    # to leave on. SWING_IGNORE_MARKET_HOURS forces it on regardless.
    "ignore_market_hours": False,
}

# Default tick size (₹) for NSE stock futures limit-price rounding.
DEFAULT_TICK_SIZE = 0.05

_BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
_DEFAULT_DB = os.path.join(_BACKEND_DIR, "reversal_strategy.db")
_LEGACY_DB = os.path.join(_BACKEND_DIR, "swing_swager.db")  # pre-rename name; still used by older installs
if not os.path.exists(_DEFAULT_DB) and os.path.exists(_LEGACY_DB):
    _DEFAULT_DB = _LEGACY_DB
DB_PATH = os.getenv("SWING_DB_PATH", _DEFAULT_DB)

# Full SQLAlchemy URL. Defaults to the local SQLite file, so demo/dev is
# unchanged. Set SWING_DB_URL=postgresql://user:pass@host/dbname to run on the
# shared Neon Postgres DB (mysql+pymysql:// URLs still work too). The engine
# stays SQLite-or-else dialect-aware in db/engine.py.
DATABASE_URL = os.getenv("SWING_DB_URL", f"sqlite:///{os.path.abspath(DB_PATH)}")
