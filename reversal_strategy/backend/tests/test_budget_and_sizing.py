"""Budget caps, the averaging reserve, and automated-execution sizing."""
from types import SimpleNamespace

import pytest

from app.services import auto_exec, budget

MARGIN_PER_LOT = 100_000.0    # pinned by conftest._isolate


@pytest.fixture
def small_budget(db, cfg):
    cfg.total_budget = 1_000_000.0      # 10 lots of margin
    cfg.soft_cap_pct = 80.0
    cfg.hard_cap_pct = 90.0
    db.commit()
    return cfg


class TestSnapshot:
    def test_empty_book(self, db, small_budget):
        snap = budget.snapshot(db, small_budget)
        assert snap["utilised"] == 0
        assert snap["reserve"] == 100_000
        assert snap["available_for_new"] == 900_000
        assert snap["cap_state"] == "OK"

    def test_paper_positions_lock_no_live_budget(self, db, small_budget, make_position):
        # Given a margin figure on purpose: the filter, not a zero, must exclude it.
        p = make_position(lots=5, is_paper=True)
        p.margin_used = 500_000.0
        db.commit()
        assert budget.snapshot(db, small_budget)["utilised"] == 0

    @pytest.mark.parametrize("lots,state", [(7, "OK"), (8, "SOFT"), (9, "HARD")])
    def test_cap_states(self, db, small_budget, make_position, lots, state):
        make_position(lots=lots)
        assert budget.snapshot(db, small_budget)["cap_state"] == state

    def test_reserve_in_use(self, db, small_budget, make_position):
        make_position(lots=10)
        snap = budget.snapshot(db, small_budget)
        assert snap["reserve_in_use"] == 100_000
        assert snap["available_for_new"] == 0


class TestCanOpenNew:
    def test_new_entry_below_hard_cap(self, db, small_budget, make_position):
        make_position(lots=7)
        assert budget.can_open_new(db, small_budget, MARGIN_PER_LOT) == (True, "", False)

    def test_new_entry_cannot_reach_the_hard_cap(self, db, small_budget, make_position):
        make_position(lots=8)
        ok, reason, _ = budget.can_open_new(db, small_budget, MARGIN_PER_LOT)
        assert not ok and "hard cap" in reason

    def test_averaging_may_use_the_reserve(self, db, small_budget, make_position):
        make_position(lots=9)
        assert budget.can_open_new(db, small_budget, MARGIN_PER_LOT,
                                   is_averaging=True) == (True, "", True)

    def test_nothing_may_exceed_the_total(self, db, small_budget, make_position):
        make_position(lots=10)
        ok, reason, _ = budget.can_open_new(db, small_budget, MARGIN_PER_LOT, is_averaging=True)
        assert not ok and "100%" in reason


AUTO = dict(total_budget=1_000_000.0, auto_risk_pct=1.0, auto_max_lots=5)


class TestSizeLots:
    def test_risk_based(self):
        # 10_000 risk budget / (10 pts * 250) = 4 lots
        assert auto_exec.size_lots(SimpleNamespace(**AUTO), 1000, 990, 250) == (4, "")

    def test_capped_at_max_lots(self):
        assert auto_exec.size_lots(SimpleNamespace(**AUTO), 1000, 999, 250) == (5, "")

    def test_one_lot_over_budget_risk(self):
        lots, why = auto_exec.size_lots(SimpleNamespace(**AUTO), 1000, 900, 250)
        assert lots == 0 and "over the" in why

    @pytest.mark.parametrize("entry,stop,lot", [(0, 990, 250), (1000, 0, 250),
                                                (1000, 1000, 250), (1000, 990, 0)])
    def test_refuses_unmeasurable_risk(self, entry, stop, lot):
        lots, why = auto_exec.size_lots(SimpleNamespace(**AUTO), entry, stop, lot)
        assert lots == 0 and why


class TestArmed:
    BASE = dict(execution_mode="automated", exit_mode="auto",
                paper_trading=False, signals_halted=False)

    def test_armed(self):
        assert auto_exec.armed(SimpleNamespace(**self.BASE)) == (True, "")

    @pytest.mark.parametrize("override", [
        {"execution_mode": "manual"}, {"exit_mode": "manual"},
        {"paper_trading": True}, {"signals_halted": True},
    ])
    def test_each_switch_stands_it_down(self, override):
        ok, why = auto_exec.armed(SimpleNamespace(**{**self.BASE, **override}))
        assert not ok and why
