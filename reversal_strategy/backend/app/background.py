"""Background workers: price feed wiring, 1-minute reconciliation, EOD, backup."""
from __future__ import annotations

import shutil
import threading
import time
from datetime import datetime

from app.core.config import IST, DB_PATH
from app.core.market_hours import now_ist
from app.core.state import STATE
from app.services import auto_rollover, exit_engine, mock_signal_feed, price_warmer
from app.services import gateway_service as gw
from app.services.reconciliation import reconcile_once
from db.engine import get_config, session


def _start_price_feed() -> None:
    # The contract map underpins every price, lot size and roll target, so it is
    # loaded before the first subscription rather than lazily on first miss.
    try:
        from app.services.scripmaster import SCRIPMASTER
        SCRIPMASTER.refresh()
    except Exception:
        pass
    gateway = gw.gateway()
    gateway.connect()
    STATE.broker_connected = gateway.is_connected()
    STATE.hub.broadcast("connection", {"broker": STATE.broker_connected})
    # Mock streams its whole universe; live subscribes symbols we already hold
    # (signals subscribe the rest on arrival).
    symbols = list(gw.universe().keys())
    if not symbols:
        db = session()
        try:
            from db.models import Position
            from sqlalchemy import select
            symbols = [p.symbol for p in db.scalars(select(Position).where(Position.status == "OPEN")).all()]
        finally:
            db.close()
    gateway.subscribe_prices(symbols, exit_engine.on_tick)


def _reconcile_loop() -> None:
    while True:
        db = session()
        try:
            cfg = get_config(db)
            interval = max(15, cfg.reconcile_seconds)
        except Exception:
            interval = 60
        finally:
            db.close()
        time.sleep(interval)
        db = session()
        try:
            reconcile_once(db, source="MINUTE")
        except Exception:
            pass
        finally:
            db.close()


def _eod_loop() -> None:
    """After the configured F&O close: full reconciliation + DB backup, once a day.

    Reads the close time from config rather than hardcoding it. The exchange
    already moved once (15:30 -> 15:40) and the session times were made editable
    from the dashboard precisely so the next change would not need a redeploy —
    but this loop kept its own copy, so editing the config would have silently
    left EOD reconciliation firing at the old time. The `hour == 15` test also
    meant any close outside the 15:00 hour would never have fired at all.
    """
    from app.core.market_hours import session_hours
    last_run_date = None
    while True:
        time.sleep(60)
        now = now_ist()
        _, close_t = session_hours()
        if now.time() >= close_t and last_run_date != now.date():
            last_run_date = now.date()
            db = session()
            try:
                reconcile_once(db, source="EOD")
            finally:
                db.close()
            _backup_db()


def _backup_db() -> None:
    try:
        import os
        # File-copy backup only makes sense for the local SQLite file. On MySQL
        # the DB lives in the server (backed up by its own tooling), and DB_PATH
        # points at a file that doesn't exist — so skip cleanly.
        from db.engine import IS_SQLITE
        if not IS_SQLITE:
            return
        src = os.path.abspath(DB_PATH)
        if os.path.exists(src):
            dst = f"{src}.{now_ist():%Y%m%d}.bak"
            shutil.copy2(src, dst)
            db = session()
            try:
                from app.core.state import audit
                audit(db, "DB_BACKUP", "", f"backup -> {dst}")
            finally:
                db.close()
    except Exception:
        pass


def _refresh_margins() -> None:
    """Refresh the NSE/broker margin table (daily basis for budget)."""
    try:
        from app.services.margin import MARGIN
        MARGIN.refresh()
    except Exception:
        pass


def launch() -> None:
    _start_price_feed()
    _refresh_margins()          # daily margin table for budget calc
    mock_signal_feed.start()  # SWING_SIGNAL_SOURCE=mock only
    price_warmer.start()   # keeps LTP real when the ticker is quiet
    auto_rollover.start()  # carries positions off an expiring contract
    threading.Thread(target=_reconcile_loop, daemon=True).start()
    threading.Thread(target=_eod_loop, daemon=True).start()
