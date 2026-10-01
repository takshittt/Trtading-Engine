"""Tests for clearing a stale per-symbol target on a fresh entry.

`symbol_targets` is keyed on (exch, tsym) and is never disabled when the
position it was set for closes. So a target set for yesterday's position would
silently re-arm today's brand-new position on the same contract (and auto-exit
it at a threshold the user never set for it). place_order now disables any
lingering target when a *fresh* position opens on the symbol — but leaves it
untouched when scaling into a position that is already open.

Run:
    poetry run pytest tests/test_symbol_target_stale_clear.py -v
"""
import asyncio
import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test_gateway.db")

import pytest

import app.routers.orders as orders_mod
from app.core import state
from app.core.security import Principal
from app.schemas.orders import PlaceOrderRequest
from db.engine import Base, SessionLocal, engine
from db.models import OrderLot, SymbolTarget

OWNER = "TESTUID"
_PRINCIPAL = Principal(kind="user")
# _StubScripmaster.get_token always returns "12345", so this is the live-cache key.
_KEY = "NSE|12345"


class _StubAuth:
    user_id = OWNER
    auth_token = "tok"

    def is_authenticated(self):
        return True


class _StubScripmaster:
    master: dict = {}

    def get_token(self, exch, tsym):
        return "12345"


class _StubBroker:
    async def placeOrder(self, token, credentials, payload):
        return {"stat": "Ok", "norenordno": "ORD123"}


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(orders_mod, "_require_legacy_auth", lambda: _StubAuth())
    monkeypatch.setattr(orders_mod, "get_broker", lambda name: _StubBroker())
    monkeypatch.setattr(orders_mod, "get_scripmaster", lambda: _StubScripmaster())
    monkeypatch.setattr(orders_mod.asyncio, "create_task", lambda coro: coro.close())
    state._symbol_targets.clear()
    yield
    state._symbol_targets.clear()


def _add_symbol_target(enabled=True, target_value=10000.0, carried_pnl=0.0):
    db = SessionLocal()
    db.add(SymbolTarget(owner_uid=OWNER, exch="NSE", tsym="RELIANCE-EQ", enabled=enabled,
                        target_value=target_value, carried_pnl=carried_pnl))
    db.commit()
    db.close()
    # Mirror the live cache the tick loop reads.
    state._symbol_targets[_KEY] = {"enabled": enabled, "target_value": target_value,
                                   "carried_pnl": carried_pnl}


def _add_open_lot():
    db = SessionLocal()
    db.add(OrderLot(owner_uid=OWNER, exch="NSE", tsym="RELIANCE-EQ", token="12345",
                    lotsize=1, product_type="M", side="B", entry_qty=100, open_qty=100,
                    avg_entry_price=500.0, status="OPEN", client_ref="lotopen123"))
    db.commit()
    db.close()


def _base_req(**kw) -> PlaceOrderRequest:
    defaults = dict(buy_or_sell="B", product_type="M", exchange="NSE",
                    tradingsymbol="RELIANCE-EQ", quantity=100, price_type="LMT", price=520.0)
    defaults.update(kw)
    return PlaceOrderRequest(**defaults)


def test_fresh_entry_disables_a_stale_symbol_target():
    # Yesterday's target, still enabled, no position currently open.
    _add_symbol_target(enabled=True, target_value=10000.0, carried_pnl=500.0)

    asyncio.run(orders_mod.place_order(_base_req(), principal=_PRINCIPAL))

    st = SessionLocal().query(SymbolTarget).filter_by(exch="NSE", tsym="RELIANCE-EQ").first()
    assert st.enabled is False
    assert st.carried_pnl == 0.0
    # target_value is preserved so the modal still remembers it as a default.
    assert st.target_value == 10000.0
    # Live cache is cleared too, so the tick loop can't fire it before the refresh.
    assert _KEY not in state._symbol_targets


def test_scaling_into_open_position_keeps_the_symbol_target():
    # A live position exists on this symbol; the target is for it.
    _add_open_lot()
    _add_symbol_target(enabled=True, target_value=10000.0)

    asyncio.run(orders_mod.place_order(_base_req(), principal=_PRINCIPAL))

    st = SessionLocal().query(SymbolTarget).filter_by(exch="NSE", tsym="RELIANCE-EQ").first()
    assert st.enabled is True
    assert st.target_value == 10000.0
    assert state._symbol_targets[_KEY]["enabled"] is True


def test_fresh_entry_without_any_symbol_target_is_a_noop():
    # No lingering target at all — placement must still succeed cleanly.
    result = asyncio.run(orders_mod.place_order(_base_req(), principal=_PRINCIPAL))

    assert result["status"] == "success"
    assert SessionLocal().query(SymbolTarget).count() == 0
