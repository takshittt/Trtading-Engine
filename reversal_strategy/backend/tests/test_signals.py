"""Signal ingest gates, and the re-gating/re-pricing done on every read."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.state import STATE
from app.services import signals
from db.models import AuditLog, Blacklist, Signal

SYM = "RELIANCE-FUT"


def _old_bar(hours=3) -> str:
    """A candle that closed well before now, as naive UTC like the bridge sends."""
    t = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    return t.replace(minute=0, second=0, microsecond=0).isoformat()


def payload(**kw) -> dict:
    base = {"symbol": SYM, "signal_type": "BUY", "timeframe": "1H",
            "signal_time": _old_bar(), "atr": 10.0, "resistance": 1060.0, "support": 980.0}
    return {**base, **kw}


@pytest.fixture(autouse=True)
def _price():
    STATE.prices[SYM] = 1000.0


class TestCandleId:
    T = datetime(2026, 10, 1, 11, 37, 12)

    def test_1h_buckets_to_the_hour(self):
        assert signals._candle_id("X", "1H", self.T) == "X|1H|2026-10-01T11:00:00"

    def test_4h_buckets_to_four_hour_blocks(self):
        assert signals._candle_id("X", "4H", self.T) == "X|4H|2026-10-01T08:00:00"

    def test_1d_buckets_to_midnight(self):
        assert signals._candle_id("X", "1D", self.T) == "X|1D|2026-10-01T00:00:00"

    def test_candle_still_open_accepts_naive_db_times(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert signals._candle_still_open("1H", now) is True
        assert signals._candle_still_open("1H", now - timedelta(hours=2)) is False


class TestIngest:
    def test_new_buy_priced_from_support_and_resistance(self, db):
        sig = signals.ingest(db, payload())
        assert sig.status == "NEW"
        assert (sig.target, sig.stop_loss) == (1060.0, 980.0)
        assert sig.signal_rr == 3.0
        assert sig.signal_ltp == 1000.0
        assert sig.candle_closed is True
        assert sig.lot_size == 250 and sig.expiry == "2026-07-31"   # from the mock universe

    def test_duplicate_candle_is_dropped(self, db):
        assert signals.ingest(db, payload()) is not None
        assert signals.ingest(db, payload()) is None
        assert len(db.scalars(select(Signal)).all()) == 1

    def test_same_candle_opposite_side_is_not_a_duplicate(self, db):
        signals.ingest(db, payload())
        assert signals.ingest(db, payload(signal_type="SELL")) is not None

    def test_outside_market_hours_is_stale(self, db, market_closed):
        sig = signals.ingest(db, payload())
        assert sig.status == "STALE" and "outside market hours" in sig.note

    def test_blacklisted(self, db):
        db.add(Blacklist(symbol=SYM))
        db.commit()
        assert signals.ingest(db, payload()).status == "BLACKLISTED"

    def test_halted(self, db, cfg):
        cfg.signals_halted = True
        db.commit()
        sig = signals.ingest(db, payload())
        assert sig.status == "STALE" and "halted" in sig.note

    def test_buy_on_held_stock_is_averaging(self, db, make_position):
        make_position(symbol=SYM, averaging_count=1)
        sig = signals.ingest(db, payload())
        assert sig.status == "AVERAGING" and "#2" in sig.note

    def test_paper_holding_does_not_tag_averaging(self, db, make_position):
        make_position(symbol=SYM, is_paper=True)
        assert signals.ingest(db, payload()).status == "NEW"

    def test_low_rr_is_stored_but_not_actionable(self, db):
        # 1000 -> target 1010 / stop 980 is 0.5 R:R, below the 1.5 default.
        sig = signals.ingest(db, payload(resistance=1010.0))
        assert sig.status == "LOW_RR" and sig.signal_rr == 0.5
        assert sig.status not in signals.ACTIONABLE

    def test_sell_carries_no_long_levels(self, db):
        sig = signals.ingest(db, payload(signal_type="SELL"))
        assert (sig.target, sig.stop_loss) == (0.0, 0.0)

    def test_forming_candle_is_recorded_and_warned(self, db):
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        sig = signals.ingest(db, payload(signal_time=now))
        assert sig.candle_closed is False
        log = db.scalar(select(AuditLog).where(AuditLog.action == "SIGNAL_RECEIVED"))
        assert log.level == "WARN" and "INTRA-CANDLE" in log.detail


class TestRefresh:
    def test_stale_signal_is_promoted_once_the_market_opens(self, db, monkeypatch):
        monkeypatch.setattr(signals, "is_market_open", lambda: False)
        sig = signals.ingest(db, payload())
        assert sig.status == "STALE"
        monkeypatch.setattr(signals, "is_market_open", lambda: True)
        assert signals.refresh(db, [sig])[0].status == "NEW"

    def test_stale_stays_stale_while_closed(self, db, monkeypatch):
        monkeypatch.setattr(signals, "is_market_open", lambda: False)
        sig = signals.ingest(db, payload())
        assert signals.refresh(db, [sig])[0].status == "STALE"

    def test_levels_stay_anchored_to_the_signal_price(self, db):
        sig = signals.ingest(db, payload(resistance=0.0, support=0.0))   # ATR-based
        before = (sig.target, sig.stop_loss)
        STATE.prices[SYM] = 1040.0
        signals.refresh(db, [sig])
        assert sig.ltp == 1040.0
        # Re-deriving from the live price would have walked the target up to 1060.
        assert (sig.target, sig.stop_loss) == before == (1020.0, 985.0)

    def test_raising_min_rr_demotes_signals_already_on_the_board(self, db, cfg):
        sig = signals.ingest(db, payload())             # R:R 3.0
        cfg.min_rr = 4.0
        db.commit()
        assert signals.refresh(db, [sig])[0].status == "LOW_RR"

    def test_settled_signals_keep_their_record(self, db):
        sig = signals.ingest(db, payload())
        sig.status, sig.note = "ACTED", "bought"
        db.commit()
        STATE.prices[SYM] = 1100.0
        signals.refresh(db, [sig])
        assert (sig.status, sig.note) == ("ACTED", "bought")
        assert sig.ltp == 1100.0                        # live price still shown
        assert sig.target == 1060.0                     # levels frozen

    def test_holding_bought_after_the_signal_turns_it_into_averaging(self, db, make_position):
        sig = signals.ingest(db, payload())
        make_position(symbol=SYM)
        assert signals.refresh(db, [sig])[0].status == "AVERAGING"


class TestEssential:
    def _sig(self, **kw):
        return Signal(**{"symbol": SYM, "signal_type": "BUY", "status": "NEW", **kw})

    def test_low_rr_buy_is_not_essential(self):
        assert not signals.is_essential(self._sig(status="LOW_RR"), set())

    def test_sell_only_matters_when_held(self):
        assert not signals.is_essential(self._sig(signal_type="SELL"), set())
        assert signals.is_essential(self._sig(signal_type="SELL"), {SYM})


def test_manual_add_normalises_and_prices(db):
    sig = signals.manual_add(db, " reliance-fut ", signal_type="hold")
    assert sig.symbol == SYM and sig.signal_type == "BUY"
    assert sig.manual_add and sig.status == "NEW"
    assert sig.target == 1040.0 and sig.stop_loss == 980.0     # % fallback, no S&R


def test_serialize_rr_is_live_but_signal_rr_is_frozen(db):
    sig = signals.ingest(db, payload())
    STATE.prices[SYM] = 1040.0
    signals.refresh(db, [sig])
    out = signals.serialize(sig, held=set())
    assert out["signal_rr"] == 3.0
    assert out["rr"] == 0.33                             # 20 reward / 60 risk at 1040
    assert out["move_pct"] == 4.0
    assert out["essential"] is True
    assert out["signal_time"].endswith("+00:00")
