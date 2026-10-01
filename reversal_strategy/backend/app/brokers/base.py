"""Broker gateway interface — the isolation seam to Shoonya.

Everything the strategy needs from the broker goes through this narrow surface.
The mock implementation makes the whole pipeline runnable with no credentials;
the live Shoonya adapter implements the same methods over the Noren API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass
class BrokerOrder:
    symbol: str
    side: str            # BUY / SELL
    qty: int
    order_type: str      # LIMIT / MARKET
    price: float = 0.0
    intent: str = "ENTRY"


@dataclass
class BrokerFill:
    broker_order_id: str
    status: str          # FILLED / REJECTED / PENDING
    filled_price: float = 0.0
    raw: dict = field(default_factory=dict)


class BrokerGateway(Protocol):
    """Dedicated Shoonya order gateway for this strategy (isolated instance)."""

    name: str

    def connect(self) -> bool: ...

    def is_connected(self) -> bool: ...

    def place_order(self, order: BrokerOrder) -> BrokerFill: ...

    def modify_order(self, broker_order_id: str, price: float) -> BrokerFill: ...

    def cancel_order(self, broker_order_id: str) -> bool: ...

    def get_order(self, broker_order_id: str) -> BrokerFill: ...

    def get_ltp(self, symbol: str) -> float: ...

    def get_quote(self, symbol: str) -> dict:
        """Top-of-book snapshot: {ltp, bid, ask}. Missing legs come back 0.0.

        Paper fills cross the spread off this, so a simulated entry pays the ask
        and a simulated exit hits the bid — the same prices a market order would
        have touched.
        """
        ...

    def get_positions(self) -> list[dict]:
        """Broker truth for 1-minute reconciliation. Each dict: symbol, qty, avg_price."""
        ...

    def get_margin_per_lot(self, symbol: str) -> float:
        """Daily margin requirement for one lot of `symbol`."""
        ...

    def get_funds(self) -> dict:
        """Broker account funds. Keys: cash, margin_available, margin_used."""
        ...

    def search_symbols(self, query: str) -> list[dict]:
        """Search tradable symbols. Each dict: symbol, lot_size, expiry."""
        ...

    def subscribe_prices(self, symbols: list[str], on_tick: Callable[[str, float], None]) -> None:
        """Start the LTP stream. on_tick(symbol, ltp) is called on every tick."""
        ...
