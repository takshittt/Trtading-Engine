"""Persistent state for the grid engine.

Everything the strategy needs to survive a crash lives here: the watchlist with
its per-instrument config, every independent lot (never averaged), every signal
received, every rollover executed, and the event log. On boot the engine loads
open lots from this DB and reconciles them against the broker — no amnesia.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from db.engine import Base

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)  # store naive IST


def today_ist() -> datetime:
    """Naive-IST midnight of the current day — the 'since the start of today'
    cutoff for today's realised P&L and same-day reconciliation queries."""
    return now_ist().replace(hour=0, minute=0, second=0, microsecond=0)


class EngineState(Base):
    """Singleton row — the master switch and boot bookkeeping."""
    __tablename__ = "engine_state"

    id         = Column(Integer, primary_key=True)   # always 1
    engine_on  = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=now_ist, onupdate=now_ist)


class UserSettings(Base):
    """Singleton row (id=1) — user preferences that aren't per-instrument:
    WhatsApp alerting (CallMeBot) for the feed-down manual-exit workflow."""
    __tablename__ = "user_settings"

    id               = Column(Integer, primary_key=True)   # always 1
    whatsapp_enabled = Column(Boolean, default=False)
    whatsapp_number  = Column(String, default="")          # E.164 incl. country code, e.g. +9198...
    callmebot_apikey = Column(String, default="")          # CallMeBot personal API key
    alert_on_feed_down = Column(Boolean, default=True)     # send manual-exit alert when Shoonya feed dies with open lots
    updated_at       = Column(DateTime, default=now_ist, onupdate=now_ist)


class Instrument(Base):
    """A watchlist entry + all of its strategy configuration.

    mode="auto"   → trades on AmiBroker webhook signals (the original flow).
    mode="ladder" → trades on user-defined manual buy levels (LadderLevel rows);
                    a separate watchlist in the UI, same math/order pipeline.
    """
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("exch", "sym", "mode", name="uq_instrument_exch_sym_mode"),)

    id        = Column(Integer, primary_key=True)
    sym       = Column(String, nullable=False)      # root symbol: "NATURALGAS", "NIFTY" (options: "NIFTY25000CE")
    exch      = Column(String, nullable=False)      # "MCX" | "NFO"
    mode      = Column(String, nullable=False, default="auto")  # "auto" | "ladder"
    tsym      = Column(String, nullable=False)      # active contract: "NATURALGAS28JUL25"
    token     = Column(String, nullable=False)      # active contract token (WS subscribe key)
    lot_size  = Column(Integer, default=1)          # units per exchange lot
    tick_size = Column(Float, default=0.05)
    expiry    = Column(String, default="")          # "28-JUL-2025" (broker format)
    enabled   = Column(Boolean, default=True)       # per-instrument strategy on/off
    fallback_symbol = Column(String, default="")    # yfinance/http symbol for price when Shoonya feed dies (e.g. "NG=F")

    # --- instrument kind (futures roll month-to-month; options expire, no auto-roll) ---
    instr_type = Column(String, default="FUT")      # "FUT" | "OPT"
    opt_type   = Column(String, default="")         # "" (future) | "CE" | "PE"
    strike     = Column(Float, default=0.0)         # option strike (0 for futures)
    underlying = Column(String, default="")         # root underlying for options, e.g. "NIFTY"

    # --- order routing (per instrument) ---
    product_type = Column(String, default="M")      # "M" = NRML/delivery (carry) | "I" = MIS/intraday
    buy_order_type  = Column(String, default="LMT")  # entries: "LMT" = limit at intended price | "MKT" = marketable (through-touch)
    sell_order_type = Column(String, default="LMT")  # TARGET exits: "LMT" | "MKT". Stop-loss ALWAYS uses MKT for a guaranteed fill.

    # --- exit config (points or percent of price) ---
    target_mode  = Column(String, default="points")   # "points" | "percent"
    target_value = Column(Float, default=0.0)
    target_chain_pct = Column(Float, default=100.0)    # chained rung target = prev entry + this % of the offset (100 = full)
    sl_mode      = Column(String, default="points")
    sl_value     = Column(Float, default=0.0)
    sl_enabled   = Column(Boolean, default=False)     # stop-losses are OFF by default — the user must explicitly enable them; True = SL active for this instrument

    # --- position sizing ---
    sizing_mode   = Column(String, default="fixed")       # "fixed" (default) | "vol_target"
    vol_formula   = Column(String, default="atr_rupee")   # "atr_rupee" (DEFAULT, dimensionally correct): risk/(ATR×lot_size)  |  "notional": risk/(price×ATR), legacy
    risk_per_rung = Column(Float, default=10000.0)         # ₹ risked per rung at 1×ATR
    fixed_lots    = Column(Integer, default=1)
    atr_period    = Column(Integer, default=14)
    min_lots      = Column(Integer, default=1)             # floor when vol is extreme (0 = skip entry instead)
    max_lots_per_rung = Column(Integer, default=10)
    max_rungs     = Column(Integer, default=8)              # max simultaneously open lots
    min_gap_points = Column(Float, default=0.0)             # new rung must be ≥ this far below last open entry (0 = off)
    max_spread_points = Column(Float, default=0.0)          # skip entry when bid/ask spread wider than this (0 = off)
    marketable_ticks = Column(Integer, default=2)           # marketable-limit buffer: automated BUY @ ask + N ticks, AMI_SELL @ bid − N ticks — crosses the spread so it fills like a market order, price-capped (0 = strict at touch)
    sell_signal_mode = Column(String, default="exit_all")   # AmiBroker SELL → "exit_all" | "ignore"

    # --- circuit breaker ---
    cb_enabled   = Column(Boolean, default=True)
    cb_threshold = Column(Float, default=20000.0)   # ₹ loss (positive number)
    cb_tripped   = Column(Boolean, default=False)
    cb_tripped_at = Column(DateTime, nullable=True)
    cb_reason    = Column(String, default="")

    # --- rollover ---
    rollover_days_before  = Column(Integer, default=-1)  # -1 = auto by instrument class
    rollover_date_override = Column(String, default="")  # "" = automated (system-suggested date) | ISO date = manual override
    rollover_state        = Column(String, default="idle")  # idle | due | rolling | done
    last_rolled_expiry   = Column(String, default="")       # expiry we already rolled away from

    # --- manual ladder (mode="ladder" only) ---
    ladder_basis           = Column(String, default="fixed")  # "fixed" (anchor − k·interval) | "support" (4h swing pivots)
    ladder_anchor_price    = Column(Float, default=0.0)       # fixed basis: price of buy 1
    ladder_interval_points = Column(Float, default=0.0)       # fixed basis: drop between consecutive buys
    ladder_num_levels      = Column(Integer, default=5)       # pending levels kept armed (auto-replenished)
    ladder_sr_lookback_days = Column(Integer, default=45)     # support basis: 4h history window for pivots
    ladder_armed           = Column(Boolean, default=False)   # user pressed Confirm — engine may fire levels
    ladder_rearm           = Column(Boolean, default=True)    # re-arm a triggered level once price recovers above it (range-bound re-buy)

    created_at = Column(DateTime, default=now_ist)

    lots = relationship("Lot", back_populates="instrument")


class LadderLevel(Base):
    """One manual-ladder buy level (B1, B2, …). PENDING levels are live triggers;
    when price trades at/below `price` the engine buys through the exact same
    sizing/target/SL/order-verify pipeline as automated signals, marks the level
    TRIGGERED and appends a fresh level at the bottom so the ladder stays
    `ladder_num_levels` deep. Every level is user-editable while PENDING."""
    __tablename__ = "ladder_levels"
    __table_args__ = (Index("ix_ladder_levels_inst_status", "instrument_id", "status"),)

    id            = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    level_no      = Column(Integer, nullable=False)          # 1-based: B1, B2, B3…
    price         = Column(Float, nullable=False)
    lots_override = Column(Integer, nullable=True)           # user-set lots for THIS level; NULL = use sizing math
    target_override = Column(Float, nullable=True)           # user-set target price for THIS level's rung; NULL = auto (chained) math
    status        = Column(String, default="PENDING")        # PENDING | TRIGGERED | CANCELLED
    source        = Column(String, default="fixed")          # fixed | support | manual | replenish
    note          = Column(String, default="")               # e.g. "4h support, 3 touches"
    lot_id        = Column(Integer, ForeignKey("lots.id"), nullable=True)  # the MOST RECENT rung this level opened
    triggered_at  = Column(DateTime, nullable=True)           # when it last fired
    sl_override   = Column(Float, nullable=True)              # user-set SL for THIS level's rung; survives re-places
    fire_count    = Column(Integer, default=0)               # times this level has executed (re-arm on recovery increments)
    shift_applied = Column(Boolean, default=False)           # grid: the levels below have already been shifted from THIS level's first fill (never re-shift on a recycled re-buy)
    created_at    = Column(DateTime, default=now_ist)


class Lot(Base):
    """One independent buy. Never averaged with siblings — this IS the ladder rung.

    entry_price is basis-adjusted on rollover (entry += new_fill - old_exit) so
    the lot's lifetime P&L, target and stop survive contract switches unchanged.
    """
    __tablename__ = "lots"
    __table_args__ = (
        Index("ix_lots_instrument_status", "instrument_id", "status"),
        Index("ix_lots_entry_time", "entry_time"),
    )

    id            = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    seq           = Column(Integer, nullable=False)     # per-instrument: N1, N2, N3…
    exch          = Column(String, nullable=False)
    contract_tsym  = Column(String, nullable=False)     # contract currently held
    contract_token = Column(String, default="")
    lots          = Column(Integer, nullable=False)     # exchange lots
    qty           = Column(Integer, nullable=False)     # units = lots × lot_size
    lot_size      = Column(Integer, default=1)

    entry_price     = Column(Float, default=0.0)        # basis-adjusted avg fill
    raw_entry_price = Column(Float, default=0.0)        # original fill, never adjusted
    entry_time      = Column(DateTime, default=now_ist)
    entry_order_id  = Column(String, default="")
    signal_id       = Column(Integer, ForeignKey("signals.id"), nullable=True)

    target_price  = Column(Float, default=0.0)
    sl_price      = Column(Float, default=0.0)
    anchor_lot_id = Column(Integer, nullable=True)      # previous rung whose entry anchored the target

    # --- broker-resting orders (LMT ladder flow) ---
    target_order_id = Column(String, default="")        # resting target SELL LIMIT at the broker ("" = none)
    target_override = Column(Float, nullable=True)      # user-edited target while RESTING — applied at fill
    sl_override     = Column(Float, nullable=True)      # user-edited SL while RESTING — applied at fill
    filled_adopted  = Column(Integer, default=0)        # units of a WORKING resting entry already adopted as OPEN (partial fills)

    status      = Column(String, default="PENDING")
    # PENDING → order sent, fill unverified   OPEN → verified fill
    # RESTING → entry LIMIT resting at the broker (no position yet; ladder LMT flow)
    # UNCONFIRMED → verify timed out, reconciler must resolve
    # TEMP_EXITED → manually paused (sold at broker) — re-enter restores it on ONE row
    # CLOSED / EXTERNAL_CLOSED / CANCELLED
    exit_reason   = Column(String, default="")          # TARGET | STOPLOSS | AMI_SELL | MANUAL | CB | EXTERNAL
    exit_price    = Column(Float, default=0.0)
    exit_time     = Column(DateTime, nullable=True)
    exit_order_id = Column(String, default="")
    exit_pending  = Column(Boolean, default=False)      # an exit order is in flight
    realized_pnl  = Column(Float, default=0.0)          # ₹, set when closed

    roll_count  = Column(Integer, default=0)
    total_basis = Column(Float, default=0.0)            # cumulative basis added on rolls

    # --- temporary exit (manual pause/re-enter to rescue a losing rung on ONE row) ---
    temp_exit_price = Column(Float, default=0.0)         # broker fill when paused
    temp_exit_loss  = Column(Float, default=0.0)         # points carried = entry − temp_exit_price (>0 = loss)
    temp_exit_time  = Column(DateTime, nullable=True)    # when paused
    temp_exit_trigger_price = Column(Float, default=0.0) # limit temp-exit: fire when lp ≤ this (OPEN lots)
    reenter_trigger_price   = Column(Float, default=0.0) # limit re-enter: fire when lp ≤ this (TEMP_EXITED lots)
    carry_recovery  = Column(Float, default=0.0)         # pts of loss ALREADY parked into target across temp-exit cycles (prevents double-count)

    atr_at_entry  = Column(Float, default=0.0)
    timeframe_min = Column(Integer, default=0)
    source        = Column(String, default="automated")  # "automated" (webhook) | "ladder" (manual levels)
    product_type  = Column(String, default="M")           # order product used at entry ("M"=NRML | "I"=MIS) — exits reuse it
    notes         = Column(Text, default="")

    instrument = relationship("Instrument", back_populates="lots")


class Signal(Base):
    """Every incoming trade signal, whatever became of it."""
    __tablename__ = "signals"

    id            = Column(Integer, primary_key=True)
    ts            = Column(DateTime, default=now_ist, index=True)
    source        = Column(String, default="amibroker")
    sym           = Column(String, default="")
    action        = Column(String, default="")     # BUY | SELL
    timeframe_min = Column(Integer, default=0)
    payload       = Column(JSON, default=dict)
    status        = Column(String, default="received")  # received | executed | skipped | failed
    reason        = Column(Text, default="")
    lot_id        = Column(Integer, nullable=True)


class RolloverEvent(Base):
    __tablename__ = "rollover_events"

    id            = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    lot_id        = Column(Integer, ForeignKey("lots.id"), nullable=True)
    ts            = Column(DateTime, default=now_ist)
    old_tsym      = Column(String, default="")
    new_tsym      = Column(String, default="")
    old_exit_price  = Column(Float, default=0.0)
    new_entry_price = Column(Float, default=0.0)
    basis         = Column(Float, default=0.0)      # new_entry − old_exit (cost of the roll)
    qty           = Column(Integer, default=0)
    status        = Column(String, default="done")  # done | failed | partial
    note          = Column(Text, default="")


class LogEntry(Base):
    """Structured event log — 1-month retention, purged daily."""
    __tablename__ = "logs"
    __table_args__ = (Index("ix_logs_ts", "ts"),)

    id       = Column(Integer, primary_key=True)
    ts       = Column(DateTime, default=now_ist)
    level    = Column(String, default="info")     # info | warn | error | trade | math
    category = Column(String, default="")         # SIGNAL | MATH | ORDER | EXIT | ROLLOVER | CB | RECONCILE | FEED | SYSTEM
    sym      = Column(String, default="")
    message  = Column(Text, default="")
    data     = Column(JSON, default=dict)


class EconEvent(Base):
    """Economic / market-regime calendar shown on the dashboard."""
    __tablename__ = "econ_events"

    id     = Column(Integer, primary_key=True)
    dt     = Column(DateTime, nullable=False)     # event time IST
    title  = Column(String, nullable=False)
    impact = Column(String, default="medium")     # low | medium | high
    region = Column(String, default="IN")
    note   = Column(Text, default="")
    auto   = Column(Boolean, default=False)       # generated by recurring rule vs user-added
