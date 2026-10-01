"""Singleton holder for the broker gateway + symbol helpers.

lot_size / expiry / subscription are delegated to the active gateway so the same
code path works for the mock (static universe) and live Shoonya (resolved via
the gateway's scripmaster/search).
"""
from __future__ import annotations

from app.brokers.shoonya_gateway import build_gateway

_GATEWAY = None


def gateway():
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = build_gateway()
    return _GATEWAY


def universe() -> dict:
    """Mock-only static universe. Empty in live mode (symbols are dynamic)."""
    try:
        from app.core.config import BROKER_MODE
        if BROKER_MODE == "mock":
            from app.brokers.mock_gateway import UNIVERSE
            return UNIVERSE
    except Exception:
        pass
    return {}


def lot_size(symbol: str) -> int:
    try:
        return gateway().lot_size(symbol) or 1
    except Exception:
        return 1


def expiry_of(symbol: str) -> str:
    try:
        return gateway().expiry_of(symbol) or ""
    except Exception:
        return ""


def ensure_subscribed(symbol: str) -> None:
    """Subscribe a symbol to the live price feed (no-op for mock)."""
    try:
        gateway().ensure_subscribed(symbol)
    except Exception:
        pass


def broker_user() -> dict:
    """Identity of the logged-in broker account, or {} when not connected."""
    try:
        return gateway().get_user() or {}
    except Exception:
        return {}


def disconnect() -> dict:
    try:
        return gateway().disconnect()
    except Exception as exc:
        return {"ok": False, "connected": False, "detail": str(exc)}


def connect_force() -> dict:
    try:
        return gateway().connect_force()
    except Exception as exc:
        return {"ok": False, "connected": False, "detail": str(exc)}


def today_ist():
    """Today's date in IST. Re-exported so callers do not have to import a
    private helper out of a specific broker module — auto_rollover did, which
    tied expiry arithmetic that is pure calendar logic to the Shoonya adapter
    and would have broken on any broker refactor."""
    from app.brokers.shoonya_gateway import _today_ist
    return _today_ist()


def parse_expiry(expd: str):
    """Broker expiry string -> date, or None. Same reasoning as today_ist()."""
    from app.brokers.shoonya_gateway import _parse_expiry
    return _parse_expiry(expd)


def quote(symbol: str) -> dict:
    """Top-of-book {ltp, bid, ask}; falls back to the streamed LTP on failure."""
    from app.core.state import STATE
    try:
        q = gateway().get_quote(symbol)
    except Exception:
        q = {"ltp": 0.0, "bid": 0.0, "ask": 0.0}
    if not q.get("ltp"):
        q["ltp"] = STATE.prices.get(symbol.upper(), 0.0)
    return q
