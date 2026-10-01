"""Test setup for Reversal Strategy.

Three things have to happen before anything from the app is imported:

* the database URL is pinned to a throwaway SQLite file. `db/engine.py` builds
  its Engine at import time from SWING_DB_URL, and the deployed .env points at
  a shared server database. A test run must never be one stray `db.commit()`
  away from the real book.
* the broker is forced to the in-process mock. The default is the LIVE Shoonya
  gateway, and an order placed by a test must not be able to reach it.
* SWING_IGNORE_MARKET_HOURS is forced off — a dev .env commonly turns it on,
  and that would short-circuit every market-hours test into "open".

`load_dotenv()` does not overwrite variables that already exist, so setting
them here wins over backend/.env.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="swing-test-")
os.environ["SWING_DB_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ["SWING_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["SWING_BROKER_MODE"] = "mock"
os.environ["SWING_SIGNAL_SOURCE"] = "none"
os.environ["SWING_IGNORE_MARKET_HOURS"] = "false"
os.environ["SWING_ENABLE_NSE_SPAN"] = "false"
os.environ["JWT_SECRET"] = "test-secret-not-a-real-key-padded-to-32b"

import pytest  # noqa: E402

from app.brokers.mock_gateway import MockShoonyaGateway  # noqa: E402
from app.core import market_hours  # noqa: E402
from app.core.state import STATE  # noqa: E402
from app.services import exit_engine  # noqa: E402
from app.services import gateway_service as gw  # noqa: E402
from app.services.margin import MARGIN  # noqa: E402
from db import engine as db_engine  # noqa: E402
from db.models import Base  # noqa: E402

MARGIN_PER_LOT = 100_000.0


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fresh schema, fresh mock broker and empty runtime state for every test."""
    assert db_engine.DATABASE_URL.startswith("sqlite:///") and _TMP in db_engine.DATABASE_URL

    Base.metadata.drop_all(db_engine._engine)
    db_engine.init_db()

    gw._GATEWAY = MockShoonyaGateway()
    STATE.prices.clear()
    exit_engine._fail_counts.clear()
    # The portfolio stop is throttled on wall time; tests call it directly.
    monkeypatch.setattr(exit_engine, "_global_last_run", 0.0)
    # data/margins.json on a dev box holds whatever the last refresh fetched;
    # a fixed per-lot margin keeps the budget arithmetic in the tests exact.
    monkeypatch.setattr(MARGIN, "margin_per_lot", lambda symbol: MARGIN_PER_LOT)
    # Default to a session that is open, so tests not about market hours are
    # not at the mercy of the clock they happen to run at.
    monkeypatch.setattr(market_hours, "_force_open", True)
    yield


@pytest.fixture
def db():
    s = db_engine.session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def cfg(db):
    return db_engine.get_config(db)


@pytest.fixture
def market_closed(monkeypatch):
    monkeypatch.setattr(market_hours, "_force_open", False)
    monkeypatch.setattr(market_hours, "is_market_open", lambda dt=None: False)
    # signals.py imported the name directly, so patch it where it is used too.
    from app.services import signals
    monkeypatch.setattr(signals, "is_market_open", lambda dt=None: False)


@pytest.fixture
def make_position(db):
    """Insert an OPEN position directly, bypassing the order path."""
    from db.models import Position

    def _make(symbol="RELIANCE-FUT", avg_price=1000.0, lots=1, lot_size=250,
              target=1100.0, stop_loss=950.0, is_paper=False, **kw):
        pos = Position(symbol=symbol, status="OPEN", avg_price=avg_price,
                       qty=lots * lot_size, lots=lots, lot_size=lot_size,
                       target=target, stop_loss=stop_loss, is_paper=is_paper,
                       margin_used=0.0 if is_paper else MARGIN_PER_LOT * lots,
                       ltp=avg_price, **kw)
        db.add(pos)
        db.commit()
        db.refresh(pos)
        return pos

    return _make
