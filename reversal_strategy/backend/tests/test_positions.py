"""Entries, averaging, level validation and exits, against the mock broker."""
import pytest
from sqlalchemy import select

from app.core.state import STATE
from app.services import positions as pos_svc
from app.services import signals
from db.models import AuditLog, Position, Signal

SYM = "RELIANCE-FUT"        # lot size 250 in the mock universe


@pytest.fixture(autouse=True)
def _price():
    STATE.prices[SYM] = 1000.0


def buy(db, lots=1, **kw):
    args = dict(symbol=SYM, lots=lots, target_mode="auto", target_method="atr",
                target_value=0.0, sl_mode="auto", sl_method="atr", sl_value=0.0, atr=10.0)
    return pos_svc.open_position(db, **{**args, **kw})


class TestOpen:
    def test_entry_fills_and_prices_levels_off_the_fill(self, db):
        res = buy(db)
        assert res["ok"], res
        p = res["position"]
        assert p["avg_price"] == 1000.10                  # LTP + 2 ticks
        assert p["qty"] == 250 and p["lots"] == 1
        assert (p["target"], p["stop_loss"]) == (1020.10, 985.10)
        assert p["margin_used"] == 100_000 and p["opened_by"] == "manual"

    def test_repeat_buy_averages_instead_of_opening_a_second_row(self, db):
        buy(db)
        STATE.prices[SYM] = 980.0
        res = buy(db)
        assert res["averaged"] and res["ok"]
        rows = db.scalars(select(Position).where(Position.status == "OPEN")).all()
        assert len(rows) == 1
        assert rows[0].qty == 500 and rows[0].averaging_count == 1
        assert rows[0].avg_price == 990.10

    def test_paper_and_live_are_separate_books(self, db):
        buy(db)
        res = buy(db, is_paper=True)
        assert "averaged" not in res and res["position"]["is_paper"]
        assert res["position"]["margin_used"] == 0

    def test_budget_hard_cap_blocks_the_entry(self, db, cfg):
        cfg.total_budget = 100_000.0       # one lot of margin = 100% > 90% hard cap
        db.commit()
        res = buy(db)
        assert not res["ok"] and "hard cap" in res["error"]
        assert db.scalars(select(Position)).all() == []

    def test_manual_stop_above_the_fill_is_dropped_not_applied(self, db):
        res = buy(db, sl_mode="manual", sl_value=1005.0)
        assert res["ok"] and res["position"]["stop_loss"] == 0.0
        assert db.scalar(select(AuditLog).where(AuditLog.action == "LEVELS_REJECTED"))

    def test_entry_settles_its_signal(self, db):
        sig = signals.manual_add(db, SYM)
        res = buy(db, signal_id=sig.id)
        db.refresh(sig)
        assert sig.status == "ACTED" and sig.position_id == res["position"]["id"]
        assert res["position"]["signal_price"] == 1000.0


class TestAverage:
    def test_limit_reached(self, db, cfg):
        cfg.max_averaging_buys = 1
        db.commit()
        buy(db)
        assert buy(db)["ok"]
        res = buy(db)
        assert not res["ok"] and res["error"] == "Averaging limit reached"

    def test_averaging_resets_the_trailing_phase(self, db):
        pid = buy(db)["position"]["id"]
        pos = db.get(Position, pid)
        pos.trailing_active, pos.target_hit, pos.trailing_sl = True, True, 1010.0
        db.commit()
        pos_svc.average_position(db, pid, 1)
        db.refresh(pos)
        assert not pos.trailing_active and pos.trailing_sl == 0.0

    def test_manual_levels_survive_averaging(self, db):
        pid = buy(db, target_mode="manual", target_value=1200.0,
                  sl_mode="manual", sl_value=900.0)["position"]["id"]
        p = pos_svc.average_position(db, pid, 1)["position"]
        assert (p["target"], p["stop_loss"]) == (1200.0, 900.0)


@pytest.mark.parametrize("tgt,sl,ok", [
    (1100, 950, True), (0, 0, True), (1100, 1000, False), (1000, 950, False), (1100, 1200, False),
])
def test_validate_levels(tgt, sl, ok):
    assert (pos_svc._validate_levels(1000.0, tgt, sl) == "") is ok


class TestExit:
    def test_full_exit_books_pnl_and_frees_margin(self, db):
        pid = buy(db)["position"]["id"]
        STATE.prices[SYM] = 1050.0
        res = pos_svc.exit_full(db, pid)
        assert res["ok"]
        pos = db.get(Position, pid)
        assert pos.status == "CLOSED" and pos.exit_reason == "MANUAL"
        assert pos.realized_pnl == round((1049.90 - 1000.10) * 250, 2)
        assert pos.margin_used == 0 and pos.exit_qty == 250 and pos.qty == 0

    def test_exit_of_a_closed_position(self, db):
        pid = buy(db)["position"]["id"]
        pos_svc.exit_full(db, pid)
        assert pos_svc.exit_full(db, pid) == {"ok": False, "error": "Position not open"}

    def test_partial_always_leaves_one_lot(self, db):
        pid = buy(db, lots=3)["position"]["id"]
        res = pos_svc.exit_partial(db, pid, 10)
        assert res["ok"] and res["position"]["lots"] == 1
        assert res["position"]["status"] == "OPEN"
        assert res["position"]["margin_used"] == 100_000

    def test_partial_refused_on_one_lot(self, db):
        pid = buy(db)["position"]["id"]
        assert not pos_svc.exit_partial(db, pid, 1)["ok"]

    def test_exit_survives_a_zero_lot_size(self, db, make_position):
        # A day the scripmaster returned nothing leaves lot_size=0 on the row;
        # the exit path divides by it.
        pos = make_position(symbol=SYM, lots=1, lot_size=250)
        pos.lot_size = 0
        db.commit()
        assert pos_svc.exit_full(db, pos.id)["ok"]
        assert db.get(Position, pos.id).lot_size == 250       # re-resolved from the broker

    def test_edit_targets_marks_levels_manual(self, db):
        pid = buy(db)["position"]["id"]
        p = pos_svc.edit_targets(db, pid, 1300.0, None)["position"]
        assert p["target"] == 1300.0 and p["target_method"] == "manual"
        assert p["sl_method"] == "atr"


class TestSerialize:
    def test_closed_trade_shows_no_unrealized(self, db):
        pid = buy(db)["position"]["id"]
        pos_svc.exit_full(db, pid)
        STATE.prices[SYM] = 1200.0
        out = pos_svc.serialize(db.get(Position, pid))
        assert out["unrealized_pnl"] == 0 and out["unrealized_pct"] == 0

    def test_realized_pct_is_over_the_whole_cost_basis(self, db, make_position):
        pos = make_position(symbol=SYM, avg_price=1000.0, lots=2)
        pos.exit_qty, pos.qty, pos.realized_pnl = 500, 0, 5000.0
        pos.status = "CLOSED"
        db.commit()
        assert pos_svc.serialize(pos)["realized_pct"] == 1.0

    def test_timestamps_carry_utc_offset(self, db):
        pid = buy(db)["position"]["id"]
        db.expire_all()
        out = pos_svc.serialize(db.get(Position, pid))
        assert out["opened_at"].endswith("+00:00")
