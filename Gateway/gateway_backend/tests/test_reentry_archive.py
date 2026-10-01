"""Tests for Option A: archiving a CLOSED round-trip when it is re-entered.

Re-entry recycles the CLOSED lot's row in place, which would otherwise erase the
finished trade from the all-time Order History. place_order snapshots that trade
into ClosedTradeArchive on broker success, and /api/lots/history unions it back
in.

Run:
    poetry run pytest tests/test_reentry_archive.py -v
"""
import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test_archive.db")

import pytest

import app.routers.lots as lots_mod
import app.routers.orders as orders_mod
from app.core.security import Principal
from app.schemas.orders import PlaceOrderRequest
from db.engine import Base, SessionLocal, engine
from db.models import ClosedTradeArchive, LotExit, OrderLot

OWNER = "TESTUID"
_PRINCIPAL = Principal(kind="user")


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


class _FailingBroker:
    async def placeOrder(self, token, credentials, payload):
        return {"stat": "Not_Ok", "emsg": "rejected"}


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(orders_mod, "_require_legacy_auth", lambda: _StubAuth())
    monkeypatch.setattr(orders_mod, "get_broker", lambda name: _StubBroker())
    monkeypatch.setattr(orders_mod, "get_scripmaster", lambda: _StubScripmaster())
    monkeypatch.setattr(orders_mod.asyncio, "create_task", lambda coro: coro.close())
    # History endpoint + its serializers.
    monkeypatch.setattr(lots_mod, "_require_legacy_auth", lambda: _StubAuth())
    monkeypatch.setattr("app.services.lots.get_scripmaster", lambda: _StubScripmaster())
    monkeypatch.setattr("app.services.pnl.get_scripmaster", lambda: _StubScripmaster())
    yield


def _add_closed_lot_with_exit(
    *, realized=1000.0, carried=0.0, exit_price=730.0, qty=100, **kw
) -> int:
    """A CLOSED copper long with one FILLED exit, so avg_exit_price is real."""
    db = SessionLocal()
    opened = datetime.utcnow() - timedelta(days=1, hours=2)
    closed = datetime.utcnow() - timedelta(days=1)
    defaults = dict(
        owner_uid=OWNER, exch="MCX", tsym="COPPER25AUGFUT", token="99",
        lotsize=1, product_type="M", side="B", entry_qty=qty, open_qty=0,
        avg_entry_price=720.0, status="CLOSED",
        client_ref=f"lot{uuid.uuid4().hex[:12]}",
        realized_pnl=realized, carried_pnl=carried,
        opened_at=opened, closed_at=closed,
    )
    defaults.update(kw)
    lot = OrderLot(**defaults)
    db.add(lot)
    db.commit()
    lot_id = lot.id
    db.add(LotExit(
        lot_id=lot_id, exit_qty=qty, filled_qty=qty, avg_exit_price=exit_price,
        client_ref=f"lotexit{uuid.uuid4().hex[:10]}", status="FILLED",
    ))
    db.commit()
    db.close()
    return lot_id


def _reentry_req(**kw) -> PlaceOrderRequest:
    defaults = dict(
        buy_or_sell="B", product_type="M", exchange="MCX",
        tradingsymbol="COPPER25AUGFUT", quantity=100, price_type="LMT", price=735.0,
    )
    defaults.update(kw)
    return PlaceOrderRequest(**defaults)


def test_closed_reentry_archives_the_finished_roundtrip():
    src_id = _add_closed_lot_with_exit(realized=1000.0, carried=200.0)
    asyncio.run(orders_mod.place_order(_reentry_req(reentry_source_lot_id=src_id),
                                       principal=_PRINCIPAL))

    db = SessionLocal()
    archives = db.query(ClosedTradeArchive).all()
    assert len(archives) == 1
    a = archives[0]
    # Snapshot captures the finished trade's own numbers, not the reopened leg's.
    assert a.source_lot_id == src_id
    assert a.owner_uid == OWNER
    assert a.side == "B"
    assert a.entry_qty == 100
    assert a.avg_entry_price == pytest.approx(720.0)
    assert a.avg_exit_price == pytest.approx(730.0)   # from the FILLED exit
    assert a.realized_pnl == pytest.approx(1000.0)    # own booked P&L, not carried
    assert a.carried_pnl == pytest.approx(200.0)
    assert a.opened_at is not None and a.closed_at is not None

    # The live row was recycled forward (carry = prior realized + carried).
    lot = db.query(OrderLot).filter(OrderLot.id == src_id).first()
    assert lot.status == "PENDING"
    assert lot.carried_pnl == pytest.approx(1200.0)
    assert lot.realized_pnl == 0.0


def test_rejected_reentry_writes_no_archive(monkeypatch):
    # Broker rejects → the CLOSED lot is restored, so archiving it would be a
    # duplicate of a still-live record. No archive row must be written.
    monkeypatch.setattr(orders_mod, "get_broker", lambda name: _FailingBroker())
    src_id = _add_closed_lot_with_exit()
    with pytest.raises(Exception):
        asyncio.run(orders_mod.place_order(_reentry_req(reentry_source_lot_id=src_id),
                                           principal=_PRINCIPAL))

    db = SessionLocal()
    assert db.query(ClosedTradeArchive).count() == 0
    lot = db.query(OrderLot).filter(OrderLot.id == src_id).first()
    assert lot.status == "CLOSED"
    assert lot.realized_pnl == pytest.approx(1000.0)


def test_open_reentry_writes_no_archive():
    # Re-entering an OPEN lot makes a fresh row (no recycle) — nothing to archive.
    src_id = _add_closed_lot_with_exit(status="OPEN", open_qty=100,
                                       realized=0.0, carried=0.0)
    asyncio.run(orders_mod.place_order(_reentry_req(reentry_source_lot_id=src_id),
                                       principal=_PRINCIPAL))

    assert SessionLocal().query(ClosedTradeArchive).count() == 0


def test_history_endpoint_unions_archive_with_live_lot():
    src_id = _add_closed_lot_with_exit(realized=1000.0, carried=0.0)
    asyncio.run(orders_mod.place_order(_reentry_req(reentry_source_lot_id=src_id),
                                       principal=_PRINCIPAL))

    rows = lots_mod.get_lots_history(limit=200, offset=0, db=SessionLocal())

    # Two rows: the recycled live lot (positive id) and the archived closed
    # round-trip (negated id).
    ids = sorted(r.id for r in rows)
    assert src_id in ids
    assert -1 in ids or any(r.id < 0 for r in rows)

    archived = next(r for r in rows if r.id < 0)
    assert archived.status == "CLOSED"
    assert archived.realized_pnl == pytest.approx(1000.0)
    assert archived.avg_exit_price == pytest.approx(730.0)

    live = next(r for r in rows if r.id == src_id)
    assert live.status == "PENDING"
    assert live.carried_pnl == pytest.approx(1000.0)


def test_reentry_chain_archives_each_leg_without_double_counting():
    # Round 1 closed +1000; re-enter. Then simulate round 2 closing +500 on the
    # recycled lot and re-enter again. Each archived leg holds only its own
    # realized, so summing realized across archives = the true cumulative.
    src_id = _add_closed_lot_with_exit(realized=1000.0, carried=0.0)
    asyncio.run(orders_mod.place_order(_reentry_req(reentry_source_lot_id=src_id),
                                       principal=_PRINCIPAL))

    # Close round 2 on the recycled lot: realized 500 on top of carried 1000.
    db = SessionLocal()
    lot = db.query(OrderLot).filter(OrderLot.id == src_id).first()
    lot.status = "CLOSED"
    lot.open_qty = 0
    lot.realized_pnl = 500.0
    lot.closed_at = datetime.utcnow()
    db.commit()
    db.close()

    asyncio.run(orders_mod.place_order(_reentry_req(reentry_source_lot_id=src_id),
                                       principal=_PRINCIPAL))

    db = SessionLocal()
    archives = db.query(ClosedTradeArchive).order_by(ClosedTradeArchive.id).all()
    assert len(archives) == 2
    assert archives[0].realized_pnl == pytest.approx(1000.0)
    assert archives[1].realized_pnl == pytest.approx(500.0)
    # Cumulative booked P&L = sum of per-leg realized, no double count.
    assert sum(a.realized_pnl for a in archives) == pytest.approx(1500.0)
    # And the live lot now carries the full running total forward.
    lot = db.query(OrderLot).filter(OrderLot.id == src_id).first()
    assert lot.carried_pnl == pytest.approx(1500.0)
