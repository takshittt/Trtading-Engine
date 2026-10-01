"""Self-contained simulated Shoonya gateway.

Random-walks a universe of NSE F&O futures so the whole strategy pipeline runs
end-to-end with zero credentials. Fills orders at (near) the requested price and
keeps an internal broker position book that the reconciliation engine reads as
"broker truth" — deliberately allowed to drift occasionally so the every-minute
reconciliation actually has something to catch.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable

from app.brokers.base import BrokerFill, BrokerOrder

# symbol -> (seed_price, lot_size, margin_per_lot, expiry)
UNIVERSE: dict[str, tuple[float, int, float, str]] = {
    "RELIANCE-FUT":  (2950.0, 250, 185_000, "2026-07-31"),
    "TCS-FUT":       (3880.0, 175, 170_000, "2026-07-31"),
    "INFY-FUT":      (1620.0, 400, 145_000, "2026-07-31"),
    "HDFCBANK-FUT":  (1710.0, 550, 210_000, "2026-07-31"),
    "ICICIBANK-FUT": (1230.0, 700, 175_000, "2026-07-31"),
    "SBIN-FUT":      (830.0,  1500, 160_000, "2026-07-31"),
    "TATAMOTORS-FUT":(985.0,  1425, 155_000, "2026-07-31"),
    "AXISBANK-FUT":  (1180.0, 625,  150_000, "2026-07-31"),
    "LT-FUT":        (3640.0, 300,  190_000, "2026-07-31"),
    "MARUTI-FUT":    (12800.0, 50,  180_000, "2026-07-31"),
}


class MockShoonyaGateway:
    name = "mock-shoonya"

    def __init__(self) -> None:
        self._prices: dict[str, float] = {s: v[0] for s, v in UNIVERSE.items()}
        self._book: dict[str, dict] = {}   # symbol -> {qty, avg_price}
        self._orders: dict[str, dict] = {}  # broker_order_id -> {symbol, qty}
        self._connected = False
        self._order_seq = 1000
        self._on_tick: Callable[[str, float], None] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # --- lifecycle -------------------------------------------------------
    def connect(self) -> bool:
        self._connected = True
        return True

    def is_connected(self) -> bool:
        return self._connected

    def disconnect(self) -> dict:
        self._connected = False
        return {"ok": True, "connected": False, "detail": "mock broker disconnected"}

    def connect_force(self) -> dict:
        self._connected = True
        return {"ok": True, "connected": True, "detail": "mock broker connected"}

    def get_user(self) -> dict:
        # A recognisable placeholder, so DEMO cannot be mistaken for a live login.
        return {"uid": "DEMO001", "account_id": "DEMO001", "name": "Demo Account",
                "email": "", "broker": "Mock"}

    # --- market data -----------------------------------------------------
    def get_ltp(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    def next_expiries(self, symbol: str) -> list[dict]:
        """The mock universe lists one contract per symbol, so there is nothing
        to roll into. Returning [] keeps the rollover UI honest offline."""
        return []

    def quote_token(self, exch: str, token: str) -> dict:
        return {"ltp": 0.0, "bid": 0.0, "ask": 0.0}

    def get_quote(self, symbol: str) -> dict:
        """Synthetic book around the mock LTP — one tick wide either side."""
        ltp = self._prices.get(symbol, 0.0)
        if ltp <= 0:
            return {"ltp": 0.0, "bid": 0.0, "ask": 0.0}
        return {"ltp": ltp, "bid": round(ltp - 0.05, 2), "ask": round(ltp + 0.05, 2)}

    def subscribe_prices(self, symbols: list[str], on_tick: Callable[[str, float], None]) -> None:
        from app.core.state import STATE
        self._on_tick = on_tick
        # Report the same health flag the live ticker does, or DEMO shows a dead
        # ticker while prices are visibly moving and the indicator loses meaning.
        STATE.feed.ticker_connected = True
        t = threading.Thread(target=self._price_loop, daemon=True)
        t.start()

    def _price_loop(self) -> None:
        while not self._stop.is_set():
            for sym, price in list(self._prices.items()):
                drift = random.uniform(-0.0015, 0.0016)  # slight upward bias
                new = round(max(1.0, price * (1 + drift)), 2)
                self._prices[sym] = new
                if self._on_tick:
                    try:
                        self._on_tick(sym, new)
                    except Exception:
                        pass
            time.sleep(1.0)

    # --- orders ----------------------------------------------------------
    def place_order(self, order: BrokerOrder) -> BrokerFill:
        with self._lock:
            self._order_seq += 1
            oid = f"MOCK{self._order_seq}"
            ltp = self._prices.get(order.symbol, order.price or 100.0)
            # Market fills at LTP; limit fills at its price if marketable, else LTP.
            fill_price = ltp if order.order_type == "MARKET" else (order.price or ltp)
            self._apply_to_book(order.symbol, order.side, order.qty, fill_price)
            self._orders[oid] = {"symbol": order.symbol, "qty": order.qty,
                                 "price": round(fill_price, 2)}
            return BrokerFill(broker_order_id=oid, status="FILLED", filled_price=round(fill_price, 2))

    def modify_order(self, broker_order_id: str, price: float) -> BrokerFill:
        return BrokerFill(broker_order_id=broker_order_id, status="FILLED", filled_price=price)

    def cancel_order(self, broker_order_id: str) -> bool:
        return True

    def get_order(self, broker_order_id: str) -> BrokerFill:
        """Order status by id. Required by BrokerGateway — execution.place() calls
        it to poll an order the broker left OPEN. The mock fills instantly and so
        never returns OPEN, but the method has to exist for the protocol to hold.
        """
        rec = self._orders.get(broker_order_id)
        if rec is None:
            return BrokerFill(broker_order_id=broker_order_id, status="REJECTED",
                              raw={"error": "unknown order"})
        return BrokerFill(
            broker_order_id=broker_order_id, status="FILLED",
            filled_price=rec.get("price", 0.0),
            raw={"qty_filled": rec["qty"], "avg_price": rec.get("price", 0.0)})

    def _apply_to_book(self, symbol: str, side: str, qty: int, price: float) -> None:
        pos = self._book.get(symbol, {"qty": 0, "avg_price": 0.0})
        signed = qty if side == "BUY" else -qty
        new_qty = pos["qty"] + signed
        if side == "BUY" and pos["qty"] >= 0:
            total = pos["avg_price"] * pos["qty"] + price * qty
            pos["avg_price"] = round(total / new_qty, 2) if new_qty else 0.0
        pos["qty"] = new_qty
        if new_qty == 0:
            pos["avg_price"] = 0.0
        self._book[symbol] = pos

    # --- reconciliation truth -------------------------------------------
    def get_positions(self) -> list[dict]:
        out = []
        for sym, pos in self._book.items():
            if pos["qty"] != 0:
                out.append({"symbol": sym, "qty": pos["qty"], "avg_price": pos["avg_price"]})
        return out

    def get_margin_per_lot(self, symbol: str) -> float:
        return UNIVERSE.get(symbol, (0, 1, 150_000, ""))[2]

    # -- symbol resolution (parity with the live gateway) -----------------
    def resolve(self, symbol: str) -> dict:
        v = UNIVERSE.get(symbol, (0.0, 1, 150_000, ""))
        return {"tsym": symbol, "token": symbol, "lot_size": v[1], "expiry": v[3], "exch": "NFO"}

    def lot_size(self, symbol: str) -> int:
        return UNIVERSE.get(symbol, (0, 1, 0, ""))[1]

    def expiry_of(self, symbol: str) -> str:
        return UNIVERSE.get(symbol, (0, 1, 0, ""))[3]

    def ensure_subscribed(self, symbol: str) -> None:
        return  # mock streams the whole universe already

    def get_funds(self) -> dict:
        """Simulated Shoonya cash balance for the account."""
        used = sum(abs(p["qty"]) for p in self._book.values())  # placeholder proxy
        return {"cash": 2_000_000.0, "margin_available": 2_000_000.0, "margin_used": 0.0}

    def search_symbols(self, query: str, exchange: str = "") -> list[dict]:
        q = (query or "").strip().upper()
        out = []
        for sym, v in UNIVERSE.items():
            if q in sym:
                out.append({"symbol": sym, "tsym": sym, "token": sym, "exch": "NFO",
                            "instr_type": "FUT", "expiry": v[3], "lot_size": v[1],
                            "strike": "", "opttype": "", "ltp": self._prices.get(sym, v[0])})
        return out
