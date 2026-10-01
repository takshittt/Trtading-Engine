"""SQLAlchemy models — the persistence layer for Reversal Strategy.

Every position, signal, order, blacklist entry, config value and audit log line
lives here so the system can restore its exact pre-crash state on restart.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.config import DEFAULT_MARKET_HOLIDAYS


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class StrategyConfig(Base):
    """Single-row live-editable strategy configuration.

    Reserve % is derived (100 - hard_cap_pct), not stored. No global target mode
    (chosen per-buy). No post-SL cooldown.
    """
    __tablename__ = "strategy_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    total_budget: Mapped[float] = mapped_column(Float)
    soft_cap_pct: Mapped[float] = mapped_column(Float)
    hard_cap_pct: Mapped[float] = mapped_column(Float)
    max_averaging_buys: Mapped[int] = mapped_column(Integer)
    atr_period: Mapped[int] = mapped_column(Integer, default=14)
    atr_target_mult: Mapped[float] = mapped_column(Float, default=2.0)
    atr_sl_mult: Mapped[float] = mapped_column(Float, default=1.5)
    target_mode: Mapped[str] = mapped_column(String(10), default="auto")   # auto | manual
    sl_mode: Mapped[str] = mapped_column(String(10), default="auto")       # auto | manual
    default_target_pct: Mapped[float] = mapped_column(Float)
    default_sl_pct: Mapped[float] = mapped_column(Float)
    stock_selection_mode: Mapped[str] = mapped_column(String(10), default="automated")
    # "manual"    -> the user buys from the dashboard; signals are only ever a suggestion
    # "automated" -> auto_exec acts on actionable signals against the REAL book
    execution_mode: Mapped[str] = mapped_column(String(10), default="manual")
    # --- Automated execution sizing & limits -----------------------------
    # Risk-based sizing: every auto entry risks the same slice of the budget,
    # measured to its own stop, so a wide stop takes fewer lots and a tight one
    # takes more. Sizing off a flat lot count instead would let stop distance
    # decide the risk, which is the thing that is supposed to be held constant.
    auto_risk_pct: Mapped[float] = mapped_column(Float, default=1.0)
    # Ceiling on one trade, so a very tight stop cannot size into a huge
    # position off a small rupee risk.
    auto_max_lots: Mapped[int] = mapped_column(Integer, default=5)
    # Ceiling on concurrent auto positions. The budget hard cap is a *rupee*
    # limit and bounds nothing about how many trades a busy morning opens.
    auto_max_positions: Mapped[int] = mapped_column(Integer, default=5)
    # "auto"   -> a stop-loss breach squares the position off straight away
    # "manual" -> it is flagged for the user instead, and nothing is sold
    exit_mode: Mapped[str] = mapped_column(String(10), default="auto")
    trailing_buffer_pct: Mapped[float] = mapped_column(Float, default=1.0)
    global_sl_pct: Mapped[float] = mapped_column(Float)
    averaging_mode: Mapped[str] = mapped_column(String(10))
    order_retries: Mapped[int] = mapped_column(Integer)
    slippage_ticks: Mapped[int] = mapped_column(Integer)
    reconcile_seconds: Mapped[int] = mapped_column(Integer)
    signals_halted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Paper trading — simulated fills off the live Shoonya bid/ask, no broker order.
    paper_trading: Mapped[bool] = mapped_column(Boolean, default=False)
    paper_lots: Mapped[int] = mapped_column(Integer, default=1)
    # Auto-rollover: carry an open position to the next expiry as the held
    # contract runs out, rather than letting it expire on the book.
    auto_rollover: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_rollover_days: Mapped[int] = mapped_column(Integer, default=2)
    # Market session (IST), live-editable: the exchange changes its hours and
    # publishes a new holiday list every year, neither of which should need a
    # redeploy. Holidays are a comma/newline separated list of YYYY-MM-DD.
    # Minimum reward:risk a signal must clear at its own signal price to be
    # actionable. 1.5 = the target must be at least 1.5x as far away as the stop.
    min_rr: Mapped[float] = mapped_column(Float, default=1.5)
    market_open_time: Mapped[str] = mapped_column(String(5), default="09:15")
    market_close_time: Mapped[str] = mapped_column(String(5), default="15:40")
    # Seeded (not "") so the ADD COLUMN backfill gives an existing database the
    # real calendar — an empty default would quietly make every holiday tradable.
    market_holidays: Mapped[str] = mapped_column(Text, default=DEFAULT_MARKET_HOLIDAYS)
    ignore_market_hours: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Signal(Base):
    # Named `signals` until this backend had to share a database with another
    # bot that already owned that name and used a completely different shape
    # (sym/action/payload vs symbol/signal_type/candle_id). Prefixed rather
    # than scoped to a separate schema so one database can host both.
    # db.engine._rename_legacy_signals() renames an existing `signals` table
    # in place on upgrade, so no history is lost.
    __tablename__ = "swing_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    signal_type: Mapped[str] = mapped_column(String(4))       # BUY / SELL
    timeframe: Mapped[str] = mapped_column(String(4))         # 1H / 4H / 1D
    signal_time: Mapped[datetime] = mapped_column(DateTime)
    candle_id: Mapped[str] = mapped_column(String(60), index=True)  # symbol|tf|candle_ts
    candle_closed: Mapped[bool] = mapped_column(Boolean, default=True)  # Amibroker sends closed-candle only
    ltp: Mapped[float] = mapped_column(Float, default=0.0)
    # Price the moment the signal landed. Frozen forever — `ltp` above is
    # rewritten live on every read, so without this the entry level the strategy
    # actually fired at would be lost the first time a tick arrived.
    signal_ltp: Mapped[float] = mapped_column(Float, default=0.0)
    atr: Mapped[float] = mapped_column(Float, default=0.0)          # from Amibroker, for AUTO target/SL
    resistance: Mapped[float] = mapped_column(Float, default=0.0)   # nearest resistance from Amibroker
    support: Mapped[float] = mapped_column(Float, default=0.0)      # nearest support from Amibroker
    # Reward:risk as it was at the signal price. The `rr` on the API is measured
    # against the live price and so drifts; this is the number the min_rr filter
    # actually judged, kept so a decision can be explained after the fact.
    signal_rr: Mapped[float] = mapped_column(Float, default=0.0)
    target: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    expiry: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(20), default="NEW")
    # NEW / STALE / BLACKLISTED / ACTED / IGNORED / AVERAGING
    # + paper-trading outcomes: EXECUTED / AVERAGED / SQUARED_OFF / EXEC_FAILED
    manual_add: Mapped[bool] = mapped_column(Boolean, default=False)  # user searched & added
    note: Mapped[str] = mapped_column(String(120), default="")
    # -- execution record (what acted on it, if anything) ---------------------
    paper: Mapped[bool] = mapped_column(Boolean, default=False)
    # The real-money counterpart of `paper`: automated execution took this one
    # against the live book. Without it the board cannot say whether a settled
    # signal cost money or only pretended to.
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    position_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exec_price: Mapped[float] = mapped_column(Float, default=0.0)
    exec_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(10), default="OPEN")  # OPEN / CLOSED
    avg_price: Mapped[float] = mapped_column(Float)
    qty: Mapped[int] = mapped_column(Integer)                 # total quantity
    lots: Mapped[int] = mapped_column(Integer)
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    target: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    target_method: Mapped[str] = mapped_column(String(20), default="manual")  # manual/atr/resistance
    sl_method: Mapped[str] = mapped_column(String(20), default="manual")      # manual/atr
    # ATR and the S&R levels as they were at entry. Kept because an averaging buy
    # has to recompute auto levels with the same inputs — the originating signal
    # is long gone by then, and without these the ATR method degrades to a
    # percentage fallback while still calling itself "atr". Support matters for
    # the same reason: without it an averaged position's stop silently stops
    # being the support level and falls back to ATR.
    atr: Mapped[float] = mapped_column(Float, default=0.0)
    resistance: Mapped[float] = mapped_column(Float, default=0.0)
    support: Mapped[float] = mapped_column(Float, default=0.0)
    # Trailing-SL phase (activated when target is first hit)
    target_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    trailing_active: Mapped[bool] = mapped_column(Boolean, default=False)
    trail_peak: Mapped[float] = mapped_column(Float, default=0.0)
    trailing_sl: Mapped[float] = mapped_column(Float, default=0.0)
    averaging_count: Mapped[int] = mapped_column(Integer, default=0)
    ltp: Mapped[float] = mapped_column(Float, default=0.0)
    expiry: Mapped[str] = mapped_column(String(20), default="")
    margin_used: Mapped[float] = mapped_column(Float, default=0.0)
    used_reserve: Mapped[bool] = mapped_column(Boolean, default=False)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    exit_price: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[str] = mapped_column(String(20), default="")  # TARGET/SL/MANUAL/GLOBAL_SL/KILL/SIGNAL
    exit_failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exit_retries: Mapped[int] = mapped_column(Integer, default=0)
    # Stop breached while exit_mode was "manual": nothing was sold, the position
    # is waiting on the user. Cleared when price recovers above the stop, when
    # the user moves the stop, or when the position is finally exited.
    sl_breached: Mapped[bool] = mapped_column(Boolean, default=False)
    sl_breached_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # -- paper trading -------------------------------------------------------
    is_paper: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Who opened it: "manual" (the user clicked Buy) or "auto" (auto_exec, or the
    # paper trader). Orthogonal to is_paper, which says which BOOK it lives in —
    # the pair is what distinguishes a position you placed from one the machine
    # placed with real money, and that is not otherwise recoverable from the row.
    # Defaults to "manual", which is exactly right for every row that predates
    # automated execution: there was nothing else that could have opened them.
    opened_by: Mapped[str] = mapped_column(String(8), default="manual")

    # -- provenance & execution timing (what fired this, and how fast) -------
    # Every leg of the round trip is stamped so the journal can answer "how long
    # between Amibroker printing the signal and the fill actually happening".
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timeframe: Mapped[str] = mapped_column(String(4), default="")
    signal_price: Mapped[float] = mapped_column(Float, default=0.0)   # LTP when the signal fired
    signal_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)      # Amibroker candle close
    signal_received_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # landed on our server
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)      # fill stamped
    exec_latency_ms: Mapped[int] = mapped_column(Integer, default=0)   # received -> filled
    # The FIRST entry leg's fill, kept apart from `avg_price`. An averaging buy
    # rewrites the weighted average, so measuring entry slippage against
    # avg_price reported every later averaging buy as slippage on the original
    # entry — a stock averaged up twice looked like a ₹4 bad fill when the
    # entry actually filled a tick off the signal.
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    entry_ltp: Mapped[float] = mapped_column(Float, default=0.0)   # LTP when the entry order went out
    entry_bid: Mapped[float] = mapped_column(Float, default=0.0)
    entry_ask: Mapped[float] = mapped_column(Float, default=0.0)

    exit_signal_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exit_executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exit_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    exit_bid: Mapped[float] = mapped_column(Float, default=0.0)
    exit_ask: Mapped[float] = mapped_column(Float, default=0.0)

    # -- excursion tracking (best/worst the trade ever went, for tuning T/SL) -
    peak_price: Mapped[float] = mapped_column(Float, default=0.0)
    trough_price: Mapped[float] = mapped_column(Float, default=0.0)
    # `qty` is decremented to 0 as a position is closed out, so the size the
    # trade actually carried has to be kept or every ₹ figure computed after
    # the close (excursions, exposure) silently evaluates to zero.
    exit_qty: Mapped[int] = mapped_column(Integer, default=0)

    orders: Mapped[list["Order"]] = relationship(back_populates="position")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[Optional[int]] = mapped_column(ForeignKey("positions.id"), nullable=True)
    broker_order_id: Mapped[str] = mapped_column(String(40), default="")
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    side: Mapped[str] = mapped_column(String(4))              # BUY / SELL
    order_type: Mapped[str] = mapped_column(String(8), default="LIMIT")  # LIMIT / MARKET
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    filled_price: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(12), default="PLACED")
    # PLACED / PENDING / FILLED / REJECTED / CANCELLED
    intent: Mapped[str] = mapped_column(String(16), default="ENTRY")
    # ENTRY / AVERAGING / TARGET / SL / PARTIAL / GLOBAL_SL / KILL / ROLLOVER
    retries: Mapped[int] = mapped_column(Integer, default=0)
    # Paper legs are simulated against the live book — the bid/ask they crossed
    # is kept so slippage-vs-LTP can be audited per leg.
    is_paper: Mapped[bool] = mapped_column(Boolean, default=False)
    bid: Mapped[float] = mapped_column(Float, default=0.0)
    ask: Mapped[float] = mapped_column(Float, default=0.0)
    ltp_at_order: Mapped[float] = mapped_column(Float, default=0.0)
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    position: Mapped["Position"] = relationship(back_populates="orders")


class Blacklist(Base):
    __tablename__ = "blacklist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(120), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Rollover(Base):
    __tablename__ = "rollovers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"))
    symbol: Mapped[str] = mapped_column(String(40))
    old_expiry: Mapped[str] = mapped_column(String(20))
    new_expiry: Mapped[str] = mapped_column(String(20))
    old_target: Mapped[float] = mapped_column(Float)
    new_target: Mapped[float] = mapped_column(Float)
    old_sl: Mapped[float] = mapped_column(Float)
    new_sl: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    symbol: Mapped[str] = mapped_column(String(40), default="", index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(10), default="INFO")  # INFO/WARN/ERROR
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
