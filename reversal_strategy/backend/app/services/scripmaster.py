"""Contract master, fetched straight from Shoonya.

Previously every symbol lookup went through the shared gateway's /api/search,
which reads a scripmaster CSV that strategy does not own. When that file goes
stale the damage is silent and total: contracts that expired weeks ago look
current, the months you would roll into do not exist, and lot sizes drift. It
was a month behind — listing JUN/JUL/AUG on 30 July, when the exchange had
moved to AUG/SEP/OCT.

So this owns its own copy. Shoonya publishes the daily master publicly, no
credentials needed:

    https://api.shoonya.com/NFO_symbols.txt.zip
    Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,
    StrikePrice,TickSize
    NFO,58247,4875,IOC,IOC25AUG26F,25-AUG-2026,FUTSTK,XX,-0.01,0.01

Tokens are the same ones the gateway quotes and streams, so nothing downstream
changes — only the map improves. The file is cached to disk and refreshed once
per trading day; a failed download keeps the last good copy rather than leaving
the system with no map at all.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import threading
import zipfile
from datetime import date, datetime
from urllib.request import Request, urlopen

from app.core.config import IST

_log = logging.getLogger("swing.scripmaster")

URL_TEMPLATE = os.getenv("SWING_SCRIPMASTER_URL", "https://api.shoonya.com/{exch}_symbols.txt.zip")
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
DOWNLOAD_TIMEOUT = 60

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def _today_ist() -> date:
    return datetime.now(IST).date()


def parse_expiry(expd: str) -> date | None:
    if not expd:
        return None
    txt = expd.strip().upper()
    parts = txt.split("-")
    if len(parts) == 3 and parts[1] in _MONTHS:
        try:
            return date(int(parts[2]), _MONTHS[parts[1]], int(parts[0]))
        except ValueError:
            return None
    try:
        return date.fromisoformat(txt)
    except ValueError:
        return None


class Scripmaster:
    """One exchange's contract list, indexed for the two questions we ask:
    "what is the live contract for this root" and "what can it roll into"."""

    def __init__(self, exchange: str = "NFO") -> None:
        self.exchange = exchange
        self._lock = threading.Lock()
        self._rows: list[dict] = []
        self._by_tsym: dict[str, dict] = {}
        self._futs_by_root: dict[str, list[dict]] = {}
        self._as_of: str = ""
        self._load_cache()

    # -- persistence ------------------------------------------------------
    @property
    def _path(self) -> str:
        return os.path.join(_DATA_DIR, f"scripmaster_{self.exchange}.txt")

    def _load_cache(self) -> None:
        try:
            with open(self._path, encoding="utf-8", errors="ignore") as fh:
                self._index(fh.read())
            self._as_of = date.fromtimestamp(os.path.getmtime(self._path)).isoformat()
        except Exception:
            self._rows, self._by_tsym, self._futs_by_root, self._as_of = [], {}, {}, ""

    # -- indexing ---------------------------------------------------------
    def _index(self, text: str) -> None:
        rows: list[dict] = []
        by_tsym: dict[str, dict] = {}
        futs: dict[str, list[dict]] = {}

        for r in csv.DictReader(io.StringIO(text)):
            tsym = (r.get("TradingSymbol") or "").strip().upper()
            if not tsym:
                continue
            instrument = (r.get("Instrument") or "").strip().upper()
            try:
                lot = int(float(r.get("LotSize") or 1))
            except ValueError:
                lot = 1
            row = {
                "tsym": tsym,
                "token": (r.get("Token") or "").strip(),
                "exch": (r.get("Exchange") or self.exchange).strip().upper(),
                "sym": (r.get("Symbol") or "").strip().upper(),
                "expiry": (r.get("Expiry") or "").strip().upper(),
                "instrument": instrument,
                "opttype": (r.get("OptionType") or "").strip().upper(),
                "strike": (r.get("StrikePrice") or "").strip(),
                "lot_size": lot,
            }
            rows.append(row)
            by_tsym[tsym] = row
            # Futures only, keyed by underlying root — this is what resolution
            # and rollover walk. Options stay in `rows` for the search box.
            if "FUT" in instrument and "OPT" not in instrument:
                futs.setdefault(row["sym"], []).append(row)

        for lst in futs.values():
            lst.sort(key=lambda x: parse_expiry(x["expiry"]) or date.max)

        with self._lock:
            self._rows, self._by_tsym, self._futs_by_root = rows, by_tsym, futs

    # -- refresh ----------------------------------------------------------
    def is_stale(self) -> bool:
        return self._as_of != _today_ist().isoformat()

    def refresh(self, force: bool = False) -> dict:
        if not force and not self.is_stale() and self._rows:
            return {"ok": True, "cached": True, "as_of": self._as_of, "count": len(self._rows)}
        url = URL_TEMPLATE.format(exch=self.exchange)
        try:
            req = Request(url, headers={"User-Agent": "reversal-strategy/1.0"})
            with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
                blob = resp.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                text = z.read(z.namelist()[0]).decode("utf-8", "ignore")
        except Exception as exc:
            # Keep whatever is already indexed — a stale map beats no map.
            _log.warning("scripmaster refresh failed (%s); keeping %s rows from %s",
                         exc, len(self._rows), self._as_of or "never")
            return {"ok": False, "error": str(exc), "as_of": self._as_of, "count": len(self._rows)}

        self._index(text)
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            pass                                    # in-memory index is what matters
        self._as_of = _today_ist().isoformat()
        _log.info("scripmaster %s refreshed: %d contracts", self.exchange, len(self._rows))
        return {"ok": True, "as_of": self._as_of, "count": len(self._rows)}

    def ensure_fresh(self) -> None:
        if self.is_stale() or not self._rows:
            self.refresh()

    # -- lookups ----------------------------------------------------------
    def by_tsym(self, tsym: str) -> dict | None:
        return self._by_tsym.get((tsym or "").strip().upper())

    def live_future(self, root: str) -> dict | None:
        """Nearest future of `root` that has not expired."""
        today = _today_ist()
        for row in self._futs_by_root.get((root or "").strip().upper(), []):
            exp = parse_expiry(row["expiry"])
            if exp is not None and exp >= today:
                return row
        return None

    def later_futures(self, root: str, after: date | None) -> list[dict]:
        """Futures of `root` expiring strictly after `after`, nearest first."""
        cutoff = after or _today_ist()
        out = []
        for row in self._futs_by_root.get((root or "").strip().upper(), []):
            exp = parse_expiry(row["expiry"])
            if exp is not None and exp > cutoff:
                out.append(row)
        return out

    def search(self, query: str, limit: int = 50) -> list[dict]:
        """Tradable contracts matching `query`, expired ones excluded.

        Exact-root matches rank above substring hits so a search for IOC does
        not lead with BIOCON, and futures rank above options.
        """
        q = (query or "").strip().upper()
        if len(q) < 2:
            return []
        today = _today_ist()
        exact: list[dict] = []
        partial: list[dict] = []
        for row in self._rows:
            exp = parse_expiry(row["expiry"])
            if exp is not None and exp < today:
                continue
            if row["sym"] == q:
                exact.append(row)
            elif q in row["tsym"] or q in row["sym"]:
                partial.append(row)

        def rank(r: dict) -> tuple:
            is_opt = "OPT" in r["instrument"]
            return (is_opt, parse_expiry(r["expiry"]) or date.max, r["tsym"])

        # Rank the full match set, never a prefix of it. Bounding the scan
        # itself buried the futures: a root like IOC has hundreds of option
        # strikes, and in file order they arrive before the three futures, so
        # an early break returned a page of calls and no contract worth trading.
        return (sorted(exact, key=rank) + sorted(partial, key=rank))[:limit]

    def as_of(self) -> str:
        return self._as_of

    def count(self) -> int:
        return len(self._rows)


SCRIPMASTER = Scripmaster(os.getenv("SWING_EXCHANGE", "NFO"))
