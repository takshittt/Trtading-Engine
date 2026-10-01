"""LIMIT order placement: retries, the OPEN-poll-cancel path, partial fills,
and the paper simulator's limit cap."""
import pytest

from app.brokers.base import BrokerFill
from app.core.state import STATE
from app.services import execution
from app.services import gateway_service as gw
from db.models import Order

SYM = "SBIN-FUT"


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(execution, "_POLL_INTERVAL", 0)
    STATE.prices[SYM] = 800.0


class TestHelpers:
    def test_limit_pays_up_on_buys_and_down_on_sells(self):
        assert execution._limit_from(800.0, SYM, "BUY", 2) == 800.10
        assert execution._limit_from(800.0, SYM, "SELL", 2) == 799.90

    def test_limit_never_below_one_tick(self):
        assert execution._limit_from(0.05, SYM, "SELL", 5) == 0.05

    def test_retry_ladder_widens(self, cfg):
        cfg.slippage_ticks = 2
        assert [execution._attempt_ticks(cfg, a) for a in range(3)] == [2, 4, 6]

    def test_filled_without_a_readable_qty_is_a_full_fill(self):
        # Reading this as 0 re-placed a filled order and bought the position twice.
        fill = BrokerFill("X", "FILLED", 800.0, raw={"qty_filled": 0})
        assert execution._filled_qty(fill, 1500) == 1500

    def test_cancelled_with_partial_fill(self):
        assert execution._filled_qty(BrokerFill("X", "REJECTED", raw={"qty_filled": 300}), 1500) == 300


class FakeGateway:
    """Scripted broker: each place_order pops the next response."""

    def __init__(self, places, polls=()):
        self.places = list(places)
        self.polls = list(polls)
        self.cancelled = []
        self.placed = []

    def get_ltp(self, symbol):
        return 800.0

    def place_order(self, order):
        self.placed.append(order)
        nxt = self.places.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def get_order(self, oid):
        return self.polls.pop(0) if self.polls else BrokerFill(oid, "OPEN")

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return True


class TestPlaceLive:
    def test_fill_against_the_mock_broker(self, db):
        res = execution.place(db, symbol=SYM, side="BUY", qty=1500, intent="ENTRY")
        assert res["status"] == "FILLED" and res["filled_price"] == 800.10
        order = db.get(Order, res["order_id"])
        assert order.status == "FILLED" and order.broker_order_id.startswith("MOCK")

    def test_retries_after_reject_and_widens_the_limit(self, db, monkeypatch):
        fake = FakeGateway([BrokerFill("A", "REJECTED"), RuntimeError("timeout"),
                            BrokerFill("C", "FILLED", 800.30, raw={"qty_filled": 1500})])
        monkeypatch.setattr(gw, "_GATEWAY", fake)
        res = execution.place(db, symbol=SYM, side="BUY", qty=1500, intent="ENTRY")
        assert res["status"] == "FILLED"
        assert [o.price for o in fake.placed] == [800.10, 800.20, 800.30]
        assert db.get(Order, res["order_id"]).retries == 2

    def test_gives_up_after_configured_retries(self, db, cfg, monkeypatch):
        cfg.order_retries = 1
        db.commit()
        fake = FakeGateway([BrokerFill("A", "REJECTED"), BrokerFill("B", "REJECTED")])
        monkeypatch.setattr(gw, "_GATEWAY", fake)
        res = execution.place(db, symbol=SYM, side="BUY", qty=1500, intent="ENTRY")
        assert res["status"] == "REJECTED" and len(fake.placed) == 2

    def test_open_order_is_polled_until_filled(self, db, monkeypatch):
        fake = FakeGateway([BrokerFill("A", "OPEN")],
                           polls=[BrokerFill("A", "OPEN"),
                                  BrokerFill("A", "FILLED", 800.05, raw={"qty_filled": 1500})])
        monkeypatch.setattr(gw, "_GATEWAY", fake)
        res = execution.place(db, symbol=SYM, side="BUY", qty=1500, intent="ENTRY")
        assert res["status"] == "FILLED" and res["filled_price"] == 800.05
        assert fake.cancelled == []

    def test_open_order_is_cancelled_and_partial_fill_honoured(self, db, monkeypatch):
        polls = [BrokerFill("A", "OPEN")] * execution._POLL_TRIES + [
            BrokerFill("A", "REJECTED", 800.05, raw={"qty_filled": 500})]
        fake = FakeGateway([BrokerFill("A", "OPEN")], polls=polls)
        monkeypatch.setattr(gw, "_GATEWAY", fake)
        res = execution.place(db, symbol=SYM, side="BUY", qty=1500, intent="ENTRY")
        assert fake.cancelled == ["A"]
        assert res["status"] == "FILLED" and res["qty_filled"] == 500
        order = db.get(Order, res["order_id"])
        assert order.status == "PARTIAL" and order.qty == 500


class TestPlacePaper:
    def test_buy_lifts_the_ask_without_touching_the_broker(self, db, monkeypatch):
        placed = []
        monkeypatch.setattr(gw.gateway(), "place_order", lambda o: placed.append(o))
        res = execution.place(db, symbol=SYM, side="BUY", qty=1500, intent="ENTRY", paper=True)
        assert res["status"] == "FILLED" and res["filled_price"] == 830.05   # mock ask
        assert placed == []
        assert db.get(Order, res["order_id"]).is_paper

    def test_sell_hits_the_bid(self, db):
        res = execution.place(db, symbol=SYM, side="SELL", qty=1500, intent="SL", paper=True)
        assert res["filled_price"] == 829.95

    def test_book_beyond_the_widest_limit_does_not_fill(self, db, monkeypatch):
        monkeypatch.setattr(gw, "quote", lambda s: {"ltp": 800.0, "bid": 799.0, "ask": 805.0})
        res = execution.place(db, symbol=SYM, side="BUY", qty=1500, intent="ENTRY", paper=True)
        assert res["status"] == "REJECTED" and "not marketable" in res["reason"]

    def test_fill_never_worse_than_the_limit(self, db, monkeypatch):
        # Limits climb 800.10 → 800.20 → 800.30; the third rung reaches the ask,
        # and the fill is the ask itself, not the limit that got there.
        monkeypatch.setattr(gw, "quote", lambda s: {"ltp": 800.0, "bid": 799.9, "ask": 800.25})
        res = execution.place(db, symbol=SYM, side="BUY", qty=1500, intent="ENTRY", paper=True)
        assert res["status"] == "FILLED" and res["filled_price"] == 800.25
        assert db.get(Order, res["order_id"]).retries == 2

    def test_no_price_rejects_instead_of_inventing_a_fill(self, db, monkeypatch):
        monkeypatch.setattr(gw, "quote", lambda s: {"ltp": 0.0, "bid": 0.0, "ask": 0.0})
        res = execution.place(db, symbol=SYM, side="BUY", qty=1500, intent="ENTRY", paper=True)
        assert res["status"] == "REJECTED" and "no live price" in res["reason"]
