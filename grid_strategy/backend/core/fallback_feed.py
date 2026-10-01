"""Fallback price source for when the Shoonya feed dies.

Shoonya is the primary and only order-capable feed. When it goes silent with
open positions, the engine still needs a *reference* price to (a) show the
operator an approximate live P&L and (b) put a number in the manual-exit alert.
This module provides that reference — it is NEVER used to auto-fire orders.

IMPORTANT CAVEATS (surfaced to the user in the alert):
  • These are DELAYED, often CORRELATED-PROXY prices (e.g. NYMEX NG=F for MCX
    NATURALGAS), not the exact MCX-INR contract price.
  • Providers are rate-limited; results are cached briefly.

Providers (settings.fallback_provider):
  "yfinance" — default; already a project dependency. Map each instrument to a
               Yahoo symbol via Instrument.fallback_symbol (e.g. "NG=F", "CL=F",
               "GC=F", "SI=F", "^NSEI", "^NSEBANK").
  "finnhub"  — https://finnhub.io/api/v1/quote?symbol=..&token=.. (needs key).
  "http"     — your own API: settings.fallback_http_url with "{sym}" substituted,
               JSON response; the first of lp/price/last/c/ltp wins.
  "off"      — no fallback price (alert still fires, P&L shown as last-known).

`quote(symbol)` returns {"lp": float, "source": str, "delayed": bool} or None.
Blocking work (yfinance) runs in a thread so the event loop never stalls.
"""

import asyncio
import logging
import time as _time

import httpx

from config.settings import settings

logger = logging.getLogger("grid.fallback")

_CACHE_TTL = 12.0   # seconds — don't hammer the free providers


class FallbackFeed:
    def __init__(self):
        self._cache: dict[str, tuple[float, dict]] = {}   # symbol → (monotonic_ts, quote)

    async def quote(self, symbol: str) -> dict | None:
        symbol = (symbol or "").strip()
        if not symbol:
            return None
        provider = (settings.fallback_provider or "yfinance").lower()
        if provider == "off":
            return None
        cached = self._cache.get(symbol)
        if cached and _time.monotonic() - cached[0] < _CACHE_TTL:
            return cached[1]
        try:
            if provider == "yfinance":
                q = await asyncio.to_thread(self._yfinance_sync, symbol)
            elif provider == "finnhub":
                q = await self._finnhub(symbol)
            elif provider == "http":
                q = await self._http(symbol)
            else:
                logger.warning("unknown fallback provider %r", provider)
                q = None
        except Exception as e:  # noqa: BLE001
            logger.warning("fallback quote for %s failed: %s", symbol, e)
            q = None
        if q and q.get("lp", 0) > 0:
            self._cache[symbol] = (_time.monotonic(), q)
            return q
        return None

    @staticmethod
    def _yfinance_sync(symbol: str) -> dict | None:
        import yfinance as yf
        t = yf.Ticker(symbol)
        lp = 0.0
        try:
            fi = t.fast_info
            lp = float(fi.get("last_price") or fi.get("lastPrice") or 0) if hasattr(fi, "get") \
                else float(getattr(fi, "last_price", 0) or 0)
        except Exception:  # noqa: BLE001
            lp = 0.0
        if lp <= 0:
            hist = t.history(period="1d", interval="1m")
            if hist is not None and len(hist):
                lp = float(hist["Close"].iloc[-1])
        if lp and lp > 0:
            return {"lp": round(lp, 4), "source": f"yfinance:{symbol}", "delayed": True}
        return None

    async def _finnhub(self, symbol: str) -> dict | None:
        key = settings.finnhub_api_key
        if not key:
            logger.warning("finnhub provider selected but FINNHUB_API_KEY is empty")
            return None
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get("https://finnhub.io/api/v1/quote",
                            params={"symbol": symbol, "token": key})
            r.raise_for_status()
            data = r.json()
        lp = float(data.get("c") or 0)   # 'c' = current price
        return {"lp": round(lp, 4), "source": f"finnhub:{symbol}", "delayed": True} if lp > 0 else None

    async def _http(self, symbol: str) -> dict | None:
        url_tmpl = settings.fallback_http_url
        if not url_tmpl:
            logger.warning("http provider selected but FALLBACK_HTTP_URL is empty")
            return None
        url = url_tmpl.replace("{sym}", httpx.QueryParams({"s": symbol})["s"]) if "{sym}" in url_tmpl else url_tmpl
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
        for k in ("lp", "price", "last", "c", "ltp"):
            v = data.get(k) if isinstance(data, dict) else None
            if v:
                try:
                    return {"lp": round(float(v), 4), "source": f"http:{symbol}", "delayed": True}
                except (TypeError, ValueError):
                    continue
        return None
