"""Mock signal source (development only).

Fabricates plausible Buy/Sell signals on the universe so the dashboard is alive
during development. Enabled with SWING_SIGNAL_SOURCE=mock.
"""
from __future__ import annotations

import random
import threading
import time

from app.core.config import SIGNAL_SOURCE
from app.core.market_hours import now_ist
from app.core.state import STATE
from app.services import gateway_service as gw
from app.services import signals as signal_svc
from db.engine import session

_TIMEFRAMES = ["1H", "4H", "1D"]


def _emit_one() -> None:
    universe = list(gw.universe().keys())
    if not universe:
        return
    symbol = random.choice(universe)
    ltp = STATE.prices.get(symbol) or gw.gateway().get_ltp(symbol) or 100.0
    db = session()
    try:
        # Real signals carry ATR + nearest resistance with the (closed-candle)
        # signal so the system can offer AUTO Target/SL.
        signal_svc.ingest(db, {
            "symbol": symbol,
            "signal_type": "BUY" if random.random() > 0.25 else "SELL",
            "timeframe": random.choice(_TIMEFRAMES),
            "signal_time": now_ist().isoformat(),
            "atr": round(ltp * random.uniform(0.008, 0.02), 2),
            "resistance": round(ltp * random.uniform(1.03, 1.08), 2),
        })
    finally:
        db.close()


def _mock_loop() -> None:
    while True:
        try:
            _emit_one()
        except Exception:
            pass
        time.sleep(random.uniform(6, 14))


def start() -> None:
    if SIGNAL_SOURCE != "mock":
        return
    threading.Thread(target=_mock_loop, daemon=True).start()
