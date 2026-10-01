"""Carries open positions to the next expiry before the held contract dies.

A futures position left on an expiring contract does not quietly continue — it
settles, and the strategy's view of it silently stops matching the broker's.
When enabled, this rolls each open position to the nearest later expiry once the
held contract is within `auto_rollover_days` of expiring.

Deliberately conservative:

  * Only inside market hours. Rolling is two live orders; placing them into a
    closed book gets rejections, not fills.
  * One attempt per position per day. A contract that cannot be rolled (no later
    expiry listed, a rejected leg) is logged and left alone rather than retried
    into a loop that could fire orders repeatedly.
  * Market orders. A limit that never fills leaves the roll half-done, and the
    whole point is that the position must not be on the near contract tomorrow.
  * P&L and levels are carried, so the rolled position reads as one continuous
    trade — the same default the manual dialog offers.
"""
from __future__ import annotations

import threading
import time
from datetime import date

from sqlalchemy import select

# Via gateway_service, not the broker module directly: expiry arithmetic is
# calendar logic, not Shoonya logic, and importing a private helper across
# the broker abstraction made this module untestable without that adapter.
from app.core.market_hours import is_market_open
from app.core.state import STATE, audit
from app.services import gateway_service as gw
from app.services import positions as pos_svc
from db.engine import get_config, session
from db.models import Position

CHECK_INTERVAL_SECONDS = 300.0

# position id -> date we last tried, so a failure is not retried in a loop.
_attempted: dict[int, date] = {}


def days_to_expiry(expiry: str) -> int | None:
    exp = gw.parse_expiry(expiry)
    return None if exp is None else (exp - gw.today_ist()).days


def due_positions(db) -> list[Position]:
    cfg = get_config(db)
    if not cfg.auto_rollover:
        return []
    today = gw.today_ist()
    due = []
    for p in db.scalars(select(Position).where(Position.status == "OPEN")).all():
        if _attempted.get(p.id) == today:
            continue
        left = days_to_expiry(p.expiry)
        if left is None or left > max(0, cfg.auto_rollover_days):
            continue
        due.append(p)
    return due


def roll_one(db, pos: Position) -> dict:
    """Roll a single position to its nearest later expiry, at market."""
    _attempted[pos.id] = gw.today_ist()
    targets = pos_svc.roll_targets(db, pos.id)
    if not targets:
        audit(db, "AUTO_ROLLOVER_SKIPPED", pos.symbol,
              f"expires {pos.expiry} but no later contract is listed — roll or close manually",
              level="WARN")
        STATE.hub.broadcast("alert", {
            "type": "AUTO_ROLLOVER",
            "message": f"{pos.symbol} expires {pos.expiry} and has no later contract listed."})
        return {"ok": False, "error": "no roll target"}

    target = targets[0]
    res = pos_svc.roll_position(
        db, pos.id, target_tsym=target["tsym"], target_expiry=target.get("expiry", ""),
        qty=pos.qty, price_type="MKT", carry_pnl=True, carry_target=True, reason="AUTO")
    if not res.get("ok"):
        audit(db, "AUTO_ROLLOVER_FAILED", pos.symbol,
              f"-> {target['tsym']}: {res.get('error')}", level="ERROR")
    return res


def run_once(db) -> int:
    if not is_market_open():
        return 0
    rolled = 0
    for pos in due_positions(db):
        try:
            if roll_one(db, pos).get("ok"):
                rolled += 1
        except Exception as exc:
            audit(db, "AUTO_ROLLOVER_ERROR", pos.symbol, str(exc), level="ERROR")
    return rolled


def _loop() -> None:
    while True:
        db = session()
        try:
            run_once(db)
        except Exception:
            pass
        finally:
            db.close()
        time.sleep(CHECK_INTERVAL_SECONDS)


def start() -> None:
    threading.Thread(target=_loop, daemon=True).start()
