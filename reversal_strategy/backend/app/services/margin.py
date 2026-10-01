"""Margin service — the basis for budget/utilisation.

WHY margin, not notional: buying one lot of an NSE stock future does NOT cost
the full contract value; the broker blocks only the SPAN + Exposure margin
(typically ~10-20% of notional). So "budget utilised" must be measured in margin
rupees, otherwise the strategy would think it can hold far fewer lots than it
actually can. Utilisation %, soft/hard caps and the averaging reserve are all
computed off this margin number.

Sources (in priority order), refreshed once per trading day:
  1. NSE SPAN file  — the authoritative daily margin per contract (span_margin +
     exposure). Downloaded each morning; parsed into {symbol: margin_per_lot}.
  2. Broker SPAN calculator (Shoonya `span_calculator`) — live per-position margin.
  3. Static fallback from the mock universe / a configured default.

The table is cached to data/margins.json so a restart mid-day reuses it and a
network hiccup never blocks trading.
"""
from __future__ import annotations

import json
import os
from datetime import date

from app.core.config import BROKER_MODE
from app.core.market_hours import now_ist

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
_CACHE = os.path.join(_DATA_DIR, "margins.json")
_DEFAULT_MARGIN = 150_000.0

# NSE publishes the consolidated daily margin file (F&O) here. The exact filename
# rotates by date; the loader is written so you only swap the URL/parse rule.
NSE_SPAN_URL = os.getenv(
    "NSE_SPAN_URL",
    "https://nsearchives.nseindia.com/archives/nsccl/span/",
)


class MarginTable:
    def __init__(self) -> None:
        self._table: dict[str, float] = {}
        self._as_of: str = ""
        self._load_cache()

    # -- persistence ------------------------------------------------------
    def _load_cache(self) -> None:
        try:
            with open(_CACHE) as fh:
                blob = json.load(fh)
            self._table = {k: float(v) for k, v in blob.get("margins", {}).items()}
            self._as_of = blob.get("as_of", "")
        except Exception:
            self._table, self._as_of = {}, ""

    def _save_cache(self) -> None:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_CACHE, "w") as fh:
            json.dump({"as_of": self._as_of, "margins": self._table}, fh, indent=2)

    # -- lookup -----------------------------------------------------------
    def margin_per_lot(self, symbol: str) -> float:
        if symbol in self._table:
            return self._table[symbol]
        # fall back to the live gateway's per-lot margin, else default
        try:
            from app.services import gateway_service as gw
            m = gw.gateway().get_margin_per_lot(symbol)
            if m:
                return m
        except Exception:
            pass
        return _DEFAULT_MARGIN

    def as_of(self) -> str:
        return self._as_of

    def is_stale(self) -> bool:
        return self._as_of != date.fromisoformat(now_ist().date().isoformat()).isoformat()

    # -- daily refresh ----------------------------------------------------
    def refresh(self) -> dict:
        """Refresh the margin table for today. Returns a small status dict."""
        today = now_ist().date().isoformat()
        try:
            if BROKER_MODE == "shoonya":
                table = self._from_broker_span()
                source = "broker_span"
            else:
                table = self._from_mock()
                source = "mock"
            # NSE SPAN file is the authoritative daily source; layer it on top
            # when available (kept best-effort so a failed download never blocks).
            nse = self._from_nse_span_file()
            if nse:
                table.update(nse)
                source = "nse_span"
            if table:
                self._table = table
                self._as_of = today
                self._save_cache()
            return {"ok": True, "as_of": self._as_of, "source": source, "count": len(self._table)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "as_of": self._as_of}

    def _from_mock(self) -> dict[str, float]:
        from app.brokers.mock_gateway import UNIVERSE
        return {sym: float(v[2]) for sym, v in UNIVERSE.items()}

    def _from_broker_span(self) -> dict[str, float]:
        """Ask Shoonya's SPAN calculator for one lot of each tracked symbol.
        Implemented in the live ShoonyaGateway; falls back to whatever it knows."""
        from app.services import gateway_service as gw
        table: dict[str, float] = {}
        g = gw.gateway()
        for sym in gw.universe().keys():
            try:
                table[sym] = g.get_margin_per_lot(sym)
            except Exception:
                continue
        return table

    def _from_nse_span_file(self) -> dict[str, float]:
        """Download + parse the NSE daily SPAN file into {symbol: margin_per_lot}.

        Left as a best-effort hook: NSE's file format (span_span.spn / PR files)
        and the exact archive filename change periodically, so wire the concrete
        parse here for production. Returning {} means 'use broker/mock source'.
        """
        if os.getenv("SWING_ENABLE_NSE_SPAN", "false").lower() not in ("1", "true", "yes"):
            return {}
        try:
            import requests
            # NOTE: fill in the exact dated filename per NSE's current scheme.
            resp = requests.get(NSE_SPAN_URL, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            # TODO: parse resp.content (SPAN .spn) → {symbol: span+exposure per lot}
            return {}
        except Exception:
            return {}


MARGIN = MarginTable()
