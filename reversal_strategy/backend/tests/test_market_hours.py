"""Session and holiday calendar — read on the tick path, edited from the UI."""
from datetime import date, datetime, time
from types import SimpleNamespace

import pytest

from app.core import market_hours as mh
from app.core.config import IST


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    """A known session, with the conftest's forced-open default undone."""
    monkeypatch.setattr(mh, "_force_open", False)
    monkeypatch.setattr(mh, "_open_t", time(9, 15))
    monkeypatch.setattr(mh, "_close_t", time(15, 40))
    monkeypatch.setattr(mh, "_holidays", frozenset({"2026-10-02"}))


def ist(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


class TestParsing:
    def test_parse_hhmm(self):
        assert mh.parse_hhmm("09:30", time(9, 15)) == time(9, 30)

    @pytest.mark.parametrize("raw", [None, "", "9", "25:00", "ab:cd"])
    def test_parse_hhmm_falls_back_instead_of_raising(self, raw):
        assert mh.parse_hhmm(raw, time(9, 15)) == time(9, 15)

    def test_parse_holidays_mixed_separators(self):
        got = mh.parse_holidays("2026-01-26, 2026-03-06\n2026-03-25;2026-04-01")
        assert got == {"2026-01-26", "2026-03-06", "2026-03-25", "2026-04-01"}

    def test_parse_holidays_drops_bad_entries_only(self):
        assert mh.parse_holidays("2026-13-01, 2026-08-15, nope") == {"2026-08-15"}

    def test_parse_holidays_empty(self):
        assert mh.parse_holidays(None) == frozenset()


class TestIsMarketOpen:
    @pytest.mark.parametrize("hh,mm,expected", [
        (9, 14, False), (9, 15, True), (12, 0, True), (15, 40, True), (15, 41, False),
    ])
    def test_session_edges(self, hh, mm, expected):
        assert mh.is_market_open(ist(2026, 10, 1, hh, mm)) is expected   # a Thursday

    def test_weekend_is_closed(self):
        assert mh.is_market_open(ist(2026, 10, 3, 11, 0)) is False       # Saturday

    def test_listed_holiday_is_closed(self):
        assert mh.is_market_open(ist(2026, 10, 2, 11, 0)) is False

    def test_forced_open_overrides_weekend(self, monkeypatch):
        monkeypatch.setattr(mh, "_force_open", True)
        assert mh.is_market_open(ist(2026, 10, 3, 3, 0)) is True

    def test_ltp_validity_tracks_session(self):
        assert mh.is_ltp_valid(ist(2026, 10, 3, 11, 0)) is False


class TestRefreshFromConfig:
    def test_reads_session_and_calendar(self):
        mh.refresh_from_config(SimpleNamespace(
            market_open_time="10:00", market_close_time="15:30",
            market_holidays="2026-10-01", ignore_market_hours=False))
        assert mh.session_hours() == (time(10, 0), time(15, 30))
        assert mh.is_holiday(date(2026, 10, 1))
        assert not mh.is_market_open(ist(2026, 10, 1, 11, 0))

    def test_missing_holidays_column_uses_seed_calendar(self):
        # None (a row predating the column) must not mean "no holidays".
        mh.refresh_from_config(SimpleNamespace(
            market_open_time="09:15", market_close_time="15:40",
            market_holidays=None, ignore_market_hours=False))
        assert mh.is_holiday(date(2026, 8, 15))


class TestMarketStatus:
    def _at(self, monkeypatch, dt):
        monkeypatch.setattr(mh, "now_ist", lambda: dt)

    def test_open(self, monkeypatch):
        self._at(monkeypatch, ist(2026, 10, 1, 11, 0))
        assert mh.market_status() == "OPEN"

    def test_pre_market_and_closed(self, monkeypatch):
        self._at(monkeypatch, ist(2026, 10, 1, 8, 0))
        assert mh.market_status() == "PRE-MARKET"
        self._at(monkeypatch, ist(2026, 10, 1, 16, 0))
        assert mh.market_status() == "CLOSED"

    def test_weekend_names_the_day(self, monkeypatch):
        self._at(monkeypatch, ist(2026, 10, 4, 11, 0))
        assert mh.market_status() == "HOLIDAY (Sunday)"

    def test_forced_open_outside_session_says_so(self, monkeypatch):
        monkeypatch.setattr(mh, "_force_open", True)
        self._at(monkeypatch, ist(2026, 10, 4, 11, 0))
        assert mh.market_status() == "OPEN (forced)"
