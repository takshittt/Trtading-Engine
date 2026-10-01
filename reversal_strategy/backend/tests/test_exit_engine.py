"""The tick-driven exit engine: trailing stops, SL, manual holds, the portfolio stop."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core import market_hours
from app.core.state import STATE
from app.services import exit_engine as ee
from db.models import AuditLog, Position

SYM = "RELIANCE-FUT"


def tick(db, ltp, symbol=SYM):
    STATE.set_price(symbol, ltp)      # exit_full prices its order off STATE
    ee._check_symbol(db, symbol, ltp)
    db.expire_all()


@pytest.fixture
def pos(make_position):
    # avg 1000, target 1100, stop 950, 1 lot of 250; trailing buffer 1%.
    return make_position(symbol=SYM)


class TestTargetAndTrailing:
    def test_target_hit_arms_the_trail_and_does_not_sell(self, db, pos):
        tick(db, 1100.0)
        p = db.get(Position, pos.id)
        assert p.status == "OPEN" and p.target_hit and p.trailing_active
        assert p.trailing_sl == 1089.0

    def test_trail_follows_new_highs(self, db, pos):
        tick(db, 1100.0)
        tick(db, 1200.0)
        p = db.get(Position, pos.id)
        assert p.trail_peak == 1200.0 and p.trailing_sl == 1188.0

    def test_falling_to_the_trail_exits(self, db, pos):
        tick(db, 1100.0)
        tick(db, 1200.0)
        tick(db, 1188.0)
        p = db.get(Position, pos.id)
        assert p.status == "CLOSED" and p.exit_reason == "TRAIL" and p.realized_pnl > 0

    def test_trail_exits_even_in_manual_exit_mode(self, db, cfg, pos):
        cfg.exit_mode = "manual"
        db.commit()
        tick(db, 1100.0)
        tick(db, 1080.0)
        assert db.get(Position, pos.id).status == "CLOSED"


class TestStopLoss:
    def test_auto_mode_squares_off(self, db, pos):
        tick(db, 949.0)
        p = db.get(Position, pos.id)
        assert p.status == "CLOSED" and p.exit_reason == "SL"

    def test_manual_mode_flags_and_sells_nothing(self, db, cfg, pos):
        cfg.exit_mode = "manual"
        db.commit()
        tick(db, 949.0)
        p = db.get(Position, pos.id)
        assert p.status == "OPEN" and p.sl_breached and p.sl_breached_at
        assert db.scalar(select(AuditLog).where(AuditLog.action == "SL_HIT_HELD"))

    def test_recovery_clears_the_flag(self, db, cfg, pos):
        cfg.exit_mode = "manual"
        db.commit()
        tick(db, 949.0)
        tick(db, 990.0)
        assert not db.get(Position, pos.id).sl_breached

    def test_excursions_are_tracked(self, db, pos):
        tick(db, 1050.0)
        tick(db, 960.0)
        p = db.get(Position, pos.id)
        assert (p.peak_price, p.trough_price) == (1050.0, 960.0)


@pytest.mark.parametrize("trailing", [False, True])
def test_one_books_cooldown_does_not_starve_the_other(db, make_position, trailing):
    # Paper row first, so it is the one the loop meets first. A `return` on its
    # cooldown used to skip the live row on the same symbol entirely.
    extra = dict(trailing_active=True, trail_peak=1200.0, trailing_sl=1188.0) if trailing else {}
    paper = make_position(symbol=SYM, is_paper=True, **extra)
    live = make_position(symbol=SYM, **extra)
    paper.exit_failed_at = datetime.now(timezone.utc)
    db.commit()
    tick(db, 1100.0 if trailing else 949.0)
    assert db.get(Position, live.id).status == "CLOSED"
    assert db.get(Position, paper.id).status == "OPEN"


class TestFailedExits:
    def test_cooldown_works_with_naive_db_timestamps(self, db, pos):
        # SQLite returns naive datetimes; subtracting from an aware now() used to
        # raise on every tick and silently disable exits for the symbol.
        pos.exit_failed_at = datetime.now(timezone.utc)
        db.commit()
        db.expire_all()
        p = db.get(Position, pos.id)
        assert p.exit_failed_at.tzinfo is None
        assert ee._is_cooling_down(p) is True
        p.exit_failed_at = datetime.now(timezone.utc) - timedelta(seconds=ee.EXIT_COOLDOWN_SEC + 1)
        assert ee._is_cooling_down(p) is False

    def test_failed_exit_is_recorded_and_retried_after_cooldown(self, db, pos, monkeypatch):
        from app.services import positions as pos_svc
        monkeypatch.setattr(pos_svc, "exit_full", lambda *a, **k: {"ok": False})
        tick(db, 949.0)
        p = db.get(Position, pos.id)
        assert p.status == "OPEN" and p.exit_retries == 1 and p.exit_failed_at
        tick(db, 948.0)                                  # inside the cooldown: no retry
        assert db.get(Position, pos.id).exit_retries == 1

    def test_on_tick_counts_failures_and_alerts(self, monkeypatch):
        alerts = []
        monkeypatch.setattr(ee, "_check_symbol", lambda *a: 1 / 0)
        monkeypatch.setattr(STATE.hub, "broadcast", lambda ev, data: alerts.append((ev, data)))
        for _ in range(ee._FAIL_ALERT_AT):
            ee.on_tick(SYM, 1000.0)
        assert ee._fail_counts[SYM] == ee._FAIL_ALERT_AT
        assert any(ev == "alert" and d["type"] == "EXIT_ENGINE_DOWN" for ev, d in alerts)

    def test_on_tick_ignores_out_of_session_prices(self, monkeypatch):
        monkeypatch.setattr(ee, "is_ltp_valid", lambda: False)
        ee.on_tick(SYM, 1.0)
        assert SYM not in STATE.prices


class TestSweepManualHolds:
    def test_exits_breached_and_clears_recovered(self, db, cfg, make_position):
        under = make_position(symbol=SYM)
        recovered = make_position(symbol="TCS-FUT", lot_size=175)
        cfg.exit_mode = "manual"
        db.commit()
        tick(db, 940.0)
        tick(db, 940.0, symbol="TCS-FUT")
        STATE.set_price("TCS-FUT", 1010.0)               # recovered, no tick yet
        assert ee.sweep_manual_holds(db) == 1
        db.expire_all()
        assert db.get(Position, under.id).status == "CLOSED"
        r = db.get(Position, recovered.id)
        assert r.status == "OPEN" and not r.sl_breached


class TestGlobalSL:
    @pytest.fixture
    def budget(self, db, cfg):
        cfg.total_budget, cfg.global_sl_pct = 100_000.0, 5.0     # stop at -5,000
        db.commit()
        return cfg

    def test_open_drawdown_squares_off_and_halts(self, db, budget, make_position):
        p = make_position(symbol=SYM, stop_loss=0.0)
        STATE.set_price(SYM, 979.0)                      # -21 * 250 = -5,250
        ee._check_global_sl(db)
        db.expire_all()
        assert db.get(Position, p.id).exit_reason == "GLOBAL_SL"
        assert budget.signals_halted

    def test_booked_loss_today_counts(self, db, budget, make_position):
        closed = make_position(symbol="TCS-FUT", lot_size=175)
        closed.status, closed.realized_pnl = "CLOSED", -4_000.0
        closed.closed_at = datetime.now(timezone.utc)
        open_ = make_position(symbol=SYM, stop_loss=0.0)
        db.commit()
        STATE.set_price(SYM, 995.0)                      # -1,250 open
        ee._check_global_sl(db)
        db.expire_all()
        assert db.get(Position, open_.id).status == "CLOSED"
        assert budget.signals_halted

    def test_flat_book_down_on_the_day_still_halts(self, db, budget, make_position):
        closed = make_position(symbol=SYM)
        closed.status, closed.realized_pnl = "CLOSED", -6_000.0
        closed.closed_at = datetime.now(timezone.utc)
        db.commit()
        ee._check_global_sl(db)
        assert budget.signals_halted

    def test_within_threshold_does_nothing(self, db, budget, make_position):
        p = make_position(symbol=SYM, stop_loss=0.0)
        STATE.set_price(SYM, 990.0)                      # -2,500
        ee._check_global_sl(db)
        assert db.get(Position, p.id).status == "OPEN" and not budget.signals_halted

    def test_paper_drawdown_never_fires_it(self, db, budget, make_position):
        p = make_position(symbol=SYM, is_paper=True, stop_loss=0.0)
        STATE.set_price(SYM, 500.0)
        ee._check_global_sl(db)
        assert db.get(Position, p.id).status == "OPEN" and not budget.signals_halted


def test_conftest_leaves_the_market_forced_open():
    # Guard for the fixtures above: if this flips, every tick test is a no-op.
    assert market_hours.is_ltp_valid()
