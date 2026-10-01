"""HTTP client for the Gateway REST API (orders, order book, quotes, candles,
positions, search). The engine never talks to Shoonya directly — broker
credentials stay inside the gateway."""

import logging
from typing import Optional

import httpx

from config.settings import settings
from gateway_client.auth import fetch_service_token

logger = logging.getLogger(__name__)


class _AuthedClient(httpx.AsyncClient):
    """httpx client that attaches the Gateway service JWT to every request and
    transparently re-logs-in + retries once on a 401 (the ~1h token expiry).

    All requests funnel through `request()` in httpx, so overriding it here means
    every GatewayRest method gets auth without any per-call change."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._token: Optional[str] = None

    # 401 details that mean OUR service token is bad (worth re-login + retry), vs
    # the broker-layer "Not authenticated with Shoonya broker" 401 (a human must
    # connect the broker — re-login won't help, so don't retry).
    _TOKEN_401_HINTS = ("token expired", "invalid token", "authorization header", "no longer exists")

    async def _login(self) -> None:
        self._token = await fetch_service_token()

    def _is_token_401(self, r: httpx.Response) -> bool:
        try:
            detail = str((r.json() or {}).get("detail", "")).lower()
        except Exception:
            return False
        return any(h in detail for h in self._TOKEN_401_HINTS)

    async def request(self, method, url, **kwargs):
        if self._token is None:
            await self._login()
        headers = httpx.Headers(kwargs.pop("headers", None))
        headers["Authorization"] = f"Bearer {self._token}"
        r = await super().request(method, url, headers=headers, **kwargs)
        if r.status_code == 401 and self._is_token_401(r):
            # service token expired / rotated → get a fresh one and retry once.
            await self._login()
            headers["Authorization"] = f"Bearer {self._token}"
            r = await super().request(method, url, headers=headers, **kwargs)
        return r


class GatewayRest:
    def __init__(self):
        self._client: Optional[_AuthedClient] = None

    async def start(self) -> None:
        if self._client is None:
            self._client = _AuthedClient(base_url=settings.gateway_base_url, timeout=15.0)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("GatewayRest not started")
        return self._client

    # ---------------- broker session ----------------

    async def broker_connected(self) -> bool:
        try:
            r = await self.client.get("/api/status")
            return bool(r.json().get("connected"))
        except Exception:
            return False

    async def broker_status(self) -> Optional[bool]:
        """Broker connectivity for the status loop, distinguishing a DEFINITIVE
        gateway answer (True/False from a clean 200) from an UNREACHABLE gateway
        (None: timeout, transport error, or 5xx).

        The engine trusts True/False immediately — the gateway is WS-primary now,
        so its `connected` is meaningful and a real "down" must gate trading at
        once — but debounces None, because a transient Grid↔Gateway blip used
        to flip the badge/gate off the way a single failed poll did.
        broker_connected() keeps its fail-closed bool for boot/guard callers."""
        try:
            r = await self.client.get("/api/status")
        except Exception:
            return None
        if r.status_code != 200:
            return None
        try:
            return bool(r.json().get("connected"))
        except Exception:
            return None

    async def connect_broker(self) -> dict:
        """Ask the shared gateway to (re)login to the broker.

        Grid holds no broker credentials — the gateway does — so this is a
        remote trigger, not a local login. The gateway short-circuits when the
        session is already live (`already_connected`), so a redundant press is a
        cheap no-op rather than a slow forced re-login. On a genuine login
        failure the broker's own reason (wrong password / TOTP, etc.) rides in
        the JSON `detail` — surface it instead of a bare status code, exactly as
        place_order does for order rejections."""
        r = await self.client.post("/api/connect")
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        if r.status_code >= 400:
            detail = body.get("detail") or (r.text or "")[:300] or r.reason_phrase
            return {"connected": False, "already_connected": False, "detail": detail}
        return {
            "connected": bool(body.get("connected")),
            "already_connected": bool(body.get("already_connected")),
            "detail": body.get("message") or body.get("detail") or "",
        }

    async def disconnect_broker(self) -> dict:
        """Log the shared gateway out of the broker.

        The broker session is shared across every Gateway client, so this stops
        prices and stop-loss monitoring for ALL of them — the UI confirms first.
        On failure we don't claim disconnected (the safe direction); the streaming
        snapshot's broker_connected reconciles the real state either way."""
        r = await self.client.post("/api/disconnect")
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        if r.status_code >= 400:
            detail = body.get("detail") or (r.text or "")[:300] or r.reason_phrase
            return {"connected": True, "detail": detail}
        return {"connected": bool(body.get("connected", False)),
                "detail": body.get("message") or body.get("detail") or ""}

    # ---------------- orders ----------------

    async def place_order(
        self, *, side: str, exchange: str, tradingsymbol: str, quantity: int,
        price_type: str = "MKT", price: float = 0.0, remarks: str = "",
        product_type: str = "M",
    ) -> Optional[str]:
        """Returns broker order id (norenordno) or raises on HTTP/broker error."""
        payload = {
            "buy_or_sell": side,
            "product_type": product_type,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "quantity": quantity,
            "discloseqty": 0,
            "price_type": price_type,
            "price": price,
            "trigger_price": 0,
            "retention": "DAY",
            "remarks": remarks,
        }
        r = await self.client.post("/api/orders", json=payload)
        if r.status_code >= 400:
            # surface the BROKER's real reason (e.g. "ALGO_CHK: MKT Order type not
            # allowed for API order") instead of a bare "502 Bad Gateway" — the
            # gateway returns it in the JSON `detail`, which raise_for_status drops.
            detail = ""
            try:
                detail = (r.json() or {}).get("detail", "")
            except Exception:
                detail = (r.text or "")[:300]
            raise RuntimeError(f"broker rejected order [{r.status_code}]: {detail or r.reason_phrase}")
        return r.json().get("order_id")

    async def cancel_order(self, order_id: str) -> bool:
        try:
            r = await self.client.delete(f"/api/orders/{order_id}")
            return r.status_code < 400
        except Exception:
            return False

    async def get_order_book(self) -> list[dict]:
        r = await self.client.get("/api/orders")
        r.raise_for_status()
        return r.json().get("orders", [])

    async def find_order(self, order_id: str) -> Optional[dict]:
        """Look one order up in the book. Order book is ground truth for fills."""
        for o in await self.get_order_book():
            if o.get("norenordno") == order_id:
                return o
        return None

    # ---------------- market data ----------------

    async def get_quote(self, exchange: str, token: str) -> dict:
        r = await self.client.get("/api/quote", params={"exchange": exchange, "token": token})
        r.raise_for_status()
        return r.json()

    async def get_candles_1m(self, exchange: str, token: str, lookback_minutes: int) -> list[dict]:
        """1-minute candles, oldest-first, each with epoch seconds in `ts`."""
        r = await self.client.get("/api/candles", params={
            "exchange": exchange, "token": token, "interval": 1,
            "lookback_minutes": lookback_minutes,
        })
        r.raise_for_status()
        out = []
        for c in r.json().get("candles", []):
            ts = c.get("time", "")
            epoch = _to_epoch(ts)
            if epoch and c.get("close"):
                out.append({"ts": epoch, "open": c["open"], "high": c["high"],
                            "low": c["low"], "close": c["close"], "volume": c.get("volume", 0)})
        return out

    async def get_candles(self, exchange: str, token: str, interval: int, lookback_minutes: int) -> list[dict]:
        r = await self.client.get("/api/candles", params={
            "exchange": exchange, "token": token, "interval": interval,
            "lookback_minutes": lookback_minutes,
        })
        r.raise_for_status()
        out = []
        for c in r.json().get("candles", []):
            epoch = _to_epoch(c.get("time", ""))
            if epoch and c.get("close"):
                out.append({"ts": epoch, "open": c["open"], "high": c["high"],
                            "low": c["low"], "close": c["close"], "volume": c.get("volume", 0)})
        return out

    # ---------------- positions ----------------

    async def get_positions_raw(self) -> list[dict]:
        """Flat list of broker positions: [{tsym, exch, netqty, ...}]"""
        r = await self.client.get("/api/positions")
        r.raise_for_status()
        data = r.json()
        out: list[dict] = []
        for grp in data.get("symbol_groups", []):
            for p in grp.get("positions", []):
                out.append(p)
        return out

    async def get_positions_grouped(self) -> dict:
        """Full broker positions response: symbol_groups (each with symbol_pnl +
        per-leg total_pnl/rpnl/urmtom) and account total_pnl. Broker-accurate,
        settlement-aware P&L (see Gateway/PNL_CALCULATION.md)."""
        r = await self.client.get("/api/positions")
        r.raise_for_status()
        return r.json()

    # ---------------- account (funds / profile) ----------------

    async def get_funds(self) -> dict:
        """Cash balance + margin utilisation: {cash, margin_used, payin, collateral}."""
        r = await self.client.get("/api/funds")
        r.raise_for_status()
        return r.json()

    async def get_order_margin(self, *, exchange: str, tradingsymbol: str, quantity: int,
                               side: str = "B", product_type: str = "M") -> dict:
        """SPAN+exposure margin required to place this order (pre-trade gate).
        Returns {required, span, expo, ...}."""
        r = await self.client.post("/api/order_margin", json={
            "exchange": exchange, "tradingsymbol": tradingsymbol,
            "quantity": quantity, "buy_or_sell": side, "product_type": product_type})
        r.raise_for_status()
        return r.json()

    async def get_user(self) -> dict:
        """Account/profile: uid, actid, email, brkname, exarr, prarr, …"""
        r = await self.client.get("/api/user")
        r.raise_for_status()
        return r.json()

    # ---------------- search ----------------

    async def search(self, exchange: str, q: str) -> list[dict]:
        r = await self.client.get("/api/search", params={"exchange": exchange, "q": q})
        r.raise_for_status()
        return r.json().get("results", [])


def _to_epoch(ts) -> int:
    """Gateway candle `time` comes as 'ssboe' epoch string or 'dd-mm-YYYY HH:MM:SS'.
    The string form is BROKER (IST) wall time — parse it as IST explicitly, never as
    the server's local timezone, or candles shift vs marker epochs on non-IST hosts."""
    if ts in (None, ""):
        return 0
    s = str(ts)
    if s.isdigit():
        return int(s)
    from datetime import datetime
    from config.settings import IST
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=IST).timestamp())
        except ValueError:
            continue
    return 0
