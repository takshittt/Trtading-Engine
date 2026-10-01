"""Keeps LTP true for everything on screen, without touching the request path.

The websocket ticker only speaks while the market is open, and only for symbols
it has been told about. Anything else — a signal that arrived overnight, a
symbol subscribed a second ago, the whole board before the first tick of the
day — has no live price at all. Signal reads deliberately do no network I/O
(a blocking quote per symbol once made that endpoint take 40s), so without this
loop those rows keep whatever price they were stored with, indefinitely.

This closes that gap from the other side: a background pass that REST-quotes
the symbols currently on display and feeds them into the same STATE.prices the
ticker writes. Reads stay instant; prices stay real.

Quotes are the slowest call the gateway has, so the pass is bounded per cycle
and prefers symbols with no price at all over merely refreshing a good one.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select

from app.core.state import STATE, audit
from app.core.market_hours import is_ltp_valid
from app.services import gateway_service as gw
from db.engine import session
from db.models import Position, Signal

_log = logging.getLogger(__name__)

INTERVAL_SECONDS = 30.0
# Used while the websocket ticker is disconnected — see _loop().
TICKER_DOWN_INTERVAL_SECONDS = 5.0
MAX_QUOTES_PER_PASS = 25
# Longer than the gateway's 20s liveness cache, so an explicit action is never
# overruled by a stale read of it.
BROKER_ACTION_GRACE_SECONDS = 45.0
# Consecutive re-checks where the gateway was UNREACHABLE (couldn't answer at
# all) tolerated before flipping the badge to down. A DEFINITIVE connected:false
# is trusted at once; only sustained unreachability — a real Swing↔Gateway
# outage, not a one-off blip — turns the badge red.
BROKER_UNREACHABLE_GRACE_POLLS = 3
_broker_unreachable = 0
# A price this old is refreshed even though one exists — long enough that a
# quiet symbol is not re-quoted constantly, short enough to stay usable.
STALE_AFTER_SECONDS = 120.0

_ACTIONABLE = ("NEW", "AVERAGING")
_last_quoted: dict[str, float] = {}


def _held_symbols(db) -> list[str]:
    return [p.symbol for p in db.scalars(select(Position).where(
        Position.status == "OPEN")).all()]


def _symbols_on_display(db) -> list[str]:
    """Open positions first — they carry money — then actionable signals, then
    the rest of the recent board.

    Settled signals (already executed, squared off, ignored) stay on screen and
    keep a live LTP column, so they need quoting too — they just queue behind
    the rows a decision is still pending on.
    """
    held = _held_symbols(db)
    signals = [s.symbol for s in db.scalars(select(Signal).where(
        Signal.status.in_(_ACTIONABLE)).order_by(Signal.created_at.desc()).limit(300)).all()]
    recent = [s.symbol for s in db.scalars(select(Signal)
                                           .order_by(Signal.created_at.desc()).limit(300)).all()]
    seen: set[str] = set()
    ordered: list[str] = []
    for sym in held + signals + recent:
        if sym not in seen:
            seen.add(sym)
            ordered.append(sym)
    return ordered


def _needs_quote(symbol: str, now: float) -> bool:
    if not STATE.prices.get(symbol):
        return True                                    # no price at all
    return now - _last_quoted.get(symbol, 0.0) > STALE_AFTER_SECONDS


def run_once(db) -> int:
    """Quote the symbols that most need it. Returns how many were updated."""
    # Don't push prices outside market hours — Saturday mock sessions etc.
    if not is_ltp_valid():
        return 0
    now = time.monotonic()
    held = set(_held_symbols(db))
    candidates = [s for s in _symbols_on_display(db) if _needs_quote(s, now)]

    # Two keys, and the order of them matters. Sorting on "has a price" ALONE
    # put every unpriced symbol first — including up to 300 signal rows — which
    # is right for a blank cell but wrong for money: an open position that
    # already had a price sorted to the back, behind the whole board, and with
    # only MAX_QUOTES_PER_PASS quotes a cycle its price could stop being
    # refreshed entirely. Open positions carry PnL and feed the exit engine, so
    # they are quoted first whether or not they currently have a price.
    candidates.sort(key=lambda s: (s not in held, bool(STATE.prices.get(s))))

    updated = 0
    for symbol in candidates[:MAX_QUOTES_PER_PASS]:
        gw.ensure_subscribed(symbol)                   # ask the ticker for it too
        price = (gw.quote(symbol) or {}).get("ltp") or 0.0
        _last_quoted[symbol] = time.monotonic()
        if price > 0:
            STATE.set_price(symbol, price)
            STATE.feed.quotes_ok += 1
            updated += 1
        else:
            # A quote that comes back empty is the signature of an unresolved
            # token or a dead broker session, and it is invisible otherwise:
            # gw.quote() swallows the error and returns a zero LTP that reads
            # exactly like a symbol nobody asked about.
            STATE.feed.quotes_empty += 1
    return updated


def _recheck_broker(db) -> None:
    """Re-run the broker handshake and correct the badge if it has gone stale.

    `STATE.broker_connected` was written once, by the single `connect()` at
    startup, and `is_connected()` only ever returns that cached boolean. Nothing
    revisited it — so a Shoonya session that expired mid-morning left the
    dashboard showing a green broker badge indefinitely while every position sat
    frozen at its entry price. A connection indicator that cannot go red is
    worse than none: it actively argues against the one correct diagnosis.
    """
    # A deliberate Connect/Disconnect wins for a grace period. The gateway caches
    # its liveness probe for 20s, so re-reading /api/status right after an action
    # returns the answer from before it — and this loop would then "correct" a
    # successful login back to disconnected.
    if time.time() - STATE.broker_action_wall < BROKER_ACTION_GRACE_SECONDS:
        return

    global _broker_unreachable
    was = STATE.broker_connected
    status = gw.gateway().status()
    if status is None:
        # Gateway unreachable — a transient blip. Hold the last-known state for a
        # few polls rather than flapping the badge to down (and spamming ERROR
        # audits); only sustained unreachability flips it.
        _broker_unreachable += 1
        if _broker_unreachable < BROKER_UNREACHABLE_GRACE_POLLS:
            return
        now = False
    else:
        # Definitive answer from the WS-primary gateway — trust it at once.
        _broker_unreachable = 0
        now = status
    if now == was:
        return
    STATE.broker_connected = now
    STATE.hub.broadcast("connection", {"broker": now})
    audit(db, "BROKER_RECONNECTED" if now else "BROKER_DISCONNECTED", "",
          "gateway /api/status answered" if now else
          "gateway /api/status did not answer — prices will freeze at their last value",
          level="INFO" if now else "ERROR")


def _refresh_contract_map(db) -> None:
    """Keep the contract map current, loudly.

    Every token, lot size and roll target comes from it, and a token belongs to
    one specific contract — so when the exchange lists a new expiry, only a
    fresh map knows its token. `ensure_fresh` re-downloads once a day; the point
    of doing it here is the alert. A silent failure would leave the system
    resolving against last week's contracts with nothing on screen to say so,
    which is not something real money should ride on.
    """
    from app.services.scripmaster import SCRIPMASTER
    was_stale = SCRIPMASTER.is_stale()
    SCRIPMASTER.ensure_fresh()
    if SCRIPMASTER.is_stale():
        audit(db, "SCRIPMASTER_STALE", "",
              f"contract map is from {SCRIPMASTER.as_of() or 'never'} and would not "
              f"re-download — tokens for newly listed expiries may be missing",
              level="ERROR")
        STATE.hub.broadcast("alert", {
            "type": "SCRIPMASTER_STALE",
            "message": f"Contract map stale ({SCRIPMASTER.as_of() or 'never'}) — refresh it before trading."})
    elif was_stale:
        audit(db, "SCRIPMASTER_REFRESHED", "",
              f"{SCRIPMASTER.count()} contracts as of {SCRIPMASTER.as_of()}")


def _loop() -> None:
    while True:
        db = session()
        # SEPARATE try blocks, deliberately. These were one, with the contract
        # map first — so any failure in it (it downloads over the network, then
        # writes an audit row and broadcasts) skipped run_once for that cycle,
        # and since the same failure recurs every cycle, price warming stopped
        # for the life of the process. Silently: the handler was a bare `pass`.
        # An unrelated subsystem must not be able to freeze every price on the
        # dashboard, so the two now fail independently.
        try:
            _recheck_broker(db)
        except Exception as exc:
            _log.warning("price_warmer: broker re-check failed: %s", exc)
        try:
            _refresh_contract_map(db)
        except Exception as exc:
            _log.warning("price_warmer: contract map refresh failed: %s", exc)
        try:
            run_once(db)
            STATE.feed.warmer_last_error = ""
        except Exception as exc:                       # never let the loop die
            # Recorded rather than swallowed. A warmer that cannot quote is the
            # difference between a stale price and no price at all, and it needs
            # to be visible from /api/summary instead of from a hunch.
            STATE.feed.warmer_last_error = f"{type(exc).__name__}: {exc}"
            _log.warning("price_warmer: pass failed: %s", exc)
        finally:
            STATE.feed.warmer_last_run_wall = time.time()
            db.close()
        # Poll harder while the ticker is down. The websocket backs off up to 15s
        # between reconnect attempts, and at a flat 30s cadence that left a
        # window of up to ~45s with no price for any symbol — long enough for a
        # stop breach to come and go unseen. REST quotes are the only source
        # during that window, so they run at the tighter interval until the
        # stream is back.
        idle = INTERVAL_SECONDS
        if not STATE.feed.ticker_connected:
            idle = TICKER_DOWN_INTERVAL_SECONDS
        time.sleep(idle)


def start() -> None:
    threading.Thread(target=_loop, daemon=True).start()
