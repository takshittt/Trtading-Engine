"""Historical data via yfinance — the backtest's stand-in for Shoonya.

Indian futures aren't on Yahoo, so instruments map to liquid proxies
(index spot / NYMEX-COMEX front contracts). Prices are treated as "points";
P&L = Δpoints × lot_size, matching how the live engine computes rupee P&L.
"""

from dataclasses import dataclass

PRESETS: list[dict] = [
    {"key": "NIFTY",      "ticker": "^NSEI",    "label": "NIFTY 50 (spot proxy)",        "lot_size": 75},
    {"key": "BANKNIFTY",  "ticker": "^NSEBANK", "label": "BANK NIFTY (spot proxy)",      "lot_size": 35},
    {"key": "NATURALGAS", "ticker": "NG=F",     "label": "Natural Gas (NYMEX, USD)",     "lot_size": 1250},
    {"key": "CRUDEOIL",   "ticker": "CL=F",     "label": "Crude Oil (NYMEX, USD)",       "lot_size": 100},
    {"key": "COPPER",     "ticker": "HG=F",     "label": "Copper (COMEX, USD)",          "lot_size": 2500},
    {"key": "GOLD",       "ticker": "GC=F",     "label": "Gold (COMEX, USD)",            "lot_size": 100},
    {"key": "SILVER",     "ticker": "SI=F",     "label": "Silver (COMEX, USD)",          "lot_size": 30},
]

# yfinance intraday history limits (approx, enforced client-side)
_MAX_DAYS = {"5m": 59, "15m": 59, "30m": 59, "60m": 729, "1d": 3650}
INTERVALS = list(_MAX_DAYS.keys())


@dataclass
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def clamp_days(interval: str, days: int) -> int:
    return max(2, min(days, _MAX_DAYS.get(interval, 59)))


def fetch_bars(ticker: str, interval: str, days: int) -> list[Bar]:
    import yfinance as yf

    if interval not in _MAX_DAYS:
        raise ValueError(f"interval must be one of {INTERVALS}")
    days = clamp_days(interval, days)
    hist = yf.Ticker(ticker).history(period=f"{days}d", interval=interval, auto_adjust=False)
    if hist is None or hist.empty:
        raise ValueError(f"yfinance returned no data for '{ticker}' ({interval}, {days}d)")
    bars: list[Bar] = []
    for idx, row in hist.iterrows():
        try:
            o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (o > 0 and h > 0 and l > 0 and c > 0):
            continue
        bars.append(Bar(ts=int(idx.timestamp()), open=o, high=h, low=l, close=c,
                        volume=float(row.get("Volume", 0) or 0)))
    if len(bars) < 30:
        raise ValueError(f"only {len(bars)} usable bars for '{ticker}' — not enough to backtest")
    return bars
