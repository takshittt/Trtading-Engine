"""Process-wide runtime state: live prices, connection health, WS hub, events."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.timeutil import iso_utc
from db.models import AuditLog


class WSHub:
    """Fan-out WebSocket broadcaster. Pushes JSON events to every dashboard."""

    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: Any) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: Any) -> None:
        self._clients.discard(ws)

    async def _send_all(self, message: dict) -> None:
        dead = []
        payload = json.dumps(message, default=str)
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    def broadcast(self, event: str, data: Any) -> None:
        """Thread-safe broadcast usable from sync background tasks."""
        message = {"event": event, "data": data, "ts": datetime.now(timezone.utc).isoformat()}
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._send_all(message), self._loop)


class PriceFeedStats:
    """Whether prices are actually moving — as opposed to the broker having
    answered once, at boot.

    `broker_connected` is a handshake result, not a heartbeat: it is written by
    a single `/api/status` call during startup and never revisited, so the
    dashboard can show a green broker badge for hours after the session behind
    it died, with every position frozen at its entry price and nothing on screen
    saying so. That is indistinguishable from a quiet market. These counters are
    what tell the two apart.
    """

    def __init__(self) -> None:
        self.last_price_wall: float = 0.0     # time.time() of the newest price
        self.updates: int = 0                 # prices written, ticker + warmer
        self.quotes_ok: int = 0               # REST quotes that returned a price
        self.quotes_empty: int = 0            # ...and ones that returned nothing
        self.warmer_last_run_wall: float = 0.0
        self.warmer_last_error: str = ""
        self.ticker_connected: bool = False

    def as_dict(self) -> dict:
        age = round(time.time() - self.last_price_wall, 1) if self.last_price_wall else None
        return {
            "last_price_at": (datetime.fromtimestamp(
                self.last_price_wall, timezone.utc).isoformat() if self.last_price_wall else None),
            "price_age_seconds": age,
            "updates": self.updates,
            "quotes_ok": self.quotes_ok,
            "quotes_empty": self.quotes_empty,
            "ticker_connected": self.ticker_connected,
            "warmer_last_run_at": (datetime.fromtimestamp(
                self.warmer_last_run_wall, timezone.utc).isoformat()
                if self.warmer_last_run_wall else None),
            "warmer_last_error": self.warmer_last_error,
            # No price has landed in two minutes during a session that is
            # supposed to be live. This is the flag worth alerting on: it is true
            # in exactly the state the broker badge lies about.
            "stalled": age is None or age > 120,
        }


class RuntimeState:
    def __init__(self) -> None:
        self.prices: dict[str, float] = {}          # symbol -> LTP
        self.broker_connected: bool = False
        # When the user last pressed Connect or Disconnect. The background
        # re-check must not overrule a deliberate action while the gateway's own
        # 20-second liveness cache is still serving the pre-action answer —
        # otherwise a successful Connect flickers back to "disconnected" a few
        # seconds later for no reason the user can see.
        self.broker_action_wall: float = 0.0
        self.last_reconcile: str | None = None
        self.feed = PriceFeedStats()
        self.hub = WSHub()

    def set_price(self, symbol: str, ltp: float) -> None:
        self.prices[symbol] = ltp
        # Stamped as a float, formatted only on read: this is the hot path, one
        # call per tick per symbol across the whole subscribed board.
        self.feed.last_price_wall = time.time()
        self.feed.updates += 1


STATE = RuntimeState()


def audit(db: Session, action: str, symbol: str = "", detail: str = "", level: str = "INFO") -> None:
    """Persist an audit line and echo it to the dashboard log stream."""
    entry = AuditLog(action=action, symbol=symbol, detail=detail, level=level)
    db.add(entry)
    db.commit()
    STATE.hub.broadcast("log", {
        "action": action, "symbol": symbol, "detail": detail,
        "level": level, "created_at": iso_utc(entry.created_at),
    })
