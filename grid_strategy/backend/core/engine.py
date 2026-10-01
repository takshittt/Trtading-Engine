"""GridEngine — the ladder/grid strategy core.

Signal flow:   AmiBroker webhook → pre-trade math (all gates logged) → market
order via gateway → fill VERIFIED against the broker order book → post-trade
math on the ACTUAL fill → independent Lot row (never averaged).

Tick flow:     gateway WS ticks (plus 1s REST quote fallback when the socket
goes quiet) → per-lot target / stop-loss checks → verified exits.

Safety nets:   60s broker reconciliation, 10s feed heartbeat, per-instrument
circuit breaker, persistent DB state reloaded and reconciled on boot.
"""

import asyncio
import logging
import time as _time
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func

from config.settings import settings
from core.event_log import event_log
from core.exits import compute_sl, compute_target, open_lot_pnl
from core.levels import fixed_levels, supports_below
from core.orders import FillReport, OrderManager, TERMINAL, _avgprc, _fillshares
from core.rollover import (
    IST, default_days_before, default_slippage_threshold, instrument_class, is_trading_holiday,
    parse_expiry, rollover_date, tooltip_for, window_for,
)
from core.sizing import compute_size
from core.tick_validator import TickValidator
from core.fallback_feed import FallbackFeed
from core.alerts import send_whatsapp
from db.engine import db_session
from db.models import (
    EconEvent, EngineState, Instrument, LadderLevel, LogEntry, Lot, RolloverEvent,
    Signal, UserSettings, now_ist, today_ist,
)
from gateway_client.rest import GatewayRest
from gateway_client.ws import GatewayWS

logger = logging.getLogger("grid.engine")

# Before a lot is closed against the broker, the shortfall must be agreed by
# this many reconcile passes AND have persisted for this long in wall-clock time.
# Passes alone were not enough: reconciles also fire on feed-recovery events, so
# a flapping feed once delivered three "passes" in 64 seconds and closed two real
# positions. Time is the honest measure of "this is not a blip".
_GHOST_CONFIRM_PASSES = 3
_GHOST_CONFIRM_SECONDS = 180.0


# A ghost-close invents an exit price from the last tick. Beyond this age that
# price is not evidence of anything, and a fabricated number in the ledger is
# worse than a rung left open for another pass.
_GHOST_MARK_MAX_AGE = 120.0

# A rung whose exit order is unresolved for longer than this is effectively
# stop-less, and the operator has to be told so.
_EXIT_STUCK_ALERT_AFTER = 300.0

# Ceiling on how often ONE rung may have its resting TARGET order re-placed.
#
# Every re-place is a live SELL at the broker. If anything ever makes a filled
# target read as unfilled, the desired-state sync re-places it every pass, and
# each one executes — a single long rung silently becomes a growing real short.
# That is exactly what a missing fill-quantity field did. The specific cause is
# fixed, but the shape of the failure is generic: the sync is a loop that places
# orders, and a loop that places real orders needs a stop. A handful of manual
# target edits stays well under this; a per-pass loop trips it within minutes.
_TARGET_REPLACE_MAX = 5
_TARGET_REPLACE_WINDOW = 900.0

# Same ceiling for a level's resting BUY. A level that keeps being rejected or
# cancelled at the broker is re-placed by the sync every retry window, forever.
# A rejected order does not fill, so this never cost money the way the target
# loop did — but that is luck, not design, and the exchange counts every one of
# them against the account's order-to-trade ratio. Deliberately looser than the
# target ceiling: a level legitimately re-places across pauses, holds and day
# rollovers, none of which are faults.
_ENTRY_REPLACE_MAX = 8
_ENTRY_REPLACE_WINDOW = 3600.0

# Exits get BACKOFF, never a ceiling.
#
# A cap here would be the dangerous kind of fix: refusing to retry an exit
# leaves a position that wanted out still open, which is the failure this whole
# system exists to prevent. But retrying a rejected exit every 5 seconds is 720
# order attempts an hour at the broker. So the retry always happens — it just
# slows down, and says so, instead of hammering.
_EXIT_RETRY_BASE = 5.0
_EXIT_RETRY_MAX = 60.0
_EXIT_FAIL_ALARM_AFTER = 5


class _BookUnreadable(Exception):
    """The broker's order book could not be read.

    Distinct from "the order is not there", because the two must never be
    confused: the duplicate-order check reads absence as permission to place.
    """


class PriceState:
    __slots__ = ("lp", "bid", "ask", "wall_ts", "mono_ts", "source")

    def __init__(self):
        self.lp = 0.0
        self.bid = 0.0
        self.ask = 0.0
        self.wall_ts = ""
        self.mono_ts = 0.0
        self.source = ""

    def age(self) -> float:
        return 1e9 if self.mono_ts == 0 else _time.monotonic() - self.mono_ts

    def as_dict(self) -> dict:
        return {"lp": self.lp, "bid": self.bid, "ask": self.ask,
                "ts": self.wall_ts, "age": round(min(self.age(), 9999), 1),
                "source": self.source}


def market_open_now(exch: str) -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    if is_trading_holiday(now.date(), exch):
        return False
    t = now.time()
    if exch == "MCX":
        return dtime(9, 0) <= t <= dtime(23, 55)
    return dtime(9, 15) <= t <= dtime(15, 30)


def _price_block(ps: "PriceState", ref: float) -> dict:
    """Price for the UI, falling back to the broker's last reported price when no
    tick is live. `source` says which one it is, so a carried-over price is never
    mistaken for a live one."""
    d = ps.as_dict()
    if not (d.get("lp") or 0) > 0 and ref > 0:
        d["lp"] = ref
        d["source"] = "broker-last"
    return d


def minutes_since_open(exch: str) -> float:
    """Minutes since this exchange opened today (negative before the bell)."""
    now = datetime.now(IST)
    start = dtime(9, 0) if exch == "MCX" else dtime(9, 15)
    opened = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    return (now - opened).total_seconds() / 60.0


# How long a level waits before retrying a placement that failed. The open is
# when a retry matters most — the broker is still releasing margin and a missed
# window costs the whole morning — so retry briskly for the first half hour and
# settle down afterwards.
_RETRY_FAST_SECS = 60.0
_RETRY_SECS = 300.0
_RETRY_FAST_WINDOW_MIN = 30.0


def level_retry_secs(exch: str) -> float:
    since = minutes_since_open(exch)
    return _RETRY_FAST_SECS if 0 <= since <= _RETRY_FAST_WINDOW_MIN else _RETRY_SECS


class GridEngine:
    def __init__(self):
        self.rest = GatewayRest()
        self.ws = GatewayWS(self._on_tick, self._on_order_update)
        self.om = OrderManager(self.rest)
        self.prices: dict[str, PriceState] = {}            # "EXCH|TOKEN" → PriceState
        self.locks: dict[int, asyncio.Lock] = {}           # instrument_id → serialize orders
        self.unmanaged: dict[int, int] = {}                # instrument_id → broker-only units
        # (contract) → (deficit, passes seen, monotonic of the first sighting).
        # A lot is only ever closed against the broker after the SAME shortfall
        # repeats across passes AND survives a wall-clock window, so neither a
        # single bad snapshot nor a burst of event-driven reconciles can destroy
        # a real position.
        self._ghost_confirm: dict[tuple, tuple[int, int, float]] = {}
        self._offhours_gap_logged: set[tuple] = set()   # say the off-hours gap once, not every pass
        self._watched_exchanges: set[str] = set()       # exchanges we hold or watch, for session checks
        self._stuck_exit_alerted: dict[int, float] = {}  # lot_id → last stuck-exit alert (hourly)
        self._exit_pending_since: dict[int, float] = {}  # lot_id → monotonic when the reconciler FIRST saw its exit unresolved
        # Last broker account snapshot (positions / funds / profile) for the P&L
        # panel. Served immediately and refreshed behind the caller: fetching it
        # inline meant three sequential broker round trips on every poll, so the
        # panel froze for seconds at a time while the rest of the dashboard —
        # which reads a pushed snapshot and never calls the broker — stayed live.
        self._broker_snap: dict = {"at": 0.0, "positions": None, "funds": None,
                                   "account": None, "error": ""}
        self._broker_snap_inflight: bool = False
        self._snap_cache: dict | None = None      # last dashboard snapshot (see snapshot())
        self._snap_cache_at: float = 0.0
        # "EXCH|TOKEN" → last price the BROKER reported for a position. Display
        # only: with no ticks (market shut, or a fresh start after the close) the
        # dashboard showed dashes for LTP and ₹0 P&L on real money. This fills
        # those in without ever reaching the trading path.
        self.ref_prices: dict[str, float] = {}
        self._session_suspect: bool = False                # latched while reads look unreadable
        self.broker_connected: bool = False
        self.global_cb_tripped: bool = False
        self.feed_state: str = "down"                      # live | stale | down
        self._exit_cooldown: dict[int, float] = {}         # lot_id → monotonic ts of last exit try
        self._stopless_warned: dict[int, float] = {}       # lot_id → monotonic ts of last "OPEN with no SL" alarm (hourly)
        self._global_cb_reason: str = ""                   # why the global CB tripped (shown in the UI, cleared on reset)
        self._cb_last_eval: dict[int, float] = {}          # instrument_id → monotonic ts (throttle)
        self._recent_signals: dict[tuple, float] = {}      # dedupe (sym,action,tf,bar_ts)
        self._ladder_last_eval: dict[int, float] = {}      # instrument_id → monotonic ts (1s throttle)
        self._ladder_cooldown: dict[int, float] = {}       # level_id → monotonic ts of last failed fire
        self._resting_last_sync: dict[int, float] = {}     # instrument_id → monotonic ts (resting-order sync throttle)
        self._order_gone_at: dict[str, float] = {}         # order_id → monotonic ts of FIRST absent-from-book sighting (2-strike rule)
        self._levels_inflight: set[int] = set()            # level_ids with a buy in progress
        self._uncertain_placements: dict[int, dict] = {}   # instrument_id → placement whose HTTP call failed (may be live at broker)
        self._temp_inflight: set[int] = set()              # lot_ids with a temp-exit/re-enter order in progress
        # Order-rate budgets: (bucket, key) → monotonic ts of each order that
        # reached the broker, plus the buckets whose loop has been halted.
        self._rate_hist: dict[tuple[str, int], list[float]] = {}
        self._rate_halted: set[tuple[str, int]] = set()
        self._exit_fails: dict[int, int] = {}              # lot_id → consecutive failed exit attempts (drives backoff)
        self._broker_net_seen: dict[tuple, int] = {}       # (exch, tsym, prod) → broker net qty, from the last reconcile
        self._broker_net_at: float = 0.0                   # monotonic of that snapshot (0 = never read)
        self.funds: dict = {}                              # cached broker funds/margin (refreshed ~15s) for the UI
        self._rolling: set[int] = set()                    # instrument_ids with an active sniper-scan / roll task
        self._level_miss_logged: dict[int, float] = {}     # level_id → monotonic ts of last "price crossed, no order" warn (1h throttle)
        self._ticks = TickValidator()                      # outlier/spike filter for every incoming price
        self._tick_reject_log: dict[str, float] = {}       # key → last monotonic we logged a rejected tick (throttle)
        self.fallback = FallbackFeed()                     # yfinance/http/finnhub reference price when Shoonya dies
        self._fallback_prices: dict[int, dict] = {}        # instrument_id → {lp, source, delayed} reference (degraded mode)
        self._degraded_since: float = 0.0                  # monotonic when Shoonya feed died with open lots (0 = healthy)
        self._last_exit_alert: float = 0.0                 # monotonic of last WhatsApp manual-exit alert
        self._alert_inflight: bool = False                 # guard against overlapping alert sends
        self._tasks: list[asyncio.Task] = []
        self.on_snapshot = None                            # async fn(dict) set by runtime
        self._engine_on: bool | None = None   # master switch cache; None = not yet read (see engine_on)
        self.booted_at = datetime.now(IST).isoformat(timespec="seconds")

    # ================= lifecycle =================

    async def start(self) -> None:
        await self.rest.start()
        await self.ws.start()
        event_log.write("SYSTEM", "Engine boot — reconciling persisted state against broker", level="info")
        await self._boot_reconcile()
        self._recover_rollover_states()
        await self._subscribe_watchlist()
        self._tasks = [
            asyncio.create_task(self._reconcile_loop()),
            asyncio.create_task(self._price_keeper_loop()),
            asyncio.create_task(self._rollover_loop()),
            asyncio.create_task(self._housekeeping_loop()),
            asyncio.create_task(self._broker_status_loop()),
        ]
        event_log.write("SYSTEM", "Engine started", level="info")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.ws.stop()
        await self.rest.stop()
        event_log.flush()   # drain queued log rows before exit

    def lock_for(self, instrument_id: int) -> asyncio.Lock:
        if instrument_id not in self.locks:
            self.locks[instrument_id] = asyncio.Lock()
        return self.locks[instrument_id]

    # ================= engine on/off =================

    def engine_on(self) -> bool:
        """The master switch, from memory.

        This is read on every buy gate, every ladder evaluation and every
        /health probe, and it used to be a round trip to a database ~36ms away —
        so a liveness check cost 148ms and the tick path paid a WAN hop it did
        not need. THIS process is the only writer (set_engine_on below), so the
        in-memory copy cannot go stale behind our back; it is seeded from the DB
        on first read in case the row was changed while the engine was down.
        """
        if self._engine_on is None:
            with db_session() as db:
                st = db.get(EngineState, 1)
                self._engine_on = bool(st and st.engine_on)
        return self._engine_on

    def set_engine_on(self, on: bool) -> None:
        with db_session() as db:
            st = db.get(EngineState, 1)
            st.engine_on = on
            db.commit()
        self._engine_on = bool(on)   # keep the cache honest, after the commit
        event_log.write("SYSTEM", f"Strategy master switch → {'ON' if on else 'OFF'}", level="warn")

    # ================= subscriptions =================

    async def _subscribe_watchlist(self) -> None:
        with db_session() as db:
            instruments = db.query(Instrument).all()
            open_lots = db.query(Lot).filter(Lot.status.in_(["OPEN", "PENDING", "UNCONFIRMED", "TEMP_EXITED"])).all()
        keys = {f"{i.exch}|{i.token}" for i in instruments if i.token}
        keys |= {f"{l.exch}|{l.contract_token}" for l in open_lots if l.contract_token}
        for k in keys:
            await self.ws.subscribe(k)

    async def prime_quote(self, exch: str, token: str) -> None:
        """Fetch one quote right now so a freshly-added instrument shows a price
        immediately instead of waiting for the 1s poll loop (helps options)."""
        try:
            q = await self.rest.get_quote(exch, token)
        except Exception:
            return
        self._apply_quote(f"{exch}|{token}", q, "poll")

    # ================= tick handling =================

    async def _on_tick(self, tick: dict) -> None:
        key = f"{tick.get('e', '')}|{tick.get('tk', '')}"
        # the gateway fans out every token ANY of its clients subscribed (e.g.
        # the Gateway UI's NFO watchlist) — drop tokens this engine never asked
        # for BEFORE validation, so they can't spam "tick rejected" logs or
        # grow the price cache
        if not self.ws.is_subscribed(key):
            return
        ps = self.prices.setdefault(key, PriceState())
        # incremental frames may carry only some fields — fall back to the last
        # cached value so validation compares like-for-like
        try:
            lp_in = float(tick["lp"]) if tick.get("lp") else 0.0
            bid = float(tick["bp1"]) if tick.get("bp1") else ps.bid
            ask = float(tick["sp1"]) if tick.get("sp1") else ps.ask
        except (TypeError, ValueError):
            return
        # last-trade-qty for the zero-volume ghost check (absent on many frames → None = skip)
        ltq: float | None = None
        if tick.get("ltq") not in (None, ""):
            try:
                ltq = float(tick["ltq"])
            except (TypeError, ValueError):
                ltq = None
        # a frame with no/zero last-traded-price is a PARTIAL (bid/ask/depth-only)
        # update, not a bad tick — keep the last good lp and refresh bid/ask quietly.
        # But never cache a CROSSED book (bid > ask = glitch signature): a polluted
        # cached book would poison the inside-spread validation of later ticks.
        if lp_in <= 0:
            if not (bid > 0 and ask > 0 and bid > ask * 1.001):
                if bid > 0:
                    ps.bid = bid
                if ask > 0:
                    ps.ask = ask
            if ps.lp <= 0:
                return                  # no real price yet — nothing to act on
            ps.mono_ts = _time.monotonic()
            ps.wall_ts = datetime.now(IST).isoformat(timespec="seconds")
            ps.source = "ws"
            return
        ok, reason, clean_bid, clean_ask = self._ticks.check(key, lp_in, bid, ask, ltq)
        if not ok:
            self._log_bad_tick(key, reason)
            return                      # do NOT update the cache — stale-but-good price stands
        ps.lp = lp_in
        ps.bid = clean_bid or ps.bid
        ps.ask = clean_ask or ps.ask
        ps.wall_ts = datetime.now(IST).isoformat(timespec="seconds")
        ps.mono_ts = _time.monotonic()
        ps.source = "ws"
        if ps.lp > 0:
            await self._check_instrument_on_price(key, ps)
            await self._check_ladder_on_price(key, ps)
            await self._check_temp_triggers(key, ps)

    def _log_bad_tick(self, key: str, reason: str) -> None:
        """Throttle rejected-tick logging to once per ~10s per token."""
        now = _time.monotonic()
        if now - self._tick_reject_log.get(key, 0.0) > 10.0:
            self._tick_reject_log[key] = now
            event_log.warn("FEED", f"tick rejected [{key}]: {reason}")

    def _apply_quote(self, key: str, q: dict, source: str) -> bool:
        """Validate a REST/fallback quote and, if sane, update the cache. Returns
        True when accepted. Shares the same outlier filter as the WS path."""
        ps = self.prices.setdefault(key, PriceState())
        try:
            lp = float(q.get("lp") or 0) or ps.lp
            bid = float(q.get("bp1") or 0) or ps.bid
            ask = float(q.get("sp1") or 0) or ps.ask
        except (TypeError, ValueError):
            return False
        if lp <= 0:
            return False    # no usable price in this quote (and none cached) — skip quietly
        ok, reason, clean_bid, clean_ask = self._ticks.check(key, lp, bid, ask)
        if not ok:
            self._log_bad_tick(key, reason)
            return False
        ps.lp = lp
        ps.bid = clean_bid or ps.bid
        ps.ask = clean_ask or ps.ask
        ps.mono_ts = _time.monotonic()
        ps.wall_ts = datetime.now(IST).isoformat(timespec="seconds")
        ps.source = source
        return True

    def _load_lots_for_order(self, oid: str) -> tuple:
        """Sync DB fetch for _on_order_update, run via asyncio.to_thread there —
        this fires on every broker order-update event (see call site)."""
        with db_session() as db:
            entry_lot = (db.query(Lot.id).filter(Lot.entry_order_id == oid,
                                                 Lot.status == "RESTING").first())
            target_lot = (db.query(Lot.id).filter(Lot.target_order_id == oid,
                                                  Lot.status == "OPEN").first())
        return entry_lot, target_lot

    async def _on_order_update(self, frame: dict) -> None:
        oid = frame.get("norenordno", "")
        if not oid:
            return
        self.om.notify_order_update(oid)
        # broker-resting orders (ladder LMT flow): route fills/cancels of watched
        # entry & target orders to their resolvers the moment the broker pushes them.
        entry_lot, target_lot = await asyncio.to_thread(self._load_lots_for_order, oid)
        if entry_lot is not None:
            asyncio.create_task(self._resolve_entry_order(entry_lot[0]))
        if target_lot is not None:
            asyncio.create_task(self._resolve_target_order(target_lot[0]))

    def _load_open_lots_for_key(self, key: str) -> tuple[list, dict]:
        """Sync DB fetch for _check_instrument_on_price, run via asyncio.to_thread
        there — this runs on EVERY price tick, so an inline call here would block
        the event loop that often (see call site)."""
        exch, token = key.split("|", 1)
        with db_session() as db:
            lots = (db.query(Lot)
                    .filter(Lot.status == "OPEN")
                    .filter(Lot.contract_token == token)
                    .filter(Lot.exch == exch)
                    .all())
            if not lots:
                return [], {}
            inst_ids = {l.instrument_id for l in lots}
            instruments = {i.id: i for i in db.query(Instrument).filter(Instrument.id.in_(inst_ids)).all()}
        return lots, instruments

    async def _check_instrument_on_price(self, key: str, ps: PriceState) -> None:
        """Exit checks + circuit-breaker evaluation for the instrument(s) on this token."""
        lots, instruments = await asyncio.to_thread(self._load_open_lots_for_key, key)
        if not lots:
            return
        inst_ids = {l.instrument_id for l in lots}

        now_m = _time.monotonic()
        for inst_id in inst_ids:
            inst = instruments.get(inst_id)
            # CB math hits the DB — evaluate at most every 5s per instrument
            if inst is not None and now_m - self._cb_last_eval.get(inst_id, 0.0) > 5.0:
                self._cb_last_eval[inst_id] = now_m
                self._eval_circuit_breaker(inst, ps.lp)

        for lot in lots:
            if lot.exit_pending:
                continue
            # An OPEN rung with sl_price ≤ 0 has NO stop — the gate below skips
            # it entirely. Entry-time and rollover guards should make this
            # impossible, so if one ever appears (legacy row, manual DB edit) it
            # must be loud rather than silent. Rate-limited to hourly per lot.
            if lot.sl_price <= 0:
                last_warn = self._stopless_warned.get(lot.id, 0.0)
                if now_m - last_warn > 3600:
                    self._stopless_warned[lot.id] = now_m
                    inst_sym = instruments.get(lot.instrument_id)
                    event_log.error(
                        "EXIT", f"rung #{lot.seq} ({lot.contract_tsym}) is OPEN with NO STOP-LOSS "
                                f"(sl_price={lot.sl_price:g}) — {lot.qty} units are unprotected. Set an SL "
                                f"from the trades table or close the rung.",
                        sym=(inst_sym.sym if inst_sym is not None else ""),
                        data={"lot_id": lot.id, "qty": lot.qty, "entry": lot.entry_price})
            # a broker-resting target order owns the TARGET exit — the tick loop
            # must not double-sell against it. SL stays engine-fired (MKT).
            hit_target = (lot.target_price > 0 and ps.lp >= lot.target_price
                          and not (lot.target_order_id or ""))
            hit_sl = lot.sl_price > 0 and ps.lp <= lot.sl_price
            if not (hit_target or hit_sl):
                continue
            cd = self._exit_cooldown.get(lot.id, 0.0)
            # Spacing grows with consecutive failures (5s → … → 60s) so a broker
            # that keeps refusing is not hit 720 times an hour, while an exit
            # that simply has not been tried yet still fires on the next tick.
            if _time.monotonic() - cd < self._exit_retry_wait(lot.id):
                continue
            self._exit_cooldown[lot.id] = _time.monotonic()
            reason = "TARGET" if hit_target else "STOPLOSS"
            asyncio.create_task(self.close_lot(lot.id, reason, trigger_price=ps.lp))

    # ================= circuit breaker =================

    def _instrument_pnl(self, inst: Instrument, ltp: float) -> tuple[float, float]:
        """(open_pnl, realized_today) in ₹ for one instrument. Paused (TEMP_EXITED)
        rungs count at their FROZEN temp-exit value — the drawdown is locked in and
        must weigh on the circuit breaker just like an open loss."""
        today0 = today_ist()
        with db_session() as db:
            open_lots = (db.query(Lot)
                         .filter(Lot.instrument_id == inst.id,
                                 Lot.status.in_(["OPEN", "TEMP_EXITED"])).all())
            closed_today = (db.query(func.coalesce(func.sum(Lot.realized_pnl), 0.0))
                            .filter(Lot.instrument_id == inst.id,
                                    Lot.status.in_(["CLOSED", "EXTERNAL_CLOSED"]),
                                    Lot.exit_time >= today0).scalar()) or 0.0
        open_pnl = 0.0
        for l in open_lots:
            if l.status == "TEMP_EXITED":
                open_pnl += open_lot_pnl(l, 0.0)   # frozen at the temp-exit fill
                continue
            px = ltp
            k = f"{l.exch}|{l.contract_token}"
            ps = self.prices.get(k)
            if ps and ps.lp > 0:
                px = ps.lp
            # px <= 0 → open_lot_pnl returns 0.0 (never mark against 0 = fake full loss)
            open_pnl += open_lot_pnl(l, px)
        return open_pnl, float(closed_today)

    def _eval_circuit_breaker(self, inst: Instrument, ltp: float) -> None:
        if not inst.cb_enabled or inst.cb_tripped or inst.cb_threshold <= 0:
            return
        open_pnl, realized_today = self._instrument_pnl(inst, ltp)
        total = open_pnl + realized_today
        if total <= -abs(inst.cb_threshold):
            with db_session() as db:
                row = db.get(Instrument, inst.id)
                row.cb_tripped = True
                row.cb_tripped_at = now_ist()
                row.cb_reason = (f"loss ₹{-total:,.0f} breached threshold ₹{inst.cb_threshold:,.0f} "
                                 f"(open ₹{open_pnl:,.0f} + today ₹{realized_today:,.0f})")
                db.commit()
            event_log.warn("CB", f"CIRCUIT BREAKER TRIPPED — {inst.sym}: loss ₹{-total:,.0f} > "
                                 f"₹{inst.cb_threshold:,.0f}. New dip-buys blocked until user resets.",
                           sym=inst.sym,
                           data={"open_pnl": round(open_pnl), "realized_today": round(realized_today)})
            # resting entry orders are pre-authorized buys — a tripped breaker must
            # pull them from the broker, not just block future placements
            asyncio.create_task(self.cancel_resting_entries(inst.id, "circuit breaker tripped"))

        if settings.global_cb_threshold > 0 and not self.global_cb_tripped:
            g_total = 0.0
            with db_session() as db:
                all_inst = db.query(Instrument).all()
            for i in all_inst:
                o, r = self._instrument_pnl(i, 0.0)
                g_total += o + r
            if g_total <= -settings.global_cb_threshold:
                self.global_cb_tripped = True
                self._global_cb_reason = (f"total loss ₹{-g_total:,.0f} breached the global threshold "
                                          f"₹{settings.global_cb_threshold:,.0f}")
                event_log.error("CB", f"GLOBAL circuit breaker tripped: total loss ₹{-g_total:,.0f}. "
                                      "ALL new buys are blocked until it is reset from the dashboard.")
                asyncio.create_task(self.cancel_resting_entries(None, "GLOBAL circuit breaker tripped"))

    def reset_global_cb(self) -> dict:
        """Clear the global circuit breaker so buying can resume.

        Without this the flag was set-only: once tripped, the ONLY way back was
        restarting the process — which also means an operator who has just
        assessed the drawdown and wants to resume has to bounce a live engine.
        Re-places resting ladder entries the trip pulled, same as the master
        switch does.
        """
        was = self.global_cb_tripped
        self.global_cb_tripped = False
        self._global_cb_reason = ""
        if was:
            event_log.warn("CB", "GLOBAL circuit breaker RESET by the user — new buys are allowed again")
            asyncio.create_task(self.sync_resting_orders(force=True, clear_holds=True))
        return {"tripped": False, "was_tripped": was}

    # ================= signal pipeline =================

    @staticmethod
    def _bar_already_traded(sym: str, action: str, tf: int, bar_ts: str, this_sig_id: int) -> bool:
        """Has this exact (symbol, action, timeframe, bar) already been EXECUTED?

        Restart-durable backstop for the in-memory dedupe map. Only `executed`
        counts: a signal we skipped or that failed should still be retryable if
        AmiBroker re-fires it, but a bar we actually bought must never be bought
        twice. Scoped to the last 24h so an old bar id can't block a new session.
        """
        if not bar_ts:
            return False
        since = datetime.now(IST).replace(tzinfo=None) - timedelta(hours=24)
        with db_session() as db:
            rows = (db.query(Signal.payload)
                    .filter(Signal.id != this_sig_id,
                            Signal.sym == sym,
                            Signal.action == action,
                            Signal.timeframe_min == tf,
                            Signal.status == "executed",
                            Signal.ts >= since)
                    .all())
        for (payload,) in rows:
            p = payload or {}
            if str(p.get("bar_time") or "") == bar_ts:
                return True
        return False

    async def handle_signal(self, payload: dict) -> dict:
        """Entry point for the AmiBroker webhook. Returns a summary dict."""
        sym = str(payload.get("symbol") or payload.get("sym") or "").upper().strip()
        action = str(payload.get("action") or payload.get("signal") or "").upper().strip()
        tf = int(payload.get("timeframe_min") or payload.get("tf") or 0)
        bar_ts = str(payload.get("bar_time") or "")
        signal_atr = payload.get("atr")

        with db_session() as db:
            sig = Signal(sym=sym, action=action, timeframe_min=tf, payload=payload)
            db.add(sig)
            db.commit()
            sig_id = sig.id
        event_log.write("SIGNAL", f"{action or '?'} signal received for {sym or '?'} "
                                  f"(tf={tf or '?'}m, signal #{sig_id})", sym=sym,
                        data={"payload": payload})

        def finish(status: str, reason: str, lot_id: int | None = None) -> dict:
            with db_session() as db:
                s = db.get(Signal, sig_id)
                s.status = status
                s.reason = reason
                s.lot_id = lot_id
                db.commit()
            if status == "skipped":
                event_log.write("SIGNAL", f"Don't execute signal #{sig_id} ({sym} {action}): {reason}",
                                level="warn", sym=sym)
            elif status == "failed":
                event_log.error("SIGNAL", f"Signal #{sig_id} ({sym} {action}) FAILED: {reason}", sym=sym)
            return {"signal_id": sig_id, "status": status, "reason": reason, "lot_id": lot_id}

        if action not in ("BUY", "SELL"):
            return finish("skipped", f"unsupported action '{action}'")

        # duplicate suppression (AmiBroker often re-fires on bar refresh)
        now_m = _time.monotonic()
        self._recent_signals = {k: v for k, v in self._recent_signals.items() if now_m - v < 900}
        if bar_ts:
            dkey = (sym, action, tf, bar_ts)
            if dkey in self._recent_signals:
                return finish("skipped", f"duplicate of a signal from the same bar ({bar_ts})")
            # The in-memory map dies with the process. A restart (deploy, crash,
            # or the engine being bounced mid-session) therefore re-opened the
            # ladder to the very next AmiBroker re-fire of a bar it had ALREADY
            # traded. The Signal table persists every webhook with its payload,
            # so it is the durable check the memory map was standing in for.
            if self._bar_already_traded(sym, action, tf, bar_ts, sig_id):
                self._recent_signals[dkey] = now_m
                return finish("skipped", f"bar {bar_ts} was already executed for {sym} {action} "
                                         "(persisted signal history) — duplicate re-fire suppressed")
            self._recent_signals[dkey] = now_m
        else:
            # no bar_time → can't dedupe by bar. Suppress rapid IDENTICAL re-fires
            # (the refresh double-hit) inside a short window without blocking a
            # genuine later signal for the same instrument.
            dkey = (sym, action, tf, "nobar")
            last = self._recent_signals.get(dkey, 0.0)
            if now_m - last < settings.nobar_dedup_window:
                return finish("skipped", f"identical {action} for {sym} within "
                                         f"{settings.nobar_dedup_window:g}s and no bar_time to dedupe "
                                         "precisely — rapid re-fire suppressed")
            self._recent_signals[dkey] = now_m

        with db_session() as db:
            # webhook signals only ever drive mode="auto" instruments — the
            # manual-ladder watchlist is driven by its own price levels
            autos = db.query(Instrument).filter(Instrument.mode == "auto")
            inst = autos.filter(func.upper(Instrument.sym) == sym).first()
            if inst is None:  # exact contract name (tsym) also accepted
                inst = autos.filter(func.upper(Instrument.tsym) == sym).first()
            if inst is None:
                # AmiBroker continuous-contract names: "NATURALGAS-I", "NIFTY-I",
                # "NATURALGAS28JUL25FUT"… → match on the watchlist root prefix
                for cand in autos.all():
                    if sym.startswith(cand.sym.upper()):
                        inst = cand
                        break
        if inst is None:
            return finish("skipped", f"'{sym}' is not in the automated-signal watchlist")

        async with self.lock_for(inst.id):
            if action == "BUY":
                return await self._execute_buy(inst, sig_id, tf, signal_atr, finish)
            return await self._execute_sell_signal(inst, sig_id, finish)

    def _mark_uncertain_placement(self, inst: Instrument, qty_units: int, expected_entry: float,
                                  prev_entry: float | None, tf: int, prod: str) -> None:
        """Remember a BUY whose HTTP placement errored but might be live at the
        broker, so the reconciler can adopt the fill as a managed lot."""
        self._uncertain_placements[inst.id] = {
            "ts": _time.monotonic(), "qty": qty_units, "expected_entry": expected_entry,
            "prev_entry": prev_entry, "tf": tf, "prod": prod, "tsym": inst.tsym,
            "token": inst.token, "exch": inst.exch,
        }
        event_log.error("ORDER", f"{inst.sym}: order placement HTTP call failed AFTER possibly reaching the "
                                 f"broker ({qty_units} units {inst.tsym}). Flagged for reconciler adoption — "
                                 "if the broker shows the position it will be taken over WITH target/SL, not "
                                 "left unmanaged.", sym=inst.sym)

    @staticmethod
    def _remaining_margin(funds: dict) -> float:
        """Margin free to deploy = total cash/limit − margin already used."""
        try:
            cash = float(funds.get("cash") or 0)
            used = float(funds.get("margin_used") or 0)
            return round(cash - used, 2)
        except (TypeError, ValueError):
            return 0.0

    async def _margin_ok(self, inst: Instrument, qty_units: int, prod: str) -> tuple[bool, str]:
        """Pre-trade funds gate via Shoonya SPAN. Blocks the buy only when we can
        POSITIVELY compute that free cash < required × safety factor. On any
        error (SPAN unavailable, bad payload, broker hiccup) it FAILS OPEN — a
        margin-check problem must never freeze legitimate trading; it just logs."""
        if not settings.margin_gate_enabled:
            return True, ""
        try:
            m = await self.rest.get_order_margin(exchange=inst.exch, tradingsymbol=inst.tsym,
                                                 quantity=qty_units, side="B", product_type=prod)
            required = float(m.get("required") or 0)
            if required <= 0:
                # couldn't get a real number → don't block, but say so — a silent
                # skip here means the margin gate is effectively OFF for this order
                event_log.warn("ORDER", f"margin gate skipped: SPAN returned no requirement "
                                        f"({m.get('required')!r}) — allowing order (fail-open)", sym=inst.sym)
                return True, ""
            funds = await self.rest.get_funds()
            # A dead Shoonya session answers with empty/zero data rather than an
            # error, and a zero limit then reads as "no money left" — that quietly
            # skipped every buy for the first half hour of a session while the
            # account actually held five figures. A live account always reports a
            # non-zero limit, so treat zero as an unreadable snapshot: say so
            # loudly and let the order through, since the broker will reject it
            # cleanly if funds really are short.
            if not funds or float(funds.get("cash") or 0) <= 0:
                event_log.error("ORDER", "margin gate: broker reported no funds at all (limit ₹0) — the "
                                         "session is most likely dead, not the account empty. RECONNECT the "
                                         "broker. Allowing this order rather than silently skipping buys.",
                                sym=inst.sym)
                return True, ""
            # gate on REMAINING margin (total − already used), not the gross balance
            available = self._remaining_margin(funds)
            need = required * settings.margin_safety_factor
            if available < need:
                return False, (f"insufficient margin: need ≈₹{need:,.0f} (SPAN+expo ₹{required:,.0f} × "
                               f"{settings.margin_safety_factor:g} safety) but only ₹{available:,.0f} remaining "
                               "(cash − used) — skipping buy to avoid a broker square-off")
            event_log.math(f"Margin OK: need ≈₹{need:,.0f} ≤ remaining ₹{available:,.0f}", sym=inst.sym)
            return True, ""
        except Exception as e:  # noqa: BLE001
            event_log.warn("ORDER", f"margin check unavailable ({e}) — allowing order (fail-open)", sym=inst.sym)
            return True, ""

    @staticmethod
    def _anchor_lot(open_lots, entry_price: float):
        """The chained-target anchor rung: the open rung with the NEAREST entry
        strictly ABOVE this entry (price-based). In a classic descending ladder
        this equals 'the previous buy' (same behaviour as before); with recycled
        levels refiring ABOVE still-open lower rungs, time-order would pick a
        LOWER anchor → target below entry → instant-exit churn. Price-order can't.
        Returns the lot or None (→ self-anchored target)."""
        above = [l for l in open_lots if (l.entry_price or 0) > entry_price]
        return min(above, key=lambda l: l.entry_price) if above else None

    @staticmethod
    def _shift_sl(old_sl: float, basis: float, *, sym: str, seq: int, tick: float) -> float:
        """Basis-shift a live stop, never letting it land at/below 0.

        A deeply negative rollover basis (or a run of them) can drag a stop to
        ≤ 0, and the tick-loop gate `lot.sl_price > 0` then reads that rung as
        having NO stop — silently, mid-life, on a position that is already open.
        Clamp to one tick instead and shout: the rung stays protected (however
        far away) and the operator gets told the risk distance is now wrong.
        """
        if old_sl <= 0:
            return old_sl                      # already stop-less; handled elsewhere
        new_sl = round(old_sl + basis, 4)
        if new_sl > 0:
            return new_sl
        floor = max(tick or 0.05, 0.05)
        event_log.error(
            "ROLLOVER",
            f"{sym} rung #{seq}: rollover basis {basis:+.2f} would move the stop {old_sl:.2f} → "
            f"{new_sl:.2f} (at/below zero), which the exit gate would read as NO STOP. Clamped to "
            f"{floor:.2f} so the rung stays protected — its risk distance is now meaningless, "
            f"review or close this rung.", sym=sym)
        return floor

    @staticmethod
    def _round_tick(price: float, tick: float) -> float:
        """Snap a limit price to the instrument's tick grid (Shoonya rejects off-grid limits)."""
        if tick and tick > 0:
            return round(round(price / tick) * tick, 4)
        return round(price, 4)

    @classmethod
    def _order_params(cls, order_type: str | None, limit_price: float, tick: float) -> tuple[str, float]:
        """Map a per-instrument order-type choice → (price_type, price) for the gateway.

        "LMT" → a limit at the intended price (tick-snapped), so the fill carries NO
                slippage buffer — this is the point of the feature.
        anything else ("MKT") → the gateway reprices to a MARKETABLE limit through the
                touch, i.e. it always fills (used for stop-loss and forced exits).
        A LMT with no usable price falls back to MKT rather than sending price 0.
        """
        if (order_type or "LMT").upper() == "LMT" and limit_price and limit_price > 0:
            return "LMT", cls._round_tick(limit_price, tick)
        return "MKT", 0.0

    @classmethod
    def _marketable_order(cls, side: str, order_type: str | None, ref_px: float,
                          ticks: int | None, tick: float) -> tuple[str, float]:
        """(price_type, price) for a MARKETABLE limit that crosses the spread so it
        fills immediately like a market order, but with a price cap:
          BUY  → LMT at ref + N ticks   (ref should be the ASK)
          SELL → LMT at ref − N ticks   (ref should be the BID)
        `order_type` 'MKT' (or a non-positive ref) → plain MKT, letting the gateway
        reprice through the touch. N=0 places the limit exactly at the touch."""
        if (order_type or "LMT").upper() != "LMT" or not ref_px or ref_px <= 0:
            return "MKT", 0.0
        buf = max(int(ticks or 0), 0) * (tick or 0.0)
        px = ref_px + buf if side == "B" else max(ref_px - buf, tick or 0.01)
        return "LMT", cls._round_tick(px, tick)

    async def _cancel_unfilled_order(self, order_id: str) -> tuple[str, int, float]:
        """After a verify TIMEOUT the order may still be RESTING at the broker (common
        with LMT). Best-effort cancel it, then report its final (status, filled_units,
        avg_price) from the order book so the caller can book a race-fill, re-arm, or
        hand the residue to the reconciler. Returns ("", 0, 0.0) when nothing is known."""
        if not order_id:
            return "", 0, 0.0
        try:
            await self.rest.cancel_order(order_id)
        except Exception:
            pass
        try:
            row = await self.rest.find_order(order_id)
        except Exception:
            row = None
        if not row:
            return "", 0, 0.0
        status = (row.get("status") or "").upper()
        try:
            filled = int(float(row.get("fillshares", "0") or 0))
        except (TypeError, ValueError):
            filled = 0
        try:
            avg = float(row.get("avgprc", "0") or 0)
        except (TypeError, ValueError):
            avg = 0.0
        return status, filled, avg

    @staticmethod
    def _exit_config_error(inst: Instrument, entry_price: float | None = None) -> str:
        """Return a human reason if the instrument's target/SL config would make
        a rung exit immediately (or never), else ''. Percent offsets must be a
        positive fraction under 100%; points offsets must be positive. This is
        the live-path analogue of the check the backtest already enforces.

        When `entry_price` is supplied the check also becomes PRICE-AWARE: a
        POINTS stop wider than the price itself (e.g. sl_value=500 on a ₹200
        contract) computes to sl_price ≤ 0, and the tick-loop exit gate
        (`lot.sl_price > 0`) then silently treats that rung as having no stop at
        all. That is the single worst failure mode in the system — an unbounded
        long — so it is refused at entry rather than discovered afterwards.
        """
        tv, sv = inst.target_value, inst.sl_value
        sl_on = getattr(inst, "sl_enabled", False) is not False
        if tv is None or tv <= 0:
            return "target offset is 0/negative — set a positive target before trading (would exit instantly)"
        if sl_on and (sv is None or sv <= 0):
            return "stop-loss offset is 0/negative — set a positive SL before trading (rung would be unprotected)"
        if inst.target_mode == "percent" and tv >= 100:
            return f"target percent {tv:g}% is out of range (must be < 100)"
        if sl_on and inst.sl_mode == "percent" and sv >= 100:
            return f"stop-loss percent {sv:g}% ≥ 100 would put the stop at/below 0 — unprotected"
        if entry_price is not None and entry_price > 0:
            if sl_on:
                sl = compute_sl(inst, entry_price)
                if sl <= 0:
                    unit = "%" if inst.sl_mode == "percent" else " pts"
                    return (f"stop-loss offset {sv:g}{unit} is ≥ the price {entry_price:.2f} — the stop would land at "
                            f"{sl:.2f} (≤ 0), which the exit gate reads as 'no stop'. Refusing to open an "
                            f"UNPROTECTED rung; set sl_value below the contract price.")
            tgt, _ = compute_target(inst, entry_price, None)
            if tgt <= entry_price:
                return (f"target {tgt:.2f} is at/below the entry {entry_price:.2f} — the rung would "
                        "round-trip instantly for a loss")
        return ""

    async def _execute_buy(self, inst: Instrument, sig_id: int, tf: int,
                           signal_atr, finish) -> dict:
        # Re-read the instrument NOW that we hold the lock. handle_signal loaded
        # `inst` before awaiting lock_for(); a prior order can hold that lock for
        # the full verify timeout, during which a tick may trip the circuit
        # breaker (or the user may disable/reconfigure). Re-fetching here means
        # every gate below sees state as of lock-acquisition, not signal-arrival.
        # (No await between this read and the gate checks, so it can't go stale.)
        with db_session() as db:
            inst = db.get(Instrument, inst.id)
            if inst is None:
                return finish("skipped", "instrument was removed from the watchlist")
        sym = inst.sym
        event_log.math(f"Doing math for buy signal #{sig_id} ({sym})", sym=sym)

        # ---- gate checks (each one logged when it blocks) ----
        if not self.engine_on():
            return finish("skipped", "strategy master switch is OFF")
        if not inst.enabled:
            return finish("skipped", f"{sym} is disabled in the watchlist")
        # exit config must be sane BEFORE we ever buy — a 0/negative target or SL
        # makes the rung round-trip at market on the first tick (target=entry,
        # sl=entry) and bleed spread+fees. Refuse rather than trade into that.
        bad_cfg = self._exit_config_error(inst)
        if bad_cfg:
            return finish("skipped", bad_cfg)
        if inst.cb_tripped:
            return finish("skipped", f"circuit breaker is ACTIVE ({inst.cb_reason or 'loss threshold hit'}) "
                                     "— turn it back on from the watchlist to resume buying")
        if self.global_cb_tripped:
            return finish("skipped", "GLOBAL circuit breaker is active")
        if inst.rollover_state in ("scanning", "rolling"):
            return finish("skipped", "rollover scan/roll in progress for this instrument")
        if not market_open_now(inst.exch):
            return finish("skipped", f"{inst.exch} market is closed")
        if not self.broker_connected:
            self.broker_connected = await self.rest.broker_connected()
            if not self.broker_connected:
                return finish("failed", "broker session is DOWN at the gateway — cannot place orders")
        if self.feed_state != "live":
            return finish("skipped", f"price feed is {self.feed_state.upper()} — system is blind, "
                                     "new entries paused (heartbeat safety)")

        with db_session() as db:
            open_lots = (db.query(Lot)
                         .filter(Lot.instrument_id == inst.id,
                                 Lot.status.in_(["OPEN", "PENDING", "UNCONFIRMED", "TEMP_EXITED"]))
                         .order_by(Lot.entry_time.asc())
                         .all())
        if len(open_lots) >= inst.max_rungs:
            return finish("skipped", f"max rungs reached ({len(open_lots)}/{inst.max_rungs})")

        key = f"{inst.exch}|{inst.token}"
        ps = self.prices.get(key)
        if ps is None or ps.lp <= 0 or ps.age() > 30:
            try:
                q = await self.rest.get_quote(inst.exch, inst.token)
                ps = self.prices.setdefault(key, PriceState())
                ps.lp = float(q.get("lp") or 0)
                ps.bid = float(q.get("bp1") or 0)
                ps.ask = float(q.get("sp1") or 0)
                ps.mono_ts = _time.monotonic()
                ps.wall_ts = datetime.now(IST).isoformat(timespec="seconds")
                ps.source = "poll"
            except Exception as e:
                return finish("failed", f"no usable price for {inst.tsym}: {e}")
        if ps.lp <= 0:
            return finish("failed", f"no usable price for {inst.tsym}")

        expected_entry = ps.ask if ps.ask > 0 else ps.lp

        # price-aware exit-config re-check: a POINTS stop wider than the contract
        # price computes to sl_price ≤ 0, which the tick-loop gate reads as "no
        # stop". Only detectable once a price is known, hence the second call.
        bad_cfg = self._exit_config_error(inst, expected_entry)
        if bad_cfg:
            return finish("skipped", bad_cfg)

        # spread guard (slippage protection)
        if inst.max_spread_points > 0 and ps.bid > 0 and ps.ask > 0:
            spread = ps.ask - ps.bid
            if spread > inst.max_spread_points:
                return finish("skipped", f"spread {spread:.2f} wider than guard "
                                         f"{inst.max_spread_points:.2f} — bad fill risk")

        # min-gap: this is a DIP ladder — a new rung must sit below the last one
        last_open = open_lots[-1] if open_lots else None
        if inst.min_gap_points > 0 and last_open is not None:
            if ps.lp > last_open.entry_price - inst.min_gap_points:
                return finish("skipped",
                              f"price {ps.lp:.2f} hasn't dipped ≥{inst.min_gap_points:g} below last rung "
                              f"entry {last_open.entry_price:.2f}")

        # ---- volatility-targeting sizing ----
        sizing = await compute_size(self.rest, inst, timeframe_min=tf,
                                    signal_atr=signal_atr, entry_price=expected_entry)
        for step in sizing.steps:
            event_log.math(step, sym=sym)
        if sizing.lots <= 0:
            return finish("skipped", f"sizing → 0 lots ({sizing.reason})")

        qty_units = sizing.lots * max(inst.lot_size, 1)
        # anchor = nearest open rung ABOVE this entry (price-based; see _anchor_lot)
        anchor_lot = self._anchor_lot(open_lots, expected_entry)
        prev_entry = anchor_lot.entry_price if anchor_lot is not None else None
        plan_tgt, plan_anchor = compute_target(inst, expected_entry, prev_entry)
        plan_sl = compute_sl(inst, expected_entry)
        event_log.math(
            f"Pre-trade plan: BUY {sizing.lots} lot(s) = {qty_units} units {inst.tsym} @ ~{expected_entry:.2f} | "
            f"target {plan_tgt:.2f} (anchor {'prev buy ' if prev_entry is not None else 'own entry '}{plan_anchor:.2f} "
            f"+ {inst.target_value:g}{'%' if inst.target_mode == 'percent' else ' pts'}) | "
            f"SL {plan_sl:.2f} (entry − {inst.sl_value:g}{'%' if inst.sl_mode == 'percent' else ' pts'})",
            sym=sym)

        # ---- pre-trade margin gate (Shoonya SPAN) ----
        prod = inst.product_type or settings.product_type
        ok_margin, margin_reason = await self._margin_ok(inst, qty_units, prod)
        if not ok_margin:
            return finish("skipped", margin_reason)

        # ---- place + verify ----
        # order type: LIMIT (default) places a plain limit exactly AT the touch
        # (the ask) — no marketable buffer; the order fills at the quoted price
        # or rests. If it doesn't execute, that is reported honestly instead of
        # paying up for the fill. MKT lets the gateway reprice through the touch.
        buy_pt, buy_px = self._marketable_order("B", inst.buy_order_type, expected_entry,
                                                0, inst.tick_size)
        try:
            order_id = await self.rest.place_order(
                side="B", exchange=inst.exch, tradingsymbol=inst.tsym,
                quantity=qty_units, price_type=buy_pt, price=buy_px, remarks="",
                product_type=prod)
        except Exception as e:
            # The HTTP call failed — but the order may have reached the broker and
            # filled. Record it so the reconciler can ADOPT any resulting broker
            # units as a managed lot (with target/SL) instead of leaving a naked,
            # stop-less position that the "unmanaged extras" logic would ignore.
            self._mark_uncertain_placement(inst, qty_units, expected_entry, prev_entry, tf, prod)
            return finish("failed", f"order placement error: {e} — reconciler will adopt any resulting broker position")
        if not order_id:
            self._mark_uncertain_placement(inst, qty_units, expected_entry, prev_entry, tf, prod)
            return finish("failed", "gateway returned no order id — reconciler will adopt any resulting broker position")

        with db_session() as db:
            seq = (db.query(func.coalesce(func.max(Lot.seq), 0))
                   .filter(Lot.instrument_id == inst.id).scalar() or 0) + 1
            lot = Lot(
                instrument_id=inst.id, seq=seq, exch=inst.exch,
                contract_tsym=inst.tsym, contract_token=inst.token,
                lots=sizing.lots, qty=qty_units, lot_size=inst.lot_size,
                entry_price=expected_entry, raw_entry_price=expected_entry,
                entry_order_id=order_id, signal_id=sig_id,
                target_price=plan_tgt, sl_price=plan_sl,
                anchor_lot_id=(anchor_lot.id if anchor_lot is not None else None),
                status="PENDING", atr_at_entry=sizing.atr, timeframe_min=tf,
                source="automated", product_type=prod,
            )
            db.add(lot)
            db.commit()
            lot_id = lot.id

        event_log.trade("ORDER", f"BUY order {order_id} sent: {sizing.lots} lot(s) {inst.tsym} — verifying fill "
                                 f"against broker order book…", sym=sym)
        report = await self.om.verify(order_id, qty_units)
        return await self._post_trade_math(inst, lot_id, sig_id, report, prev_entry, finish)

    async def _post_trade_math(self, inst: Instrument, lot_id: int, sig_id: int,
                               report: FillReport, prev_entry: float | None, finish,
                               target_override: float | None = None,
                               sl_override: float | None = None) -> dict:
        """After-execution math: recompute target/SL from the ACTUAL fill.

        target_override (>0) pins the target to a user-set price for this rung
        (manual ladder level with an edited target) instead of the chained math.
        P&L stays exact either way — it is always (exit − entry) × qty; the
        target only decides WHERE the exit fires, and chaining for later rungs
        anchors on ENTRY prices, never targets, so an override never distorts
        another rung's math.
        """
        sym = inst.sym
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if report.status in ("REJECTED", "CANCELLED", "ERROR") or report.filled_qty <= 0 and report.status != "TIMEOUT":
                lot.status = "CANCELLED"
                lot.notes = f"entry not filled: {report.status} {report.error}"
                db.commit()
                return finish("failed", f"buy order {report.order_id or ''} {report.status}: {report.error or report.raw_status}")

            if report.status == "TIMEOUT":
                lot.status = "UNCONFIRMED"
                lot.notes = "fill verification timed out — reconciler will resolve against the broker"
                db.commit()
                event_log.warn("ORDER", f"Order {report.order_id} verification TIMED OUT — lot #{lot.seq} "
                                        "marked UNCONFIRMED (no assumption made; reconciler owns it now)", sym=sym)
                return finish("executed", "order sent but unverified (UNCONFIRMED)", lot_id)

            # A fill with no average price from the broker. The stand-in is the
            # ask captured BEFORE the order was sent — a pre-trade quote, not
            # something the broker ever reported — and the target and stop are
            # then derived from it. Keep the trade (it really happened) but mark
            # the number as approximate wherever a person reads it, instead of
            # letting a guess pass for a fill.
            price_approx = not (report.avg_price > 0)
            actual_price = report.avg_price if report.avg_price > 0 else lot.entry_price
            actual_qty = report.filled_qty
            actual_lots = max(actual_qty // max(inst.lot_size, 1), 1)

            partial = ""
            if report.status == "PARTIAL":
                partial = (f" (PARTIAL FILL: {actual_qty}/{report.requested_qty} units — ladder math "
                           f"updated to the real quantity so exits can never over-sell)")

            tgt, anchor = compute_target(inst, actual_price, prev_entry)
            sl = compute_sl(inst, actual_price)
            if target_override is not None and target_override > 0:
                tgt = round(float(target_override), 4)
            if sl_override is not None and sl_override > 0 and getattr(inst, "sl_enabled", False) is not False:
                sl = round(float(sl_override), 4)
            lot.entry_price = actual_price
            lot.raw_entry_price = actual_price
            lot.qty = actual_qty
            lot.lots = actual_lots
            lot.target_price = tgt
            lot.sl_price = sl
            lot.status = "OPEN"
            if price_approx:
                lot.notes = ((lot.notes or "") +
                             f" entry ≈{actual_price:g} (APPROX — broker reported no fill price; target and"
                             " stop derive from it. Verify at the broker);")
                event_log.warn("ORDER", f"rung #{lot.seq} {sym}: the broker gave no fill price — entry recorded "
                                        f"as ≈{actual_price:g} from the pre-trade quote, and target/SL are "
                                        "built on it. VERIFY the real fill.", sym=sym)
            db.commit()
            seq = lot.seq

        tgt_note = "user-set target" if (target_override is not None and target_override > 0) else \
            f"anchor {'previous rung ' if prev_entry is not None else 'own entry '}{anchor:.2f}"
        event_log.trade(
            "ORDER",
            f"Buy {actual_lots} lot(s) {inst.tsym} FILLED @ {actual_price:.2f} → rung #{seq}{partial} | "
            f"post-trade math: target {tgt:.2f} ({tgt_note}), SL {sl:.2f}",
            sym=sym,
            data={"order_id": report.order_id, "qty_units": actual_qty, "avg_price": actual_price,
                  "target": tgt, "sl": sl})
        await self.ws.subscribe(f"{inst.exch}|{inst.token}")
        return finish("executed", f"rung #{seq} opened", lot_id)

    async def _execute_sell_signal(self, inst: Instrument, sig_id: int, finish) -> dict:
        if inst.sell_signal_mode == "ignore":
            return finish("skipped", "SELL signals are configured to be ignored for this instrument")
        with db_session() as db:
            open_lots = db.query(Lot).filter_by(instrument_id=inst.id, status="OPEN").all()
        if not open_lots:
            return finish("skipped", "no open rungs to sell")
        event_log.trade("EXIT", f"AmiBroker SELL → closing all {len(open_lots)} open rung(s) of {inst.sym}",
                        sym=inst.sym)
        ok = 0
        for lot in open_lots:
            res = await self._close_lot_inner(lot.id, "AMI_SELL")
            ok += 1 if res else 0
        return finish("executed", f"closed {ok}/{len(open_lots)} rungs on SELL signal")

    # ================= manual ladder =================

    async def _settle_ladder_levels(self, inst: Instrument) -> None:
        """Reconcile each executed (TRIGGERED) level against its rung's fate:

          rung still live (OPEN/PENDING/UNCONFIRMED/TEMP_EXITED) → leave it flagged.
          rung CLOSED at TARGET  + re-arm on → RE-ARM: level PENDING again (recycle),
                                               so the same level buys on the next dip.
          rung closed any other way (SL / manual / CB / external / roll / re-arm off)
                                               → RETIRE: level CANCELLED, removed from
                                               the ladder (the client's chosen "retire
                                               on stop-loss" behaviour).
          rung entry never filled (CANCELLED) → RE-ARM (no position was taken).

        A level holds at most ONE open rung at a time — it can't re-fire until its
        current rung is gone — so dips never stack multiple rungs on one level."""
        rearmed: list[tuple[int, float, int]] = []
        retired: list[tuple[int, float, str]] = []
        paused: tuple[int, float] | None = None
        with db_session() as db:
            trig = (db.query(LadderLevel)
                    .filter(LadderLevel.instrument_id == inst.id,
                            LadderLevel.status.in_(["TRIGGERED", "FILLED"]),
                            LadderLevel.lot_id.isnot(None)).all())
            # B1 = the TOP rung of the current ladder = its highest-priced level
            # that is still part of the ladder (price-based, so it survives
            # re-confirm renumbering and the downward shift of the levels below).
            top_price = (db.query(func.max(LadderLevel.price))
                         .filter(LadderLevel.instrument_id == inst.id,
                                 LadderLevel.status != "CANCELLED").scalar())
            changed = False
            for lv in trig:
                lot = db.get(Lot, lv.lot_id)
                if lot is not None and lot.status in ("OPEN", "PENDING", "UNCONFIRMED", "TEMP_EXITED"):
                    continue    # rung still alive — keep the executed tag
                changed = True
                target_hit = lot is not None and lot.status == "CLOSED" and lot.exit_reason == "TARGET"
                is_b1 = top_price is not None and (lv.price or 0.0) >= top_price - 1e-9
                if lot is None or lot.status == "CANCELLED":
                    lv.status = "PENDING"; lv.lot_id = None   # no position was opened → free to try again
                elif target_hit and is_b1:
                    # B1 (top rung) hit target → the whole grid has cashed out
                    # in profit (B1's target is the highest, so it clears LAST).
                    # PAUSE the strategy: disarm the ladder and wait for a manual
                    # re-arm — do NOT auto-restart. The level is kept (PENDING) so
                    # re-arming resumes the ladder; any still-open lower rung is
                    # left to exit on its own.
                    lv.status = "PENDING"; lv.lot_id = None
                    inst_row = db.get(Instrument, inst.id)
                    if inst_row is not None:
                        inst_row.ladder_armed = False
                    paused = (lv.level_no, lv.price)
                elif target_hit and inst.ladder_rearm:
                    lv.status = "PENDING"; lv.lot_id = None
                    rearmed.append((lv.level_no, lv.price, lv.fire_count or 0))
                else:
                    lv.status = "CANCELLED"; lv.lot_id = None
                    retired.append((lv.level_no, lv.price, lot.exit_reason or lot.status))
            if changed:
                db.commit()
        for no, price, fc in rearmed:
            event_log.write("LADDER", f"{inst.sym} B{no} @ {price:g} hit target → level re-armed "
                                      f"(completed cycle #{fc}); active again for the next dip", sym=inst.sym)
        for no, price, why in retired:
            event_log.write("LADDER", f"{inst.sym} B{no} @ {price:g} rung closed ({why}) → level retired "
                                      "(not re-bought)", sym=inst.sym)
        if paused is not None:
            no, price = paused
            event_log.trade("LADDER", f"{inst.sym} B{no} @ {price:g} (top rung) hit target → grid cycle "
                                      "COMPLETE. Ladder PAUSED and disarmed — re-arm to start a new cycle.",
                            sym=inst.sym)

    def _load_ladder_instruments_for_key(self, exch: str, token: str) -> list:
        """Sync DB fetch for _check_ladder_on_price's existence check, run via
        asyncio.to_thread there — this runs on EVERY price tick, for every
        token, even when nothing on it is a ladder instrument (see call site)."""
        with db_session() as db:
            return (db.query(Instrument)
                    .filter(Instrument.mode == "ladder", Instrument.exch == exch,
                            Instrument.token == token).all())

    async def _check_ladder_on_price(self, key: str, ps: PriceState) -> None:
        """Entry checks for armed manual-ladder instruments on this token:
        when price trades at/below the highest PENDING level, fire a buy
        through the exact same pipeline as automated signals."""
        exch, token = key.split("|", 1)
        now_m = _time.monotonic()
        insts = await asyncio.to_thread(self._load_ladder_instruments_for_key, exch, token)
        for inst in insts:
            if not inst.enabled:
                continue
            if now_m - self._ladder_last_eval.get(inst.id, 0.0) < 1.0:
                continue
            self._ladder_last_eval[inst.id] = now_m

            # settle executed levels whose rung has CLOSED — runs even while paused so
            # the levels table only ever tags a level "executed" while its rung is live.
            await self._settle_ladder_levels(inst)

            if self._uses_resting_entries(inst):
                # Broker-resting flow: entries are LIMIT orders RESTING at the
                # exchange — a tick can never fire a buy here (ghost-tick immune).
                # The tick path only (a) places re-established levels once price
                # recovers above them, and (b) keeps the desired-state sync warm.
                with db_session() as db:
                    recov = (db.query(LadderLevel)
                             .filter(LadderLevel.instrument_id == inst.id,
                                     LadderLevel.status == "AWAIT_RECOVERY",
                                     LadderLevel.price < ps.lp).all())
                    for lv in recov:
                        # price recovered above the level → it becomes a NORMAL
                        # pending level; the desired-state sync then swaps the
                        # active order up to it (cancel lower, place higher)
                        lv.status = "PENDING"
                        lv.note = "price recovered above level"
                    if recov:
                        db.commit()
                for lv in recov:
                    event_log.write("LADDER", f"{inst.sym} B{lv.level_no} @ {lv.price:g}: price recovered "
                                              "above the level — active buy again", sym=inst.sym)
                # HONESTY: a PENDING level with NO order resting at the broker
                # that price has already crossed below did NOT buy — record why
                # on the level and warn once, instead of leaving the user to
                # assume the buy happened.
                with db_session() as db:
                    missed = (db.query(LadderLevel)
                              .filter(LadderLevel.instrument_id == inst.id,
                                      LadderLevel.status == "PENDING",
                                      LadderLevel.lot_id.is_(None),
                                      LadderLevel.price > ps.lp).all())
                    changed = False
                    for lv in missed:
                        if "order not executed" not in (lv.note or ""):
                            lv.note = ("order not executed — price crossed the level while no "
                                       "limit order was resting at the broker")
                            changed = True
                    if changed:
                        db.commit()
                for lv in missed:
                    if now_m - self._level_miss_logged.get(lv.id, 0.0) > 3600.0:
                        self._level_miss_logged[lv.id] = now_m
                        event_log.warn("LADDER", f"{inst.sym} B{lv.level_no} @ {lv.price:g}: price crossed "
                                                 "the level but NO limit order was resting at the broker — "
                                                 "buy NOT executed (see the level's note)", sym=inst.sym)
                asyncio.create_task(self.sync_resting_orders(inst.id))
                continue

            if not inst.ladder_armed:               # paused — no firing (settle still ran)
                continue
            if not market_open_now(inst.exch):      # no signal/log spam off-hours
                continue

            with db_session() as db:
                lvl = (db.query(LadderLevel)
                       .filter(LadderLevel.instrument_id == inst.id,
                               LadderLevel.status == "PENDING",
                               LadderLevel.price >= ps.lp)
                       .order_by(LadderLevel.price.desc())
                       .first())      # one level per evaluation — B1 before B2
            if lvl is None or lvl.id in self._levels_inflight:
                continue
            if now_m - self._ladder_cooldown.get(lvl.id, 0.0) < 30.0:
                continue
            self._levels_inflight.add(lvl.id)
            asyncio.create_task(self._trigger_ladder_level(inst.id, lvl.id, ps.lp))

    async def _trigger_ladder_level(self, inst_id: int, level_id: int, trigger_price: float) -> None:
        try:
            async with self.lock_for(inst_id):
                await self._trigger_ladder_level_inner(inst_id, level_id, trigger_price)
        except Exception:
            logger.exception("ladder level trigger failed")
            self._ladder_cooldown[level_id] = _time.monotonic()
        finally:
            self._levels_inflight.discard(level_id)

    def _ladder_skip(self, level_id: int, sig_id: int, sym: str, label: str, reason: str) -> None:
        self._ladder_cooldown[level_id] = _time.monotonic()
        with db_session() as db:
            s = db.get(Signal, sig_id)
            s.status = "skipped"
            s.reason = reason
            lvl = db.get(LadderLevel, level_id)
            if lvl is not None:
                lvl.note = f"order not executed — {reason}"
            db.commit()
        event_log.write("LADDER", f"{sym} {label} not executed: {reason}", level="warn", sym=sym)

    async def _trigger_ladder_level_inner(self, inst_id: int, level_id: int,
                                          trigger_price: float) -> None:
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
            lvl = db.get(LadderLevel, level_id)
        if inst is None or lvl is None or lvl.status != "PENDING":
            return
        sym, label = inst.sym, f"B{lvl.level_no}"

        with db_session() as db:
            sig = Signal(source="ladder", sym=sym, action="BUY", timeframe_min=240,
                         payload={"level_no": lvl.level_no, "level_price": lvl.price,
                                  "trigger_price": trigger_price,
                                  "basis": inst.ladder_basis})
            db.add(sig)
            db.commit()
            sig_id = sig.id
        event_log.write("LADDER", f"{sym} ladder level {label} hit: price {trigger_price:.2f} ≤ "
                                  f"level {lvl.price:.2f} — running the buy math", sym=sym,
                        data={"level_no": lvl.level_no, "level_price": lvl.price})

        # ---- the same gate checks as the automated buy path ----
        if not self.engine_on():
            return self._ladder_skip(level_id, sig_id, sym, label, "strategy master switch is OFF")
        if not inst.enabled or not inst.ladder_armed:
            return self._ladder_skip(level_id, sig_id, sym, label, "instrument disabled / ladder not confirmed")
        bad_cfg = self._exit_config_error(inst)
        if bad_cfg:
            return self._ladder_skip(level_id, sig_id, sym, label, bad_cfg)
        if inst.cb_tripped:
            return self._ladder_skip(level_id, sig_id, sym, label,
                                     f"circuit breaker is ACTIVE ({inst.cb_reason or 'loss threshold hit'})")
        if self.global_cb_tripped:
            return self._ladder_skip(level_id, sig_id, sym, label, "GLOBAL circuit breaker is active")
        if inst.rollover_state in ("scanning", "rolling"):
            return self._ladder_skip(level_id, sig_id, sym, label, "rollover scan/roll in progress")
        if not market_open_now(inst.exch):
            return self._ladder_skip(level_id, sig_id, sym, label, f"{inst.exch} market is closed")
        if not self.broker_connected:
            self.broker_connected = await self.rest.broker_connected()
            if not self.broker_connected:
                return self._ladder_skip(level_id, sig_id, sym, label, "broker session is DOWN at the gateway")
        if self.feed_state != "live":
            return self._ladder_skip(level_id, sig_id, sym, label,
                                     f"price feed is {self.feed_state.upper()} — entries paused")

        with db_session() as db:
            open_lots = (db.query(Lot)
                         .filter(Lot.instrument_id == inst.id,
                                 Lot.status.in_(["OPEN", "PENDING", "UNCONFIRMED", "TEMP_EXITED"]))
                         .order_by(Lot.entry_time.asc())
                         .all())
        # NOTE: no max-rungs gate for ladders — the ladder is FINITE now (the
        # user defines exactly N levels, and exposure is bounded by them)

        key = f"{inst.exch}|{inst.token}"
        ps = self.prices.get(key)
        if ps is None or ps.lp <= 0 or ps.age() > 30:
            try:
                q = await self.rest.get_quote(inst.exch, inst.token)
                ps = self.prices.setdefault(key, PriceState())
                ps.lp = float(q.get("lp") or 0)
                ps.bid = float(q.get("bp1") or 0)
                ps.ask = float(q.get("sp1") or 0)
                ps.mono_ts = _time.monotonic()
                ps.wall_ts = datetime.now(IST).isoformat(timespec="seconds")
                ps.source = "poll"
            except Exception as e:
                return self._ladder_skip(level_id, sig_id, sym, label, f"no usable price: {e}")
        if ps.lp <= 0:
            return self._ladder_skip(level_id, sig_id, sym, label, "no usable price")
        if ps.lp > lvl.price:
            # price bounced back above the level between tick and execution
            self._ladder_cooldown[level_id] = _time.monotonic() - 25.0   # re-armable in ~5s
            return self._ladder_skip(level_id, sig_id, sym, label,
                                     f"price {ps.lp:.2f} bounced back above level {lvl.price:.2f}")

        expected_entry = ps.ask if ps.ask > 0 else ps.lp

        # price-aware exit-config re-check — see _exit_config_error (a points SL
        # wider than the price would leave this rung with no stop at all)
        bad_cfg = self._exit_config_error(inst, expected_entry)
        if bad_cfg:
            return self._ladder_skip(level_id, sig_id, sym, label, bad_cfg)

        if inst.max_spread_points > 0 and ps.bid > 0 and ps.ask > 0:
            spread = ps.ask - ps.bid
            if spread > inst.max_spread_points:
                return self._ladder_skip(level_id, sig_id, sym, label,
                                         f"spread {spread:.2f} wider than guard {inst.max_spread_points:.2f}")

        # min-gap (ladder): enforce spacing to the NEAREST open rung in either
        # direction — a recycled level legitimately refires ABOVE still-open lower
        # rungs, so the automated path's "must be below the last buy" rule would
        # wrongly block every recycle re-buy.
        if inst.min_gap_points > 0 and open_lots:
            nearest = min(abs(ps.lp - (l.entry_price or 0)) for l in open_lots)
            if nearest < inst.min_gap_points:
                return self._ladder_skip(level_id, sig_id, sym, label,
                                         f"price {ps.lp:.2f} is within {nearest:.2f} of an open rung "
                                         f"(min gap {inst.min_gap_points:g})")

        # ---- sizing: per-level override wins, else the same vol-target math ----
        atr_val = 0.0
        if lvl.lots_override and lvl.lots_override > 0:
            lots = int(lvl.lots_override)
            hard_cap = max(int(getattr(settings, "max_lots_hard_cap", 50)), 1)
            if lots > hard_cap:
                event_log.warn("LADDER", f"{label}: lots override {lots} exceeds the system hard cap — "
                                         f"clamped to {hard_cap} (MAX_LOTS_HARD_CAP)", sym=sym)
                lots = hard_cap
            event_log.math(f"{label}: user override → {lots} lot(s) for this level "
                           "(sizing math bypassed by explicit level config)", sym=sym)
        else:
            sizing = await compute_size(self.rest, inst, timeframe_min=240,
                                        signal_atr=None, entry_price=expected_entry)
            for step in sizing.steps:
                event_log.math(step, sym=sym)
            if sizing.lots <= 0:
                return self._ladder_skip(level_id, sig_id, sym, label,
                                         f"sizing → 0 lots ({sizing.reason})")
            lots = sizing.lots
            atr_val = sizing.atr

        qty_units = lots * max(inst.lot_size, 1)
        # anchor = nearest open rung ABOVE this entry (price-based; see _anchor_lot)
        anchor_lot = self._anchor_lot(open_lots, expected_entry)
        prev_entry = anchor_lot.entry_price if anchor_lot is not None else None
        tgt_override = lvl.target_override if (lvl.target_override and lvl.target_override > 0) else None
        sl_override = lvl.sl_override if (lvl.sl_override and lvl.sl_override > 0) else None
        plan_tgt, plan_anchor = compute_target(inst, expected_entry, prev_entry)
        if tgt_override is not None:
            plan_tgt = round(float(tgt_override), 4)
        plan_sl = compute_sl(inst, expected_entry)
        if sl_override is not None and getattr(inst, "sl_enabled", False) is not False:
            plan_sl = round(float(sl_override), 4)
        tgt_desc = (f"target {plan_tgt:.2f} (user-set)" if tgt_override is not None else
                    f"target {plan_tgt:.2f} (anchor "
                    f"{'prev buy ' if prev_entry is not None else 'own entry '}{plan_anchor:.2f} "
                    f"+ {inst.target_value:g}{'%' if inst.target_mode == 'percent' else ' pts'})")
        event_log.math(
            f"Ladder pre-trade plan {label}: BUY {lots} lot(s) = {qty_units} units {inst.tsym} "
            f"@ ~{expected_entry:.2f} | {tgt_desc} | SL {plan_sl:.2f}", sym=sym)

        # ---- pre-trade margin gate (Shoonya SPAN) ----
        prod = inst.product_type or settings.product_type
        ok_margin, margin_reason = await self._margin_ok(inst, qty_units, prod)
        if not ok_margin:
            return self._ladder_skip(level_id, sig_id, sym, label, margin_reason)

        # ---- place + verify (identical to the automated path) ----
        # LIMIT (default) buys at the exact LEVEL price — no slippage above your
        # rung; MKT lets the gateway reprice to a marketable limit through the touch.
        buy_pt, buy_px = self._order_params(inst.buy_order_type, lvl.price, inst.tick_size)
        try:
            order_id = await self.rest.place_order(
                side="B", exchange=inst.exch, tradingsymbol=inst.tsym,
                quantity=qty_units, price_type=buy_pt, price=buy_px, remarks="",
                product_type=prod)
        except Exception as e:
            return self._ladder_skip(level_id, sig_id, sym, label, f"order placement error: {e}")
        if not order_id:
            return self._ladder_skip(level_id, sig_id, sym, label, "gateway returned no order id")

        with db_session() as db:
            seq = (db.query(func.coalesce(func.max(Lot.seq), 0))
                   .filter(Lot.instrument_id == inst.id).scalar() or 0) + 1
            lot = Lot(
                instrument_id=inst.id, seq=seq, exch=inst.exch,
                contract_tsym=inst.tsym, contract_token=inst.token,
                lots=lots, qty=qty_units, lot_size=inst.lot_size,
                entry_price=expected_entry, raw_entry_price=expected_entry,
                entry_order_id=order_id, signal_id=sig_id,
                target_price=plan_tgt, sl_price=plan_sl,
                target_override=tgt_override, sl_override=sl_override,
                anchor_lot_id=(anchor_lot.id if anchor_lot is not None else None),
                status="PENDING", atr_at_entry=atr_val, timeframe_min=240,
                source="ladder", product_type=prod, notes=f"ladder level {label} @ {lvl.price}",
            )
            db.add(lot)
            db.commit()
            lot_id = lot.id
            row = db.get(LadderLevel, level_id)
            row.status = "TRIGGERED"
            row.lot_id = lot_id
            row.triggered_at = now_ist()
            row.fire_count = (row.fire_count or 0) + 1   # history: how many times this level executed (survives re-arm)
            db.commit()

        event_log.trade("LADDER", f"BUY order {order_id} sent for ladder {label}: {lots} lot(s) "
                                  f"{inst.tsym} — verifying fill…", sym=sym)

        def finish(status: str, reason: str, lot_id_: int | None = None) -> dict:
            with db_session() as db:
                s = db.get(Signal, sig_id)
                s.status = status
                s.reason = reason
                s.lot_id = lot_id_
                db.commit()
            return {"signal_id": sig_id, "status": status, "reason": reason, "lot_id": lot_id_}

        report = await self.om.verify(order_id, qty_units)
        await self._post_trade_math(inst, lot_id, sig_id, report, prev_entry, finish,
                                    target_override=tgt_override, sl_override=sl_override)

        with db_session() as db:
            lot_row = db.get(Lot, lot_id)
            entry_failed = lot_row is not None and lot_row.status == "CANCELLED"
            if entry_failed:
                # order never filled — re-arm the level (with cooldown) so it can fire again
                row = db.get(LadderLevel, level_id)
                row.status = "PENDING"
                row.lot_id = None
                row.triggered_at = None
                db.commit()
        if entry_failed:
            self._ladder_cooldown[level_id] = _time.monotonic()
            event_log.warn("LADDER", f"{sym} {label} buy did not fill — level re-armed", sym=sym)
            return

        # GRID: the entry filled (MKT path) — cascade the ACTUAL fill down to
        # the levels below, exactly as the resting-LMT path does. _post_trade_math
        # has already written the real fill onto lot.entry_price.
        with db_session() as db:
            inst_row = db.get(Instrument, inst_id)
            lvl_row = db.get(LadderLevel, level_id)
            lot_row = db.get(Lot, lot_id)
            if inst_row is not None and lvl_row is not None and lot_row is not None:
                self._apply_ladder_shift(db, inst_row, lvl_row,
                                         lot_row.entry_price or lvl_row.price)
                db.commit()

        # The ladder is FINITE: the user's configured levels are ALL the levels.
        # After the last level fires there is no further buy until the user adds
        # or increases levels — the ladder is never auto-extended downward.

    # ================= broker-resting ladder orders (LMT flow) =================
    # For ladder instruments with buy_order_type == "LMT" the engine does NOT
    # fire entries off ticks. The active level's LIMIT order RESTS at the broker
    # — the exchange fills it on real trades only, so a ghost/replayed tick in
    # our feed can never open a position. Each filled rung's TARGET then rests
    # as a sell LIMIT the same way (placed ONLY after the entry fills). The
    # stop-loss stays engine-monitored (MKT on trigger — guaranteed fill).

    @staticmethod
    def _uses_resting_entries(inst: Instrument) -> bool:
        return inst.mode == "ladder" and (inst.buy_order_type or "LMT").upper() == "LMT"

    @staticmethod
    def _uses_resting_targets(inst: Instrument) -> bool:
        # resting targets are part of the resting-order architecture (opted in by
        # buy_order_type LMT) — a legacy MKT-entry ladder keeps its old behavior
        return (inst.mode == "ladder"
                and (inst.buy_order_type or "LMT").upper() == "LMT"
                and (inst.sell_order_type or "LMT").upper() == "LMT")

    # ---- broker-truth helpers (never trust an unconfirmed cancel/absence) ----

    def _order_gone_strike(self, oid: str) -> bool:
        """Two-strike confirmation that an order has truly vanished from a
        SUCCESSFULLY fetched order book: the first miss only records a
        timestamp; a second miss ≥90s later confirms. Any sighting clears."""
        now = _time.monotonic()
        first = self._order_gone_at.get(oid)
        if first is None:
            self._order_gone_at[oid] = now
            return False
        return now - first >= 90.0

    def _order_seen(self, oid: str) -> None:
        self._order_gone_at.pop(oid, None)

    async def _cancel_confirmed(self, order_id: str) -> tuple[bool, str, int, float]:
        """Cancel + CONFIRM against the broker book. Returns
        (dead, status, filled_units, avg_price). dead=True ONLY when the book
        proves the order is finished (terminal status, or confirmed absent via
        the two-strike rule). dead=False means the order MAY STILL BE LIVE —
        callers must abort their operation instead of proceeding as if
        cancelled (the #1 double-sell/orphan-order hazard)."""
        if not order_id:
            return True, "", 0, 0.0
        try:
            await self.rest.cancel_order(order_id)
        except Exception:
            pass
        try:
            row = await self.rest.find_order(order_id)
        except Exception:
            return False, "", 0, 0.0            # book unreadable — outcome unknown
        if row is None:
            if self._order_gone_strike(order_id):
                self._order_seen(order_id)
                return True, "GONE", 0, 0.0     # confirmed absent (2 good fetches ≥90s apart)
            return False, "", 0, 0.0
        self._order_seen(order_id)
        status = (row.get("status") or "").upper()
        filled, avg = _fillshares(row), _avgprc(row)
        if status in TERMINAL:
            return True, status, filled, avg
        return False, status, filled, avg       # still working at the broker

    async def _find_untracked_order(self, side: str, tsym: str, price: float,
                                    qty: int, prod: str) -> str | None:
        """Scan today's book for a LIVE order matching side/contract/price/qty
        that no lot tracks — the fingerprint of a placement whose HTTP reply was
        lost. Adopting it instead of placing again prevents duplicate orders."""
        try:
            book = await self.rest.get_order_book()
        except Exception as e:
            # This scan is the ONLY thing standing between a lost placement reply
            # and a duplicate order, so an unreadable book must not read as "no
            # such order exists". Raise so the caller holds instead of placing.
            raise _BookUnreadable(str(e)) from e
        with db_session() as db:
            known: set[str] = set()
            for (a, b, c) in db.query(Lot.entry_order_id, Lot.target_order_id, Lot.exit_order_id).all():
                for v in (a, b, c):
                    if v:
                        known.add(v)
        for o in book:
            try:
                o_price = float(o.get("prc") or o.get("price") or 0)
                o_qty = int(float(o.get("qty") or 0))
            except (TypeError, ValueError):
                continue
            if (o.get("trantype") == side and o.get("tsym") == tsym
                    and (o.get("status") or "").upper() not in TERMINAL
                    and abs(o_price - price) < 1e-6 and o_qty == qty
                    and (o.get("prd") or "M") == prod
                    and (o.get("norenordno") or "") not in known):
                return o.get("norenordno")
        return None

    async def sync_resting_orders(self, inst_id: int | None = None, force: bool = False,
                                  clear_holds: bool = False) -> None:
        """Desired-state reconciliation for broker-resting orders (idempotent).
        Ensures (a) exactly one active level has a live resting BUY per armed
        ladder (plus any user re-established levels), (b) every OPEN rung that
        wants a resting target has one, (c) resting entries are CANCELLED while
        the strategy is held (switch off / paused / CB / rolling / disabled).

        `clear_holds` forgets the per-level retry cooldowns first, and belongs
        ONLY on user actions that plausibly fix whatever caused the last refusal
        — a config edit, arming, resetting a breaker, confirming a ladder.

        Without it, correcting the setting that blocked a level did nothing for
        up to five minutes: a refusal stamps a cooldown, and the retry window is
        five minutes outside the open. Someone changes the exact field the log
        complained about, watches the sync run, and sees the level still sitting
        there — indistinguishable from the fix not having worked.

        Deliberately NOT applied to the automatic callers. A level whose order
        the broker keeps rejecting also re-syncs, and clearing its cooldown there
        would retry immediately and burn the whole placement budget in seconds
        instead of backing off for five minutes.
        """
        with db_session() as db:
            q = db.query(Instrument).filter(Instrument.mode == "ladder")
            if inst_id is not None:
                q = q.filter(Instrument.id == inst_id)
            insts = q.all()
            if clear_holds:
                lq = db.query(LadderLevel.id)
                if inst_id is not None:
                    lq = lq.filter(LadderLevel.instrument_id == inst_id)
                for (lid,) in lq.all():
                    self._ladder_cooldown.pop(lid, None)
        now_m = _time.monotonic()
        for inst in insts:
            if not self._uses_resting_entries(inst) and not self._uses_resting_targets(inst):
                continue
            if not force and now_m - self._resting_last_sync.get(inst.id, 0.0) < 5.0:
                continue
            self._resting_last_sync[inst.id] = now_m
            try:
                await self._sync_one_instrument(inst.id)
            except Exception:
                logger.exception("resting-order sync failed for instrument %s", inst.id)

    async def _sync_one_instrument(self, inst_id: int) -> None:
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
        if inst is None:
            return
        hold = (not self.engine_on() or not inst.enabled or not inst.ladder_armed
                or inst.cb_tripped or self.global_cb_tripped
                or inst.rollover_state in ("scanning", "rolling"))
        if self._uses_resting_entries(inst):
            if hold:
                await self.cancel_resting_entries(inst_id, "strategy hold (switch/pause/CB/rollover)")
            elif market_open_now(inst.exch) and self.broker_connected:
                with db_session() as db:
                    active_lvls = (db.query(LadderLevel)
                                   .filter(LadderLevel.instrument_id == inst_id,
                                           LadderLevel.status.in_(["PENDING", "PLACED"]))
                                   .order_by(LadderLevel.price.desc()).all())
                    placed_ids = [l.id for l in active_lvls if l.status == "PLACED"]
                    top_lvl = active_lvls[0] if active_lvls else None
                    top_lvl_id = top_lvl.id if top_lvl is not None else None
                    top_lvl_price = top_lvl.price if top_lvl is not None else None
                    top_lvl_pending = top_lvl is not None and top_lvl.status == "PENDING"
                    # legacy pending entry orders (UNCONFIRMED lots) count as the
                    # active buy too — the ladder keeps at most ONE pending buy
                    _lvl_price_by_lot = {lv.lot_id: lv.price
                                         for lv in db.query(LadderLevel)
                                         .filter(LadderLevel.instrument_id == inst_id,
                                                 LadderLevel.lot_id.isnot(None)).all()}
                    legacy = [(l.id, _lvl_price_by_lot.get(l.id) or l.entry_price)
                              for l in db.query(Lot)
                              .filter(Lot.instrument_id == inst_id,
                                      Lot.status == "UNCONFIRMED").all()]
                legacy.sort(key=lambda t: t[1], reverse=True)
                legacy_top = legacy[0][1] if legacy else None
                # INVARIANT: exactly ONE pending buy in total, at the highest
                # price — counting BOTH level orders and legacy pending orders.
                if legacy_top is not None and (top_lvl_price is None or legacy_top >= top_lvl_price):
                    # a legacy order IS the active buy — no level order may rest
                    for pid in placed_ids:
                        await self._retire_placed_level(
                            inst_id, pid, "a higher buy order is already pending at the broker")
                    for lot_id, _p in legacy[1:]:
                        await self._retire_unconfirmed_entry(
                            inst_id, lot_id, "outranked by a higher pending buy order")
                else:
                    for lot_id, _p in legacy:
                        await self._retire_unconfirmed_entry(
                            inst_id, lot_id, "outranked by a higher active level")
                    for pid in placed_ids:
                        if pid != top_lvl_id:
                            await self._retire_placed_level(
                                inst_id, pid, "outranked by a higher active level")
                    if (top_lvl_pending
                            and top_lvl_id not in self._levels_inflight
                            and (_time.monotonic() - self._ladder_cooldown.get(top_lvl_id, 0.0)
                                 > level_retry_secs(inst.exch))):
                        self._levels_inflight.add(top_lvl_id)
                        try:
                            await self._place_level_order(inst_id, top_lvl_id)
                        finally:
                            self._levels_inflight.discard(top_lvl_id)
        # resting targets protect OPEN rungs — they are maintained even while the
        # strategy is held (exits must never lose protection when buying stops)
        if (self._uses_resting_targets(inst) and self.broker_connected
                and market_open_now(inst.exch)):
            with db_session() as db:
                open_ids = [l.id for l in db.query(Lot)
                            .filter(Lot.instrument_id == inst_id, Lot.status == "OPEN",
                                    Lot.exit_pending == False,                      # noqa: E712
                                    func.coalesce(Lot.target_order_id, "") == "").all()
                            if (l.target_price or 0) > 0]
            for lid in open_ids:
                async with self.lock_for(inst_id):
                    await self._place_target_order_locked(lid)

    async def _retire_unconfirmed_entry(self, inst_id: int, lot_id: int, reason: str) -> None:
        """Cancel a legacy UNCONFIRMED entry order that is outranked — the
        ladder keeps at most ONE pending buy. Race-fill safe; the level behind
        it re-arms via the settle pass once the lot cancels."""
        async with self.lock_for(inst_id):
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                if lot is None or lot.status != "UNCONFIRMED":
                    return
                oid = lot.entry_order_id
            if not oid:
                return
            dead, _s, filled, avg = await self._cancel_confirmed(oid)
            if filled > 0:
                await self._apply_entry_fill_locked(lot_id, filled, avg)
                return
            if not dead:
                return
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                if lot is None or lot.status != "UNCONFIRMED":
                    return
                lot.status = "CANCELLED"
                lot.notes = (lot.notes or "") + f" {reason};"
                if lot.signal_id:
                    s = db.get(Signal, lot.signal_id)
                    if s:
                        s.status = "skipped"
                        s.reason = reason
                db.commit()
        event_log.write("LADDER", f"pending buy order {oid} retired ({reason})")

    async def _retire_placed_level(self, inst_id: int, placed_level_id: int, reason: str) -> None:
        """Cancel a PLACED level's resting BUY (confirmed) and return the level
        to PENDING — used whenever a higher level outranks it. Race-fill safe."""
        async with self.lock_for(inst_id):
            with db_session() as db:
                lvl = db.get(LadderLevel, placed_level_id)
                if lvl is None or lvl.status != "PLACED":
                    return
                lot_id = lvl.lot_id
                lot = db.get(Lot, lot_id) if lot_id else None
                oid = lot.entry_order_id if (lot is not None and lot.status == "RESTING") else ""
            if oid:
                dead, _s, filled, avg = await self._cancel_confirmed(oid)
                if filled > 0:
                    await self._apply_entry_fill_locked(lot_id, filled, avg)
                    return              # it filled — the normal flow takes over
                if not dead:
                    return              # cancel unconfirmed — retry next pass
            with db_session() as db:
                lot = db.get(Lot, lot_id) if lot_id else None
                if lot is not None and lot.status == "RESTING":
                    lot.status = "CANCELLED"
                    lot.notes = (lot.notes or "") + f" {reason};"
                    if lot.signal_id:
                        s = db.get(Signal, lot.signal_id)
                        if s:
                            s.status = "skipped"
                            s.reason = reason
                lvl = db.get(LadderLevel, placed_level_id)
                if lvl is not None and lvl.status == "PLACED":
                    lvl.status = "PENDING"
                    lvl.lot_id = None
                db.commit()
                lno = lvl.level_no if lvl is not None else "?"
        event_log.write("LADDER", f"B{lno}: resting BUY retired ({reason}) — the higher level holds "
                                  "the active order now", level="info")

    async def confirm_ladder(self, inst_id: int, basis: str, anchor_price: float,
                             interval_points: float, num_levels: int,
                             sr_lookback_days: int, levels: list[dict]) -> dict:
        """Replace the pending ladder atomically UNDER THE INSTRUMENT LOCK —
        no placement can interleave, resting entries are cancelled (confirmed)
        inline, and stale SKIPPED/AWAIT_RECOVERY rows die with the old ladder."""
        async with self.lock_for(inst_id):
            with db_session() as db:
                resting = db.query(Lot).filter(Lot.instrument_id == inst_id,
                                               Lot.status == "RESTING").all()
                r_ids = [(l.id, l.entry_order_id) for l in resting]
            for lot_id, oid in r_ids:
                dead, _s, filled, avg = await self._cancel_confirmed(oid)
                if filled > 0:
                    await self._apply_entry_fill_locked(lot_id, filled, avg)
                    continue
                if not dead:
                    return {"ok": False,
                            "reason": "could not confirm cancel of the resting order at the broker — "
                                      "try again in a minute"}
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    lvl = db.query(LadderLevel).filter(LadderLevel.lot_id == lot_id).first()
                    if lot is not None and lot.status == "RESTING":
                        lot.status = "CANCELLED"
                        lot.notes = (lot.notes or "") + " ladder reconfigured;"
                        if lot.signal_id:
                            s = db.get(Signal, lot.signal_id)
                            if s:
                                s.status = "skipped"
                                s.reason = "ladder reconfigured"
                    if lvl is not None:
                        lvl.status = "PENDING"
                        lvl.lot_id = None
                    db.commit()
            with db_session() as db:
                inst = db.get(Instrument, inst_id)
                if inst is None or inst.mode != "ladder":
                    return {"ok": False, "reason": "ladder instrument not found"}
                inst.ladder_basis = "support" if basis == "support" else "fixed"
                inst.ladder_anchor_price = anchor_price
                inst.ladder_interval_points = interval_points
                inst.ladder_num_levels = max(min(num_levels, 20), 1)
                inst.ladder_sr_lookback_days = max(sr_lookback_days, 7)
                inst.ladder_armed = True
                # the WHOLE replaceable ladder dies: PENDING + user-facing
                # SKIPPED/AWAIT_RECOVERY (stale ones would keep auto-buying old prices)
                # This is a REPLACE: every level not in the incoming set is gone.
                # Name the casualties. A level silently deleted here is a buy the
                # user still believes is armed, and the only symptom is a ladder
                # that is quietly shorter than the number of levels they set.
                doomed = (db.query(LadderLevel)
                          .filter(LadderLevel.instrument_id == inst_id,
                                  LadderLevel.status.in_(["PENDING", "SKIPPED", "AWAIT_RECOVERY"]))
                          .all())
                kept = {round(float(lv["price"]), 2) for lv in levels}
                dropped = sorted({round(float(d.price), 2) for d in doomed} - kept, reverse=True)
                (db.query(LadderLevel)
                 .filter(LadderLevel.instrument_id == inst_id,
                         LadderLevel.status.in_(["PENDING", "SKIPPED", "AWAIT_RECOVERY"]))
                 .delete(synchronize_session=False))
                start_no = (db.query(func.coalesce(func.max(LadderLevel.level_no), 0))
                            .filter(LadderLevel.instrument_id == inst_id).scalar() or 0)
                for i, lv in enumerate(levels, start=1):
                    lv_price = round(float(lv["price"]), 4)
                    # GRID: freeze each level's target NOW = its own price +
                    # offset (self-anchored). Stored as target_override so it is
                    # pinned at fill time and does NOT move when the buy price
                    # shifts down later — a cheaper fill widens the profit. A
                    # target the user explicitly typed still wins.
                    frozen_tgt = (round(float(lv["target_override"]), 4)
                                  if lv.get("target_override")
                                  else compute_target(inst, lv_price, None)[0])
                    db.add(LadderLevel(
                        instrument_id=inst_id, level_no=start_no + i,
                        price=lv_price,
                        lots_override=(int(lv["lots_override"]) if lv.get("lots_override") else None),
                        target_override=frozen_tgt,
                        sl_override=(round(float(lv["sl_override"]), 4)
                                     if lv.get("sl_override") else None),
                        status="PENDING",
                        source=inst.ladder_basis or "manual"))
                db.commit()
                sym, n = inst.sym, len(levels)
        event_log.write("LADDER", f"{sym} ladder CONFIRMED & armed: {n} level(s), basis {basis}, "
                                  f"first buy @ {levels[0]['price']:g}", sym=sym)
        if dropped:
            event_log.warn("LADDER", f"{sym}: ladder replace REMOVED {len(dropped)} level(s) not in the "
                                     f"confirmed set — {', '.join(f'{p:g}' for p in dropped)}. They will not "
                                     "buy. Re-add them from the Levels view if that was not intended.",
                           sym=sym, data={"dropped": dropped})
        asyncio.create_task(self.sync_resting_orders(inst_id, force=True, clear_holds=True))
        return {"ok": True, "levels_saved": n}

    async def _place_level_task(self, inst_id: int, level_id: int) -> None:
        """Background wrapper used by the tick path (AWAIT_RECOVERY placements)."""
        try:
            await self._place_level_order(inst_id, level_id)
        except Exception:
            logger.exception("resting level placement failed")
            self._ladder_cooldown[level_id] = _time.monotonic()
        finally:
            self._levels_inflight.discard(level_id)

    async def _place_level_order(self, inst_id: int, level_id: int) -> None:
        async with self.lock_for(inst_id):
            await self._place_level_order_locked(inst_id, level_id)

    async def _place_level_order_locked(self, inst_id: int, level_id: int) -> None:
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
            lvl = db.get(LadderLevel, level_id)
        if inst is None or lvl is None or lvl.status not in ("PENDING", "AWAIT_RECOVERY"):
            return
        sym, label = inst.sym, f"B{lvl.level_no}"

        def hold(reason: str) -> None:
            self._ladder_cooldown[level_id] = _time.monotonic()
            event_log.write("LADDER", f"{sym} {label}: resting order NOT placed — {reason}",
                            level="warn", sym=sym)

        if not self.engine_on() or not inst.enabled or not inst.ladder_armed:
            return          # silent — sync will re-place when the hold lifts
        if inst.cb_tripped or self.global_cb_tripped or inst.rollover_state in ("scanning", "rolling"):
            return
        if not market_open_now(inst.exch) or not self.broker_connected:
            return
        # price-aware: the level price IS the intended entry, so a points SL
        # wider than it (→ sl_price ≤ 0 = no stop) is caught before we rest a
        # buy order at the broker.
        bad_cfg = self._exit_config_error(inst, lvl.price)
        if bad_cfg:
            return hold(bad_cfg)
        with db_session() as db:
            open_lots = (db.query(Lot)
                         .filter(Lot.instrument_id == inst_id,
                                 Lot.status.in_(["OPEN", "PENDING", "UNCONFIRMED", "TEMP_EXITED"]))
                         .all())
        # (no max-rungs gate — the finite ladder itself bounds simultaneous rungs)
        if inst.min_gap_points > 0 and open_lots:
            nearest = min(abs(lvl.price - (l.entry_price or 0)) for l in open_lots)
            if nearest < inst.min_gap_points:
                return hold(f"level {lvl.price:.2f} is within {nearest:.2f} of an open rung "
                            f"(min gap {inst.min_gap_points:g})")

        # ---- sizing at the LEVEL price (the actual intended entry) ----
        atr_val = 0.0
        if lvl.lots_override and lvl.lots_override > 0:
            lots = int(lvl.lots_override)
            hard_cap = max(int(getattr(settings, "max_lots_hard_cap", 50)), 1)
            if lots > hard_cap:
                event_log.warn("LADDER", f"{label}: lots override {lots} exceeds the system hard cap — "
                                         f"clamped to {hard_cap} (MAX_LOTS_HARD_CAP)", sym=sym)
                lots = hard_cap
        else:
            sizing = await compute_size(self.rest, inst, timeframe_min=240,
                                        signal_atr=None, entry_price=lvl.price)
            if sizing.lots <= 0:
                return hold(f"sizing → 0 lots ({sizing.reason})")
            lots, atr_val = sizing.lots, sizing.atr
        qty_units = lots * max(inst.lot_size, 1)

        prod = inst.product_type or settings.product_type
        ok_margin, margin_reason = await self._margin_ok(inst, qty_units, prod)
        if not ok_margin:
            return hold(margin_reason)

        if not self._rate_allow(
                "entry", level_id, _ENTRY_REPLACE_MAX, _ENTRY_REPLACE_WINDOW,
                category="LADDER", sym=sym,
                halt_msg=(f"{sym} {label}: its resting BUY has been placed {_ENTRY_REPLACE_MAX} times in "
                          f"the last hour. A level that keeps needing to be re-placed is being rejected "
                          f"or cancelled at the broker, and every attempt counts against the account's "
                          f"order-to-trade ratio. Placement for THIS level is stopped until the engine "
                          f"restarts — the other levels are unaffected. Check the broker for why its "
                          f"orders are not sticking.")):
            return

        buy_px = self._round_tick(lvl.price, inst.tick_size)
        # a previous placement's HTTP reply may have been lost while the broker
        # accepted the order — ADOPT a matching untracked live BUY instead of
        # placing a duplicate at the same level
        try:
            order_id = await self._find_untracked_order("B", inst.tsym, buy_px, qty_units, prod)
        except _BookUnreadable as e:
            # Can't tell whether an earlier placement is already resting, so
            # placing now risks buying this level twice. Wait for a readable book.
            return hold(f"order book unreadable ({e}) — holding rather than risking a duplicate buy")
        if order_id:
            event_log.warn("LADDER", f"{sym} {label}: adopted an untracked live BUY LIMIT already resting "
                                     f"at {buy_px:g} (earlier placement's reply was lost) — no duplicate "
                                     "order placed", sym=sym)
        else:
            try:
                order_id = await self.rest.place_order(
                    side="B", exchange=inst.exch, tradingsymbol=inst.tsym,
                    quantity=qty_units, price_type="LMT", price=buy_px, remarks="",
                    product_type=prod)
            except Exception as e:
                return hold(f"order placement error: {e} — if the broker accepted it anyway, "
                            "the next sync adopts it instead of duplicating")
            if not order_id:
                return hold("gateway returned no order id")

        # planned target/SL from the limit price (display only — recomputed from
        # the ACTUAL fill when the order executes)
        anchor_lot = self._anchor_lot([l for l in open_lots if l.status == "OPEN"], buy_px)
        prev_entry = anchor_lot.entry_price if anchor_lot is not None else None
        plan_tgt, _pa = compute_target(inst, buy_px, prev_entry)
        if lvl.target_override and lvl.target_override > 0:
            plan_tgt = round(float(lvl.target_override), 4)
        plan_sl = compute_sl(inst, buy_px)
        if lvl.sl_override and lvl.sl_override > 0:
            plan_sl = round(float(lvl.sl_override), 4)

        with db_session() as db:
            sig = Signal(source="ladder", sym=sym, action="BUY", timeframe_min=240,
                         status="placed",
                         reason=f"BUY LIMIT {qty_units}u resting at broker @ {buy_px:g}",
                         payload={"level_no": lvl.level_no, "level_price": lvl.price,
                                  "limit_price": buy_px, "resting": True})
            db.add(sig)
            db.commit()
            sig_id = sig.id
            seq = (db.query(func.coalesce(func.max(Lot.seq), 0))
                   .filter(Lot.instrument_id == inst_id).scalar() or 0) + 1
            lot = Lot(
                instrument_id=inst_id, seq=seq, exch=inst.exch,
                contract_tsym=inst.tsym, contract_token=inst.token,
                lots=lots, qty=qty_units, lot_size=inst.lot_size,
                entry_price=buy_px, raw_entry_price=buy_px,
                entry_order_id=order_id, signal_id=sig_id,
                target_price=plan_tgt, sl_price=plan_sl,
                anchor_lot_id=(anchor_lot.id if anchor_lot is not None else None),
                status="RESTING", atr_at_entry=atr_val, timeframe_min=240,
                source="ladder", product_type=prod,
                target_override=lvl.target_override, sl_override=lvl.sl_override,
                notes=f"ladder level {label} @ {lvl.price} (resting at broker)",
            )
            db.add(lot)
            db.commit()
            lot_id = lot.id
            row = db.get(LadderLevel, level_id)
            row.status = "PLACED"
            row.lot_id = lot_id
            if "order not executed" in (row.note or ""):
                row.note = ""            # the miss is over — a live order rests now
            db.commit()
        self._rate_note("entry", level_id)
        event_log.trade("LADDER", f"{sym} {label}: BUY LIMIT {lots} lot(s) = {qty_units}u {inst.tsym} "
                                  f"@ {buy_px:g} RESTING at broker (order {order_id}) — fills only on a "
                                  "real trade at/below the level", sym=sym,
                        data={"level_no": lvl.level_no, "limit": buy_px, "order_id": order_id})

    def _apply_ladder_shift(self, db, inst: Instrument, level, actual_price: float) -> None:
        """GRID shift. On a level's FIRST fill, slide every still-PENDING level
        BELOW it down by (level price − actual fill). The next buy then sits one
        interval below where we ACTUALLY got in, and the shift compounds down the
        ladder (B2 = B1_fill − interval, B3 = B2_fill − interval, …).

        This level's OWN price is left as configured (it has already filled); only
        the levels beneath move. Frozen targets (target_override) are deliberately
        NOT touched — a cheaper fill therefore widens that rung's profit.

        Runs at most once per level (shift_applied guard): after a rung hits target
        and the level re-arms, a later re-buy must NOT ratchet the ladder down
        again. Operates on the caller's session; the caller commits.
        """
        if level is None or level.shift_applied:
            return
        level.shift_applied = True
        delta = round((level.price or 0.0) - actual_price, 4)
        if delta <= 0:
            return    # filled at/above the level price — no gap to pass downward
        below = (db.query(LadderLevel)
                 .filter(LadderLevel.instrument_id == inst.id,
                         LadderLevel.status == "PENDING",
                         LadderLevel.price < level.price)
                 .all())
        for lv in below:
            lv.price = round(lv.price - delta, 4)
        if below:
            event_log.write("LADDER", f"{inst.sym} B{level.level_no} filled {actual_price:g} "
                                      f"(level {level.price:g}, −{delta:g}) → {len(below)} level(s) below "
                                      f"shifted down {delta:g}; frozen targets unchanged", sym=inst.sym)

    async def _apply_entry_fill_locked(self, lot_id: int, filled: int, avg: float) -> None:
        """RESTING (or legacy UNCONFIRMED) entry filled at the broker → real OPEN
        rung with post-fill math. Caller MUST hold the instrument lock."""
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None or lot.status not in ("RESTING", "UNCONFIRMED"):
                return
            inst = db.get(Instrument, lot.instrument_id)
            lvl = db.query(LadderLevel).filter(LadderLevel.lot_id == lot_id).first()
            actual = avg if avg > 0 else lot.entry_price
            qty = filled if filled > 0 else lot.qty
            others = (db.query(Lot).filter(Lot.instrument_id == lot.instrument_id,
                                           Lot.status == "OPEN", Lot.id != lot.id).all())
            prev = self._anchor_lot(others, actual)
            prev_entry = prev.entry_price if prev is not None else None
            tgt, _anchor = compute_target(inst, actual, prev_entry)
            tgt_ovr = lot.target_override or (lvl.target_override if lvl is not None else None)
            if tgt_ovr and tgt_ovr > 0:
                tgt = round(float(tgt_ovr), 4)
            sl = compute_sl(inst, actual)
            sl_ovr = lot.sl_override or (lvl.sl_override if lvl is not None else None)
            if sl_ovr and sl_ovr > 0:
                sl = round(float(sl_ovr), 4)
            lot.status = "OPEN"
            lot.entry_price = actual
            lot.raw_entry_price = actual
            lot.qty = qty
            lot.lots = max(qty // max(lot.lot_size, 1), 1)
            lot.entry_time = now_ist()
            lot.target_price = tgt
            lot.sl_price = sl
            lot.anchor_lot_id = prev.id if prev is not None else None
            if lot.signal_id:
                s = db.get(Signal, lot.signal_id)
                if s:
                    s.status = "executed"
                    s.reason = f"resting order filled @ {actual:g}"
                    s.lot_id = lot_id
            if lvl is not None:
                lvl.status = "FILLED"
                lvl.fire_count = (lvl.fire_count or 0) + 1
                lvl.triggered_at = now_ist()
                # A gap through the level fills BETTER than the price you set (a
                # buy limit is a ceiling), so the trade and the level would show
                # two different numbers. Record where the money actually went —
                # this level's OWN price stays as configured; the GRID shift
                # below re-prices only the levels BENEATH it from the actual fill.
                tick = float(inst.tick_size or 0.0)
                if lvl.price and abs(actual - lvl.price) > max(tick, 1e-9):
                    lvl.note = (f"gap-{'down' if actual < lvl.price else 'up'} fill: level {lvl.price:g} → "
                                f"filled {actual:g} · target frozen, SL from {actual:g}")
                # GRID: cascade the cheaper entry down to the levels below.
                self._apply_ladder_shift(db, inst, lvl, actual)
            db.commit()
            sym, seq = inst.sym, lot.seq
        event_log.trade("LADDER", f"{sym} resting BUY FILLED at broker: rung #{seq} {qty}u @ {actual:g} "
                                  f"→ target {tgt:g} | SL {sl:g}", sym=sym,
                        data={"lot_id": lot_id, "fill": actual, "target": tgt, "sl": sl})
        await self._place_target_order_locked(lot_id)

    async def _adopt_partial_entry(self, lot_id: int, cum_filled: int, avg: float) -> None:
        """A WORKING resting entry has partial fills: adopt the newly-filled
        units as their own OPEN rung so a real position never sits without
        SL/target/P&L/CB coverage. The RESTING shell keeps working for the
        remainder. Caller MUST hold the instrument lock."""
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None or lot.status != "RESTING":
                return
            inc = int(cum_filled) - int(lot.filled_adopted or 0)
            if inc <= 0 or inc > lot.qty:
                return
            inst = db.get(Instrument, lot.instrument_id)
            lvl = db.query(LadderLevel).filter(LadderLevel.lot_id == lot_id).first()
            actual = avg if avg > 0 else lot.entry_price
            others = db.query(Lot).filter(Lot.instrument_id == lot.instrument_id,
                                          Lot.status == "OPEN").all()
            prev = self._anchor_lot(others, actual)
            prev_entry = prev.entry_price if prev is not None else None
            tgt, _a = compute_target(inst, actual, prev_entry)
            tgt_ovr = lot.target_override or (lvl.target_override if lvl is not None else None)
            if tgt_ovr and tgt_ovr > 0:
                tgt = round(float(tgt_ovr), 4)
            sl = compute_sl(inst, actual)
            sl_ovr = lot.sl_override or (lvl.sl_override if lvl is not None else None)
            if sl_ovr and sl_ovr > 0:
                sl = round(float(sl_ovr), 4)
            seq2 = (db.query(func.coalesce(func.max(Lot.seq), 0))
                    .filter(Lot.instrument_id == lot.instrument_id).scalar() or 0) + 1
            sib = Lot(instrument_id=lot.instrument_id, seq=seq2, exch=lot.exch,
                      contract_tsym=lot.contract_tsym, contract_token=lot.contract_token,
                      lots=max(inc // max(lot.lot_size, 1), 1), qty=inc, lot_size=lot.lot_size,
                      entry_price=actual, raw_entry_price=actual,
                      entry_order_id=lot.entry_order_id, signal_id=lot.signal_id,
                      target_price=tgt, sl_price=sl,
                      anchor_lot_id=(prev.id if prev is not None else None),
                      status="OPEN", atr_at_entry=lot.atr_at_entry, timeframe_min=240,
                      source="ladder", product_type=lot.product_type,
                      notes=f"partial fill adopted from resting rung #{lot.seq} ({inc}u);")
            db.add(sib)
            lot.qty = max(lot.qty - inc, 0)
            if lot.qty > 0:
                lot.lots = max(lot.qty // max(lot.lot_size, 1), 1)
            lot.filled_adopted = int(cum_filled)
            db.commit()
            sib_id = sib.id
            sym = inst.sym if inst else ""
        event_log.trade("LADDER", f"{sym}: PARTIAL fill on resting entry — adopted {inc}u @ {actual:g} as "
                                  f"its own OPEN rung (SL/target armed); remainder keeps resting", sym=sym)
        await self._place_target_order_locked(sib_id)

    async def _resolve_entry_order(self, lot_id: int) -> None:
        """Check a RESTING lot's order against the broker book and settle it."""
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None or lot.status != "RESTING":
                return
            oid, inst_id, placed_at = lot.entry_order_id, lot.instrument_id, lot.entry_time
            adopted = int(lot.filled_adopted or 0)
        try:
            row = await self.rest.find_order(oid)
        except Exception:
            return                      # book unavailable — try next pass
        vanished = False
        if row is None:
            # book fetch SUCCEEDED but the order is absent. This can be (a) book
            # lag right after placement, (b) a transient partial/empty book, or
            # (c) the order truly no longer exists (e.g. yesterday's day order).
            # Guard (a) with a placement grace window and (b) with the 2-strike
            # rule before ever acting.
            if placed_at and (now_ist() - placed_at).total_seconds() < 120:
                return
            if not self._order_gone_strike(oid):
                return
            self._order_seen(oid)
            vanished = True
            status, filled, avg = "CANCELLED", 0, 0.0
        else:
            self._order_seen(oid)
            status = (row.get("status") or "").upper()
            filled, avg = _fillshares(row), _avgprc(row)
            if status not in TERMINAL:
                # WORKING order — adopt any not-yet-adopted partial fill so a real
                # position never sits without SL/P&L/CB coverage
                if filled > adopted:
                    async with self.lock_for(inst_id):
                        await self._adopt_partial_entry(lot_id, filled, avg)
                return
            filled = max(filled - adopted, 0)     # only the units NOT yet adopted

        # an order that vanished from the book may have FILLED (transient book
        # flake mid-session, or yesterday's book is gone). Same for a COMPLETE
        # row whose fill-qty field is missing: COMPLETE means the broker FILLED
        # it — it must NEVER fall through to the cancel path (that would orphan
        # a live position). Cross-check broker positions before declaring
        # anything expired: an untracked surplus ≥ our qty means it filled.
        prev_session = bool(placed_at and placed_at.date() < now_ist().date())
        complete_no_qty = (not vanished and status == "COMPLETE"
                           and filled == 0 and adopted == 0)
        if vanished or complete_no_qty:
            try:
                positions = await self.rest.get_positions_raw()
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    if lot is None or lot.status != "RESTING":
                        return
                    tracked = sum(l.qty for l in db.query(Lot)
                                  .filter(Lot.status == "OPEN",
                                          Lot.contract_tsym == lot.contract_tsym,
                                          func.coalesce(Lot.product_type, "M") == (lot.product_type or "M"))
                                  .all())
                    need_qty, tsym_l, prod_l = lot.qty, lot.contract_tsym, (lot.product_type or "M")
                broker_net = 0
                for p in positions:
                    if p.get("tsym") == tsym_l and (p.get("prd") or "M") == prod_l:
                        broker_net += int(float(p.get("netqty", "0") or 0))
                if broker_net >= tracked + need_qty:
                    async with self.lock_for(inst_id):
                        await self._apply_entry_fill_locked(lot_id, need_qty, 0.0)
                    event_log.warn("LADDER", "resting order gone from the book (or COMPLETE with no fill "
                                             "qty) but the broker HOLDS the position — adopted as a FILLED "
                                             "rung at the limit price (verify at the broker)", sym="")
                    await self.sync_resting_orders(inst_id, force=True)
                    return
                if complete_no_qty:
                    # broker says COMPLETE but the position surplus isn't visible
                    # yet — keep the lot RESTING; the next pass settles it
                    return
            except Exception:
                # Positions unreadable. The order is already absent from the book,
                # so whether it filled is exactly what this call was meant to
                # settle — and falling through would cancel the lot and re-place
                # the buy, which on a filled order means a SECOND live position
                # the engine then refuses to manage. Unknown is not "did not
                # fill": leave it RESTING and settle on a pass that can read.
                return

        filled_now = False
        async with self.lock_for(inst_id):
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                if lot is None or lot.status != "RESTING":
                    return
            if filled > 0:
                await self._apply_entry_fill_locked(lot_id, filled, avg)
                filled_now = True
            elif adopted > 0:
                # order finished and every filled unit was already adopted as a
                # sibling rung — retire the shell, mark the level FILLED
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    lvl = db.query(LadderLevel).filter(LadderLevel.lot_id == lot_id).first()
                    lot.status = "CANCELLED"
                    lot.notes = (lot.notes or "") + " resting entry done — fills adopted as sibling rung(s);"
                    if lvl is not None:
                        lvl.status = "FILLED"
                        lvl.fire_count = (lvl.fire_count or 0) + 1
                        lvl.triggered_at = now_ist()
                    if lot.signal_id:
                        s = db.get(Signal, lot.signal_id)
                        if s:
                            s.status = "executed"
                            s.reason = "filled via partial adoptions"
                    db.commit()
                filled_now = True
            else:
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    inst = db.get(Instrument, inst_id)
                    lvl = db.query(LadderLevel).filter(LadderLevel.lot_id == lot_id).first()
                    lot.status = "CANCELLED"
                    lot.notes = (lot.notes or "") + f" resting entry {status.lower()} at broker;"
                    if lot.signal_id:
                        s = db.get(Signal, lot.signal_id)
                        if s:
                            s.status = "skipped"
                            s.reason = f"resting order {status.lower()} at broker"
                    exch = inst.exch if inst else ""
                    in_session = market_open_now(exch) if exch else False
                    lno = lvl.level_no if lvl is not None else "?"
                    if lvl is not None:
                        lvl.lot_id = None
                        if status == "REJECTED":
                            lvl.status = "PENDING"
                            lvl.note = "order rejected — will retry"
                            self._ladder_cooldown[lvl.id] = _time.monotonic()
                        elif vanished and in_session and not prev_session:
                            # absent from a flaky book with NO explicit cancel row
                            # and no position found — re-queue instead of SKIP;
                            # placement dedups by adopting a matching live order
                            lvl.status = "PENDING"
                            lvl.note = "order missing from broker book — re-places automatically"
                        elif in_session and not prev_session:
                            lvl.status = "SKIPPED"
                            lvl.note = "cancelled at broker (outside dashboard)"
                        else:
                            # previous-session day order (or off-hours) → expiry,
                            # NOT a user cancel — re-queue for the next session
                            lvl.status = "PENDING"
                            lvl.note = "day order expired — re-places next session"
                    db.commit()
                    sym = inst.sym if inst else ""
                if status == "REJECTED":
                    event_log.warn("LADDER", f"{sym} B{lno}: resting order REJECTED by broker — "
                                             "level re-queued with cooldown", sym=sym)
                elif vanished and in_session and not prev_session:
                    event_log.warn("LADDER", f"{sym} B{lno}: resting order missing from the broker book "
                                             "with no cancel or fill found — level re-queued, the order "
                                             "re-places automatically", sym=sym)
                elif in_session and not prev_session:
                    event_log.warn("LADDER", f"{sym} B{lno}: resting order was CANCELLED at the broker "
                                             "outside the dashboard — level marked SKIPPED "
                                             "(re-establish it from the Levels view)", sym=sym)
                else:
                    event_log.write("LADDER", f"{sym} B{lno}: day order expired at session end — "
                                              "the level re-places automatically next session", sym=sym)
        await self.sync_resting_orders(inst_id, force=True)

    def _rate_allow(self, bucket: str, key: int, limit: int, window: float,
                    *, halt_msg: str, category: str = "EXIT", sym: str = "") -> bool:
        """Shared ceiling for any loop that places REAL ORDERS.

        Every automatic order in this engine is placed by a loop that re-asserts
        a desired state, and such a loop only stops when reality matches what it
        wants. When it cannot — because a read is wrong, or a broker keeps
        refusing — it does not stall, it repeats, and every repetition is a live
        order. That is not a hypothetical: a filled target that read as unfilled
        put out one real SELL per reconcile pass until someone noticed.

        So each of those loops gets a budget. Tripping it is loud and latched:
        an operator told that placement has stopped can act, whereas an
        unbounded loop is only ever discovered by its damage.

        Counted on SUCCESS only (see _rate_note) — a placement that errored
        created no order, and charging it here would let a broker having a bad
        minute disarm the loop for the rest of the window.
        """
        k = (bucket, key)
        if k in self._rate_halted:
            return False
        now = _time.monotonic()
        hist = [t for t in self._rate_hist.get(k, []) if now - t < window]
        self._rate_hist[k] = hist
        if len(hist) < limit:
            return True
        self._rate_halted.add(k)
        event_log.error(category, halt_msg, sym=sym,
                        data={"bucket": bucket, "key": key, "placements": len(hist),
                              "window_s": window})
        return False

    def _rate_note(self, bucket: str, key: int) -> None:
        """Record an order that actually reached the broker."""
        self._rate_hist.setdefault((bucket, key), []).append(_time.monotonic())

    def _exit_retry_wait(self, lot_id: int) -> float:
        """How long to wait before retrying this rung's exit.

        Doubles per consecutive failure and stops at a minute. Never becomes
        "give up": the position still wants out, and a stop-loss that refuses to
        retry is a worse bug than the one this is fixing.
        """
        n = self._exit_fails.get(lot_id, 0)
        return min(_EXIT_RETRY_BASE * (2 ** n), _EXIT_RETRY_MAX)

    def _note_exit_result(self, lot_id: int, ok: bool, sym: str = "") -> None:
        if ok:
            self._exit_fails.pop(lot_id, None)
            return
        n = self._exit_fails.get(lot_id, 0) + 1
        self._exit_fails[lot_id] = n
        if n == _EXIT_FAIL_ALARM_AFTER or (n > _EXIT_FAIL_ALARM_AFTER and n % 20 == 0):
            event_log.error(
                "EXIT",
                f"rung #{lot_id}: {n} consecutive failed exit attempts. Retries are now spaced "
                f"{self._exit_retry_wait(lot_id):.0f}s apart instead of {_EXIT_RETRY_BASE:.0f}s so the "
                f"broker is not hammered, but this rung is NOT exiting — find out why (margin, RMS "
                f"block, wrong product type) and close it by hand if needed.", sym=sym,
                data={"lot_id": lot_id, "consecutive_failures": n})

    async def _place_target_order_locked(self, lot_id: int) -> None:
        """Rest the TARGET sell LIMIT at the broker for an OPEN rung.
        Caller MUST hold the instrument lock. No-op when not applicable."""
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if (lot is None or lot.status != "OPEN" or lot.exit_pending
                    or (lot.target_order_id or "")):
                return
            inst = db.get(Instrument, lot.instrument_id)
        if inst is None or not self._uses_resting_targets(inst):
            return
        if (lot.target_price or 0) <= 0 or not market_open_now(lot.exch) or not self.broker_connected:
            return
        if not self._rate_allow(
                "target", lot_id, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW, sym=inst.sym,
                halt_msg=(f"rung #{lot.seq} {inst.sym}: its resting TARGET has been placed "
                          f"{_TARGET_REPLACE_MAX} times in {_TARGET_REPLACE_WINDOW / 60:.0f} minutes. "
                          f"Each one is a live SELL, so this is stopped NOW rather than continuing to "
                          f"short the account. CHECK THE BROKER for duplicate sell orders/fills on this "
                          f"contract. The rung keeps its engine-side tick exit; resting-target placement "
                          f"stays off for this rung until the engine restarts.")):
            return
        # NEVER rest a SELL unless the broker is known to hold the units.
        #
        # A resting target is only protection while the position exists; against
        # a flat account it is an order to go SHORT. Our own row saying OPEN is
        # not enough — that row can outlive the position (an exit whose order
        # left the per-day book, a manual close at the broker, a fill we failed
        # to read), and in every one of those cases the rung gets re-armed and
        # the sync rests a sell into nothing. Checked against the reconciler's
        # last positions snapshot, so it costs no extra broker call.
        prod_key = (lot.exch, lot.contract_tsym, lot.product_type or "M")
        if self._broker_net_at and _time.monotonic() - self._broker_net_at < 300.0:
            held = self._broker_net_seen.get(prod_key, 0)
            if held < lot.qty:
                event_log.warn(
                    "EXIT",
                    f"{inst.sym}: NOT resting rung #{lot.seq}'s target — the broker holds {held}u of "
                    f"{lot.contract_tsym} but this rung claims {lot.qty}u. Selling that would open a SHORT "
                    f"rather than close a long. The rung stays engine-monitored and the position check "
                    f"will settle the difference.", sym=inst.sym,
                    data={"lot_id": lot_id, "broker_held": held, "lot_qty": lot.qty})
                return
        px = self._round_tick(lot.target_price, inst.tick_size)
        prod = lot.product_type or settings.product_type
        # adopt an untracked live SELL at this price/qty (lost-reply dedup) —
        # otherwise a retry after an HTTP timeout would stack duplicate sells
        try:
            oid = await self._find_untracked_order("S", lot.contract_tsym, px, lot.qty, prod)
        except _BookUnreadable as e:
            # Without a readable book we cannot tell whether a sell for this rung
            # is already resting, and placing blind is how a rung ends up with two.
            # Defer — the engine's own tick exit still covers the target meanwhile.
            # (This also has to be caught rather than raised: refresh_target_order
            # runs on the PUT /lots/{id} request path, so an unreadable book would
            # otherwise fail a user's target edit with a 500.)
            event_log.warn("EXIT", f"{inst.sym}: order book unreadable ({e}) — not resting rung "
                                   f"#{lot.seq}'s target yet rather than risking a duplicate sell; "
                                   "the tick exit still covers it", sym=inst.sym)
            return
        if oid:
            event_log.warn("EXIT", f"{inst.sym}: adopted an untracked live SELL LIMIT @ {px:g} as rung "
                                   f"#{lot.seq}'s target (earlier placement's reply was lost)", sym=inst.sym)
        else:
            try:
                oid = await self.rest.place_order(
                    side="S", exchange=lot.exch, tradingsymbol=lot.contract_tsym,
                    quantity=lot.qty, price_type="LMT", price=px,
                    remarks=f"target_rest_l{lot_id}", product_type=prod)
            except Exception as e:
                event_log.warn("EXIT", f"{inst.sym}: could not rest target for rung #{lot.seq} ({e}) — "
                                       "engine tick-exit stays active; will retry (lost-reply orders are "
                                       "adopted, not duplicated)", sym=inst.sym)
                return
            if not oid:
                return
        ok = False
        with db_session() as db:
            l2 = db.get(Lot, lot_id)
            if (l2 is not None and l2.status == "OPEN" and not l2.exit_pending
                    and not (l2.target_order_id or "")):
                l2.target_order_id = oid
                db.commit()
                ok = True
        if not ok:      # lot changed while placing — never leave an orphan sell
            await self.rest.cancel_order(oid)
            return
        self._rate_note("target", lot_id)
        event_log.trade("EXIT", f"{inst.sym}: TARGET LIMIT {lot.qty}u @ {px:g} RESTING at broker for "
                                f"rung #{lot.seq} (order {oid})", sym=inst.sym,
                        data={"lot_id": lot_id, "target": px, "order_id": oid})

    async def _resolve_target_order(self, lot_id: int) -> None:
        """Check an OPEN lot's resting target against the broker book and settle it."""
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if (lot is None or lot.status != "OPEN" or lot.exit_pending
                    or not (lot.target_order_id or "")):
                return                  # exit_pending → an exit path owns this lot now
            oid, inst_id = lot.target_order_id, lot.instrument_id
        try:
            row = await self.rest.find_order(oid)
        except Exception:
            return
        if row is None:
            # absent from a good book: could be book lag — NEVER act on one
            # sighting (acting wrongly re-places a second live SELL = double-sell)
            if not self._order_gone_strike(oid):
                return
            self._order_seen(oid)
            status, filled, avg = "CANCELLED", 0, 0.0
        else:
            self._order_seen(oid)
            status = (row.get("status") or "").upper()
            if status not in TERMINAL:
                return
            filled, avg = _fillshares(row), _avgprc(row)

        # COMPLETE means the broker FILLED it. If the book row carries no fill
        # quantity, that is missing data about a completed order — never evidence
        # that nothing happened. Reading it as "unfilled" sends the lot down the
        # re-rest path below, which places ANOTHER sell against a position that
        # is already gone; once per reconcile pass, that is how one long rung
        # became a growing real short. Assume the fill the status asserts.
        complete_no_qty = (status == "COMPLETE" and filled <= 0)

        closed = False
        async with self.lock_for(inst_id):
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                if (lot is None or lot.status != "OPEN" or lot.target_order_id != oid
                        or lot.exit_pending):
                    return
                inst = db.get(Instrument, inst_id)
                sym, seq = (inst.sym if inst else ""), lot.seq
                if complete_no_qty:
                    filled = lot.qty
                    event_log.warn("EXIT", f"rung #{seq} {sym}: resting target {oid} is COMPLETE at the "
                                           f"broker but the order book reported no fill quantity — booking "
                                           f"it as fully filled at the target price. VERIFY the fill at the "
                                           f"broker; the alternative is re-selling a position we no longer "
                                           f"hold.", sym=sym)
                if filled > 0 and avg <= 0:
                    # terminal fill with missing avgprc: a sell LIMIT fills at or
                    # above its limit — book at the target price, never treat a
                    # real fill as "cancelled" (that would re-place & double-sell)
                    avg = lot.target_price
                if filled >= lot.qty and avg > 0:
                    pnl = (avg - lot.entry_price) * lot.qty
                    lot.status = "CLOSED"
                    lot.exit_reason = "TARGET"
                    lot.exit_price = avg
                    lot.exit_time = now_ist()
                    lot.exit_order_id = oid
                    lot.realized_pnl = (lot.realized_pnl or 0.0) + pnl
                    lot.exit_pending = False
                    lot.target_order_id = ""
                    db.commit()
                    closed = True
                    event_log.trade("EXIT", f"Rung #{seq} {sym} CLOSED (TARGET, resting limit) @ {avg:.2f} "
                                            f"| P&L ₹{lot.realized_pnl:,.2f}", sym=sym,
                                    data={"lot_id": lot_id, "exit": avg,
                                          "pnl": round(lot.realized_pnl, 2)})
                elif filled > 0 and avg > 0:
                    pnl = (avg - lot.entry_price) * filled
                    lot.realized_pnl = (lot.realized_pnl or 0.0) + pnl
                    lot.qty -= filled
                    lot.lots = max(lot.qty // max(lot.lot_size, 1), 1)
                    lot.target_order_id = ""
                    lot.notes = (lot.notes or "") + f" partial target fill {filled}u@{avg:.2f};"
                    db.commit()
                    event_log.warn("EXIT", f"Rung #{seq} {sym} PARTIAL target fill {filled}u @ {avg:.2f} "
                                           f"(₹{pnl:,.2f} booked) — target re-rests for the remainder",
                                   sym=sym)
                else:
                    lot.target_order_id = ""
                    db.commit()
                    event_log.warn("EXIT", f"Rung #{seq} {sym}: resting target order {status} at broker — "
                                           "re-resting it (engine tick-exit covers the gap)", sym=sym)
        if closed:
            with db_session() as db:
                inst = db.get(Instrument, inst_id)
            if inst is not None:
                await self._settle_ladder_levels(inst)
        await self.sync_resting_orders(inst_id, force=True)

    async def cancel_resting_entries(self, inst_id: int | None, reason: str) -> int:
        """Cancel resting ENTRY orders (all instruments when inst_id is None).
        Levels go back to PENDING (a system hold, not a user skip). Resting
        TARGETS are never touched here — open rungs stay protected."""
        with db_session() as db:
            q = db.query(Lot).filter(Lot.status == "RESTING")
            if inst_id is not None:
                q = q.filter(Lot.instrument_id == inst_id)
            ids = [(l.id, l.instrument_id) for l in q.all()]
        n = 0
        for lot_id, iid in ids:
            async with self.lock_for(iid):
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    oid = lot.entry_order_id if (lot is not None and lot.status == "RESTING") else ""
                if not oid:
                    continue
                dead, _status, filled, avg = await self._cancel_confirmed(oid)
                if filled > 0:          # raced a real fill — it's a rung now
                    await self._apply_entry_fill_locked(lot_id, filled, avg)
                    continue
                if not dead:
                    # cancel NOT confirmed — the BUY may still be live at the
                    # broker. Leave the lot RESTING (tracked!) and retry on the
                    # next sync/reconcile pass instead of forgetting a live order.
                    # NOTE: event_log.warn() takes no `level` — it IS the warn
                    # level. Passing one raised TypeError out of this loop, which
                    # is reached exactly when a cancel is unconfirmed: Flatten,
                    # Stop Strategy, pause and the CB trip all failed with a 500
                    # mid-operation, and the CB's own fire-and-forget cancel died
                    # silently, leaving pre-authorised buys live at the broker.
                    event_log.warn("LADDER", f"could not confirm cancel of resting entry {oid} "
                                             f"({reason}) — keeping it tracked, will retry")
                    continue
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    lvl = db.query(LadderLevel).filter(LadderLevel.lot_id == lot_id).first()
                    lot.status = "CANCELLED"
                    lot.notes = (lot.notes or "") + f" resting entry cancelled: {reason};"
                    if lot.signal_id:
                        s = db.get(Signal, lot.signal_id)
                        if s:
                            s.status = "skipped"
                            s.reason = f"resting order cancelled: {reason}"
                    if lvl is not None:
                        lvl.status = "PENDING"
                        lvl.lot_id = None
                    db.commit()
                n += 1
        if n:
            event_log.write("LADDER", f"cancelled {n} resting entry order(s): {reason}", level="warn")
        return n

    async def skip_pending_lot(self, lot_id: int) -> dict:
        """User pressed Cancel on a pending order: cancel at broker, mark the
        level SKIPPED, activate the next level."""
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None:
                return {"ok": False, "reason": "lot not found"}
            if lot.status not in ("RESTING", "UNCONFIRMED"):
                return {"ok": False, "reason": f"lot is {lot.status}, not a pending order"}
            inst_id = lot.instrument_id
        nxt_price = None
        async with self.lock_for(inst_id):
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                if lot is None or lot.status not in ("RESTING", "UNCONFIRMED"):
                    return {"ok": False, "reason": "order state changed — refresh and retry"}
                oid = lot.entry_order_id
            dead, _status, filled, avg = await self._cancel_confirmed(oid)
            if filled > 0:
                await self._apply_entry_fill_locked(lot_id, filled, avg)
                return {"ok": False,
                        "reason": "the order FILLED at the broker just now — it is a live rung, not cancellable"}
            if not dead:
                return {"ok": False,
                        "reason": "the broker did not confirm the cancel — the order may still be live; "
                                  "try again in a minute"}
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                inst = db.get(Instrument, inst_id)
                lvl = db.query(LadderLevel).filter(LadderLevel.lot_id == lot_id).first()
                lot.status = "CANCELLED"
                lot.notes = (lot.notes or "") + " level skipped by user;"
                if lot.signal_id:
                    s = db.get(Signal, lot.signal_id)
                    if s:
                        s.status = "skipped"
                        s.reason = "level skipped by user"
                lno = "?"
                if lvl is not None:
                    lvl.status = "SKIPPED"
                    lvl.lot_id = None
                    lvl.note = "skipped by user"
                    lno = lvl.level_no
                    nxt = (db.query(LadderLevel)
                           .filter(LadderLevel.instrument_id == inst_id,
                                   LadderLevel.status == "PENDING",
                                   LadderLevel.price < lvl.price)
                           .order_by(LadderLevel.price.desc()).first())
                    nxt_price = nxt.price if nxt is not None else None
                db.commit()
                sym = inst.sym if inst else ""
        event_log.write("LADDER", f"{sym} B{lno} SKIPPED by user — resting order cancelled; "
                                  f"next level {f'@ {nxt_price:g}' if nxt_price else '(none below)'} activates",
                        level="warn", sym=sym)
        await self.sync_resting_orders(inst_id, force=True)
        return {"ok": True, "next_level_price": nxt_price}

    async def cancel_and_pause_pending(self, lot_id: int) -> dict:
        """User chose "Cancel & stop buying" on a pending order: pause (un-arm)
        the ladder and pull EVERY resting buy from the broker — no lower level
        activates. Open rungs keep their targets and stop-losses (pausing never
        drops position protection). Re-arm to resume buying."""
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None:
                return {"ok": False, "reason": "order not found"}
            if lot.status not in ("RESTING", "UNCONFIRMED"):
                return {"ok": False, "reason": f"order is {lot.status}, not a pending order"}
            inst_id = lot.instrument_id
        # 1) un-arm FIRST so the desired-state sync cannot re-place anything
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
            inst.ladder_armed = False
            db.commit()
            sym = inst.sym if inst else ""
        # 2) cancel THIS order (confirmed, race-safe). A race-fill becomes a rung;
        #    the ladder still stays paused.
        async with self.lock_for(inst_id):
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                oid = lot.entry_order_id if (lot is not None
                                             and lot.status in ("RESTING", "UNCONFIRMED")) else ""
            if oid:
                dead, _s, filled, avg = await self._cancel_confirmed(oid)
                if filled > 0:
                    await self._apply_entry_fill_locked(lot_id, filled, avg)
                    event_log.write("LADDER", f"{sym}: the pending buy FILLED as it was being cancelled — "
                                              "it is now a live rung; the ladder is PAUSED", level="warn", sym=sym)
                    return {"ok": False, "filled": True,
                            "reason": "the order FILLED at the broker just now — it is a live rung. "
                                      "The ladder is paused; manage the rung from Open trades."}
                if not dead:
                    return {"ok": False,
                            "reason": "the broker did not confirm the cancel — the order may still be live. "
                                      "The ladder is paused; try Cancel again in a minute."}
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    lvl = db.query(LadderLevel).filter(LadderLevel.lot_id == lot_id).first()
                    if lot is not None and lot.status in ("RESTING", "UNCONFIRMED"):
                        lot.status = "CANCELLED"
                        lot.notes = (lot.notes or "") + " cancelled by user — ladder paused;"
                        if lot.signal_id:
                            s = db.get(Signal, lot.signal_id)
                            if s:
                                s.status = "skipped"
                                s.reason = "cancelled by user — ladder paused"
                    if lvl is not None and lvl.status == "PLACED":
                        lvl.status = "PENDING"
                        lvl.lot_id = None
                    db.commit()
        # 3) pull any OTHER resting entries (we are stopping ALL buying)
        await self.cancel_resting_entries(inst_id, "buying paused by user")
        event_log.write("LADDER", f"{sym}: pending buy CANCELLED and ladder PAUSED by user — no lower buy "
                                  "will be placed. Open rungs keep their targets/stops. Re-arm to resume.",
                        level="warn", sym=sym)
        return {"ok": True, "paused": True}

    async def reestablish_level(self, level_id: int) -> dict:
        """User re-establishes a SKIPPED level. Price above the level → place the
        resting order now; price at/below → wait for recovery, then place."""
        with db_session() as db:
            lvl = db.get(LadderLevel, level_id)
            if lvl is None:
                return {"ok": False, "reason": "level not found"}
            if lvl.status != "SKIPPED":
                return {"ok": False, "reason": f"level is {lvl.status} — only SKIPPED levels can be re-established"}
            inst = db.get(Instrument, lvl.instrument_id)
            if inst is None:
                return {"ok": False, "reason": "instrument not found"}
            inst_id, price = inst.id, lvl.price
            key = f"{inst.exch}|{inst.token}"
            sym, lno = inst.sym, lvl.level_no
        with db_session() as db:
            row = db.get(LadderLevel, level_id)
            row.status = "AWAIT_RECOVERY"
            row.note = "re-established by user"
            db.commit()
        ps = self.prices.get(key)
        lp = ps.lp if ps and ps.lp > 0 else 0.0
        if lp > price:
            # price is above the level → the level becomes a NORMAL pending level
            # again. The desired-state sync enforces the single-active-order rule:
            # if this level outranks the currently placed lower one, the lower
            # order is cancelled and this one is placed (the swap).
            with db_session() as db:
                row = db.get(LadderLevel, level_id)
                row.status = "PENDING"
                row.note = "re-established by user"
                db.commit()
            await self.sync_resting_orders(inst_id, force=True)
            with db_session() as db:
                row = db.get(LadderLevel, level_id)
                placed = row is not None and row.status == "PLACED"
            event_log.write("LADDER", f"{sym} B{lno} re-established by user — normal pending level again"
                                      f"{' (BUY LIMIT resting at broker)' if placed else ''}", sym=sym)
            return {"ok": True, "placed": placed, "await_recovery": False}
        event_log.write("LADDER", f"{sym} B{lno} re-established — price is at/below the level; it becomes "
                                  f"an active buy once price trades back above {price:g}", sym=sym)
        return {"ok": True, "placed": False, "await_recovery": True}

    async def refresh_target_order(self, lot_id: int) -> None:
        """Target price edited on an OPEN rung with a resting target: cancel the
        old order and rest a new one at the new price (race-fill safe)."""
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None or lot.status != "OPEN":
                return
            inst_id, oid = lot.instrument_id, (lot.target_order_id or "")
        async with self.lock_for(inst_id):
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                # re-validate under the lock: a resolver/exit may have settled the
                # lot (or replaced its target) while we waited
                if lot is None or lot.status != "OPEN" or (lot.target_order_id or "") != oid:
                    return
            if oid:
                dead, _status, filled, avg = await self._cancel_confirmed(oid)
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    if lot is None or lot.status != "OPEN" or (lot.target_order_id or "") != oid:
                        return
                    if filled > 0 and avg <= 0:
                        avg = lot.target_price
                    if not dead and filled < lot.qty:
                        db.commit()
                        event_log.warn("EXIT", f"rung #{lot.seq}: could NOT confirm cancel of the old target "
                                               "order — edit deferred (no duplicate sell); try again")
                        return
                    if filled >= lot.qty and avg > 0:
                        pnl = (avg - lot.entry_price) * lot.qty
                        lot.status = "CLOSED"
                        lot.exit_reason = "TARGET"
                        lot.exit_price = avg
                        lot.exit_time = now_ist()
                        lot.exit_order_id = oid
                        lot.realized_pnl = (lot.realized_pnl or 0.0) + pnl
                        lot.exit_pending = False
                        lot.target_order_id = ""
                        db.commit()
                        event_log.trade("EXIT", f"rung #{lot.seq} closed at the PREVIOUS target while it "
                                                f"was being edited @ {avg:.2f} | P&L ₹{lot.realized_pnl:,.2f}")
                        return
                    lot.target_order_id = ""
                    if 0 < filled < lot.qty and avg > 0:
                        lot.realized_pnl = (lot.realized_pnl or 0.0) + (avg - lot.entry_price) * filled
                        lot.qty -= filled
                        lot.lots = max(lot.qty // max(lot.lot_size, 1), 1)
                        lot.notes = (lot.notes or "") + f" partial target fill {filled}u@{avg:.2f} (edit race);"
                    db.commit()
            await self._place_target_order_locked(lot_id)

    async def move_placed_level(self, level_id: int, new_price: float) -> dict:
        """Edit the price of a PLACED level: cancel its resting order, update the
        level, re-place at the new price."""
        with db_session() as db:
            lvl = db.get(LadderLevel, level_id)
            if lvl is None or lvl.status != "PLACED":
                return {"ok": False, "reason": "level is not PLACED"}
            inst_id, lot_id = lvl.instrument_id, lvl.lot_id
        async with self.lock_for(inst_id):
            with db_session() as db:
                lvl = db.get(LadderLevel, level_id)
                if lvl is None or lvl.status != "PLACED":
                    return {"ok": False, "reason": "level state changed (filled?) while editing — refresh"}
            if lot_id:
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    oid = lot.entry_order_id if (lot is not None and lot.status == "RESTING") else ""
                if oid:
                    dead, _status, filled, avg = await self._cancel_confirmed(oid)
                    if filled > 0:
                        await self._apply_entry_fill_locked(lot_id, filled, avg)
                        return {"ok": False, "reason": "order filled while editing — the level is now a live rung"}
                    if not dead:
                        return {"ok": False, "reason": "the broker did not confirm the cancel — try again in a minute"}
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    if lot is not None and lot.status == "RESTING":
                        lot.status = "CANCELLED"
                        lot.notes = (lot.notes or "") + " level price edited — order re-placed;"
                        db.commit()
            with db_session() as db:
                lvl = db.get(LadderLevel, level_id)
                lvl.price = round(new_price, 4)
                lvl.status = "PENDING"
                lvl.lot_id = None
                lvl.source = "manual"
                lvl.note = "user-edited"
                db.commit()
        await self.sync_resting_orders(inst_id, force=True)
        return {"ok": True}

    async def delete_placed_level(self, level_id: int) -> dict:
        """Delete a PLACED level: cancel its resting order first."""
        with db_session() as db:
            lvl = db.get(LadderLevel, level_id)
            if lvl is None or lvl.status != "PLACED":
                return {"ok": False, "reason": "level is not PLACED"}
            inst_id, lot_id = lvl.instrument_id, lvl.lot_id
        async with self.lock_for(inst_id):
            with db_session() as db:
                lvl = db.get(LadderLevel, level_id)
                if lvl is None or lvl.status != "PLACED":
                    return {"ok": False, "reason": "level state changed (filled?) while deleting — refresh"}
            if lot_id:
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    oid = lot.entry_order_id if (lot is not None and lot.status == "RESTING") else ""
                if oid:
                    dead, _status, filled, avg = await self._cancel_confirmed(oid)
                    if filled > 0:
                        await self._apply_entry_fill_locked(lot_id, filled, avg)
                        return {"ok": False, "reason": "order filled while deleting — the level is now a live rung"}
                    if not dead:
                        return {"ok": False, "reason": "the broker did not confirm the cancel — try again in a minute"}
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    if lot is not None and lot.status == "RESTING":
                        lot.status = "CANCELLED"
                        lot.notes = (lot.notes or "") + " level deleted by user;"
                        db.commit()
            with db_session() as db:
                lvl = db.get(LadderLevel, level_id)
                if lvl is not None:
                    db.delete(lvl)
                    db.commit()
        await self.sync_resting_orders(inst_id, force=True)
        return {"ok": True}

    # levels safe to wipe: they hold no confirmed position. PLACED has a resting
    # BUY at the broker that must be cancelled (confirmed) first; the other three
    # have no order behind them. FILLED/TRIGGERED (open rungs) and CANCELLED
    # (history) are deliberately excluded — clearing must never orphan a live
    # position or rewrite the record.
    _CLEARABLE_LEVEL_STATUSES = ("PENDING", "SKIPPED", "AWAIT_RECOVERY", "PLACED")

    async def clear_ladder_levels(self, inst_id: int) -> dict:
        """Remove EVERY clearable level for a ladder in one action, under the
        instrument lock. PENDING/SKIPPED/AWAIT_RECOVERY are deleted outright; a
        PLACED level's resting BUY is cancelled (confirmed) at the broker before
        its row is removed. Open rungs (FILLED/TRIGGERED) and history (CANCELLED)
        are left untouched. A PLACED order that races a fill becomes a live rung
        and is kept; one whose cancel the broker won't confirm is left in place
        (never delete a level while its buy may still be live)."""
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
            if inst is None or inst.mode != "ladder":
                return {"ok": False, "reason": "ladder instrument not found"}
            sym = inst.sym
        cleared = filled = kept = 0
        async with self.lock_for(inst_id):
            with db_session() as db:
                targets = [(l.id, l.status, l.lot_id) for l in db.query(LadderLevel)
                           .filter(LadderLevel.instrument_id == inst_id,
                                   LadderLevel.status.in_(self._CLEARABLE_LEVEL_STATUSES)).all()]
            for level_id, status, lot_id in targets:
                # PLACED → cancel-confirm its resting entry before deleting the row
                if status == "PLACED" and lot_id:
                    with db_session() as db:
                        lot = db.get(Lot, lot_id)
                        oid = lot.entry_order_id if (lot is not None and lot.status == "RESTING") else ""
                    if oid:
                        dead, _s, fl, avg = await self._cancel_confirmed(oid)
                        if fl > 0:
                            await self._apply_entry_fill_locked(lot_id, fl, avg)
                            filled += 1
                            continue        # it filled while clearing — now a live rung, keep it
                        if not dead:
                            kept += 1
                            continue        # cancel unconfirmed — the buy may be live; leave the level
                        with db_session() as db:
                            lot = db.get(Lot, lot_id)
                            if lot is not None and lot.status == "RESTING":
                                lot.status = "CANCELLED"
                                lot.notes = (lot.notes or "") + " levels cleared by user;"
                                if lot.signal_id:
                                    s = db.get(Signal, lot.signal_id)
                                    if s:
                                        s.status = "skipped"
                                        s.reason = "levels cleared by user"
                                db.commit()
                # delete the row — re-check under the lock: a PENDING level may have
                # raced into TRIGGERED (a real buy) since the snapshot; if so, keep it.
                with db_session() as db:
                    lvl = db.get(LadderLevel, level_id)
                    if lvl is not None and lvl.status in self._CLEARABLE_LEVEL_STATUSES:
                        db.delete(lvl)
                        db.commit()
                        cleared += 1
                    else:
                        kept += 1
        event_log.write("LADDER", f"{sym} ladder CLEARED: {cleared} level(s) removed"
                        + (f", {filled} filled mid-clear (kept as open rung)" if filled else "")
                        + (f", {kept} kept (fired or cancel unconfirmed — retry)" if kept else ""),
                        level="warn", sym=sym)
        await self.sync_resting_orders(inst_id, force=True)
        return {"ok": True, "cleared": cleared, "filled": filled, "kept": kept}

    async def detach_ladder_level(self, level_id: int) -> dict:
        """Remove an EXECUTED or retired level from the ladder WITHOUT touching
        its position. FILLED/TRIGGERED: the open rung stays OPEN and keeps its own
        target/stop (managed from Trades) — only the ladder-level row is dropped,
        so it no longer recycles/re-arms. CANCELLED: pure history cleanup. Runs
        under the instrument lock. There is no cascade from level → lot, so the
        Lot is untouched by the delete."""
        with db_session() as db:
            lvl = db.get(LadderLevel, level_id)
            if lvl is None:
                return {"ok": True, "removed": False}      # already gone
            inst_id, status, lot_id = lvl.instrument_id, lvl.status, lvl.lot_id
        if status not in ("FILLED", "TRIGGERED", "CANCELLED"):
            return {"ok": False,
                    "reason": f"level is {status} — remove it the normal way, not as an executed level"}
        async with self.lock_for(inst_id):
            with db_session() as db:
                lvl = db.get(LadderLevel, level_id)
                if lvl is None:
                    return {"ok": True, "removed": False}
                # re-read under the lock — a settle pass may have recycled/retired it
                status = lvl.status
                if status not in ("FILLED", "TRIGGERED", "CANCELLED"):
                    return {"ok": False,
                            "reason": f"level changed to {status} while removing — refresh and retry"}
                inst = db.get(Instrument, inst_id)
                sym, no, price = inst.sym, lvl.level_no, lvl.price
                open_rung = False
                if lot_id:
                    lot = db.get(Lot, lot_id)
                    open_rung = lot is not None and lot.status in (
                        "OPEN", "PENDING", "UNCONFIRMED", "TEMP_EXITED")
                db.delete(lvl)      # drop ONLY the level row; the lot/position stays
                db.commit()
        event_log.write("LADDER", f"{sym} level B{no} @ {price:g} removed from the ladder"
                        + (" — its OPEN position stays open and is managed from Trades"
                           if open_rung else " (retired history)"),
                        level="info", sym=sym)
        return {"ok": True, "removed": True, "open_rung": open_rung}

    async def ladder_preview(self, inst_id: int, basis: str, anchor_price: float,
                             interval_points: float, num_levels: int,
                             sr_lookback_days: int) -> dict:
        """Compute (without saving) the initial ladder levels for the UI."""
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
        if inst is None:
            raise ValueError("instrument not found")
        key = f"{inst.exch}|{inst.token}"
        ps = self.prices.get(key)
        ltp = ps.lp if ps and ps.lp > 0 else 0.0
        if ltp <= 0:
            try:
                q = await self.rest.get_quote(inst.exch, inst.token)
                ltp = float(q.get("lp") or 0)
            except Exception:
                ltp = 0.0
        n = max(min(int(num_levels or 5), 20), 1)

        levels: list[dict] = []
        if basis == "support":
            ref = anchor_price if anchor_price > 0 else ltp
            if ref <= 0:
                raise ValueError("no reference price — market quote unavailable; enter an anchor price")
            lookback_min = max(int(sr_lookback_days or 45), 7) * 24 * 60
            candles = await self.rest.get_candles(inst.exch, inst.token, 240, lookback_min)
            zones = supports_below(candles, ref, n)
            for z in zones:
                levels.append({"price": z.price, "note": f"4h support, {z.touches} touch(es)"})
            if not zones:
                raise ValueError(f"no 4h swing supports found below {ref:.2f} in the last "
                                 f"{sr_lookback_days or 45} days — widen the lookback or use fixed intervals")
        else:
            anchor = anchor_price if anchor_price > 0 else ltp
            if anchor <= 0:
                raise ValueError("enter an anchor price (market quote unavailable)")
            if interval_points <= 0:
                raise ValueError("interval must be > 0")
            for p in fixed_levels(anchor, interval_points, n):
                levels.append({"price": p, "note": f"anchor {anchor:g} − k×{interval_points:g}"})

        # GRID: each level's target is SELF-ANCHORED and FIXED at setup —
        # target = this level's OWN price + the configured offset. It never chains
        # to another rung, and it is frozen here so it does NOT move when the buy
        # price later shifts down on a cheaper fill; a cheaper fill therefore just
        # widens the profit on that rung. This is the number confirm freezes into
        # each level's target_override.
        for lv in levels:
            tgt, _ = compute_target(inst, lv["price"], None)
            lv["target"] = tgt
        return {"ltp": ltp, "basis": basis, "levels": levels}

    # ================= exits =================

    async def close_lot(self, lot_id: int, reason: str, trigger_price: float | None = None) -> bool:
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None:
                return False
            inst_id = lot.instrument_id
        async with self.lock_for(inst_id):
            ok = await self._close_lot_inner(lot_id, reason, trigger_price)
        # Feeds the retry spacing: a run of failures backs the next attempt off
        # and eventually says so, instead of re-sending the same doomed order
        # every five seconds for the rest of the session.
        self._note_exit_result(lot_id, ok)
        return ok

    async def _close_lot_inner(self, lot_id: int, reason: str,
                               trigger_price: float | None = None) -> bool:
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None:
                return False
            # a TEMP_EXITED lot is ALREADY flat at the broker — permanently closing
            # it just books the carried loss (no sell) and marks it CLOSED.
            if lot.status == "TEMP_EXITED" and lot.exit_pending:
                return False               # re-enter buy in flight — refuse until it settles
            if lot.status == "TEMP_EXITED":
                inst = db.get(Instrument, lot.instrument_id)
                booked = (lot.temp_exit_price - lot.entry_price) * lot.qty   # = −carry×qty
                lot.status = "CLOSED"
                lot.exit_reason = reason
                lot.exit_price = lot.temp_exit_price
                lot.exit_time = now_ist()
                lot.realized_pnl = (lot.realized_pnl or 0.0) + booked
                lot.temp_exit_trigger_price = 0.0
                lot.reenter_trigger_price = 0.0
                db.commit()
                event_log.trade("EXIT", f"Paused rung #{lot.seq} {inst.sym} CLOSED without re-entry ({reason}) — "
                                        f"booked carried P&L ₹{booked:,.2f}", sym=inst.sym)
                return True
            if lot.status != "OPEN" or lot.exit_pending:
                return False
            inst = db.get(Instrument, lot.instrument_id)
            lot.exit_pending = True
            lot.exit_reason = reason
            db.commit()
            sym, seq, qty, tsym, exch = inst.sym, lot.seq, lot.qty, lot.contract_tsym, lot.exch
            entry = lot.entry_price
            prod = lot.product_type or settings.product_type
            sell_ot, tick, tgt_price = inst.sell_order_type, inst.tick_size, lot.target_price
            # marketable buffer removed — exits sell as a plain limit AT the bid
            token, mkt_ticks = lot.contract_token, 0
            tgt_oid = lot.target_order_id or ""

        # a broker-resting TARGET order would double-sell against this exit —
        # cancel it first and CONFIRM the cancel; if it raced a fill, book THAT
        # close instead of selling. An UNCONFIRMED cancel aborts the exit: selling
        # while the target may still be live is how naked shorts are born.
        if tgt_oid:
            dead, _st, filled_t, avg_t = await self._cancel_confirmed(tgt_oid)
            if filled_t > 0 and avg_t <= 0:
                avg_t = tgt_price if tgt_price > 0 else 0.0   # sell LMT fills at ≥ its limit
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                if lot is None or lot.status != "OPEN":
                    return bool(lot is not None and lot.status == "CLOSED")   # resolver settled it meanwhile
                if filled_t >= lot.qty and avg_t > 0:
                    pnl = (avg_t - lot.entry_price) * lot.qty
                    lot.status = "CLOSED"
                    lot.exit_reason = "TARGET"
                    lot.exit_price = avg_t
                    lot.exit_time = now_ist()
                    lot.exit_order_id = tgt_oid
                    lot.target_order_id = ""
                    lot.realized_pnl = (lot.realized_pnl or 0.0) + pnl
                    lot.exit_pending = False
                    db.commit()
                    event_log.trade("EXIT", f"Rung #{seq} {sym} closed at its resting TARGET @ {avg_t:.2f} "
                                            f"just before the {reason} exit | P&L ₹{lot.realized_pnl:,.2f}",
                                    sym=sym, data={"lot_id": lot_id, "exit": avg_t})
                    return True
                if 0 < filled_t < lot.qty and avg_t > 0:
                    lot.realized_pnl = (lot.realized_pnl or 0.0) + (avg_t - lot.entry_price) * filled_t
                    lot.qty -= filled_t
                    lot.lots = max(lot.qty // max(lot.lot_size, 1), 1)
                    lot.notes = (lot.notes or "") + f" partial resting-target fill {filled_t}u@{avg_t:.2f};"
                if not dead:
                    lot.exit_pending = False
                    db.commit()
                    event_log.warn("EXIT", f"Rung #{seq} {sym}: could NOT confirm cancel of the resting "
                                           f"target — {reason} exit ABORTED (no double-sell); will retry",
                                   sym=sym)
                    return False
                lot.target_order_id = ""
                qty = lot.qty
                db.commit()

        # order type on exit:
        #  • TARGET   → LIMIT at the target price (no negative slippage).
        #  • AMI_SELL → MARKETABLE limit at bid − N ticks: fills immediately like a
        #    market order but with a price FLOOR (no blind slippage). Falls back to
        #    MKT if the book has no bid or the instrument's sell type is MKT.
        #  • STOP-LOSS / MANUAL / CB / flatten / roll → MARKET, so a forced exit
        #    ALWAYS fills — a resting limit that never fills would leave it naked.
        if reason == "TARGET":
            ex_pt, ex_px = self._order_params(sell_ot, tgt_price, tick)
        elif reason == "AMI_SELL":
            ps_now = self.prices.get(f"{exch}|{token}")
            cur_bid = ps_now.bid if (ps_now and ps_now.bid > 0) else 0.0
            ex_pt, ex_px = self._marketable_order("S", sell_ot, cur_bid, mkt_ticks, tick)
        else:
            ex_pt, ex_px = "MKT", 0.0
        trig = f" @ trigger {trigger_price:.2f}" if trigger_price else ""
        event_log.trade("EXIT", f"{reason}: selling rung #{seq} ({qty} units {tsym}){trig} — "
                                f"entry {entry:.2f} [{ex_pt}]", sym=sym)
        report = await self.om.execute(side="S", exchange=exch, tradingsymbol=tsym,
                                       qty_units=qty, remarks=f"exit_grid_l{lot_id}",
                                       price_type=ex_pt, price=ex_px, product_type=prod)

        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if report.ok:
                filled, avg = report.filled_qty, report.avg_price
                # A broker that reports a fill with NO average price is missing
                # data, not selling at zero. Multiplying that zero out books the
                # rung's whole notional as a loss — a 75u NIFTY rung lands
                # -16.5 lakh in the ledger, which then feeds the circuit breaker
                # and pulls resting buys across every instrument. Five sibling
                # paths already refuse to price a fill this way; this one did
                # not. Fall back to the price we actually aimed at and say so.
                if filled > 0 and avg <= 0:
                    avg = tgt_price if (reason == "TARGET" and tgt_price > 0) else lot.entry_price
                    event_log.error("EXIT", f"rung #{lot.seq} {sym}: broker reported {filled}u filled with NO "
                                            f"average price. Booking the exit at {avg:g} rather than at 0, "
                                            f"which would have recorded a phantom loss of the full notional. "
                                            f"VERIFY the real fill at the broker.", sym=sym)
                pnl_part = (avg - lot.entry_price) * filled
                if filled >= lot.qty:
                    lot.status = "CLOSED"
                    lot.exit_price = avg
                    lot.exit_time = now_ist()
                    lot.exit_order_id = report.order_id
                    lot.realized_pnl = (lot.realized_pnl or 0.0) + pnl_part
                    lot.exit_pending = False
                    db.commit()
                    event_log.trade("EXIT", f"Rung #{lot.seq} {sym} CLOSED ({reason}) @ {avg:.2f} | "
                                            f"P&L ₹{lot.realized_pnl:,.2f}", sym=sym,
                                    data={"lot_id": lot_id, "exit": avg, "pnl": round(lot.realized_pnl, 2)})
                    return True
                # partial exit — shrink the lot, keep the remainder armed
                lot.qty -= filled
                lot.lots = max(lot.qty // max(lot.lot_size, 1), 1)
                lot.realized_pnl = (lot.realized_pnl or 0.0) + pnl_part
                lot.exit_pending = False
                lot.notes = (lot.notes or "") + f" partial exit {filled}u@{avg:.2f};"
                db.commit()
                event_log.warn("EXIT", f"Rung #{lot.seq} {sym} PARTIALLY exited {filled}u @ {avg:.2f} "
                                       f"(₹{pnl_part:,.2f} booked). {lot.qty}u remain armed — will retry.",
                               sym=sym)
                return False
            if report.status == "TIMEOUT":
                lot.exit_order_id = report.order_id
                db.commit()   # exit_pending stays True while we deal with the resting order
            else:
                lot.exit_pending = False
                db.commit()
                event_log.error("EXIT", f"Exit for rung #{lot.seq} {sym} {report.status}: "
                                        f"{report.error or report.raw_status} — will retry on next trigger",
                                sym=sym)
                return False

        # TIMEOUT: an unfilled LIMIT may be RESTING at the broker. While exit_pending
        # freezes the lot, its STOP-LOSS is disabled too — so we cancel the resting
        # order NOW instead of leaving it. Then: book a race-fill, re-arm on a clean
        # cancel, or (still unknown) leave exit_pending for the reconciler.
        status, filled, avg = await self._cancel_unfilled_order(report.order_id)
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if filled > 0 and avg > 0:
                pnl_part = (avg - lot.entry_price) * filled
                if filled >= lot.qty:      # filled before the cancel landed — book the close
                    lot.status = "CLOSED"
                    lot.exit_price = avg
                    lot.exit_time = now_ist()
                    lot.realized_pnl = (lot.realized_pnl or 0.0) + pnl_part
                    lot.exit_pending = False
                    db.commit()
                    event_log.trade("EXIT", f"Rung #{lot.seq} {sym} CLOSED ({reason}) @ {avg:.2f} on late fill | "
                                            f"P&L ₹{lot.realized_pnl:,.2f}", sym=sym,
                                    data={"lot_id": lot_id, "exit": avg, "pnl": round(lot.realized_pnl, 2)})
                    return True
                lot.qty -= filled          # partial fill then cancelled — shrink, book, re-arm rest
                lot.lots = max(lot.qty // max(lot.lot_size, 1), 1)
                lot.realized_pnl = (lot.realized_pnl or 0.0) + pnl_part
                lot.exit_pending = False
                lot.notes = (lot.notes or "") + f" partial exit {filled}u@{avg:.2f} (timeout-cancel);"
                db.commit()
                event_log.warn("EXIT", f"Rung #{lot.seq} {sym} PARTIALLY exited {filled}u @ {avg:.2f} before "
                                       f"cancel. {lot.qty}u remain armed.", sym=sym)
                return False
            if status in ("REJECTED", "CANCELED", "CANCELLED", "INVALID"):
                lot.exit_pending = False   # resting order is gone — target/SL re-armed
                lot.notes = (lot.notes or "") + " unfilled exit cancelled on timeout — re-armed;"
                db.commit()
                event_log.warn("EXIT", f"Unfilled exit for rung #{lot.seq} {sym} cancelled — target/SL "
                                       "re-armed for the next trigger", sym=sym)
                return False
            lot.notes = (lot.notes or "") + " exit verification TIMEOUT — reconciler owns it;"
            db.commit()   # exit_pending stays True so the tick loop can't double-sell
            event_log.warn("EXIT", f"Exit order {report.order_id} for rung #{lot.seq} UNVERIFIED "
                                   "(timeout, cancel unconfirmed). No re-fire until the reconciler "
                                   "resolves it.", sym=sym)
            return False

    async def flatten_instrument(self, instrument_id: int, reason: str = "MANUAL") -> int:
        # resting entry orders would BUY BACK right after the flatten — kill them first
        await self.cancel_resting_entries(instrument_id, f"flatten ({reason})")
        # ...and so would an armed re-entry on a PAUSED rung, which this used to
        # miss entirely: the query below only ever selected OPEN, so a rung in
        # temp-exit kept its trigger and bought the moment price touched it —
        # after the operator had pressed Flatten. Disarm the trigger; the rung
        # itself stays paused and flat, which is the safe resting state.
        with db_session() as db:
            armed = (db.query(Lot)
                     .filter(Lot.instrument_id == instrument_id,
                             Lot.status == "TEMP_EXITED",
                             Lot.reenter_trigger_price > 0).all())
            for l in armed:
                l.reenter_trigger_price = 0.0
                l.notes = (l.notes or "") + f" re-entry disarmed by flatten ({reason});"
            if armed:
                db.commit()
        for l in armed:
            event_log.warn("ORDER", f"flatten ({reason}): disarmed the armed re-entry on paused rung "
                                    f"#{l.seq} — it would have bought back after the flatten", sym="")
        with db_session() as db:
            lots = db.query(Lot).filter_by(instrument_id=instrument_id, status="OPEN").all()
        n = 0
        for lot in lots:
            if await self.close_lot(lot.id, reason):
                n += 1
        return n

    # ================= temporary exit / re-enter =================
    # Pause a losing rung (sell at broker) then re-enter lower on the SAME row.
    # The re-entry uses the ROLLOVER basis math: shift entry, target AND sl by
    # basis = new_fill − temp_exit_price, so the ₹ risk/reward is preserved and
    # the SL never ends up above the (lowered) entry. entry = new_fill + carried
    # loss falls out of this exactly. No realized P&L is booked at pause time —
    # the basis absorbs it on re-enter (identical to how rollover is accounted).

    async def temp_exit_now(self, lot_id: int) -> dict:
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None:
                return {"ok": False, "reason": "lot not found"}
            if lot.status != "OPEN" or lot.exit_pending:
                return {"ok": False, "reason": f"lot is {lot.status}" + (" (exit pending)" if lot.exit_pending else "")}
            inst = db.get(Instrument, lot.instrument_id)
            lot.exit_pending = True
            db.commit()
            sym, seq, qty, tsym, exch = inst.sym, lot.seq, lot.qty, lot.contract_tsym, lot.exch
            entry = lot.entry_price
            prod = lot.product_type or settings.product_type
            tgt_oid = lot.target_order_id or ""

        # cancel a broker-resting TARGET first (CONFIRMED) — it would double-sell
        # the rung. If it raced a full fill, the rung just closed at target.
        if tgt_oid:
            dead, _st, filled_t, avg_t = await self._cancel_confirmed(tgt_oid)
            if filled_t > 0 and avg_t <= 0:
                with db_session() as db:
                    _l = db.get(Lot, lot_id)
                    avg_t = _l.target_price if (_l and _l.target_price > 0) else 0.0
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                if lot is None or lot.status != "OPEN":
                    return {"ok": False, "reason": "rung state changed while cancelling its target — refresh"}
                if filled_t >= lot.qty and avg_t > 0:
                    pnl = (avg_t - lot.entry_price) * lot.qty
                    lot.status = "CLOSED"
                    lot.exit_reason = "TARGET"
                    lot.exit_price = avg_t
                    lot.exit_time = now_ist()
                    lot.exit_order_id = tgt_oid
                    lot.target_order_id = ""
                    lot.realized_pnl = (lot.realized_pnl or 0.0) + pnl
                    lot.exit_pending = False
                    db.commit()
                    event_log.trade("EXIT", f"Rung #{seq} {sym} closed at its resting TARGET @ {avg_t:.2f} "
                                            f"— temp-exit not needed | P&L ₹{lot.realized_pnl:,.2f}", sym=sym)
                    return {"ok": False, "reason": "rung closed at its target just now — no pause needed"}
                if 0 < filled_t < lot.qty and avg_t > 0:
                    lot.realized_pnl = (lot.realized_pnl or 0.0) + (avg_t - lot.entry_price) * filled_t
                    lot.qty -= filled_t
                    lot.lots = max(lot.qty // max(lot.lot_size, 1), 1)
                    lot.notes = (lot.notes or "") + f" partial resting-target fill {filled_t}u@{avg_t:.2f};"
                if not dead:
                    lot.exit_pending = False
                    db.commit()
                    event_log.warn("EXIT", f"Rung #{seq} {sym}: could NOT confirm cancel of the resting "
                                           "target — temp-exit ABORTED (no double-sell); try again", sym=sym)
                    return {"ok": False, "reason": "could not confirm the target-order cancel at the broker — try again"}
                lot.target_order_id = ""
                qty = lot.qty
                db.commit()

        report = await self.om.execute(side="S", exchange=exch, tradingsymbol=tsym, qty_units=qty,
                                       remarks=f"tempexit_l{lot_id}", product_type=prod)
        if report.status == "TIMEOUT":
            # The sell may be RESTING at the broker — cancel it, then either book a
            # race-fill, re-arm cleanly, or leave exit_pending for the reconciler's
            # stuck-exit pass (exit_order_id set below so it can find the order).
            status, filled_x, avg_x = await self._cancel_unfilled_order(report.order_id)
            if filled_x > 0 and avg_x > 0:
                report = FillReport(order_id=report.order_id,
                                    status="COMPLETE" if filled_x >= qty else "PARTIAL",
                                    filled_qty=filled_x, avg_price=avg_x, requested_qty=qty)
            elif status in ("REJECTED", "CANCELED", "CANCELLED", "INVALID"):
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    lot.exit_pending = False
                    db.commit()
                event_log.warn("EXIT", f"Temp-exit rung #{seq} {sym} did not fill — order cancelled; "
                                       "rung stays OPEN, try again", sym=sym)
                return {"ok": False, "reason": "temp-exit did not fill (order cancelled) — try again"}
            else:
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    lot.exit_order_id = report.order_id
                    db.commit()   # exit_pending stays True → reconciler stuck-exit pass owns it
                event_log.warn("EXIT", f"Temp-exit rung #{seq} {sym} UNVERIFIED (timeout, cancel "
                                       "unconfirmed) — reconciler will resolve it", sym=sym)
                return {"ok": False, "reason": "temp-exit unverified — reconciler will resolve"}
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if not report.ok:
                lot.exit_pending = False
                db.commit()
                event_log.error("EXIT", f"Temp-exit rung #{seq} {sym} FAILED: {report.status} "
                                        f"{report.error or report.raw_status} — lot untouched", sym=sym)
                return {"ok": False, "reason": f"broker: {report.status} {report.error}"}
            fill = report.avg_price if report.avg_price > 0 else entry
            filled = report.filled_qty if report.filled_qty > 0 else qty
            remainder = qty - filled
            if remainder > 0:
                # PARTIAL: only `filled` units sold → pause exactly those; the
                # unsold remainder stays a normal OPEN rung (never untracked).
                seq2 = (db.query(func.coalesce(func.max(Lot.seq), 0))
                        .filter(Lot.instrument_id == lot.instrument_id).scalar() or 0) + 1
                db.add(Lot(
                    instrument_id=lot.instrument_id, seq=seq2, exch=lot.exch,
                    contract_tsym=lot.contract_tsym, contract_token=lot.contract_token,
                    lots=max(remainder // max(lot.lot_size, 1), 1), qty=remainder, lot_size=lot.lot_size,
                    entry_price=entry, raw_entry_price=lot.raw_entry_price, entry_time=lot.entry_time,
                    target_price=lot.target_price, sl_price=lot.sl_price, product_type=lot.product_type,
                    source=lot.source, status="OPEN", notes=f"split from rung #{seq} on partial temp-exit"))
                lot.qty = filled
                lot.lots = max(filled // max(lot.lot_size, 1), 1)
                event_log.warn("EXIT", f"PARTIAL temp-exit rung #{seq} {sym}: only {filled}/{qty}u sold — "
                                       f"{remainder}u kept as a new OPEN rung", sym=sym)
            lot.status = "TEMP_EXITED"
            lot.temp_exit_price = fill
            lot.temp_exit_loss = round(entry - fill, 4)     # >0 = carried loss
            lot.temp_exit_time = now_ist()
            lot.temp_exit_trigger_price = 0.0               # limit trigger consumed
            lot.exit_pending = False
            lot.notes = (lot.notes or "") + f" temp-exit @ {fill:.2f} (carry {entry - fill:+.2f});"
            db.commit()
        event_log.trade("EXIT", f"⏸ TEMP-EXIT rung #{seq} {sym}: sold {qty}u @ {fill:.2f} (entry {entry:.2f}, "
                                f"carrying {entry - fill:+.2f} pts). Paused on the ladder — re-enter to resume.",
                        sym=sym, data={"lot_id": lot_id, "temp_exit_price": fill, "carry_pts": round(entry - fill, 4)})
        return {"ok": True, "temp_exit_price": fill, "carry_pts": round(entry - fill, 4)}

    def _reentry_blocked(self, inst) -> str:
        """Why a paused rung must NOT be bought back right now, or "" to proceed.

        Mirrors the gate block in _execute_buy. Kept as its own method because
        re-entry is reached from two places — the user's button and the armed
        limit trigger in _check_temp_triggers — and only the second one is a
        surprise. Both are buys, so both are gated.
        """
        if not self.engine_on():
            return "strategy master switch is OFF — turn it on to re-enter this rung"
        if inst is None:
            return "instrument not found"
        if not inst.enabled:
            return f"{inst.sym} is disabled in the watchlist"
        if inst.cb_tripped:
            return (f"circuit breaker is ACTIVE ({inst.cb_reason or 'loss threshold hit'}) — "
                    "re-entry blocked until it is reset")
        if self.global_cb_tripped:
            return "GLOBAL circuit breaker is active — re-entry blocked"
        if inst.rollover_state in ("scanning", "rolling"):
            return "rollover scan/roll in progress for this instrument"
        if not market_open_now(inst.exch):
            return f"{inst.exch} market is closed"
        if not self.broker_connected:
            return "broker session is DOWN at the gateway — cannot place orders"
        return ""

    async def reenter_now(self, lot_id: int) -> dict:
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None:
                return {"ok": False, "reason": "lot not found"}
            if lot.status != "TEMP_EXITED":
                return {"ok": False, "reason": f"lot is {lot.status}, not TEMP_EXITED"}
            if lot.exit_pending:
                return {"ok": False, "reason": "re-enter already in progress for this rung"}
            inst = db.get(Instrument, lot.instrument_id)
            # Re-entering is a BUY, and it was the only buy in the system with no
            # gates on it. An armed limit re-entry therefore fired straight
            # through the master switch, a tripped circuit breaker, a paused
            # ladder and Flatten — the controls an operator reaches for when
            # they want everything to STOP. A rung sitting in temp-exit is flat
            # at the broker and losing nothing; refusing to re-open it costs
            # nothing, while buying against a halt is the one thing those
            # controls exist to prevent.
            gate = self._reentry_blocked(inst)
            if gate:
                lot.exit_pending = False
                db.commit()
                return {"ok": False, "reason": gate}
            lot.exit_pending = True     # double-click guard while the buy is in flight
            db.commit()
            qty, tsym, exch, seq, sym = lot.qty, lot.contract_tsym, lot.exch, lot.seq, inst.sym
            lot_tick = inst.tick_size or 0.05
            prod = lot.product_type or settings.product_type
            temp_exit_price = lot.temp_exit_price or 0.0
            old_entry, old_tgt, old_sl = lot.entry_price, lot.target_price, lot.sl_price
        report = await self.om.execute(side="B", exchange=exch, tradingsymbol=tsym, qty_units=qty,
                                       remarks=f"reenter_l{lot_id}", product_type=prod)
        if report.status == "TIMEOUT":
            # The buy may be RESTING at the broker. Cancel it; book a race-fill if it
            # filled first; otherwise the rung stays paused. If the cancel outcome is
            # unknown, reconcile #2 will adopt any resulting broker units as external.
            status, filled_x, avg_x = await self._cancel_unfilled_order(report.order_id)
            if filled_x > 0 and avg_x > 0:
                report = FillReport(order_id=report.order_id,
                                    status="COMPLETE" if filled_x >= qty else "PARTIAL",
                                    filled_qty=filled_x, avg_price=avg_x, requested_qty=qty)
            else:
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    lot.exit_pending = False
                    db.commit()
                if status in ("REJECTED", "CANCELED", "CANCELLED", "INVALID"):
                    event_log.warn("ORDER", f"Re-enter rung #{seq} {sym} did not fill — order cancelled; "
                                            "still paused, try again", sym=sym)
                    return {"ok": False, "reason": "re-enter did not fill (order cancelled) — try again"}
                event_log.error("ORDER", f"Re-enter rung #{seq} {sym} UNVERIFIED (timeout, cancel "
                                         "unconfirmed) — check the Gateway UI; the reconciler will adopt "
                                         "any filled units as external", sym=sym)
                return {"ok": False, "reason": "re-enter unverified — check broker orders"}
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if not report.ok:
                lot.exit_pending = False
                db.commit()
                event_log.error("ORDER", f"Re-enter rung #{seq} {sym} FAILED: {report.status} "
                                         f"{report.error or report.raw_status} — still TEMP_EXITED, retry", sym=sym)
                return {"ok": False, "reason": f"broker: {report.status} {report.error}"}
            new_fill = report.avg_price if report.avg_price > 0 else (temp_exit_price or old_entry)
            filled = report.filled_qty if report.filled_qty > 0 else qty
            remainder = qty - filled
            basis = new_fill - temp_exit_price     # ROLLOVER math — preserves distances
            if remainder > 0:
                # PARTIAL: only `filled` units bought back → the rest stays PAUSED
                # as a sibling (its own temp_exit_price, re-enterable independently).
                seq2 = (db.query(func.coalesce(func.max(Lot.seq), 0))
                        .filter(Lot.instrument_id == lot.instrument_id).scalar() or 0) + 1
                db.add(Lot(
                    instrument_id=lot.instrument_id, seq=seq2, exch=lot.exch,
                    contract_tsym=lot.contract_tsym, contract_token=lot.contract_token,
                    lots=max(remainder // max(lot.lot_size, 1), 1), qty=remainder, lot_size=lot.lot_size,
                    entry_price=old_entry, raw_entry_price=old_entry, entry_time=lot.entry_time,
                    target_price=old_tgt, sl_price=old_sl, product_type=lot.product_type, source=lot.source,
                    status="TEMP_EXITED", temp_exit_price=temp_exit_price,
                    temp_exit_loss=round(old_entry - temp_exit_price, 4), temp_exit_time=now_ist(),
                    carry_recovery=lot.carry_recovery or 0.0,   # inherit already-parked loss so the split never double-counts
                    notes=f"split from rung #{seq} on partial re-enter"))
                event_log.warn("ORDER", f"PARTIAL re-enter rung #{seq} {sym}: only {filled}/{qty}u bought — "
                                        f"{remainder}u remain PAUSED as a new rung", sym=sym)
            # ---- per-cycle carried-loss accounting (prevents double-counting) ----
            # `drawdown` = how far below the (basis-preserved) entry we sold on THIS
            # pause; a profit-pause is ≤0 → clamped to 0 (never lowers the target).
            # `carry_recovery` holds the DEEPEST drawdown already parked into the
            # target across previous pauses. We add only the INCREMENTAL new loss, so
            # pausing again at the same/shallower price adds nothing (a break-even
            # round-trip parks ₹0) — that is the double-count the client reported.
            drawdown = max(round(old_entry - temp_exit_price, 4), 0.0)
            old_recovery = lot.carry_recovery or 0.0
            new_recovery = max(drawdown, old_recovery)
            carry_add = round(new_recovery - old_recovery, 4)     # ≥ 0, incremental only
            lot.carry_recovery = round(new_recovery, 4)
            lot.entry_price = round(old_entry + basis, 4)
            lot.raw_entry_price = lot.entry_price
            # TARGET absorbs the basis shift + only the NEW loss parked this cycle:
            #   target += basis + (deepest-drawdown-so-far − already-parked)
            lot.target_price = round(old_tgt + basis + carry_add, 4) if old_tgt > 0 else old_tgt
            # SL keeps its original risk distance (basis-shifted only), clamped
            # above zero so a negative basis can never silently disarm the stop
            lot.sl_price = self._shift_sl(old_sl, basis, sym=sym, seq=seq, tick=lot_tick)
            lot.qty = filled
            lot.lots = max(filled // max(lot.lot_size, 1), 1)
            lot.status = "OPEN"
            lot.exit_pending = False    # release the re-enter in-flight guard
            lot.temp_exit_price = 0.0
            lot.temp_exit_loss = 0.0
            lot.temp_exit_time = None
            lot.reenter_trigger_price = 0.0
            lot.notes = (lot.notes or "") + (f" re-enter @ {new_fill:.2f} basis {basis:+.2f} → entry "
                                             f"{lot.entry_price:.2f}, parked +{carry_add:g} (recovery "
                                             f"{new_recovery:g} pts);")
            db.commit()
            new_entry, new_tgt, new_sl = lot.entry_price, lot.target_price, lot.sl_price
        event_log.trade("ORDER", f"▶ RE-ENTER rung #{seq} {sym}: bought {qty}u @ {new_fill:.2f} (basis {basis:+.2f}) | "
                                 f"entry {old_entry:.2f}→{new_entry:.2f}, target {old_tgt:.2f}→{new_tgt:.2f} "
                                 f"(parked +{carry_add:g}, total recovery {new_recovery:g} pts), "
                                 f"SL {old_sl:.2f}→{new_sl:.2f}.", sym=sym,
                        data={"lot_id": lot_id, "new_fill": new_fill, "basis": round(basis, 4),
                              "entry": new_entry, "parked": carry_add, "recovery": new_recovery})
        # rung is OPEN again — re-rest its target order at the (shifted) target
        asyncio.create_task(self.sync_resting_orders(lot.instrument_id, force=True))
        return {"ok": True, "new_fill": new_fill, "basis": round(basis, 4), "entry": new_entry}

    async def _locked_temp_exit(self, lot_id: int, inst_id: int) -> None:
        try:
            async with self.lock_for(inst_id):
                await self.temp_exit_now(lot_id)
        except Exception:
            logger.exception("triggered temp-exit failed")
        finally:
            self._temp_inflight.discard(lot_id)

    async def _locked_reenter(self, lot_id: int, inst_id: int) -> None:
        try:
            async with self.lock_for(inst_id):
                await self.reenter_now(lot_id)
        except Exception:
            logger.exception("triggered re-enter failed")
        finally:
            self._temp_inflight.discard(lot_id)

    def _load_temp_triggers_for_key(self, exch: str, token: str) -> tuple[list, list]:
        """Sync DB fetch for _check_temp_triggers, run via asyncio.to_thread
        there — this runs on EVERY price tick (see call site)."""
        with db_session() as db:
            te = (db.query(Lot.id, Lot.instrument_id, Lot.temp_exit_trigger_price)
                  .filter(Lot.status == "OPEN", Lot.exch == exch, Lot.contract_token == token,
                          Lot.temp_exit_trigger_price > 0, Lot.exit_pending == False).all())  # noqa: E712
            re = (db.query(Lot.id, Lot.instrument_id, Lot.reenter_trigger_price)
                  .filter(Lot.status == "TEMP_EXITED", Lot.exch == exch, Lot.contract_token == token,
                          Lot.reenter_trigger_price > 0, Lot.exit_pending == False).all())  # noqa: E712
        return te, re

    async def _check_temp_triggers(self, key: str, ps: PriceState) -> None:
        """Fire armed limit temp-exit / re-enter orders when price reaches them
        (both are 'price falls to level' triggers on a dip ladder)."""
        exch, token = key.split("|", 1)
        te, re = await asyncio.to_thread(self._load_temp_triggers_for_key, exch, token)
        for lid, iid, trig in te:
            if ps.lp <= trig and lid not in self._temp_inflight:
                self._temp_inflight.add(lid)
                event_log.write("EXIT", f"limit temp-exit hit: price {ps.lp:.2f} ≤ {trig:.2f} — pausing rung", level="warn")
                asyncio.create_task(self._locked_temp_exit(lid, iid))
        for lid, iid, trig in re:
            if ps.lp <= trig and lid not in self._temp_inflight:
                self._temp_inflight.add(lid)
                event_log.write("ORDER", f"limit re-enter hit: price {ps.lp:.2f} ≤ {trig:.2f} — re-entering rung", level="warn")
                asyncio.create_task(self._locked_reenter(lid, iid))

    # ================= rollover =================

    async def _rollover_loop(self) -> None:
        while True:
            try:
                await self._rollover_check()
            except Exception:
                logger.exception("rollover check failed")
            await asyncio.sleep(settings.rollover_check_interval)

    def _roll_meta(self, inst: Instrument) -> dict:
        if (inst.instr_type or "FUT") == "OPT":
            # options are NOT month-rolled — they expire. Never mark due.
            return {"class": "option", "days_before": 0, "expiry": inst.expiry,
                    "mode": "auto", "rollover_date": "", "suggested_date": "", "state": "idle",
                    "due": False, "window_open": False,
                    "window_reason": "options expire — not auto-rolled",
                    "windows_text": "Options expire on their expiry date and are not month-rolled — "
                                    "exit or add a fresh contract before expiry.",
                    "tooltip": "OPTION contract — the engine does not auto-roll options (they expire "
                               "rather than continue month-to-month). Exit or re-add before expiry."}
        cls = instrument_class(inst.exch, inst.sym)
        days = inst.rollover_days_before if inst.rollover_days_before >= 0 else default_days_before(cls)
        exp = parse_expiry(inst.expiry)
        # system-suggested (automated) date, always computed so the UI can show it
        # even while the user is in manual mode.
        auto_rd = rollover_date(exp, days, inst.exch) if exp else None
        # manual override: a valid ISO date wins; anything unparseable falls back
        # to automated (never crashes the snapshot).
        override = None
        ov_str = (inst.rollover_date_override or "").strip()
        if ov_str:
            try:
                override = date.fromisoformat(ov_str)
            except ValueError:
                override = None
        manual = override is not None
        rd = override if manual else auto_rd
        win = window_for(cls, datetime.now(IST))
        due = bool(rd and datetime.now(IST).date() >= rd)
        return {"class": cls, "days_before": days, "expiry": inst.expiry,
                "mode": "manual" if manual else "auto",
                "rollover_date": rd.isoformat() if rd else "",
                "suggested_date": auto_rd.isoformat() if auto_rd else "",
                "state": inst.rollover_state or "idle",
                "due": due, "window_open": win.open_now, "window_reason": win.reason,
                "windows_text": win.windows_text,
                "tooltip": tooltip_for(inst.exch, inst.sym, inst.expiry, days)}

    async def _rollover_check(self) -> None:
        with db_session() as db:
            instruments = db.query(Instrument).all()
        for inst in instruments:
            meta = self._roll_meta(inst)
            queued = inst.rollover_state == "queued"   # manual roll waiting for a window
            if not meta["due"] and not queued:
                if inst.rollover_state not in ("idle",):
                    with db_session() as db:
                        row = db.get(Instrument, inst.id)
                        row.rollover_state = "idle"
                        db.commit()
                continue
            if inst.id in self._rolling:
                continue   # a sniper-scan / roll task is already running for this instrument
            exp = parse_expiry(inst.expiry)
            expired = bool(exp and datetime.now(IST).date() > exp)
            with db_session() as db:
                open_lots = db.query(Lot).filter_by(instrument_id=inst.id, status="OPEN").count()
            if meta["due"] and inst.rollover_state == "idle":
                with db_session() as db:
                    row = db.get(Instrument, inst.id)
                    row.rollover_state = "due"
                    db.commit()
                event_log.write("ROLLOVER", f"{inst.sym} entered rollover window: roll date "
                                            f"{meta['rollover_date']} reached (expiry {inst.expiry}). "
                                            f"Waiting for liquidity window — {meta['windows_text']}",
                                sym=inst.sym)
            # emergency: contract expired (engine was off?) → roll whenever market is open.
            # zero open lots → nothing to trade, so re-point the watchlist entry to the
            # next contract immediately (no liquidity window needed) — this is what keeps
            # an instrument configured ONCE rolling month after month.
            window_ok = meta["window_open"] or expired or open_lots == 0
            if not window_ok:
                continue
            if open_lots > 0 and not market_open_now(inst.exch):
                continue
            if not self.broker_connected:
                continue
            # launch the Sniper scan + roll as a background task so one instrument's
            # multi-minute scan never blocks the checks for the others.
            self._rolling.add(inst.id)
            asyncio.create_task(self._snipe_and_roll(inst.id, emergency=expired))

    async def _snipe_and_roll(self, instrument_id: int, emergency: bool = False) -> None:
        """Orchestrate a Smart-Sniper rollover: find the next contract, scan the
        window for a tight spread (unless emergency / nothing to trade), then roll.

        DELIBERATELY runs WITHOUT the instrument lock: entries are gated by
        rollover_state ("scanning"/"rolling") and each per-lot roll leg claims
        its lot via exit_pending, so holding the lock here would only (a)
        deadlock against cancel_resting_entries / sync_resting_orders which
        re-acquire it, and (b) block stop-loss exits for the whole (possibly
        hours-long) sniper scan — both previously-live bugs."""
        try:
            with db_session() as db:
                inst = db.get(Instrument, instrument_id)
                if inst is None:
                    return
                open_lots = db.query(Lot).filter_by(instrument_id=instrument_id, status="OPEN").count()
            nxt = await self._next_contract(inst)
            if nxt is None or not nxt["token"]:
                event_log.error("ROLLOVER", f"could not find next contract after {inst.tsym} — retry next cycle",
                                sym=inst.sym)
                with db_session() as db:
                    row = db.get(Instrument, instrument_id)
                    if row and row.rollover_state in ("scanning", "queued"):
                        row.rollover_state = "due"
                        db.commit()
                return
            if not emergency and open_lots > 0:
                with db_session() as db:
                    row = db.get(Instrument, instrument_id)
                    row.rollover_state = "scanning"
                    db.commit()
                await self._snipe_spread(inst, nxt)
            await self._execute_rollover(instrument_id, emergency=emergency, nxt=nxt)
        except Exception:
            logger.exception("snipe_and_roll failed")
            with db_session() as db:
                row = db.get(Instrument, instrument_id)
                if row and row.rollover_state in ("scanning", "rolling"):
                    row.rollover_state = "due"     # never leave entries gated forever
                    db.commit()
        finally:
            self._rolling.discard(instrument_id)

    async def _snipe_spread(self, inst: Instrument, nxt: dict) -> str:
        """Poll old+next contract quotes inside the liquidity window and fire only
        when the COMBINED bid/ask spread is tight; force a roll in the final 2 min
        of the window (failsafe) so a volatile market never blocks the roll."""
        cls = instrument_class(inst.exch, inst.sym)
        threshold = default_slippage_threshold(cls)
        now = datetime.now(IST)
        win = window_for(cls, now)
        if win.end is not None:
            deadline = datetime.combine(now.date(), win.end).replace(tzinfo=IST) - timedelta(minutes=2)
        else:
            deadline = now + timedelta(minutes=8)   # no known window end → cap the scan
        event_log.write("ROLLOVER", f"{inst.sym}: Sniper scanning for combined spread ≤ {threshold:g} pts "
                                    f"(failsafe force-roll at {deadline.strftime('%H:%M')} IST)",
                        sym=inst.sym, level="info")
        while True:
            now = datetime.now(IST)
            if now >= deadline:
                event_log.warn("ROLLOVER", f"{inst.sym}: Sniper FAILSAFE — spread never tightened before the "
                                           "window close; forcing a market roll now", sym=inst.sym)
                return "failsafe"
            try:
                q_old = await self.rest.get_quote(inst.exch, inst.token)
                q_new = await self.rest.get_quote(inst.exch, nxt["token"])
                bo, ao = float(q_old.get("bp1") or 0), float(q_old.get("sp1") or 0)
                bn, an = float(q_new.get("bp1") or 0), float(q_new.get("sp1") or 0)
            except Exception:
                await asyncio.sleep(1.5)
                continue
            if bo > 0 and ao > 0 and bn > 0 and an > 0:
                total = (ao - bo) + (an - bn)
                if total <= threshold:
                    event_log.trade("ROLLOVER", f"{inst.sym}: Sniper HIT — combined spread {total:.2f} ≤ "
                                                f"{threshold:g} (old {ao - bo:.2f} + new {an - bn:.2f}) → rolling now",
                                    sym=inst.sym)
                    return "tight"
            await asyncio.sleep(1.5)

    async def request_manual_rollover(self, instrument_id: int) -> dict:
        """Manual 'Roll now' = QUEUE FOR SNIPER. If we're inside a valid liquidity
        window right now → start the Sniper scan immediately; otherwise mark the
        instrument 'queued' and the rollover loop will start the scan the moment
        the window opens."""
        with db_session() as db:
            inst = db.get(Instrument, instrument_id)
            if inst is None:
                return {"ok": False, "reason": "instrument not found"}
            if (inst.instr_type or "FUT") == "OPT":
                return {"ok": False, "reason": "options are not month-rolled"}
            open_lots = db.query(Lot).filter_by(instrument_id=instrument_id, status="OPEN").count()
            exch, sym, expiry = inst.exch, inst.sym, inst.expiry
        cls = instrument_class(exch, sym)
        exp = parse_expiry(expiry)
        expired = bool(exp and datetime.now(IST).date() > exp)
        win = window_for(cls, datetime.now(IST))
        ready = win.open_now or expired or open_lots == 0
        if ready and self.broker_connected and (open_lots == 0 or market_open_now(exch)):
            if instrument_id in self._rolling:
                return {"ok": True, "state": "scanning", "reason": "already scanning/rolling"}
            self._rolling.add(instrument_id)
            asyncio.create_task(self._snipe_and_roll(instrument_id, emergency=expired))
            return {"ok": True, "state": "scanning",
                    "reason": "inside liquidity window — Sniper scan started"}
        with db_session() as db:
            row = db.get(Instrument, instrument_id)
            row.rollover_state = "queued"
            db.commit()
        event_log.write("ROLLOVER", f"{sym}: manual roll QUEUED — will Sniper-roll when the liquidity window "
                                    f"opens ({win.windows_text})", sym=sym, level="warn")
        return {"ok": True, "state": "queued",
                "reason": "outside the liquidity window — queued for the next window"}

    def _recover_rollover_states(self) -> None:
        """On boot, an interrupted 'scanning'/'rolling' can't resume mid-flight —
        reset it to 'due' so the loop re-evaluates and re-scans when the window is
        open. 'queued' (manual intent) is preserved across restarts."""
        with db_session() as db:
            rows = db.query(Instrument).filter(Instrument.rollover_state.in_(["scanning", "rolling"])).all()
            for r in rows:
                r.rollover_state = "due"
            if rows:
                db.commit()
                event_log.write("ROLLOVER", f"recovered {len(rows)} instrument(s) from interrupted "
                                            "scanning/rolling → will re-scan when the window opens")

    async def _next_contract(self, inst: Instrument) -> Optional[dict]:
        """Find the next-month future for the same root via gateway search."""
        try:
            rows = await self.rest.search(inst.exch, inst.sym)
        except Exception as e:
            event_log.error("ROLLOVER", f"scrip search failed: {e}", sym=inst.sym)
            return None
        cur_exp = parse_expiry(inst.expiry)
        futs = []
        for r in rows:
            if "FUT" not in (r.get("instrumenttype") or "").upper():
                continue
            if (r.get("sym") or "").upper() != inst.sym.upper() and not r.get("tsym", "").upper().startswith(inst.sym.upper()):
                continue
            e = parse_expiry(r.get("expd", ""))
            if e and cur_exp and e > cur_exp:
                futs.append((e, r))
        if not futs:
            return None
        futs.sort(key=lambda x: x[0])
        e, r = futs[0]
        return {"tsym": r.get("tsym", ""), "token": r.get("token", ""),
                "expd": r.get("expd", ""), "lotsize": int(r.get("lotsize") or inst.lot_size or 1)}

    async def _execute_rollover(self, instrument_id: int, emergency: bool = False,
                                nxt: Optional[dict] = None) -> None:
        with db_session() as db:
            inst = db.get(Instrument, instrument_id)
            open_lots = (db.query(Lot).filter_by(instrument_id=instrument_id, status="OPEN")
                         .order_by(Lot.entry_time.asc()).all())
        if nxt is None:
            nxt = await self._next_contract(inst)
        if nxt is None or not nxt["token"]:
            event_log.error("ROLLOVER", f"could not find next contract after {inst.tsym} — retry next cycle",
                            sym=inst.sym)
            return

        with db_session() as db:
            row = db.get(Instrument, instrument_id)
            row.rollover_state = "rolling"
            db.commit()
        event_log.trade("ROLLOVER", f"ROLLOVER START {inst.sym}: {inst.tsym} → {nxt['tsym']} "
                                    f"({len(open_lots)} open rung(s)){' [EMERGENCY — contract expired]' if emergency else ''}",
                        sym=inst.sym)
        # resting entry orders sit on the EXPIRING contract — cancel them; their
        # levels go back to PENDING, get basis-shifted below, and re-place on the
        # new contract via the sync after the roll completes.
        await self.cancel_resting_entries(instrument_id, "rollover in progress")
        await self.ws.subscribe(f"{inst.exch}|{nxt['token']}")

        # STRICTLY SEQUENTIAL, one position at a time: each rung's SELL (old
        # contract) is placed AND verified, then its BUY (new contract) is
        # placed AND verified, before the next rung's roll begins. A rung whose
        # legs did not complete stops nothing else, but no two rungs are ever
        # mid-roll at the same time.
        all_ok = True
        basis_seen: list[float] = []
        for lot in open_lots:
            ok = await self._roll_one_lot(inst, lot.id, nxt, basis_seen)
            all_ok = all_ok and ok

        await self._finalize_rollover(instrument_id, nxt, basis_seen)

    async def _finalize_rollover(self, instrument_id: int, nxt: dict,
                                 basis_seen: list[float],
                                 partial_expected: bool = False) -> None:
        """Post-roll bookkeeping shared by the full roll and the per-lot quick
        roll: shift pending ladder levels by the realized basis, switch the
        instrument onto the new contract once NO open lot remains on the old
        one, and re-assert resting orders. `partial_expected` (quick roll of a
        single rung) leaves the remaining old-contract rungs alone instead of
        queueing an automatic retry roll."""
        with db_session() as db:
            inst = db.get(Instrument, instrument_id)
        if inst is None:
            return

        # basis for shifting PENDING ladder levels onto the new contract's price
        # plane: exact (avg of rolled lots) when lots rolled, else quote-estimated
        ladder_shift: float | None = None
        if inst.mode == "ladder":
            if basis_seen:
                ladder_shift = sum(basis_seen) / len(basis_seen)
            else:
                ladder_shift = await self._estimate_roll_basis(inst, nxt)

        with db_session() as db:
            row = db.get(Instrument, instrument_id)
            still_old = (db.query(Lot).filter_by(instrument_id=instrument_id, status="OPEN",
                                                 contract_tsym=inst.tsym).count())
            if still_old == 0:
                old_expiry = row.expiry
                row.tsym = nxt["tsym"]
                row.token = nxt["token"]
                row.expiry = nxt["expd"]
                row.lot_size = nxt["lotsize"]
                row.last_rolled_expiry = old_expiry
                row.rollover_state = "idle"
                if row.mode == "ladder":
                    # shift every level the user can still activate — leaving
                    # SKIPPED/AWAIT_RECOVERY rows on the old-contract price
                    # plane would hand a re-established level a stale price
                    pend = (db.query(LadderLevel)
                            .filter(LadderLevel.instrument_id == instrument_id,
                                    LadderLevel.status.in_(["PENDING", "SKIPPED", "AWAIT_RECOVERY"]))
                            .all())
                    if ladder_shift is not None and pend:
                        for lv in pend:
                            lv.price = round(lv.price + ladder_shift, 4)
                        if row.ladder_anchor_price > 0:
                            row.ladder_anchor_price = round(row.ladder_anchor_price + ladder_shift, 4)
                db.commit()
                if row.mode == "ladder":
                    if ladder_shift is not None:
                        event_log.trade("ROLLOVER", f"{inst.sym}: pending ladder levels shifted by basis "
                                                    f"{ladder_shift:+.2f} onto {nxt['tsym']} — ladder economics "
                                                    "unchanged", sym=inst.sym)
                    else:
                        event_log.warn("ROLLOVER", f"{inst.sym}: could not estimate the roll basis — pending "
                                                   "ladder levels kept at their old-contract prices; review them",
                                       sym=inst.sym)
                event_log.trade("ROLLOVER", f"ROLLOVER COMPLETE {inst.sym}: now tracking {nxt['tsym']} "
                                            f"(expiry {nxt['expd']}). Targets/SLs basis-adjusted — "
                                            "P&L continuity preserved.", sym=inst.sym)
            elif partial_expected:
                # quick roll of ONE rung — the user chose to leave the rest on
                # the old contract; do not queue an automatic retry
                if row.rollover_state in ("scanning", "rolling"):
                    row.rollover_state = "idle"
                db.commit()
                event_log.write("ROLLOVER", f"{still_old} rung(s) remain on {inst.tsym} by choice "
                                            "(quick roll) — roll them individually or via Roll now",
                                sym=inst.sym)
            else:
                row.rollover_state = "due"   # partial roll — retry remaining lots next window
                db.commit()
                event_log.warn("ROLLOVER", f"{still_old} rung(s) still on {inst.tsym} — will retry in the "
                                           "next liquidity window", sym=inst.sym)
        # re-assert resting orders on whatever contract is now active (new-month
        # entry level + basis-adjusted targets for the rolled rungs)
        await self.sync_resting_orders(instrument_id, force=True)

    async def _adopt_uncertain_placements(self, broker_net: dict[tuple, int]) -> None:
        """A BUY whose HTTP placement errored may still be live at the broker.
        If the broker now shows units we don't track for that instrument, take
        them over as a managed OPEN lot (target/SL computed) instead of leaving a
        naked position. Markers expire after 15 min so a genuinely-failed order
        doesn't adopt an unrelated later position."""
        if not self._uncertain_placements:
            return
        now_m = _time.monotonic()
        for inst_id, rec in list(self._uncertain_placements.items()):
            if now_m - rec["ts"] > 900:                     # 15-min TTL
                self._uncertain_placements.pop(inst_id, None)
                continue
            broker = broker_net.get((rec["exch"], rec["tsym"], rec.get("prod", "M")), 0)
            if broker <= 0:
                continue                                    # nothing there yet — wait (until TTL)
            async with self.lock_for(inst_id):
                with db_session() as db:
                    inst = db.get(Instrument, inst_id)
                    if inst is None:
                        self._uncertain_placements.pop(inst_id, None)
                        continue
                    tracked = (db.query(func.coalesce(func.sum(Lot.qty), 0))
                               .filter(Lot.instrument_id == inst_id,
                                       Lot.contract_tsym == rec["tsym"],
                                       Lot.status.in_(["OPEN", "PENDING", "UNCONFIRMED"])).scalar() or 0)
                    extra = int(broker) - int(tracked)
                    if extra <= 0:
                        # the order was already tracked (normal verify won the race) — clear
                        self._uncertain_placements.pop(inst_id, None)
                        continue
                    take = min(extra, int(rec["qty"]))
                    entry = float(rec["expected_entry"]) or 0.0
                    prev_entry = rec.get("prev_entry")
                    tgt, _anchor = compute_target(inst, entry, prev_entry) if entry > 0 else (0.0, entry)
                    sl = compute_sl(inst, entry) if entry > 0 else 0.0
                    seq = (db.query(func.coalesce(func.max(Lot.seq), 0))
                           .filter(Lot.instrument_id == inst_id).scalar() or 0) + 1
                    db.add(Lot(
                        instrument_id=inst_id, seq=seq, exch=rec["exch"],
                        contract_tsym=rec["tsym"], contract_token=rec["token"],
                        lots=max(take // max(inst.lot_size, 1), 1), qty=take, lot_size=inst.lot_size,
                        entry_price=entry, raw_entry_price=entry,
                        target_price=tgt, sl_price=sl, status="OPEN",
                        timeframe_min=rec.get("tf", 0), source="recovered", product_type=rec["prod"],
                        notes=("ADOPTED by reconciler after an uncertain placement (HTTP call failed but the "
                               f"broker filled it). Entry ≈{entry:.2f} (approx — real fill unknown); target/SL "
                               "computed from it. Verify against the broker."),
                    ))
                    db.commit()
                event_log.error("RECONCILE", f"{inst.sym}: ADOPTED {take} orphaned unit(s) of {rec['tsym']} as "
                                             f"rung #{seq} (approx entry {entry:.2f}, target {tgt:.2f}, SL {sl:.2f}) "
                                             "— position is now managed with a stop. VERIFY the real fill price.",
                                sym=inst.sym)
                await self.ws.subscribe(f"{rec['exch']}|{rec['token']}")
            self._uncertain_placements.pop(inst_id, None)

    async def _estimate_roll_basis(self, inst: Instrument, nxt: dict) -> float | None:
        """new-contract price − old-contract price from live quotes (used to
        shift pending ladder levels when no lots rolled to give an exact basis)."""
        try:
            q_old = await self.rest.get_quote(inst.exch, inst.token)
            q_new = await self.rest.get_quote(inst.exch, nxt["token"])
            lp_old = float(q_old.get("lp") or 0)
            lp_new = float(q_new.get("lp") or 0)
            if lp_old > 0 and lp_new > 0:
                return round(lp_new - lp_old, 4)
        except Exception:
            pass
        return None

    async def _list_roll_targets(self, inst: Instrument, max_targets: int = 6) -> list[dict]:
        """Every LISTED later future of the same root, nearest expiry first,
        each with a live top-of-book quote. The manual-roll picker chooses from
        these — a roll always lands on a listed contract, never a typed date."""
        try:
            rows = await self.rest.search(inst.exch, inst.sym)
        except Exception as e:
            event_log.error("ROLLOVER", f"scrip search failed: {e}", sym=inst.sym)
            return []
        cur_exp = parse_expiry(inst.expiry)
        futs = []
        for r in rows:
            if "FUT" not in (r.get("instrumenttype") or "").upper():
                continue
            if ((r.get("sym") or "").upper() != inst.sym.upper()
                    and not (r.get("tsym") or "").upper().startswith(inst.sym.upper())):
                continue
            e = parse_expiry(r.get("expd", ""))
            if e and cur_exp and e > cur_exp:
                futs.append((e, r))
        futs.sort(key=lambda x: x[0])
        out = []
        for _e, r in futs[:max_targets]:
            t = {"tsym": r.get("tsym", ""), "token": r.get("token", ""),
                 "expiry": r.get("expd", ""),
                 "lot_size": int(r.get("lotsize") or inst.lot_size or 1),
                 "ltp": 0.0, "bid": 0.0, "ask": 0.0}
            try:
                q = await self.rest.get_quote(inst.exch, t["token"])
                t["ltp"] = float(q.get("lp") or 0)
                t["bid"] = float(q.get("bp1") or 0)
                t["ask"] = float(q.get("sp1") or 0)
            except Exception:
                pass
            out.append(t)
        return out

    async def rollover_plan(self, instrument_id: int, target_token: str = "") -> dict:
        """Everything the rollover drawer shows: the listed roll targets, and —
        per OPEN rung — the money math of rolling it NOW into the chosen (or
        nearest) contract. Preview only; execution basis always comes from the
        actual fills of the two legs."""
        with db_session() as db:
            inst = db.get(Instrument, instrument_id)
            if inst is None:
                return {"ok": False, "reason": "instrument not found"}
            open_lots = (db.query(Lot).filter_by(instrument_id=instrument_id, status="OPEN")
                         .order_by(Lot.entry_time.asc()).all())
        targets = await self._list_roll_targets(inst)
        if not targets:
            return {"ok": False, "reason": "no later contract listed for this root"}
        chosen = next((t for t in targets if target_token and t["token"] == target_token), targets[0])
        try:
            q_old = await self.rest.get_quote(inst.exch, inst.token)
            cur_ltp = float(q_old.get("lp") or 0)
            cur_bid = float(q_old.get("bp1") or 0)
        except Exception:
            ps = self.prices.get(f"{inst.exch}|{inst.token}")
            cur_ltp = ps.lp if ps else 0.0
            cur_bid = ps.bid if ps else 0.0
        basis = (round(chosen["ltp"] - cur_ltp, 4)
                 if (chosen["ltp"] > 0 and cur_ltp > 0) else None)
        lots_out = []
        for l in open_lots:
            leg_pnl = round((cur_ltp - l.entry_price) * l.qty, 2) if cur_ltp > 0 else None
            lots_out.append({
                "lot_id": l.id, "seq": l.seq, "contract": l.contract_tsym,
                "qty": l.qty, "lots": l.lots,
                "entry": l.entry_price, "target": l.target_price, "sl": l.sl_price,
                "ltp": cur_ltp or None,
                "leg_pnl": leg_pnl,                    # P&L booked on the old leg if rolled now
                "basis": basis,                        # est. roll cost per unit (far − near)
                "roll_cost": round(basis * l.qty, 2) if basis is not None else None,
                "entry_after": round(l.entry_price + basis, 4) if basis is not None else None,
                "target_after": (round(l.target_price + basis, 4)
                                 if basis is not None and (l.target_price or 0) > 0 else None),
                "sl_after": (round(l.sl_price + basis, 4)
                             if basis is not None and (l.sl_price or 0) > 0 else None),
                "exit_pending": bool(l.exit_pending),
            })
        return {"ok": True, "sym": inst.sym,
                "current": {"tsym": inst.tsym, "expiry": inst.expiry,
                            "ltp": cur_ltp, "bid": cur_bid},
                "chosen": chosen, "targets": targets, "basis": basis,
                "rollover": self._roll_meta(inst), "lots": lots_out}

    async def roll_single_lot(self, lot_id: int, target_tsym: str = "",
                              target_token: str = "") -> dict:
        """Quick roll: sell + re-buy ONE open rung now, into the chosen listed
        contract (or the nearest later one). Both legs verified sequentially —
        the same _roll_one_lot pipeline the full roll uses, so basis math,
        partial-fill and failure handling are identical."""
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None or lot.status != "OPEN":
                return {"ok": False, "reason": "lot not found or not OPEN"}
            if lot.exit_pending:
                return {"ok": False, "reason": "an exit/roll is already in progress for this rung"}
            inst = db.get(Instrument, lot.instrument_id)
            if inst is None:
                return {"ok": False, "reason": "instrument not found"}
            inst_id = inst.id
        if (inst.instr_type or "FUT") == "OPT":
            return {"ok": False, "reason": "options are not month-rolled"}
        if not self.broker_connected:
            return {"ok": False, "reason": "broker is disconnected"}
        if not market_open_now(inst.exch):
            return {"ok": False, "reason": "market is closed"}
        if inst_id in self._rolling:
            return {"ok": False, "reason": "a rollover is already running for this instrument"}
        if target_token:
            targets = await self._list_roll_targets(inst)
            nxt_t = next((t for t in targets
                          if t["token"] == target_token
                          or (target_tsym and t["tsym"] == target_tsym)), None)
            if nxt_t is None:
                return {"ok": False, "reason": f"contract {target_tsym or target_token} is not a listed "
                                               "later future of this root"}
            nxt = {"tsym": nxt_t["tsym"], "token": nxt_t["token"],
                   "expd": nxt_t["expiry"], "lotsize": nxt_t["lot_size"]}
        else:
            nxt = await self._next_contract(inst)
            if nxt is None or not nxt["token"]:
                return {"ok": False, "reason": "no later contract listed"}
        self._rolling.add(inst_id)
        try:
            event_log.trade("ROLLOVER", f"QUICK ROLL {inst.sym} rung: {inst.tsym} → {nxt['tsym']} "
                                        "(single rung, both legs verified)", sym=inst.sym)
            await self.ws.subscribe(f"{inst.exch}|{nxt['token']}")
            basis_seen: list[float] = []
            ok = await self._roll_one_lot(inst, lot_id, nxt, basis_seen)
            await self._finalize_rollover(inst_id, nxt, basis_seen, partial_expected=True)
            return {"ok": ok,
                    "basis": (round(basis_seen[0], 4) if basis_seen else None),
                    "reason": "" if ok else "roll did not complete — see the event log"}
        finally:
            self._rolling.discard(inst_id)

    async def roll_all_now(self, instrument_id: int, target_tsym: str,
                           target_token: str) -> dict:
        """Manual roll into a USER-CHOSEN listed contract, executed immediately
        (no sniper queue — the user picked the moment and the contract). Rolls
        every open rung strictly one at a time via the standard pipeline."""
        with db_session() as db:
            inst = db.get(Instrument, instrument_id)
            if inst is None:
                return {"ok": False, "reason": "instrument not found"}
        if (inst.instr_type or "FUT") == "OPT":
            return {"ok": False, "reason": "options are not month-rolled"}
        if not self.broker_connected:
            return {"ok": False, "reason": "broker is disconnected"}
        if not market_open_now(inst.exch):
            return {"ok": False, "reason": "market is closed"}
        if instrument_id in self._rolling:
            return {"ok": False, "reason": "a rollover is already running for this instrument"}
        targets = await self._list_roll_targets(inst)
        nxt_t = next((t for t in targets
                      if t["token"] == target_token
                      or (target_tsym and t["tsym"] == target_tsym)), None)
        if nxt_t is None:
            return {"ok": False, "reason": f"contract {target_tsym or target_token} is not a listed "
                                           "later future of this root"}
        nxt = {"tsym": nxt_t["tsym"], "token": nxt_t["token"],
               "expd": nxt_t["expiry"], "lotsize": nxt_t["lot_size"]}
        self._rolling.add(instrument_id)
        try:
            await self._execute_rollover(instrument_id, emergency=False, nxt=nxt)
            return {"ok": True, "state": "rolled", "target": nxt["tsym"]}
        except Exception as e:
            logger.exception("roll_all_now failed")
            return {"ok": False, "reason": f"roll failed: {e}"}
        finally:
            self._rolling.discard(instrument_id)

    async def _roll_one_lot(self, inst: Instrument, lot_id: int, nxt: dict,
                            basis_out: list[float] | None = None) -> bool:
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None or lot.status != "OPEN" or lot.exit_pending:
                return False
            lot.exit_pending = True
            db.commit()
            qty, seq = lot.qty, lot.seq
            prod = lot.product_type or settings.product_type
            roll_tgt_oid = lot.target_order_id or ""

        # a broker-resting TARGET on the expiring contract would double-sell the
        # leg mid-roll — cancel it first (CONFIRMED). Race-fill → the rung closed
        # at target; nothing left to roll. Unconfirmed cancel → abort this roll.
        if roll_tgt_oid:
            dead, _st, filled_t, avg_t = await self._cancel_confirmed(roll_tgt_oid)
            if filled_t > 0 and avg_t <= 0:
                with db_session() as db:
                    _l = db.get(Lot, lot_id)
                    avg_t = _l.target_price if (_l and _l.target_price > 0) else 0.0
            if not dead and filled_t < qty:
                with db_session() as db:
                    lot = db.get(Lot, lot_id)
                    lot.exit_pending = False
                    db.commit()
                event_log.warn("ROLLOVER", f"rung #{seq}: could NOT confirm cancel of the resting target — "
                                           "roll for this rung deferred to the next window", sym=inst.sym)
                return False
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                lot.target_order_id = ""
                if filled_t >= lot.qty and avg_t > 0:
                    pnl = (avg_t - lot.entry_price) * lot.qty
                    lot.status = "CLOSED"
                    lot.exit_reason = "TARGET"
                    lot.exit_price = avg_t
                    lot.exit_time = now_ist()
                    lot.exit_order_id = roll_tgt_oid
                    lot.realized_pnl = (lot.realized_pnl or 0.0) + pnl
                    lot.exit_pending = False
                    db.commit()
                    event_log.trade("ROLLOVER", f"rung #{seq} closed at its resting TARGET @ {avg_t:.2f} "
                                                f"just before the roll — nothing to roll | "
                                                f"P&L ₹{lot.realized_pnl:,.2f}", sym=inst.sym)
                    return True
                if 0 < filled_t < lot.qty and avg_t > 0:
                    lot.realized_pnl = (lot.realized_pnl or 0.0) + (avg_t - lot.entry_price) * filled_t
                    lot.qty -= filled_t
                    lot.lots = max(lot.qty // max(lot.lot_size, 1), 1)
                    lot.notes = (lot.notes or "") + f" partial resting-target fill {filled_t}u@{avg_t:.2f} (pre-roll);"
                qty = lot.qty
                db.commit()

        # leg 1 — close the expiring contract
        sell = await self.om.execute(side="S", exchange=inst.exch, tradingsymbol=lot.contract_tsym,
                                     qty_units=qty, remarks=f"exit_roll_l{lot_id}",
                                     product_type=prod)
        if not sell.ok:
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                lot.exit_pending = False
                db.commit()
            event_log.error("ROLLOVER", f"rung #{seq}: could not close old leg ({sell.status} "
                                        f"{sell.error}) — rung left untouched", sym=inst.sym)
            return False
        sold_qty, sold_avg = sell.filled_qty, sell.avg_price
        if sold_qty > 0 and sold_avg <= 0:
            # basis = new_avg - sold_avg. A zero here is not a price, and it
            # shifts entry/target/SL by an entire contract value — the next tick
            # then stops the rung out at market and books a loss that never
            # happened. The buy leg is already guarded a few lines below; this
            # one was not.
            with db_session() as db:
                _l = db.get(Lot, lot_id)
                sold_avg = _l.entry_price if _l is not None else 0.0
            event_log.error("ROLLOVER", f"rung #{seq}: old leg filled {sold_qty}u with NO average price — "
                                        f"using {sold_avg:g} so the roll basis stays sane. VERIFY at the "
                                        f"broker; this rung's P&L is approximate.", sym=inst.sym)

        # leg 2 — reopen in the next month
        buy = await self.om.execute(side="B", exchange=inst.exch, tradingsymbol=nxt["tsym"],
                                    qty_units=sold_qty, remarks="", product_type=prod)
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if not buy.ok:
                # old leg (maybe PARTIALLY) sold, new leg failed. Book only what was
                # actually sold; an unsold remainder is still HELD on the expiring
                # contract and must stay tracked — never silently orphaned.
                pnl = (sold_avg - lot.entry_price) * sold_qty
                lot.realized_pnl = (lot.realized_pnl or 0.0) + pnl
                remainder = qty - sold_qty
                if remainder > 0:
                    lot.qty = remainder
                    lot.lots = max(remainder // max(lot.lot_size, 1), 1)
                    lot.exit_pending = False
                    lot.notes = (lot.notes or "") + (f" roll: sold {sold_qty}u @ {sold_avg:.2f} (booked "
                                                     f"₹{pnl:,.2f}), buy-leg failed ({buy.status}); "
                                                     f"{remainder}u remain OPEN on the expiring contract;")
                    db.commit()
                    event_log.error("ROLLOVER", f"rung #{seq}: PARTIAL old-leg close {sold_qty}u @ {sold_avg:.2f} "
                                                f"booked ₹{pnl:,.2f}, then new-month buy FAILED ({buy.status} "
                                                f"{buy.error}). {remainder}u still OPEN on the expiring contract "
                                                "— roll will retry. Check margin!", sym=inst.sym)
                    return False
                lot.status = "CLOSED"
                lot.exit_reason = "ROLL_FAILED"
                lot.exit_price = sold_avg
                lot.exit_time = now_ist()
                lot.exit_pending = False
                lot.notes = (lot.notes or "") + f" rollover buy-leg failed ({buy.status});"
                db.commit()
                event_log.error("ROLLOVER", f"rung #{seq}: old leg closed @ {sold_avg:.2f} but new-month buy "
                                            f"FAILED ({buy.status} {buy.error}). Rung closed with "
                                            f"P&L ₹{pnl:,.2f} — NOT re-opened. Check margin!", sym=inst.sym)
                return False

            # missing avg on a filled buy would poison the basis — fall back to a
            # zero-basis roll (economics preserved) rather than a wild shift.
            new_avg = buy.avg_price if buy.avg_price > 0 else sold_avg
            basis = new_avg - sold_avg
            if basis_out is not None:
                basis_out.append(basis)
            remainder = qty - sold_qty
            if remainder > 0:
                # split: un-sold remainder stays on the old contract as its own rung
                with db_session() as db2:
                    seq2 = (db2.query(func.coalesce(func.max(Lot.seq), 0))
                            .filter(Lot.instrument_id == inst.id).scalar() or 0) + 1
                    db2.add(Lot(
                        instrument_id=inst.id, seq=seq2, exch=lot.exch,
                        contract_tsym=lot.contract_tsym, contract_token=lot.contract_token,
                        lots=max(remainder // max(lot.lot_size, 1), 1), qty=remainder,
                        lot_size=lot.lot_size, entry_price=lot.entry_price,
                        raw_entry_price=lot.raw_entry_price, entry_time=lot.entry_time,
                        target_price=lot.target_price, sl_price=lot.sl_price,
                        product_type=prod, source=lot.source,
                        status="OPEN", notes=f"split from rung #{seq} on partial rollover"))
                    db2.commit()

            old_entry, old_tgt, old_sl = lot.entry_price, lot.target_price, lot.sl_price
            dropped = sold_qty - buy.filled_qty
            if dropped > 0:
                # units sold on the old leg but NOT re-bought (partial buy fill) are a
                # closed position — book their realized P&L at the OLD entry, or that
                # money silently vanishes from the ledger.
                drop_pnl = (sold_avg - old_entry) * dropped
                lot.realized_pnl = (lot.realized_pnl or 0.0) + drop_pnl
                lot.notes = (lot.notes or "") + (f" roll partial re-buy: {dropped}u not re-opened, "
                                                 f"booked ₹{drop_pnl:,.2f};")
                event_log.warn("ROLLOVER", f"rung #{seq}: buy leg filled only {buy.filled_qty}/{sold_qty}u — "
                                           f"booked ₹{drop_pnl:,.2f} for the {dropped}u not re-opened",
                               sym=inst.sym)
            lot.qty = buy.filled_qty
            lot.lots = max(buy.filled_qty // max(nxt["lotsize"], 1), 1)
            lot.lot_size = nxt["lotsize"]
            lot.contract_tsym = nxt["tsym"]
            lot.contract_token = nxt["token"]
            lot.entry_price = round(old_entry + basis, 4)
            lot.target_price = round(old_tgt + basis, 4) if old_tgt > 0 else old_tgt
            # clamped above zero — a negative basis must never silently disarm the
            # stop on a rung that is still open (see _shift_sl)
            lot.sl_price = self._shift_sl(old_sl, basis, sym=inst.sym, seq=seq,
                                          tick=nxt.get("ticksize") or inst.tick_size or 0.05)
            lot.roll_count = (lot.roll_count or 0) + 1
            lot.total_basis = (lot.total_basis or 0.0) + basis
            lot.exit_pending = False
            db.commit()

            db.add(RolloverEvent(
                instrument_id=inst.id, lot_id=lot_id, old_tsym=inst.tsym, new_tsym=nxt["tsym"],
                old_exit_price=sold_avg, new_entry_price=new_avg, basis=round(basis, 4),
                qty=sold_qty, status="done"))
            db.commit()

        event_log.trade(
            "ROLLOVER",
            f"rung #{seq} rolled: sold {inst.tsym} @ {sold_avg:.2f}, bought {nxt['tsym']} @ "
            f"{new_avg:.2f} (basis {basis:+.2f}) | entry {old_entry:.2f}→{old_entry + basis:.2f}, "
            f"target {old_tgt:.2f}→{(old_tgt + basis) if old_tgt > 0 else 0:.2f}, "
            f"SL {old_sl:.2f}→{(old_sl + basis) if old_sl > 0 else 0:.2f} — economics unchanged",
            sym=inst.sym,
            data={"basis": round(basis, 4), "old_exit": sold_avg, "new_entry": buy.avg_price})
        return True

    # ================= reconciliation =================

    async def _boot_reconcile(self) -> None:
        try:
            self.broker_connected = await self.rest.broker_connected()
        except Exception:
            self.broker_connected = False
        if not self.broker_connected:
            event_log.warn("RECONCILE", "Gateway/broker not connected at boot — engine will keep "
                                        "retrying; persisted lots remain authoritative until then")
            return
        await self._reconcile_once(boot=True)

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.reconcile_interval)
            try:
                if self.broker_connected:
                    await self._reconcile_once()
            except Exception:
                logger.exception("reconcile failed")

    def _has_open_lots(self) -> bool:
        with db_session() as db:
            return db.query(Lot).filter(Lot.status == "OPEN").count() > 0

    async def _session_looks_alive(self) -> bool:
        """Second opinion on whether the broker session still returns real data.

        A dead Shoonya session answers with empty 200s instead of errors, so an
        empty positions list cannot be trusted on its own. Funds is a cheap,
        independent probe: a live account returns a balance, a dead session
        returns nothing at all.
        """
        try:
            funds = await self.rest.get_funds()
        except Exception:
            return False
        return bool(funds) and funds.get("cash") is not None

    async def _recover_filled_cancelled_lots(self) -> None:
        """Restore lots that were CANCELLED even though their entry order actually
        FILLED (a fill-verify data gap). Only touches lots cancelled TODAY whose
        order is COMPLETE with real fill shares — so it recovers a genuinely-held
        position instead of orphaning it, and never resurrects a truly-rejected one."""
        today0 = today_ist()
        with db_session() as db:
            cancelled = (db.query(Lot)
                         .filter(Lot.status == "CANCELLED", Lot.entry_order_id != "",
                                 Lot.entry_time >= today0).all())
        if not cancelled:
            return
        try:
            book = await self.rest.get_order_book()
        except Exception:
            return
        by_oid = {o.get("norenordno"): o for o in book if o.get("norenordno")}
        for lot in cancelled:
            o = by_oid.get(lot.entry_order_id)
            if not o or (o.get("status") or "").upper() != "COMPLETE":
                continue
            try:
                filled = int(float(o.get("fillshares", "0") or 0))
                avg = float(o.get("avgprc", "0") or 0)
            except (TypeError, ValueError):
                continue
            if filled <= 0:
                continue
            async with self.lock_for(lot.instrument_id):
                with db_session() as db:
                    l = db.get(Lot, lot.id)
                    if l is None or l.status != "CANCELLED":
                        continue
                    inst = db.get(Instrument, l.instrument_id)
                    others = (db.query(Lot)
                              .filter(Lot.instrument_id == l.instrument_id, Lot.status == "OPEN",
                                      Lot.id != l.id).all())
                    l.status = "OPEN"
                    l.entry_price = avg or l.entry_price
                    l.raw_entry_price = l.entry_price
                    l.qty = filled
                    l.lots = max(filled // max(l.lot_size, 1), 1)
                    prev = self._anchor_lot(others, l.entry_price)   # nearest open entry ABOVE
                    tgt, _ = compute_target(inst, l.entry_price, prev.entry_price if prev else None)
                    if l.target_override and l.target_override > 0:
                        tgt = round(float(l.target_override), 4)     # user edited it while pending
                    l.target_price = tgt
                    sl = compute_sl(inst, l.entry_price)
                    if l.sl_override and l.sl_override > 0:
                        sl = round(float(l.sl_override), 4)
                    l.sl_price = sl
                    l.exit_pending = False
                    l.notes = (l.notes or "") + " recovered: order was COMPLETE but the lot had been cancelled;"
                    db.commit()
                    sym, seq, ep, tp, sp = inst.sym, l.seq, l.entry_price, l.target_price, l.sl_price
            event_log.trade("RECONCILE", f"RECOVERED rung #{seq} {sym}: order {lot.entry_order_id} was COMPLETE @ "
                                         f"{ep:.2f} — restored to OPEN (target {tp:.2f}, SL {sp:.2f}); it had been "
                                         "wrongly cancelled.", sym=sym)

    async def _reconcile_once(self, boot: bool = False) -> None:
        # 0) broker-resting orders: adopt entry fills / expiries, verify resting
        #    targets, then re-assert the desired state (places anything missing)
        with db_session() as db:
            resting_ids = [l.id for l in db.query(Lot).filter(Lot.status == "RESTING").all()]
            target_ids = [l.id for l in db.query(Lot)
                          .filter(Lot.status == "OPEN",
                                  func.coalesce(Lot.target_order_id, "") != "").all()]
        for lid in resting_ids:
            await self._resolve_entry_order(lid)
        for lid in target_ids:
            await self._resolve_target_order(lid)
        await self.sync_resting_orders()

        # 1) resolve UNCONFIRMED entries and stuck exits from the order book
        with db_session() as db:
            unconfirmed = db.query(Lot).filter(Lot.status == "UNCONFIRMED").all()
            stuck_exits = db.query(Lot).filter(Lot.status == "OPEN", Lot.exit_pending == True).all()  # noqa: E712
        # a lot whose exit resolved simply stops appearing here — forget its
        # first-seen stamp so a later exit is timed from scratch, not from an
        # old one (and the map can't grow for the life of the process)
        _still_pending = {l.id for l in stuck_exits}
        self._exit_pending_since = {k: v for k, v in self._exit_pending_since.items()
                                    if k in _still_pending}
        for lot in unconfirmed:
            try:
                row = await self.rest.find_order(lot.entry_order_id)
            except Exception:
                continue
            if row is None:
                # Absent from today's book. Same-day absence can be book lag —
                # leave it alone. But a PREVIOUS-session UNCONFIRMED order will
                # never reappear (the old book is gone): settle it against the
                # broker POSITIONS so the lot doesn't block its instrument
                # forever. Untracked surplus ≥ our qty → it filled; else cancel.
                if not lot.entry_time or lot.entry_time.date() >= now_ist().date():
                    continue
                try:
                    positions = await self.rest.get_positions_raw()
                except Exception:
                    continue
                with db_session() as db:
                    l = db.get(Lot, lot.id)
                    if l is None or l.status != "UNCONFIRMED":
                        continue
                    tracked = sum(x.qty for x in db.query(Lot)
                                  .filter(Lot.status == "OPEN",
                                          Lot.contract_tsym == l.contract_tsym,
                                          func.coalesce(Lot.product_type, "M") == (l.product_type or "M"))
                                  .all())
                    need_qty, tsym_l, prod_l = l.qty, l.contract_tsym, (l.product_type or "M")
                broker_net = 0
                for p in positions:
                    if p.get("tsym") == tsym_l and (p.get("prd") or "M") == prod_l:
                        broker_net += int(float(p.get("netqty", "0") or 0))
                with db_session() as db:
                    l = db.get(Lot, lot.id)
                    if l is None or l.status != "UNCONFIRMED":
                        continue
                    if broker_net >= tracked + need_qty:
                        l.status = "OPEN"
                        l.raw_entry_price = l.entry_price
                        inst = db.get(Instrument, l.instrument_id)
                        others = (db.query(Lot).filter(Lot.instrument_id == l.instrument_id,
                                                       Lot.status == "OPEN", Lot.id != l.id).all())
                        prev = self._anchor_lot(others, l.entry_price)
                        tgt, _ = compute_target(inst, l.entry_price, prev.entry_price if prev else None)
                        if l.target_override and l.target_override > 0:
                            tgt = round(float(l.target_override), 4)
                        l.target_price = tgt
                        sl = compute_sl(inst, l.entry_price)
                        if l.sl_override and l.sl_override > 0:
                            sl = round(float(l.sl_override), 4)
                        l.sl_price = sl
                        l.notes = (l.notes or "") + (" unconfirmed order gone from the book but the broker "
                                                     "HOLDS the position — restored OPEN;")
                        db.commit()
                        event_log.warn("RECONCILE", f"UNCONFIRMED rung #{l.seq} restored to OPEN — order gone "
                                                    "from the book but the broker holds the position "
                                                    "(verify at the broker)")
                    else:
                        l.status = "CANCELLED"
                        l.notes = (l.notes or "") + (" unconfirmed order from a previous session gone from "
                                                     "the book, no broker position — cancelled;")
                        db.commit()
                        event_log.write("RECONCILE", f"UNCONFIRMED rung #{l.seq} resolved: previous-session "
                                                     "order gone from the book and no broker position — "
                                                     "lot cancelled", level="info")
                continue
            status = (row.get("status") or "").upper()
            filled = int(float(row.get("fillshares", "0") or 0))
            avg = float(row.get("avgprc", "0") or 0)
            with db_session() as db:
                l = db.get(Lot, lot.id)
                if status == "COMPLETE" and filled > 0:
                    l.status = "OPEN"
                    l.entry_price = avg or l.entry_price
                    l.raw_entry_price = l.entry_price
                    l.qty = filled
                    l.lots = max(filled // max(l.lot_size, 1), 1)
                    inst = db.get(Instrument, l.instrument_id)
                    others = (db.query(Lot).filter(Lot.instrument_id == l.instrument_id,
                                                   Lot.status == "OPEN", Lot.id != l.id).all())
                    prev = self._anchor_lot(others, l.entry_price)   # nearest open entry ABOVE
                    tgt, _ = compute_target(inst, l.entry_price, prev.entry_price if prev else None)
                    if l.target_override and l.target_override > 0:
                        tgt = round(float(l.target_override), 4)     # user edited it while pending
                    l.target_price = tgt
                    sl = compute_sl(inst, l.entry_price)
                    if l.sl_override and l.sl_override > 0:
                        sl = round(float(l.sl_override), 4)
                    l.sl_price = sl
                    db.commit()
                    event_log.trade("RECONCILE", f"UNCONFIRMED rung #{l.seq} resolved: FILLED {filled}u @ "
                                                 f"{avg:.2f} → OPEN with recomputed target/SL", sym=inst.sym)
                elif status in ("REJECTED", "CANCELED", "CANCELLED", "INVALID"):
                    l.status = "CANCELLED"
                    l.notes = (l.notes or "") + f" resolved by reconciler: {status};"
                    db.commit()
                    event_log.write("RECONCILE", f"UNCONFIRMED rung #{l.seq} resolved: order {status} — "
                                                 "lot cancelled, no position", level="info")
        for lot in stuck_exits:
            # A rung frozen by an unresolved exit has NO working stop: every exit
            # path skips a lot while exit_pending is set. That is the same danger
            # as a rung with no stop configured, which shouts hourly — this used
            # to say nothing at all, so the rung looked normally armed. An exit
            # left pending overnight can never resolve either, because the order
            # book is per-day and its order will never reappear.
            if lot.exit_pending:
                # Measure from when the RECONCILER first saw this exit unresolved,
                # not from lot.exit_time — that field is only ever written together
                # with status=CLOSED, so on an OPEN lot it is always None and the
                # 5-minute grace collapsed to "always", firing this alarm at an
                # exit that was one second old. A lot still pending across a
                # restart has no recorded start and is alerted immediately, which
                # is right: an exit that survived a restart really is stuck.
                first_seen = self._exit_pending_since.setdefault(lot.id, _time.monotonic())
                stuck_for = _time.monotonic() - first_seen
                if (stuck_for > _EXIT_STUCK_ALERT_AFTER
                        and _time.monotonic() - self._stuck_exit_alerted.get(lot.id, 0.0) > 3600.0):
                    self._stuck_exit_alerted[lot.id] = _time.monotonic()
                    event_log.error("EXIT", f"rung #{lot.seq} is OPEN but its exit order "
                                            f"({lot.exit_order_id or 'never placed'}) is unresolved — the rung "
                                            "is FROZEN and its stop-loss is not working. Resolve or close it "
                                            "at the broker.", sym="")
            if not lot.exit_order_id:
                continue
            try:
                row = await self.rest.find_order(lot.exit_order_id)
            except Exception:
                continue
            if row is None:
                # The exit order is not in the book. The book is per-DAY, so an
                # exit left pending overnight can never be found again — and this
                # branch used to just `continue`, which froze the rung forever:
                # exit_pending blocks every exit path, so the rung sat OPEN with
                # NO WORKING STOP and the UI showed "EXITING…" indefinitely. The
                # position reconciler could not help either, because it
                # deliberately defers to this resolver whenever our own exit is
                # unresolved — the two waited on each other.
                #
                # Once the order is CONFIRMED absent (two good fetches ≥90s
                # apart), stop claiming a sell is in flight: release the rung.
                # That re-arms its stop immediately and hands the position
                # difference to the ghost-close pass, which settles it under its
                # own guards (repeat passes, a wall-clock window, market open,
                # and a recent price) instead of inventing an exit here.
                if not self._order_gone_strike(lot.exit_order_id):
                    continue
                self._order_seen(lot.exit_order_id)
                with db_session() as db:
                    l = db.get(Lot, lot.id)
                    if l is None or l.status != "OPEN" or not l.exit_pending:
                        continue
                    gone_oid = l.exit_order_id
                    l.exit_pending = False
                    l.exit_order_id = ""
                    l.notes = (l.notes or "") + (f" exit order {gone_oid} vanished from the broker book — "
                                                 "rung released and re-armed; reconciler owns the position;")
                    db.commit()
                    seq_g = l.seq
                event_log.warn("RECONCILE", f"rung #{seq_g}: exit order {gone_oid} is no longer in the broker "
                                            "book (previous session, or cancelled outside the dashboard). The "
                                            "rung was frozen with no working stop — releasing it now. Its "
                                            "target/SL are armed again and the position check will settle any "
                                            "difference. VERIFY at the broker whether that sell filled.")
                continue
            status = (row.get("status") or "").upper()
            filled = int(float(row.get("fillshares", "0") or 0))
            avg = float(row.get("avgprc", "0") or 0)
            still_working = False
            with db_session() as db:
                l = db.get(Lot, lot.id)
                inst = db.get(Instrument, l.instrument_id)
                if status == "COMPLETE" and filled >= l.qty:
                    l.status = "CLOSED"
                    l.exit_price = avg
                    l.exit_time = now_ist()
                    l.realized_pnl = (l.realized_pnl or 0.0) + (avg - l.entry_price) * l.qty
                    l.exit_pending = False
                    db.commit()
                    event_log.trade("RECONCILE", f"stuck exit for rung #{l.seq} resolved: CLOSED @ {avg:.2f} "
                                                 f"P&L ₹{l.realized_pnl:,.2f}", sym=inst.sym)
                elif status in ("REJECTED", "CANCELED", "CANCELLED", "INVALID"):
                    if 0 < filled < l.qty and avg > 0:
                        # partial fill before the cancel/rejection — book the sold part
                        # and shrink, otherwise the next exit would OVER-SELL the rung.
                        l.realized_pnl = (l.realized_pnl or 0.0) + (avg - l.entry_price) * filled
                        l.qty -= filled
                        l.lots = max(l.qty // max(l.lot_size, 1), 1)
                        l.notes = (l.notes or "") + f" partial exit {filled}u@{avg:.2f} (reconciled);"
                    l.exit_pending = False
                    db.commit()
                    event_log.warn("RECONCILE", f"stuck exit for rung #{l.seq}: order {status}"
                                                f"{f' ({filled}u filled, booked)' if filled else ''} — re-armed "
                                                "for the next trigger", sym=inst.sym)
                else:
                    still_working = True   # order still resting at the broker
            if still_working:
                # A resting exit freezes the lot (exit_pending) AND disables its stop-loss.
                # Cancel it; next reconcile pass books/re-arms from the terminal status.
                try:
                    await self.rest.cancel_order(lot.exit_order_id)
                    event_log.warn("RECONCILE", f"stuck exit for rung #{lot.seq}: order still working — "
                                                "cancel requested; next pass will re-arm or book it")
                except Exception:
                    pass

        # 2) compare internal open qty vs broker net position, per contract
        try:
            positions = await self.rest.get_positions_raw()
        except Exception as e:
            event_log.warn("RECONCILE", f"could not fetch broker positions: {e}")
            self._ghost_confirm.clear()      # a failed read proves nothing about our lots
            return

        # An EMPTY position list is ambiguous: a genuinely flat account looks
        # identical to a session that was invalidated mid-day, because Shoonya
        # then answers every call with an empty 200 rather than an error. Trusting
        # it once closed a lot the broker was still holding, so when we hold lots
        # and the broker claims none, prove the session is alive before believing it.
        if not positions and self._has_open_lots():
            if not await self._session_looks_alive():
                self._ghost_confirm.clear()
                if not self._session_suspect:
                    self._session_suspect = True
                    event_log.error("RECONCILE", "broker returned NO positions while the engine holds open "
                                                 "lots, and the session failed an independent health check — "
                                                 "treating this snapshot as unreadable. No lots will be closed. "
                                                 "Reconnect the broker from the gateway.")
                return
        if self._session_suspect:
            self._session_suspect = False
            event_log.trade("RECONCILE", "broker reads are readable again — reconciliation resumed")
        # key by (exch, tsym, PRODUCT) and SUM — the broker keeps MIS ('I') and
        # NRML ('M') as separate positions on the same contract. Netting them would
        # confuse our MIS lots with an external NRML position (and vice-versa),
        # risking a wrong ghost-close. We match our lots to the SAME product only.
        broker_net: dict[tuple, int] = {}
        for p in positions:
            try:
                key = (p.get("exch", ""), p.get("tsym", ""), (p.get("prd", "") or "M"))
                broker_net[key] = broker_net.get(key, 0) + int(float(p.get("netqty", "0") or 0))
            except (TypeError, ValueError):
                continue
            # The broker keeps quoting a last price for a held contract even with
            # the exchange shut, so remember it for the dashboard.
            try:
                tok, lp = p.get("token", ""), float(p.get("lp") or 0)
                if tok and lp > 0:
                    self.ref_prices[f"{p.get('exch', '')}|{tok}"] = lp
            except (TypeError, ValueError):
                pass
        # Keep the snapshot: it is the only record of what the broker ACTUALLY
        # holds, and resting a sell without consulting it is how a rung that is
        # already flat gets a live SELL order rested against nothing.
        self._broker_net_seen = broker_net
        self._broker_net_at = _time.monotonic()

        # recover lots wrongly CANCELLED while their order actually FILLED (fill-
        # verify data gap) — restore them to OPEN so we manage what we truly hold.
        await self._recover_filled_cancelled_lots()

        # adopt any placement whose HTTP call failed but which actually reached
        # the broker — otherwise it sits as a naked, stop-less position that the
        # "unmanaged extras" branch below would deliberately ignore.
        await self._adopt_uncertain_placements(broker_net)

        with db_session() as db:
            instruments = db.query(Instrument).all()
            open_lots = db.query(Lot).filter(Lot.status == "OPEN").order_by(Lot.entry_time.asc()).all()
        by_contract: dict[tuple, list[Lot]] = {}
        for l in open_lots:
            by_contract.setdefault((l.exch, l.contract_tsym, l.product_type or "M"), []).append(l)
        inst_by_id = {i.id: i for i in instruments}

        for (exch, tsym, prd), lots in by_contract.items():
            internal = sum(l.qty for l in lots)
            broker = broker_net.get((exch, tsym, prd), 0)
            if broker >= internal:
                self._ghost_confirm.pop((exch, tsym, prd), None)   # shortfall gone — start over
                self._offhours_gap_logged.discard((exch, tsym, prd))
                extra = broker - internal
                inst_id = lots[0].instrument_id
                prev_extra = self.unmanaged.get(inst_id, 0)
                self.unmanaged[inst_id] = extra
                if extra > 0 and extra != prev_extra:
                    event_log.warn("RECONCILE", f"{tsym}: broker holds {extra} MORE units than the ladder "
                                                "tracks (manual/external buys?). Engine will NOT touch them.",
                                   sym=inst_by_id[inst_id].sym if inst_id in inst_by_id else "")
                continue
            # broker < internal → possibly some of our lots were closed externally.
            sym = inst_by_id.get(lots[0].instrument_id).sym if lots[0].instrument_id in inst_by_id else tsym
            # SAFETY 1: if the broker is net SHORT while we hold LONGs, there is an
            # external opposite-side position on this exact contract+product (the
            # broker nets everything into one number). We cannot tell our lots apart
            # from the user's manual trades, so we must NOT auto-close ours — doing
            # so destroys a real, correctly-held position. Warn once and leave it.
            if broker < 0:
                if self.unmanaged.get(lots[0].instrument_id) != -1:
                    self.unmanaged[lots[0].instrument_id] = -1   # sentinel: external opposite pos
                    event_log.warn("RECONCILE", f"{tsym}: broker is net SHORT {broker}u while the engine holds "
                                                f"{internal}u LONG — you have a MANUAL/external position on this "
                                                "contract. Engine will KEEP managing its own lots and will NOT "
                                                "auto-close them (can't net your trades against ours).", sym=sym)
                continue
            deficit = internal - broker
            k = f"{exch}|{lots[0].contract_token}"
            px = self.prices.get(k)
            # The exit price written here lands in the ledger as fact and feeds
            # the circuit breaker, so it must be a price we actually saw
            # recently. A stale tick misstates the P&L; no tick at all used to
            # write ₹0.00 as the exit price of a real trade.
            mark = px.lp if px and px.lp > 0 and px.age() <= _GHOST_MARK_MAX_AGE else 0.0
            if mark <= 0:
                self._ghost_confirm.pop((exch, tsym, prd), None)
                event_log.warn("RECONCILE", f"{tsym}: broker is short of the ladder, but there is no recent "
                                            "price to book an exit at — leaving the rungs open rather than "
                                            "closing them at a made-up price", sym=sym)
                continue
            now_dt = now_ist()
            # only lots older than the grace window are eligible to be ghost-closed
            eligible = [l for l in lots if not (l.entry_time and (now_dt - l.entry_time).total_seconds() < 120)]
            if not eligible:
                continue   # all our lots are freshly filled — let the broker settle, retry next cycle
            # OUR OWN sell in flight is not an external close. While an exit order
            # is pending, both the reason and the real fill price are knowable
            # from that order, so guessing instead is pure loss of information —
            # it booked a filled TARGET as EXTERNAL at an invented price. Leave
            # these to the stuck-exit resolver, which books the actual fill.
            ours_pending = [l for l in eligible if l.exit_pending]
            eligible = [l for l in eligible if not l.exit_pending]
            if not eligible:
                if ours_pending:
                    event_log.warn("RECONCILE", f"{tsym}: broker is {deficit}u short, but our own exit order is "
                                                "still unresolved — waiting for it to book rather than calling "
                                                "this an external close", sym=sym)
                continue
            # SAFETY 3: only an OPEN session can testify that a position is gone.
            # Outside market hours the broker legitimately reports nothing —
            # settlement, maintenance, a dead overnight session — and believing
            # that once closed two live positions at 06:22 and cancelled their
            # resting exits. Off-hours we never close anything, full stop.
            if not market_open_now(exch):
                self._ghost_confirm.pop((exch, tsym, prd), None)
                if (exch, tsym, prd) not in self._offhours_gap_logged:
                    self._offhours_gap_logged.add((exch, tsym, prd))
                    event_log.warn("RECONCILE", f"{tsym}: broker shows {broker}u vs the ladder's {internal}u, "
                                                "but the market is CLOSED — a shut broker reports nothing and "
                                                "that is not evidence of a close. Leaving the rungs alone.",
                                   sym=sym)
                continue
            # SAFETY 4: never destroy a lot on a single reading. The SAME shortfall
            # has to survive both several passes AND a wall-clock window, so a
            # momentary bad snapshot (half-dead session, a fill still settling) —
            # or a burst of feed-recovery reconciles — costs a short delay instead
            # of a real position.
            ck = (exch, tsym, prd)
            prev = self._ghost_confirm.get(ck)
            now_m = _time.monotonic()
            if prev and prev[0] == deficit:
                seen, first_at = prev[1] + 1, prev[2]
            else:
                seen, first_at = 1, now_m
            self._ghost_confirm[ck] = (deficit, seen, first_at)
            held_for = now_m - first_at
            if seen < _GHOST_CONFIRM_PASSES or held_for < _GHOST_CONFIRM_SECONDS:
                event_log.warn("RECONCILE", f"{tsym}: broker shows {broker}u but the ladder holds {internal}u — "
                                            f"unconfirmed ({seen}/{_GHOST_CONFIRM_PASSES} passes, "
                                            f"{held_for:.0f}s/{_GHOST_CONFIRM_SECONDS:.0f}s), nothing closed yet",
                               sym=sym)
                continue
            self._ghost_confirm.pop(ck, None)
            event_log.warn("RECONCILE", f"GHOST POSITION FIX {tsym}: ladder says {internal}u but broker "
                                        f"says {broker}u — closing newest settled rungs to match reality "
                                        f"(deficit {deficit}u)", sym=sym)
            for l in sorted(eligible, key=lambda x: x.entry_time, reverse=True):   # LIFO
                if deficit <= 0:
                    break
                take = min(l.qty, deficit)
                orphan_tgt = ""
                with db_session() as db:
                    row = db.get(Lot, l.id)
                    orphan_tgt = row.target_order_id or ""
                    row.target_order_id = ""   # its resting SELL must die with the position
                    if take >= row.qty:
                        row.status = "EXTERNAL_CLOSED"
                        row.exit_reason = "EXTERNAL"
                        row.exit_price = mark
                        row.exit_time = now_ist()
                        row.realized_pnl = (row.realized_pnl or 0.0) + ((mark - row.entry_price) * row.qty if mark > 0 else 0.0)
                        row.exit_pending = False
                        row.notes = (row.notes or "") + " closed by reconciler (broker had no position);"
                    else:
                        # partial external close — book the closed units' P&L (at the
                        # last mark) before shrinking, or it vanishes from the ledger.
                        if mark > 0:
                            row.realized_pnl = (row.realized_pnl or 0.0) + (mark - row.entry_price) * take
                        row.qty -= take
                        row.lots = max(row.qty // max(row.lot_size, 1), 1)
                        row.notes = (row.notes or "") + (f" shrunk {take}u by reconciler"
                                                         f"{f' (booked @ {mark:g})' if mark > 0 else ''};")
                    db.commit()
                if orphan_tgt:
                    # best-effort: an externally-closed rung's resting target would
                    # otherwise become an untracked naked short if it ever filled
                    await self.rest.cancel_order(orphan_tgt)
                    event_log.warn("RECONCILE", f"cancelled the resting target order of externally-closed "
                                                f"rung #{l.seq} ({orphan_tgt})", sym=sym)
                deficit -= take

        if boot:
            event_log.write("RECONCILE", "Boot reconciliation finished — internal ladder now matches broker")

    # ================= feed-death fallback + manual-exit alert =================

    def _open_lots_by_contract(self) -> dict[tuple, list[Lot]]:
        with db_session() as db:
            lots = db.query(Lot).filter(Lot.status == "OPEN").order_by(Lot.entry_time.asc()).all()
        out: dict[tuple, list[Lot]] = {}
        for l in lots:
            out.setdefault((l.instrument_id, l.exch, l.contract_tsym), []).append(l)
        return out

    def manual_exit_plan(self) -> list[dict]:
        """What the operator must do BY HAND to flatten, given the broker only
        shows an AVERAGED position but we hold independent rungs. Per contract:
        net side + total lots/qty to trade, the rung breakdown (so the averaged
        broker view maps back to our ledger), last-known price, est P&L, and the
        (clearly-labelled proxy) fallback reference price."""
        groups = self._open_lots_by_contract()
        if not groups:
            return []
        with db_session() as db:
            insts = {i.id: i for i in db.query(Instrument).all()}
        plan: list[dict] = []
        for (inst_id, exch, tsym), lots in groups.items():
            inst = insts.get(inst_id)
            lot_size = max((inst.lot_size if inst else lots[0].lot_size) or 1, 1)
            net_qty = sum(l.qty for l in lots)          # all rungs are long buys → net long
            k = f"{exch}|{lots[0].contract_token}"
            ps = self.prices.get(k)
            last_lp = ps.lp if ps and ps.lp > 0 else 0.0
            last_age = round(ps.age(), 0) if ps else None
            est_pnl = sum((last_lp - l.entry_price) * l.qty for l in lots) if last_lp > 0 else None
            fb = self._fallback_prices.get(inst_id)
            plan.append({
                "instrument_id": inst_id,
                "sym": inst.sym if inst else tsym,
                "exch": exch, "tsym": tsym, "lot_size": lot_size,
                "action": "SELL" if net_qty >= 0 else "BUY",   # long → SELL to flatten
                "total_lots": abs(net_qty) // lot_size,
                "total_qty": abs(net_qty),
                "rungs": [{"seq": l.seq, "lots": l.lots, "qty": l.qty,
                           "entry": round(l.entry_price, 4)} for l in lots],
                "last_lp": round(last_lp, 4) if last_lp else None,
                "last_lp_age": last_age,
                "est_pnl": round(est_pnl, 2) if est_pnl is not None else None,
                "fallback": fb,
            })
        return plan

    @staticmethod
    def _format_exit_alert(plan: list[dict]) -> str:
        lines = ["⚠️ GRID: Shoonya price feed is DOWN.",
                 "The engine can no longer auto-exit. Flatten these MANUALLY in your broker",
                 "(broker shows an averaged position; below is your true rung ledger):", ""]
        total_est = 0.0
        have_est = False
        for g in plan:
            head = f"• {g['sym']} ({g['tsym']}, {g['exch']}): {g['action']} {g['total_lots']} lot(s) = {g['total_qty']} units"
            lines.append(head)
            rung_txt = ", ".join(f"#{r['seq']} {r['lots']}L@{r['entry']:g}" for r in g["rungs"])
            lines.append(f"   rungs: {rung_txt}")
            if g["last_lp"] is not None:
                age = f" ({int(g['last_lp_age'])}s old)" if g.get("last_lp_age") is not None else ""
                pnl = f", est P&L ₹{g['est_pnl']:,.0f}" if g.get("est_pnl") is not None else ""
                lines.append(f"   last Shoonya price {g['last_lp']:g}{age}{pnl}")
            if g.get("est_pnl") is not None:
                total_est += g["est_pnl"]; have_est = True
            if g.get("fallback"):
                fb = g["fallback"]
                lines.append(f"   proxy ref: {fb.get('source', '?')} ≈ {fb.get('lp')} (DELAYED, not exact MCX price)")
            lines.append("")
        if have_est:
            lines.append(f"Est. total open P&L at last-known prices: ₹{total_est:,.0f}")
        lines.append("Prices are last-known/delayed. Verify in your broker before acting.")
        return "\n".join(lines)

    async def _refresh_fallback_prices(self) -> None:
        """Pull proxy reference prices for open-lot instruments (degraded mode)."""
        with db_session() as db:
            rows = (db.query(Instrument.id, Instrument.fallback_symbol)
                    .filter(Instrument.id.in_(
                        db.query(Lot.instrument_id).filter(Lot.status == "OPEN").distinct())).all())
        wanted = [(iid, sym) for iid, sym in rows if sym]
        if not wanted:
            return
        results = await asyncio.gather(*(self.fallback.quote(sym) for _iid, sym in wanted),
                                       return_exceptions=True)
        for (iid, _sym), q in zip(wanted, results):
            if isinstance(q, dict) and q:
                self._fallback_prices[iid] = q

    def _user_alert_settings(self) -> dict:
        with db_session() as db:
            s = db.get(UserSettings, 1)
            if s is None:
                return {}
            return {"enabled": bool(s.whatsapp_enabled), "number": s.whatsapp_number or "",
                    "apikey": s.callmebot_apikey or "", "on_feed_down": bool(s.alert_on_feed_down)}

    async def _send_exit_alert(self, plan: list[dict]) -> None:
        cfg = self._user_alert_settings()
        if not (cfg.get("enabled") and cfg.get("on_feed_down")):
            return
        try:
            text = self._format_exit_alert(plan)
            ok, detail = await send_whatsapp(cfg["number"], cfg["apikey"], text)
            if ok:
                event_log.warn("FEED", f"WhatsApp manual-exit alert sent to {cfg['number']} ({detail})")
            else:
                event_log.error("FEED", f"WhatsApp manual-exit alert FAILED: {detail} — check "
                                        "P&L → Alerts settings (number + CallMeBot key)")
        finally:
            self._alert_inflight = False

    async def _check_feed_health(self) -> None:
        """Detect Shoonya feed death WITH open positions → enter degraded mode:
        pull proxy prices for the UI and fire a WhatsApp manual-exit alert. Clear
        on recovery (the reconciler then matches any manual exits)."""
        now_m = _time.monotonic()
        # A shut exchange sends nothing — that is not the feed being "down", and
        # telling the operator to exit manually at 3am is a false alarm that
        # trains them to ignore the real one.
        shoonya_down = self._feed_expected_now() and (
            (not self.broker_connected) or (self.ws.seconds_since_rx() > settings.feed_down_after))
        with db_session() as db:
            has_open = db.query(Lot.id).filter(Lot.status == "OPEN").first() is not None

        if shoonya_down and has_open:
            entering = self._degraded_since == 0.0
            if entering:
                self._degraded_since = now_m
                event_log.error("FEED", "DEGRADED: Shoonya feed down with OPEN positions — engine cannot "
                                        "auto-exit. Falling back to reference prices; alerting operator to "
                                        "exit manually.")
            try:
                await self._refresh_fallback_prices()
            except Exception:
                logger.exception("fallback price refresh failed")
            cfg = self._user_alert_settings()
            due = entering or (now_m - self._last_exit_alert > settings.alert_repeat_after)
            if cfg.get("enabled") and cfg.get("on_feed_down") and due and not self._alert_inflight:
                plan = self.manual_exit_plan()
                if plan:
                    self._last_exit_alert = now_m
                    self._alert_inflight = True
                    asyncio.create_task(self._send_exit_alert(plan))   # don't block the loop
        else:
            if self._degraded_since != 0.0:
                event_log.write("FEED", "RECOVERED: Shoonya feed restored — reconciling now to match any manual "
                                        "exits done during the outage and update P&L.", level="warn")
                self._degraded_since = 0.0
                self._last_exit_alert = 0.0
                self._fallback_prices = {}
                asyncio.create_task(self._reconcile_after_recovery())

    async def _refresh_broker_snapshot(self) -> None:
        """Fetch positions, funds and profile CONCURRENTLY into the cache."""
        try:
            pos, funds, acct = await asyncio.gather(
                self.rest.get_positions_grouped(),
                self.rest.get_funds(),
                self.rest.get_user(),
                return_exceptions=True,
            )
            errs = []
            if isinstance(pos, Exception):
                errs.append(f"positions: {pos}")
                pos = self._broker_snap.get("positions")     # keep the last good one
            if isinstance(funds, Exception):
                errs.append(f"funds: {funds}")
                funds = self._broker_snap.get("funds")
            if isinstance(acct, Exception):
                acct = self._broker_snap.get("account")
            self._broker_snap = {"at": _time.monotonic(), "positions": pos, "funds": funds,
                                 "account": acct, "error": " ".join(errs)}
        except Exception as e:
            logger.warning("broker snapshot refresh failed: %s", e)
        finally:
            self._broker_snap_inflight = False

    async def broker_snapshot(self, max_age: float = 3.0) -> dict:
        """The account snapshot, returned IMMEDIATELY.

        A stale-but-labelled number beats a frozen panel: the caller gets the
        last snapshot straight away and a refresh runs behind it when the cache
        has aged past `max_age`. On a failed refresh the previous values are
        kept rather than blanked, so a broker hiccup doesn't wipe the screen.
        """
        if (not self._broker_snap_inflight
                and _time.monotonic() - self._broker_snap["at"] > max_age
                and self.broker_connected):
            self._broker_snap_inflight = True
            asyncio.create_task(self._refresh_broker_snapshot())
        return self._broker_snap

    def display_price(self, exch: str, token: str) -> float:
        """Best price to SHOW: the live tick when there is one, otherwise the last
        price the broker reported for the position.

        Display only, deliberately. Entries and exits read self.prices, so a
        reference price carried over a closed market can never trigger a trade —
        it only stops the screen claiming ₹0 on a position that is really up or
        down money.
        """
        ps = self.prices.get(f"{exch}|{token}")
        if ps and ps.lp > 0:
            return ps.lp
        return self.ref_prices.get(f"{exch}|{token}", 0.0)

    def _feed_expected_now(self) -> bool:
        """Are ticks expected right now? False only when every exchange we watch
        is shut. Unknown (nothing watched yet) counts as expected, so a cold
        start never mistakes itself for a closed market."""
        ex = self._watched_exchanges
        return True if not ex else any(market_open_now(e) for e in ex)

    async def _reconcile_after_recovery(self) -> None:
        """Immediately re-match internal lots vs broker after the feed returns,
        rather than waiting for the next 60s reconcile tick."""
        try:
            if self.broker_connected:
                await self._reconcile_once()
        except Exception:
            logger.exception("post-recovery reconcile failed")

    # ================= price keeper / heartbeat / UI push =================

    def _load_price_keeper_watch_set(self) -> tuple[list, list]:
        """Sync DB fetch for _price_keeper_loop, run via asyncio.to_thread so a
        slow/contended query doesn't block the event loop (see call site)."""
        with db_session() as db:
            instruments = db.query(Instrument).all()
            lot_tokens = (db.query(Lot.exch, Lot.contract_token)
                          .filter(Lot.status.in_(["OPEN", "TEMP_EXITED"]))
                          .distinct().all())
        return instruments, lot_tokens

    async def _price_keeper_loop(self) -> None:
        """Every second: refresh stale prices over REST, maintain feed_state,
        and push a snapshot to the UI so prices tick every second on screen."""
        last_feed = ""
        while True:
            try:
                # feed heartbeat
                silent = self.ws.seconds_since_rx()
                # Silence outside market hours is the correct state, not a fault:
                # the exchange is shut, so there is nothing to receive. Calling it
                # STALE overnight buried the log, raised DEGRADED alerts about a
                # closed market, and — since every "recovery" kicks a reconcile —
                # drove the reconciler far faster than its own safety windows.
                if not self._feed_expected_now():
                    self.feed_state = "closed"
                elif self.ws.connected and silent < settings.heartbeat_stale_after:
                    self.feed_state = "live"
                elif self.ws.connected:
                    self.feed_state = "stale"
                else:
                    self.feed_state = "down"
                if self.feed_state != last_feed:
                    if self.feed_state == "live":
                        event_log.write("FEED", "Price feed LIVE (WebSocket healthy)")
                    elif self.feed_state == "closed":
                        event_log.write("FEED", "Market closed — price feed idle, no ticks expected")
                    else:
                        event_log.warn("FEED", f"Price feed {self.feed_state.upper()} "
                                               f"(no WS frame for {min(silent, 9999):.0f}s) — new entries paused, "
                                               "REST quote fallback engaged")
                    last_feed = self.feed_state

                # REST quote fallback for anything stale (watchlist + open-lot contracts).
                # Off the event loop: this runs every second, and the DB is now a WAN
                # round trip — inline, a single slow/contended query here freezes the
                # whole app (every request, every other loop) for as long as it takes.
                instruments, lot_tokens = await asyncio.to_thread(self._load_price_keeper_watch_set)
                self._watched_exchanges = {i.exch for i in instruments if i.exch}
                keys = {(i.exch, i.token) for i in instruments if i.token}
                keys |= {(e, t) for e, t in lot_tokens if t}
                stale = []
                for exch, token in keys:
                    k = f"{exch}|{token}"
                    ps = self.prices.setdefault(k, PriceState())
                    if ps.age() > settings.quote_poll_after and market_open_now(exch):
                        stale.append((k, exch, token))
                for chunk_start in range(0, len(stale), 4):
                    chunk = stale[chunk_start:chunk_start + 4]
                    results = await asyncio.gather(
                        *(self.rest.get_quote(exch, token) for _, exch, token in chunk),
                        return_exceptions=True)
                    for (k, _e, _t), q in zip(chunk, results):
                        if isinstance(q, Exception):
                            continue
                        if self._apply_quote(k, q, "poll"):
                            ps = self.prices[k]
                            if ps.lp > 0:
                                await self._check_instrument_on_price(k, ps)
                                await self._check_ladder_on_price(k, ps)
                                await self._check_temp_triggers(k, ps)

                # feed-death → fallback reference + WhatsApp manual-exit alert
                await self._check_feed_health()

                if self.on_snapshot is not None:
                    # The producer: always rebuilds, and refreshes the cache that
                    # every request-time reader then serves from.
                    await self.on_snapshot(self.snapshot(max_age=0))
            except Exception:
                logger.exception("price keeper iteration failed")
            await asyncio.sleep(settings.ui_push_interval)

    async def _broker_status_loop(self) -> None:
        # Consecutive polls where the gateway was UNREACHABLE (None). A definitive
        # True/False answer resets it; only sustained unreachability flips us down.
        unreachable = 0
        while True:
            prev = self.broker_connected
            status = await self.rest.broker_status()
            if status is None:
                # Transient Grid↔Gateway blip — hold the last known state for a
                # few polls rather than flapping the badge/gate to "down".
                unreachable += 1
                if unreachable >= settings.broker_status_grace_polls:
                    self.broker_connected = False
            else:
                # Definitive answer from the WS-primary gateway — trust it at once.
                unreachable = 0
                self.broker_connected = status
            if self.broker_connected != prev:
                if self.broker_connected:
                    event_log.write("BROKER", "Broker connection restored")
                else:
                    event_log.warn("BROKER", "Broker connection lost — Gateway reports disconnected")
            if self.broker_connected:
                try:
                    self.funds = await self.rest.get_funds() or {}
                except Exception:
                    pass   # keep the last cached funds
            await asyncio.sleep(settings.broker_status_poll_interval)

    async def _housekeeping_loop(self) -> None:
        while True:
            try:
                # Both are sync DB work — off the event loop so a slow/contended
                # WAN query here doesn't freeze the whole app for its duration.
                n = await asyncio.to_thread(event_log.purge_old)
                if n:
                    logger.info("purged %d old log rows", n)
                await asyncio.to_thread(self._seed_econ_events)
            except Exception:
                logger.exception("housekeeping failed")
            await asyncio.sleep(6 * 3600)

    # ================= econ events =================

    def _seed_econ_events(self) -> None:
        """Regenerate the recurring macro calendar for the next 14 days."""
        now = datetime.now(IST).replace(tzinfo=None)
        horizon = now + timedelta(days=14)
        with db_session() as db:
            db.query(EconEvent).filter(EconEvent.auto == True, EconEvent.dt >= now).delete()  # noqa: E712
            d = now.date()
            while d <= horizon.date():
                if d.weekday() == 3:  # Thursday
                    db.add(EconEvent(dt=datetime.combine(d, dtime(20, 0)), auto=True, impact="high",
                                     region="US", title="EIA Natural Gas Storage Report",
                                     note="NG whipsaw window 19:30–21:00 IST — no NG rollovers/entries"))
                if d.weekday() == 2:  # Wednesday
                    db.add(EconEvent(dt=datetime.combine(d, dtime(20, 0)), auto=True, impact="medium",
                                     region="US", title="EIA Petroleum Status Report",
                                     note="Crude volatility 20:00–21:00 IST"))
                d += timedelta(days=1)
            # expiry / roll dates for the watchlist
            for inst in db.query(Instrument).all():
                exp = parse_expiry(inst.expiry)
                if not exp or exp > horizon.date():
                    continue
                db.add(EconEvent(dt=datetime.combine(exp, dtime(23, 30 if inst.exch == 'MCX' else 15)),
                                 auto=True, impact="high", region="IN",
                                 title=f"{inst.sym} futures expiry ({inst.tsym})",
                                 note="engine auto-rolls before this"))
                meta = self._roll_meta(inst)
                if meta["rollover_date"]:
                    rd = datetime.strptime(meta["rollover_date"], "%Y-%m-%d").date()
                    if now.date() <= rd <= horizon.date():
                        db.add(EconEvent(dt=datetime.combine(rd, dtime(12, 0)), auto=True, impact="medium",
                                         region="IN", title=f"{inst.sym} rollover day",
                                         note=meta["windows_text"]))
            db.commit()

    # ================= snapshot for UI =================

    def snapshot(self, max_age: float = 10.0) -> dict:
        """The dashboard snapshot, reusing the one the push loop just built.

        Rebuilding costs several round trips to a database that lives off-site,
        and the push loop already rebuilds it every second — so answering a
        request by rebuilding it again made the P&L panel crawl while the rest
        of the dashboard, reading the pushed copy, stayed fluid. The producer
        (the push loop) passes max_age=0 and refreshes this cache; everyone else
        serves that copy.

        The window is deliberately far longer than the producer's one-second
        cadence: it is a safety valve for the producer having stalled, not a
        freshness target. Tightening it to about a second put readers back to
        rebuilding whenever an iteration ran long, which is exactly the cost
        this exists to avoid.
        """
        now = _time.monotonic()
        if max_age > 0 and self._snap_cache is not None and now - self._snap_cache_at <= max_age:
            return self._snap_cache
        snap = self._build_snapshot()
        self._snap_cache, self._snap_cache_at = snap, now
        return snap

    def _build_snapshot(self) -> dict:
        # Every query below is a round trip to an off-site database — about 35ms
        # each — and it runs on the event loop, so each one freezes the whole
        # engine for that long. This is rebuilt once a second, so per-instrument
        # queries inside the loop below cost 35ms × instruments EVERY second and
        # stalled unrelated requests. Fetch today's realised P&L for every
        # instrument in ONE grouped query instead.
        with db_session() as db:
            instruments = db.query(Instrument).all()
            open_lots = db.query(Lot).filter(Lot.status.in_(["OPEN", "PENDING", "UNCONFIRMED", "TEMP_EXITED", "RESTING"])).all()
            ladder_ids = [i.id for i in instruments if i.mode == "ladder"]
            ladder_rows = (db.query(LadderLevel)
                           .filter(LadderLevel.instrument_id.in_(ladder_ids))
                           .order_by(LadderLevel.level_no.asc()).all()) if ladder_ids else []
            _today0 = today_ist()
            realized_today_by_inst: dict[int, float] = {
                inst_id: float(total or 0.0)
                for inst_id, total in (db.query(Lot.instrument_id,
                                                func.coalesce(func.sum(Lot.realized_pnl), 0.0))
                                       .filter(Lot.status.in_(["CLOSED", "EXTERNAL_CLOSED"]),
                                               Lot.exit_time >= _today0)
                                       .group_by(Lot.instrument_id).all())
            }
        today0 = _today0
        by_inst: dict[int, list] = {}
        for l in open_lots:
            by_inst.setdefault(l.instrument_id, []).append(l)
        levels_by_inst: dict[int, list] = {}
        for lv in ladder_rows:
            levels_by_inst.setdefault(lv.instrument_id, []).append(lv)

        inst_snaps = []
        total_open_pnl = 0.0
        for inst in instruments:
            key = f"{inst.exch}|{inst.token}"
            ps = self.prices.get(key) or PriceState()
            lots = by_inst.get(inst.id, [])
            open_pnl = 0.0
            for l in lots:
                if l.status == "TEMP_EXITED":
                    open_pnl += open_lot_pnl(l, 0.0)   # frozen at the temp-exit fill
                    continue
                if l.status != "OPEN":
                    continue
                px = self.display_price(l.exch, l.contract_token) or self.display_price(inst.exch, inst.token)
                open_pnl += open_lot_pnl(l, px)
            realized_today = realized_today_by_inst.get(inst.id, 0.0)
            total_open_pnl += open_pnl
            ladder_block = None
            if inst.mode == "ladder":
                lvls = levels_by_inst.get(inst.id, [])
                # "Next buy" = the HIGHEST price at which a buy will actually
                # happen next, across every real source: live pending entry
                # orders at the broker (RESTING lots; legacy UNCONFIRMED via
                # their level's price) and activatable levels (PLACED /
                # AWAIT_RECOVERY / queued PENDING). Skips, re-establishes and
                # edits all flow through this single truth.
                _lvl_price_by_lot = {lv.lot_id: lv.price for lv in lvls if lv.lot_id}
                _tgt_by_lot = {l.id: (l.target_price or 0.0) for l in lots}
                _nb = [lv.price for lv in lvls
                       if lv.status in ("PENDING", "PLACED", "AWAIT_RECOVERY")]
                for l in lots:
                    if l.status == "RESTING":
                        _nb.append(l.entry_price)
                    elif l.status == "UNCONFIRMED":
                        p = _lvl_price_by_lot.get(l.id)
                        if p:
                            _nb.append(p)
                ladder_block = {
                    "next_buy_price": (round(max(_nb), 4) if _nb else None),
                    "basis": inst.ladder_basis or "fixed",
                    "anchor_price": inst.ladder_anchor_price or 0.0,
                    "interval_points": inst.ladder_interval_points or 0.0,
                    "num_levels": inst.ladder_num_levels or 5,
                    "sr_lookback_days": inst.ladder_sr_lookback_days or 45,
                    "armed": bool(inst.ladder_armed),
                    "rearm": bool(inst.ladder_rearm),
                    "levels": [{
                        "id": lv.id, "level_no": lv.level_no, "price": lv.price,
                        "lots_override": lv.lots_override, "target_override": lv.target_override,
                        # The price this rung will actually exit at. Until now the
                        # levels view could only show a manual override, so a rung
                        # with an engine-computed target showed a blank — the one
                        # number the user most wants to check was the one missing.
                        "target_live": (_tgt_by_lot.get(lv.lot_id) or lv.target_override or None),
                        "sl_override": lv.sl_override,
                        "sl_auto": compute_sl(inst, lv.price),
                        "status": lv.status,
                        "source": lv.source, "note": lv.note or "", "lot_id": lv.lot_id,
                        "fire_count": lv.fire_count or 0,
                        "triggered_at": lv.triggered_at.isoformat(timespec="seconds") if lv.triggered_at else "",
                    } for lv in lvls],
                }
            inst_snaps.append({
                "id": inst.id, "sym": inst.sym, "exch": inst.exch, "tsym": inst.tsym,
                "token": inst.token, "lot_size": inst.lot_size, "expiry": inst.expiry,
                "enabled": inst.enabled, "mode": inst.mode or "auto",
                "instr_type": inst.instr_type or "FUT",
                "opt_type": inst.opt_type or "",
                "strike": inst.strike or 0.0,
                "underlying": inst.underlying or "",
                "ladder": ladder_block,
                "price": _price_block(ps, self.ref_prices.get(key, 0.0)),
                "open_rungs": sum(1 for l in lots if l.status == "OPEN"),
                "pending_rungs": sum(1 for l in lots if l.status in ("PENDING", "UNCONFIRMED")),
                "resting_rungs": sum(1 for l in lots if l.status == "RESTING"),
                "open_pnl": round(open_pnl, 2),
                "realized_today": round(float(realized_today), 2),
                "cb": {"enabled": inst.cb_enabled, "threshold": inst.cb_threshold,
                       "tripped": inst.cb_tripped, "reason": inst.cb_reason},
                "config": {
                    "target_mode": inst.target_mode, "target_value": inst.target_value,
                    "target_chain_pct": inst.target_chain_pct if inst.target_chain_pct is not None else 100.0,
                    "sl_mode": inst.sl_mode, "sl_value": inst.sl_value,
                    "sl_enabled": getattr(inst, "sl_enabled", False) is not False,
                    "tick_size": inst.tick_size or 0.05,
                    "sizing_mode": inst.sizing_mode, "vol_formula": inst.vol_formula,
                    "risk_per_rung": inst.risk_per_rung, "fixed_lots": inst.fixed_lots,
                    "atr_period": inst.atr_period, "min_lots": inst.min_lots,
                    "max_lots_per_rung": inst.max_lots_per_rung, "max_rungs": inst.max_rungs,
                    "min_gap_points": inst.min_gap_points, "max_spread_points": inst.max_spread_points,
                    "marketable_ticks": inst.marketable_ticks if inst.marketable_ticks is not None else 2,
                    "sell_signal_mode": inst.sell_signal_mode,
                    "rollover_days_before": inst.rollover_days_before,
                    "rollover_date_override": inst.rollover_date_override or "",
                    "fallback_symbol": inst.fallback_symbol or "",
                    "product_type": inst.product_type or "M",
                    "buy_order_type": inst.buy_order_type or "LMT",
                    "sell_order_type": inst.sell_order_type or "LMT",
                    "ladder_rearm": bool(inst.ladder_rearm),
                },
                "rollover": self._roll_meta(inst),
                "unmanaged_qty": self.unmanaged.get(inst.id, 0),
            })

        return {
            "ts": datetime.now(IST).isoformat(timespec="seconds"),
            "engine_on": self.engine_on(),
            "broker_connected": self.broker_connected,
            "feed": {"state": self.feed_state,
                     "seconds_since_rx": round(min(self.ws.seconds_since_rx(), 9999), 1),
                     "reconnects": self.ws.reconnect_count},
            "global_cb": {"threshold": settings.global_cb_threshold, "tripped": self.global_cb_tripped,
                          "reason": self._global_cb_reason},
            "total_open_pnl": round(total_open_pnl, 2),
            "funds": {
                "cash": round(float(self.funds.get("cash") or 0), 2),
                "margin_used": round(float(self.funds.get("margin_used") or 0), 2),
                "remaining": self._remaining_margin(self.funds),
                "have": bool(self.funds),
            },
            "instruments": inst_snaps,
            "booted_at": self.booted_at,
            "degraded": {
                "active": self._degraded_since != 0.0,
                "since_s": round(_time.monotonic() - self._degraded_since, 0) if self._degraded_since else 0,
                "reason": "Shoonya feed down with open positions — auto-exit disabled, manual exit required"
                          if self._degraded_since else "",
                "fallback_provider": settings.fallback_provider,
            },
        }
