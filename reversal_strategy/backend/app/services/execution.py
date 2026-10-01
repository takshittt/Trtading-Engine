"""Smart Order Module — LIMIT orders only.

Every order (entry, averaging, target, SL, exits, rollover) is a LIMIT order:

    limit price = LTP ± (slippage_ticks × tick_size)
      BUY  -> LTP + slippage   (pay up a little to ensure the fill)
      SELL -> LTP − slippage

If the broker rejects it, retry up to `order_retries` times, widening the
slippage each attempt. Every order is persisted and tracked through its
lifecycle: PLACED -> FILLED / REJECTED. This is the only path to the broker.

Paper orders (`paper=True`) never reach the broker at all. They are filled
against the live top-of-book instead — buys lift the ask, sells hit the bid —
but never past the same limit price a real order would have carried. A book
outside that limit does not fill, exactly as it would not fill live.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.brokers.base import BrokerFill, BrokerOrder
from app.core.config import DEFAULT_TICK_SIZE
from app.core.state import STATE, audit
from app.core.timeutil import as_utc, iso_utc
from app.services import gateway_service as gw
from db.engine import get_config
from db.models import Order

# How long a LIMIT order is given to fill before the remainder is cancelled.
_POLL_TRIES = 10
_POLL_INTERVAL = 0.5


def _filled_qty(fill: BrokerFill, requested: int) -> int:
    """How much of `requested` actually filled.

    A FILLED order whose quantity cannot be read is a *full* fill, not an empty
    one. Reading it as `raw.get("qty_filled", requested)` looked equivalent but
    is not: get_order always writes the key, so a response missing both flqty and
    fillshares stored a literal 0, the default never applied, and a genuinely
    filled order was thrown away and re-placed — buying the same position twice.
    """
    qty = int(fill.raw.get("qty_filled") or 0) if fill.raw else 0
    if qty <= 0 and fill.status == "FILLED":
        return requested
    return qty


def tick_size(symbol: str) -> float:
    return DEFAULT_TICK_SIZE


def _limit_from(ltp: float, symbol: str, side: str, slippage_ticks: int) -> float:
    adj = slippage_ticks * tick_size(symbol)
    px = ltp + adj if side == "BUY" else ltp - adj
    return round(max(tick_size(symbol), px), 2)


def limit_price(symbol: str, side: str, slippage_ticks: int) -> float:
    ltp = STATE.prices.get(symbol) or gw.gateway().get_ltp(symbol)
    return _limit_from(ltp, symbol, side, slippage_ticks)


def _attempt_ticks(cfg, attempt: int) -> int:
    """Slippage allowance on retry `attempt` — the live ladder, shared so the
    paper simulation cannot be more generous than a real order would be."""
    slippage = cfg.slippage_ticks
    return slippage + attempt * max(1, slippage)


def place_paper(db: Session, *, symbol: str, side: str, qty: int, intent: str,
                signal_id: int | None = None, received_at: datetime | None = None) -> dict:
    """Simulate a LIMIT fill against the live book. No broker call is made.

    A BUY lifts the ask and a SELL hits the bid — but never beyond the limit
    price a real order would have carried (LTP ± slippage_ticks, widened once
    per retry, exactly as `place()` does). Without that cap the simulation paid
    whatever the book asked, which is a MARKET order wearing a LIMIT label: a
    wide or stale quote showed up as an enormous "slippage" that the live path
    could never actually have suffered, because the order would simply not have
    filled.

    If the book is one-sided (illiquid contract, feed dry) the fill falls back
    to LTP; with no price at all, or a book beyond the widest limit, the order
    is rejected rather than invented — a fill the strategy could not have got
    must not enter the test data.
    """
    cfg = get_config(db)
    q = gw.quote(symbol)
    ltp, bid, ask = q.get("ltp") or 0.0, q.get("bid") or 0.0, q.get("ask") or 0.0
    touch = (ask if side == "BUY" else bid) or ltp     # what the book is offering

    now = datetime.now(timezone.utc)
    received = as_utc(received_at)
    latency = int((now - received).total_seconds() * 1000) if received else 0

    # Walk the same retry ladder the live path walks and stop at the first
    # attempt whose limit reaches the touch price.
    attempts = cfg.order_retries + 1
    limit = _limit_from(ltp, symbol, side, _attempt_ticks(cfg, 0)) if ltp > 0 else 0.0
    used = 0
    marketable = False
    for attempt in range(attempts):
        limit = _limit_from(ltp, symbol, side, _attempt_ticks(cfg, attempt)) if ltp > 0 else 0.0
        used = attempt
        if limit > 0 and (touch <= limit if side == "BUY" else touch >= limit):
            marketable = True
            break

    # Cross the book, never worse than the limit that got there.
    price = min(touch, limit) if side == "BUY" else max(touch, limit)

    order = Order(symbol=symbol, side=side, order_type="LIMIT", qty=qty,
                  price=round(limit, 2), status="PLACED", intent=intent,
                  is_paper=True, bid=bid, ask=ask, ltp_at_order=ltp, retries=used,
                  signal_id=signal_id, latency_ms=max(0, latency))
    db.add(order)
    db.commit()
    db.refresh(order)

    if touch <= 0 or ltp <= 0:
        order.status = "REJECTED"
        db.commit()
        audit(db, "PAPER_ORDER_REJECTED", symbol,
              f"{side} {qty} — no price available (bid={bid} ask={ask} ltp={ltp})", level="WARN")
        STATE.hub.broadcast("order", serialize(order))
        return {"order_id": order.id, "status": "REJECTED", "filled_price": 0.0,
                "reason": "no live price (bid/ask/LTP all zero)",
                "bid": bid, "ask": ask, "ltp": ltp, "latency_ms": order.latency_ms}

    if not marketable:
        order.status = "REJECTED"
        db.commit()
        audit(db, "PAPER_ORDER_UNFILLED", symbol,
              f"{side} {qty} LIMIT {limit} not marketable after {cfg.order_retries} retries — "
              f"book bid={bid} ask={ask} ltp={ltp} is {abs(touch - limit):.2f} away. "
              f"A real order would have rested unfilled.", level="WARN")
        STATE.hub.broadcast("order", serialize(order))
        return {"order_id": order.id, "status": "REJECTED", "filled_price": 0.0,
                "reason": (f"LIMIT {limit} not marketable — "
                           f"{'ask' if side == 'BUY' else 'bid'} "
                           f"{touch} is {abs(touch - limit):.2f} beyond it"),
                "bid": bid, "ask": ask, "ltp": ltp, "latency_ms": order.latency_ms}

    order.status = "FILLED"
    order.filled_price = round(price, 2)
    order.broker_order_id = f"PAPER-{order.id}"
    db.commit()
    audit(db, "PAPER_ORDER_FILLED", symbol,
          f"{side} {qty} LIMIT {limit} filled @ {order.filled_price} "
          f"({'ask' if side == 'BUY' else 'bid'}) bid={bid} ask={ask} ltp={ltp} "
          f"slip_vs_ltp={round(order.filled_price - ltp, 2)} retries={used} "
          f"lat={order.latency_ms}ms intent={intent}")
    STATE.hub.broadcast("order", serialize(order))
    return {"order_id": order.id, "status": "FILLED", "filled_price": order.filled_price,
            "broker_order_id": order.broker_order_id,
            "bid": bid, "ask": ask, "ltp": ltp, "latency_ms": order.latency_ms}


def place(db: Session, *, symbol: str, side: str, qty: int, intent: str,
          paper: bool = False, signal_id: int | None = None,
          received_at: datetime | None = None) -> dict:
    """Place a LIMIT order with retries. Returns {order_id, status, filled_price}."""
    if paper:
        return place_paper(db, symbol=symbol, side=side, qty=qty, intent=intent,
                           signal_id=signal_id, received_at=received_at)
    cfg = get_config(db)
    gateway = gw.gateway()
    price = limit_price(symbol, side, _attempt_ticks(cfg, 0))

    order = Order(symbol=symbol, side=side, order_type="LIMIT", qty=qty,
                  price=price, status="PLACED", intent=intent)
    db.add(order)
    db.commit()
    db.refresh(order)

    last_status = "REJECTED"
    for attempt in range(cfg.order_retries + 1):
        order.retries = attempt
        # widen slippage on each retry to improve fill odds
        price = limit_price(symbol, side, _attempt_ticks(cfg, attempt))
        order.price = price
        try:
            fill = gateway.place_order(BrokerOrder(
                symbol=symbol, side=side, qty=qty, order_type="LIMIT",
                price=price, intent=intent))
            last_status = fill.status
            
            if fill.status == "OPEN":
                # Poll for up to 5 seconds to await fill
                for _ in range(_POLL_TRIES):
                    time.sleep(_POLL_INTERVAL)
                    poll_fill = gateway.get_order(fill.broker_order_id)
                    if poll_fill.status != "OPEN":
                        fill = poll_fill
                        break

                if fill.status == "OPEN":
                    # Still open after 5s. Cancel the remainder.
                    gateway.cancel_order(fill.broker_order_id)
                    time.sleep(_POLL_INTERVAL)
                    fill = gateway.get_order(fill.broker_order_id)

            qty_filled = _filled_qty(fill, qty)
            # A cancelled order with partial fills acts as a partial fill
            if fill.status == "FILLED" or (fill.status == "REJECTED" and qty_filled > 0):
                if qty_filled > 0:
                    order.status = "FILLED" if qty_filled == qty else "PARTIAL"
                    order.filled_price = fill.filled_price
                    order.broker_order_id = fill.broker_order_id
                    order.qty = qty_filled
                    db.commit()
                    audit(db, "ORDER_FILLED", symbol,
                          f"{side} {qty_filled}/{qty} LIMIT @ {fill.filled_price} (slip {order.retries}) intent={intent}")
                    STATE.hub.broadcast("order", serialize(order))
                    return {"order_id": order.id, "status": "FILLED",
                            "filled_price": fill.filled_price,
                            "broker_order_id": fill.broker_order_id,
                            "qty_filled": qty_filled}
                else:
                    last_status = "CANCELLED_NO_FILL"
        except Exception as exc:  # network/API glitch -> retry
            last_status = f"ERROR:{exc}"
        db.commit()

    order.status = "REJECTED"
    db.commit()
    audit(db, "ORDER_REJECTED", symbol,
          f"{side} {qty} LIMIT after {cfg.order_retries} retries ({last_status})", level="ERROR")
    STATE.hub.broadcast("order", serialize(order))
    return {"order_id": order.id, "status": "REJECTED", "filled_price": 0.0}


def attach_position(db: Session, order_id: int, position_id: int) -> None:
    order = db.get(Order, order_id)
    if order:
        order.position_id = position_id
        db.commit()


def serialize(o: Order) -> dict:
    return {
        "id": o.id, "broker_order_id": o.broker_order_id, "symbol": o.symbol,
        "side": o.side, "order_type": o.order_type, "qty": o.qty, "price": o.price,
        "filled_price": o.filled_price, "status": o.status, "intent": o.intent,
        "retries": o.retries, "is_paper": o.is_paper,
        "bid": o.bid, "ask": o.ask, "ltp_at_order": o.ltp_at_order,
        "signal_id": o.signal_id, "latency_ms": o.latency_ms,
        "created_at": iso_utc(o.created_at),
    }
