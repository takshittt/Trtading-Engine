"""Live Shoonya order gateway — REST + WebSocket CLIENT to the isolated Shoonya
Gateway that holds the credentials (the Grid gateway).

Verified against Gateway/gateway_backend/server.py:
  GET  /api/status                              -> {"connected": bool, ...}
  GET  /api/funds                               -> {cash, margin_used, payin, collateral}
  GET  /api/positions                           -> {symbol_groups:[{items:[{tsym,netqty,...}]}], total_pnl}
  POST /api/orders  PlaceOrderRequest           -> order response
       {buy_or_sell:"B"/"S", product_type:"M", exchange:"NFO", tradingsymbol,
        quantity, price_type:"LMT", price, remarks}
  POST /api/order_margin {exchange,tradingsymbol,quantity,buy_or_sell,product_type}
                                                -> {required, span, expo}
  GET  /api/search?exchange=NFO&q=...           -> {results:[{tsym,token,lotsize,expd,instrumenttype}]}
  GET  /api/quote?exchange=NFO&token=...        -> {lp,bp1,sp1,ti,...}
  WS   /api/ws/ticker  send {action:"subscribe", symbols:["NFO|<token>"]}
                       recv {tk:<token>, lp:<ltp>, ...}

Broker credentials, TLS-1.2 pinning, OAuth keepalive, WS reconnect and order
emsg-preservation all live INSIDE that gateway. This strategy never touches
Shoonya directly, so a broker/auth failure can't reach the strategy core.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime
from typing import Callable, Optional

import requests

from app.brokers.base import BrokerFill, BrokerOrder
from app.core.config import EXCHANGE, IST, PRODUCT_TYPE

_log = logging.getLogger("swing.shoonya")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def _today_ist() -> date:
    """Expiry is an exchange-calendar date, so compare against IST, not UTC."""
    return datetime.now(IST).date()


def _parse_expiry(expd: str) -> date | None:
    """Shoonya's `expd` is "30-JUN-2026"; ISO is accepted too. None if unusable.

    A contract whose expiry cannot be read is treated as unusable rather than
    assumed live — guessing here is what puts an expired token on the feed.
    """
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

BASE = os.getenv("SWING_GATEWAY_BASE_URL", "http://localhost:8000").rstrip("/")
WS_URL = os.getenv("SWING_GATEWAY_WS_URL", "ws://localhost:8000/api/ws/ticker")
GATEWAY_SECRET = os.getenv("SWING_GATEWAY_SECRET", "")

# Service-identity credentials issued by the shared Gateway's
# scripts/register_service.py. When set, this strategy authenticates as a
# machine service (client_id + client_secret -> short-lived service JWT) on
# every REST call and the ticker WS — the same handshake grid_strategy uses.
# When absent, we fall back to the legacy X-Gateway-Secret header so a
# standalone/mock gateway with no auth still works.
GATEWAY_CLIENT_ID = os.getenv("SWING_GATEWAY_CLIENT_ID", "")
GATEWAY_CLIENT_SECRET = os.getenv("SWING_GATEWAY_CLIENT_SECRET", "")
USE_SERVICE_AUTH = bool(GATEWAY_CLIENT_ID and GATEWAY_CLIENT_SECRET)


def _fetch_service_token() -> str:
    """Exchange client credentials for a fresh service JWT at the Gateway.
    Raises on non-2xx (bad/absent credentials, or a gateway that predates
    service auth) so the caller degrades to disconnected rather than trading
    unauthenticated."""
    r = requests.post(f"{BASE}/api/auth/service-token", json={
        "client_id": GATEWAY_CLIENT_ID, "client_secret": GATEWAY_CLIENT_SECRET,
    }, timeout=15)
    r.raise_for_status()
    return r.json()["token"]


def _ws_url_with_auth() -> str:
    """The ticker WS can't send an Authorization header, so the service JWT
    rides as ?token= (matching the Gateway's ws router). A fresh token is
    fetched per (re)connect, so the ~1h expiry is absorbed by the reconnect
    loop. Falls through to the bare URL when service auth is off."""
    if not USE_SERVICE_AUTH:
        return WS_URL
    sep = "&" if "?" in WS_URL else "?"
    return f"{WS_URL}{sep}token={_fetch_service_token()}"


class _AuthedSession(requests.Session):
    """requests.Session that attaches the Gateway service JWT to every call and
    transparently re-logs-in + retries once on a token-expiry 401. All verbs
    funnel through request(), so every gateway call is covered without a
    per-call change. Ported from grid_strategy's async _AuthedClient."""

    # 401 details that mean OUR service token is stale (worth a re-login + retry),
    # as opposed to the broker-layer "not authenticated with Shoonya" 401 that a
    # re-login can't fix.
    _TOKEN_401_HINTS = ("token expired", "invalid token", "authorization header", "no longer exists")

    def __init__(self) -> None:
        super().__init__()
        self._token: str | None = None

    def _login(self) -> None:
        self._token = _fetch_service_token()

    def _is_token_401(self, r: requests.Response) -> bool:
        try:
            detail = str((r.json() or {}).get("detail", "")).lower()
        except Exception:
            return False
        return any(h in detail for h in self._TOKEN_401_HINTS)

    def request(self, method, url, **kwargs):  # type: ignore[override]
        if self._token is None:
            self._login()
        headers = {**(kwargs.pop("headers", None) or {}), "Authorization": f"Bearer {self._token}"}
        r = super().request(method, url, headers=headers, **kwargs)
        if r.status_code == 401 and self._is_token_401(r):
            self._login()
            headers["Authorization"] = f"Bearer {self._token}"
            r = super().request(method, url, headers=headers, **kwargs)
        return r


class ShoonyaGateway:
    name = "shoonya"

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}          # symbol -> ltp
        self._scrip: dict[str, dict] = {}            # symbol -> {tsym, token, lot_size, expiry, exch}
        self._token_to_symbol: dict[str, str] = {}   # "NFO|123" -> symbol
        self._token_only: dict[str, str] = {}        # "123" -> symbol (exchange-agnostic)
        self._wanted: set[str] = set()               # "NFO|123" — replayed on reconnect
        self._connected = False
        self._user: dict = {}                        # cached broker identity
        self._on_tick: Callable[[str, float], None] | None = None
        self._ws = None
        self._ws_send_lock = threading.Lock()
        # Service-JWT auth against the shared Gateway when credentials are set;
        # otherwise the legacy shared-secret header for a standalone gateway.
        if USE_SERVICE_AUTH:
            self._sess = _AuthedSession()
        else:
            self._sess = requests.Session()
            if GATEWAY_SECRET:
                self._sess.headers["X-Gateway-Secret"] = GATEWAY_SECRET

    # -- lifecycle --------------------------------------------------------
    def connect(self) -> bool:
        try:
            r = self._sess.get(f"{BASE}/api/status", timeout=8)
            self._connected = bool(r.ok and r.json().get("connected"))
        except Exception:
            self._connected = False
        return self._connected

    def is_connected(self) -> bool:
        return self._connected

    def status(self) -> Optional[bool]:
        """Broker connectivity for the periodic re-check, distinguishing a
        DEFINITIVE gateway answer (True/False from a clean 200) from an
        UNREACHABLE gateway (None: timeout, transport error, non-200).

        The re-check trusts True/False at once — the gateway is WS-primary now, so
        its `connected` is meaningful and a real "down" must show immediately —
        but the caller debounces None, so a transient blip no longer flaps the
        badge to disconnected the way a single failed /api/status read did.
        connect() keeps its fail-closed bool for the startup handshake."""
        try:
            r = self._sess.get(f"{BASE}/api/status", timeout=8)
        except Exception:
            return None
        if not r.ok:
            return None
        try:
            val = bool(r.json().get("connected"))
        except Exception:
            return None
        self._connected = val   # keep is_connected() coherent on a definitive read
        return val

    def disconnect(self) -> dict:
        """Log the gateway out of Shoonya and drop its cached session token.

        The gateway clears `susertoken` from its own store on the way out, which
        is what stops the next Connect reusing a token the broker has already
        killed. Local state is updated from THIS call's result rather than by
        re-reading /api/status, because the gateway caches its liveness probe for
        20 seconds — polling straight afterwards returns the pre-disconnect
        answer and the dashboard would keep showing a session that is gone.
        """
        try:
            r = self._sess.post(f"{BASE}/api/disconnect", timeout=15)
            ok = bool(r.ok)
            detail = "" if ok else (r.text or "")[:200]
        except Exception as exc:
            ok, detail = False, str(exc)
        # Believe the disconnect either way: if the call failed we no longer know
        # the session is good, and claiming connected is the dangerous direction.
        self._connected = False
        self._user = {}
        return {"ok": ok, "connected": False, "detail": detail}

    def connect_force(self) -> dict:
        """Genuine re-login, bypassing the gateway's same-day token cache.

        `connect()` only reads /api/status — it reports on a session, it does not
        establish one. The gateway reuses a cached token whenever one was issued
        today, so a session Shoonya invalidated mid-day is never replaced by a
        plain restart. /api/connect passes force=True, which skips both the
        cached token and the cached OAuth code and drives a fresh login.
        Selenium is involved, so this is slow — hence the long timeout.
        """
        already = False
        try:
            r = self._sess.post(f"{BASE}/api/connect", timeout=180)
            body = r.json() if r.content else {}
            ok = bool(r.ok and body.get("connected"))
            # The gateway short-circuits a redundant connect (session already
            # live) rather than burning a fresh ~180s login — surface that so the
            # UI can say "already connected" instead of implying a real re-login.
            already = bool(body.get("already_connected"))
            detail = body.get("message") or body.get("detail") or ""
        except Exception as exc:
            ok, detail = False, str(exc)
        self._connected = ok
        if ok:
            self._user = {}                # force a re-read of the identity
        return {"ok": ok, "connected": ok, "already_connected": already, "detail": detail}

    def get_user(self) -> dict:
        """Who the gateway is logged in as. Cached — the dashboard polls often
        and this never changes within a session."""
        if self._user:
            return self._user
        try:
            r = self._sess.get(f"{BASE}/api/user", timeout=10)
            if not r.ok:
                return {}
            d = r.json() or {}
        except Exception:
            return {}
        uid = (d.get("uid") or d.get("actid") or "").strip()
        if not uid:
            return {}
        self._user = {
            "uid": uid,
            "account_id": (d.get("actid") or "").strip(),
            "name": (d.get("uname") or d.get("name") or "").strip(),
            "email": (d.get("email") or "").strip(),
            "broker": (d.get("brkname") or "Shoonya").strip(),
        }
        return self._user

    # -- symbol resolution (Amibroker symbol -> Shoonya scrip) -----------
    def resolve(self, symbol: str) -> dict:
        """Resolve a display symbol to {tsym, token, lot_size, expiry, exch}.

        Three rules, each of which was previously violated with real consequences:

        *Exact root match.* The gateway search is a substring query, so "IOC"
        also returns BIOCON30JUN26F (B-IOC-ON). Taking the first row could
        therefore price a signal off an entirely different company. Candidates
        must match the requested root on the scrip's own `sym` field.

        *Never expired.* Results are not ordered by expiry, so the first future
        returned was routinely a contract that had already expired — it has no
        live ticks, which is why its price sat frozen at a stale or zero value.
        Expired contracts are filtered out and the nearest live expiry wins.

        *Futures only.* The old fallback accepted any row when no future
        matched, so an option could be selected and its premium shown as the
        underlying's price — a ₹7,000 stock reading ₹9. It is better to return
        no price than a wrong one, so an unmatched symbol now resolves to an
        empty token and prices as unavailable.

        The cache is expiry-aware: a contract that rolls over is re-resolved
        rather than served stale forever.
        """
        symbol = symbol.upper()
        cached = self._scrip.get(symbol)
        if cached is not None and not self._is_expired(cached.get("expiry", "")):
            return cached

        base = symbol.replace("-FUT", "").replace("-I", "").replace("-EQ", "").strip()
        info = {"tsym": symbol, "token": "", "lot_size": 1, "expiry": "", "exch": EXCHANGE,
                "sym": base}

        # Our own contract master first — it is refreshed daily from Shoonya and
        # does not depend on the shared gateway's copy staying current.
        hit = self._from_scripmaster(symbol, base)
        if hit is not None:
            self._scrip[symbol] = hit
            self._token_to_symbol[f"{hit['exch']}|{hit['token']}"] = symbol
            self._token_only[hit["token"]] = symbol
            return hit

        try:
            rows = self._sess.get(f"{BASE}/api/search",
                                  params={"exchange": EXCHANGE, "q": base},
                                  timeout=8).json().get("results", [])
        except Exception:
            rows = []

        pick = self._pick_contract(rows, symbol, base)
        if pick is not None:
            info = {"tsym": pick.get("tsym") or symbol, "token": str(pick.get("token") or ""),
                    "lot_size": int(pick.get("lotsize") or 1), "expiry": pick.get("expd") or "",
                    "exch": pick.get("exch") or EXCHANGE,
                    "sym": (pick.get("sym") or base).upper()}
        elif rows:
            _log.warning("resolve(%s): no live futures contract matched (%d rows searched)",
                         symbol, len(rows))

        self._scrip[symbol] = info
        if info["token"]:
            self._token_to_symbol[f"{info['exch']}|{info['token']}"] = symbol
            self._token_only[info["token"]] = symbol
        return info

    @staticmethod
    def _from_scripmaster(symbol: str, base: str) -> dict | None:
        """Exact contract, else the nearest live future for the root."""
        from app.services.scripmaster import SCRIPMASTER
        try:
            SCRIPMASTER.ensure_fresh()
            row = SCRIPMASTER.by_tsym(symbol) or SCRIPMASTER.live_future(base)
        except Exception:
            return None
        if not row or not row.get("token"):
            return None
        return {"tsym": row["tsym"], "token": row["token"], "lot_size": row["lot_size"],
                "expiry": row["expiry"], "exch": row["exch"], "sym": row["sym"]}

    def _pick_contract(self, rows: list, symbol: str, base: str) -> dict | None:
        """Nearest non-expired future whose root matches, or the exact contract.

        An explicit contract (a manual add or a held position names its own
        tsym, e.g. NAUKRI28JUL26F) is honoured as given — including after it
        expires, so an open position keeps identifying the thing it holds.
        """
        for r in rows:
            if (r.get("tsym") or "").upper() == symbol:
                return r

        live: list[tuple[date, dict]] = []
        for r in rows:
            instr = (r.get("instrumenttype") or "").upper()
            if "FUT" not in instr or "OPT" in instr:
                continue
            if (r.get("sym") or "").upper() != base:      # substring match guard
                continue
            expiry = _parse_expiry(r.get("expd") or "")
            if expiry is None or expiry < _today_ist():
                continue
            live.append((expiry, r))

        if not live:
            return None
        live.sort(key=lambda x: x[0])                     # nearest live expiry
        return live[0][1]

    @staticmethod
    def _is_expired(expd: str) -> bool:
        d = _parse_expiry(expd)
        return d is not None and d < _today_ist()

    def next_expiries(self, symbol: str) -> list[dict]:
        """Contracts this position could roll into: same underlying, futures
        only, expiry strictly after the one held, nearest first.

        Matching is on the scrip's own `sym` so LEAD never pulls in LEADMINI —
        the same substring trap that made resolve() price one stock off another.
        Empty when the held contract is already the furthest listed expiry.
        """
        info = self.resolve(symbol)
        current = _parse_expiry(info.get("expiry", ""))
        base = symbol.replace("-FUT", "").replace("-I", "").replace("-EQ", "").strip().upper()
        # An explicit contract (NAUKRI28JUL26F) carries its own root; recover it
        # from the resolved scrip rather than the display symbol.
        root = (info.get("sym") or base).upper()

        from app.services.scripmaster import SCRIPMASTER
        try:
            SCRIPMASTER.ensure_fresh()
            later = SCRIPMASTER.later_futures(root, current)
        except Exception:
            later = []
        if later:
            return [{"tsym": r["tsym"], "token": r["token"], "exch": r["exch"],
                     "expiry": r["expiry"], "lot_size": r["lot_size"]} for r in later]

        try:
            rows = self._sess.get(f"{BASE}/api/search",
                                  params={"exchange": EXCHANGE, "q": root},
                                  timeout=8).json().get("results", [])
        except Exception:
            return []

        out: list[tuple[date, dict]] = []
        for r in rows:
            instr = (r.get("instrumenttype") or "").upper()
            if "FUT" not in instr or "OPT" in instr:
                continue
            if (r.get("sym") or "").upper() != root:
                continue
            exp = _parse_expiry(r.get("expd") or "")
            if exp is None or exp <= (current or _today_ist()):
                continue
            out.append((exp, {
                "tsym": r.get("tsym") or "", "token": str(r.get("token") or ""),
                "exch": r.get("exch") or EXCHANGE, "expiry": r.get("expd") or "",
                "lot_size": int(r.get("lotsize") or 1),
            }))
        out.sort(key=lambda t: t[0])
        return [row for _e, row in out]

    def quote_token(self, exch: str, token: str) -> dict:
        """Top-of-book for a contract we may not have resolved by name."""
        out = {"ltp": 0.0, "bid": 0.0, "ask": 0.0}
        if not token:
            return out
        try:
            q = self._sess.get(f"{BASE}/api/quote",
                               params={"exchange": exch or EXCHANGE, "token": token},
                               timeout=6).json()
        except Exception:
            return out
        for key, field in (("ltp", "lp"), ("bid", "bp1"), ("ask", "sp1")):
            try:
                val = float(q.get(field) or 0.0)
            except (TypeError, ValueError):
                val = 0.0
            if val > 0:
                out[key] = val
        return out

    def lot_size(self, symbol: str) -> int:
        return self.resolve(symbol).get("lot_size", 1)

    def expiry_of(self, symbol: str) -> str:
        return self.resolve(symbol).get("expiry", "")

    # -- market data ------------------------------------------------------
    def get_ltp(self, symbol: str) -> float:
        symbol = symbol.upper()
        if symbol in self._prices:
            return self._prices[symbol]
        info = self.resolve(symbol)
        if not info["token"]:
            return 0.0
        try:
            q = self._sess.get(f"{BASE}/api/quote",
                               params={"exchange": info["exch"], "token": info["token"]}, timeout=6).json()
            return float(q.get("lp") or 0.0)
        except Exception:
            return 0.0

    def get_quote(self, symbol: str) -> dict:
        """Top-of-book {ltp, bid, ask} for paper fills. Zeros when unavailable."""
        symbol = symbol.upper()
        info = self.resolve(symbol)
        out = {"ltp": self._prices.get(symbol, 0.0), "bid": 0.0, "ask": 0.0}
        if not info["token"]:
            return out
        try:
            q = self._sess.get(f"{BASE}/api/quote",
                               params={"exchange": info["exch"], "token": info["token"]},
                               timeout=6).json()
        except Exception:
            return out
        # Shoonya field names: lp = last price, bp1 / sp1 = best bid / best ask.
        # They come back as empty strings when the feed has nothing to give.
        for key, field in (("ltp", "lp"), ("bid", "bp1"), ("ask", "sp1")):
            try:
                val = float(q.get(field) or 0.0)
            except (TypeError, ValueError):
                val = 0.0
            if val > 0:
                out[key] = val
        return out

    def subscribe_prices(self, symbols: list[str], on_tick: Callable[[str, float], None]) -> None:
        self._on_tick = on_tick
        for s in symbols:
            self.resolve(s)   # warm cache + token map
        threading.Thread(target=self._ws_loop, daemon=True).start()

    def ensure_subscribed(self, symbol: str) -> None:
        """Subscribe a symbol's token to the live feed (called on signal/position).

        `_wanted` records the intent and is replayed in full on every reconnect.
        Previously the key was marked subscribed before the send, so anything
        registered while the socket was down was recorded as done and never
        actually streamed — the symbol's price then sat frozen forever.
        """
        info = self.resolve(symbol)
        if not info["token"]:
            return
        key = f"{info['exch']}|{info['token']}"
        if key in self._wanted:
            return
        self._wanted.add(key)
        self._ws_send({"action": "subscribe", "symbols": [key]})

    def _ws_send(self, msg: dict) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            with self._ws_send_lock:
                ws.send(json.dumps(msg))
            return True
        except Exception:
            return False

    def _ws_loop(self) -> None:
        from app.core.state import STATE
        import asyncio
        try:
            import websockets  # type: ignore
        except Exception:
            # Without this the thread returned in silence, `self._ws` stayed
            # None forever, every ensure_subscribed() send failed closed, and
            # the only remaining price source was the warmer's REST quotes —
            # with nothing anywhere saying the ticker had never started.
            _log.error("ticker disabled: the 'websockets' package is not installed")
            STATE.feed.ticker_connected = False
            return

        async def run() -> None:
            backoff = 1.0
            while True:
                try:
                    # Fresh service token per (re)connect — see _ws_url_with_auth.
                    # A token-fetch failure raises here and is handled by the same
                    # backoff/reconnect path as a dropped socket.
                    async with __import__("websockets").connect(_ws_url_with_auth(), ping_interval=10, ping_timeout=10) as ws:
                        self._ws = _SyncWS(ws, asyncio.get_event_loop())
                        backoff = 1.0
                        STATE.feed.ticker_connected = True
                        _log.info("ticker connected (%d symbols)", len(self._wanted))
                        if self._wanted:
                            await ws.send(json.dumps({"action": "subscribe",
                                                      "symbols": list(self._wanted)}))
                        async for raw in ws:
                            try:
                                t = json.loads(raw)
                            except Exception:
                                continue
                            token = t.get("tk")
                            lp = t.get("lp")
                            if token is None or lp is None:
                                continue
                            sym = (self._token_to_symbol.get(f"{EXCHANGE}|{token}")
                                   or self._token_only.get(str(token)))
                            if not sym:
                                continue
                            px = float(lp)
                            self._prices[sym] = px
                            if self._on_tick:
                                self._on_tick(sym, px)
                except Exception as exc:
                    self._ws = None
                    STATE.feed.ticker_connected = False
                    _log.warning("ticker disconnected: %s", exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 15.0)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run())

    # -- orders (LIMIT only) ---------------------------------------------
    def place_order(self, order: BrokerOrder) -> BrokerFill:
        info = self.resolve(order.symbol)
        payload = {
            "buy_or_sell": "B" if order.side == "BUY" else "S",
            "product_type": PRODUCT_TYPE,
            "exchange": info["exch"],
            "tradingsymbol": info["tsym"],
            "quantity": order.qty,
            "price_type": "LMT",
            "price": order.price,
            "retention": "DAY",
            "remarks": ("exit-" if order.side == "SELL" else "") + order.intent.lower(),
        }
        try:
            r = self._sess.post(f"{BASE}/api/orders", json=payload, timeout=12)
            d = r.json()
            if not r.ok:
                return BrokerFill(broker_order_id="", status="REJECTED", raw=d)
            status = (d.get("status") or d.get("order_status") or "").upper()
            oid = str(d.get("norenordno") or d.get("order_id") or d.get("orderid") or "")
            
            # PENDING / OPEN means it was placed successfully but not filled yet
            if "COMPLETE" in status or "FILLED" in status:
                mapped_status = "FILLED"
            elif "REJECT" in status or "CANCEL" in status:
                mapped_status = "REJECTED"
            elif bool(oid):
                mapped_status = "OPEN"
            else:
                mapped_status = "REJECTED"

            return BrokerFill(
                broker_order_id=oid,
                status=mapped_status,
                filled_price=float(d.get("avgprc") or d.get("avg") or order.price),
                raw=d)
        except Exception as exc:
            return BrokerFill(broker_order_id="", status="REJECTED", raw={"error": str(exc)})

    def modify_order(self, broker_order_id: str, price: float) -> BrokerFill:
        try:
            r = self._sess.put(f"{BASE}/api/orders/{broker_order_id}", json={"price": price, "price_type": "LMT"}, timeout=10)
            return BrokerFill(broker_order_id=broker_order_id, status="FILLED" if r.ok else "REJECTED", filled_price=price, raw=r.json())
        except Exception as exc:
            return BrokerFill(broker_order_id=broker_order_id, status="REJECTED", raw={"error": str(exc)})

    def cancel_order(self, broker_order_id: str) -> bool:
        try:
            return self._sess.delete(f"{BASE}/api/orders/{broker_order_id}", timeout=10).ok
        except Exception:
            return False

    def get_order(self, broker_order_id: str) -> BrokerFill:
        try:
            payload = {"norenordno": broker_order_id}
            r = self._sess.post(f"{BASE}/api/SingleOrdHist", json=payload, timeout=8)
            if not r.ok:
                return BrokerFill(broker_order_id=broker_order_id, status="ERROR", raw=r.json() if r.content else {})
            
            # Shoonya returns a list of history records for the order. The first (index 0) is the latest.
            history = r.json()
            if isinstance(history, list) and history:
                d = history[0]
            elif isinstance(history, dict):
                d = history
            else:
                return BrokerFill(broker_order_id=broker_order_id, status="ERROR", raw={"error": "unexpected format"})
                
            status = (d.get("status") or d.get("order_status") or "").upper()
            
            if "COMPLETE" in status or "FILLED" in status:
                mapped_status = "FILLED"
            elif "REJECT" in status or "CANCEL" in status:
                mapped_status = "REJECTED"
            else:
                mapped_status = "OPEN"
                
            # Parse filled quantity and price
            qty_filled = int(d.get("flqty") or d.get("fillshares") or 0)
            avg_price = float(d.get("avgprc") or d.get("avg") or 0.0)
            
            # Return raw so execution can parse partial fills
            return BrokerFill(
                broker_order_id=broker_order_id,
                status=mapped_status,
                filled_price=avg_price,
                raw={"qty_filled": qty_filled, "avg_price": avg_price, "original": d}
            )
        except Exception as exc:
            return BrokerFill(broker_order_id=broker_order_id, status="ERROR", raw={"error": str(exc)})

    # -- reconciliation truth (broker positions) -------------------------
    def get_positions(self) -> list[dict]:
        try:
            data = self._sess.get(f"{BASE}/api/positions", timeout=10).json()
        except Exception:
            return []
        out = []
        groups = data.get("symbol_groups", []) if isinstance(data, dict) else []
        # The gateway nests rows under "positions", not "items". Reading the
        # wrong key made this return an EMPTY list on every call — and an empty
        # book is not harmless here: reconciliation reads "broker has nothing"
        # as every open position being a ghost and closes them internally, so a
        # live position would keep running with no stop while the dashboard
        # showed flat. "items" is kept as a fallback in case the shape changes.
        rows = [it for g in groups for it in (g.get("positions") or g.get("items") or [])] \
               if groups else (data if isinstance(data, list) else [])
        for row in rows:
            qty = int(float(row.get("netqty") or row.get("qty") or 0))
            if qty == 0:
                continue
            tsym = row.get("tsym") or row.get("symbol") or ""
            # `exchange` is carried so reconciliation can tell this strategy's
            # book apart from anything else trading the same broker account.
            out.append({"symbol": self._symbol_for_tsym(tsym), "qty": qty,
                        "avg_price": self._avg_price_of(row, qty),
                        "exchange": (row.get("exch") or "").upper()})
        return out

    def _avg_price_of(self, row: dict, qty: int) -> float:
        """Average price for a net position, picked by side.

        None of netavgprc / avgprc / avg_price exist on these rows — the gateway
        sends buyavgprc, sellavgprc and upldprc — so the old lookup always fell
        through to 0.0. That mattered: reconciliation SYNCS pos.avg_price to
        whatever the broker reports, so a zero would have overwritten the real
        entry price and taken the position's PnL with it.

        A position carried from a previous day reports 0 for both day averages;
        upldprc (the carry-forward upload price) is its effective average, which
        is why it is the fallback rather than 0.
        """
        side = "buyavgprc" if qty > 0 else "sellavgprc"
        for key in (side, "netavgprc", "avgprc", "avg_price", "upldprc"):
            try:
                val = float(row.get(key) or 0.0)
            except (TypeError, ValueError):
                val = 0.0
            if val > 0:
                return val
        return 0.0

    def _symbol_for_tsym(self, tsym: str) -> str:
        for sym, info in self._scrip.items():
            if info.get("tsym") == tsym:
                return sym
        return tsym

    # -- margin (SPAN calculator via gateway) ----------------------------
    def get_margin_per_lot(self, symbol: str) -> float:
        info = self.resolve(symbol)
        try:
            r = self._sess.post(f"{BASE}/api/order_margin", json={
                "exchange": info["exch"], "tradingsymbol": info["tsym"],
                "quantity": info["lot_size"], "buy_or_sell": "B", "product_type": PRODUCT_TYPE}, timeout=10)
            d = r.json()
            return float(d.get("required") or (d.get("span", 0) + d.get("expo", 0)))
        except Exception:
            return 0.0

    # -- funds ------------------------------------------------------------
    def get_funds(self) -> dict:
        try:
            d = self._sess.get(f"{BASE}/api/funds", timeout=10).json()
            cash = float(d.get("cash") or 0.0)
            used = float(d.get("margin_used") or d.get("marginused") or 0.0)
            return {"cash": cash, "margin_available": cash, "margin_used": used}
        except Exception:
            return {"cash": 0.0, "margin_available": 0.0, "margin_used": 0.0}

    # -- search (futures AND options) ------------------------------------
    def search_symbols(self, query: str, exchange: str = "") -> list[dict]:
        exch = (exchange or EXCHANGE)

        from app.services.scripmaster import SCRIPMASTER
        try:
            SCRIPMASTER.ensure_fresh()
            hits = SCRIPMASTER.search(query, limit=50)
        except Exception:
            hits = []
        if hits:
            for r in hits:                      # warm resolve so an Add prices at once
                self._scrip.setdefault(r["tsym"], {
                    "tsym": r["tsym"], "token": r["token"], "lot_size": r["lot_size"],
                    "expiry": r["expiry"], "exch": r["exch"], "sym": r["sym"]})
                self._token_to_symbol[f"{r['exch']}|{r['token']}"] = r["tsym"]
                self._token_only[r["token"]] = r["tsym"]
            return [{
                "symbol": r["tsym"], "tsym": r["tsym"], "token": r["token"], "exch": r["exch"],
                "instr_type": "OPT" if "OPT" in r["instrument"] else "FUT",
                "expiry": r["expiry"], "lot_size": r["lot_size"],
                "strike": r["strike"] if "OPT" in r["instrument"] else "",
                "opttype": r["opttype"] if r["opttype"] not in ("XX", "") else "",
                "ltp": 0.0,
            } for r in hits]

        try:
            rows = self._sess.get(f"{BASE}/api/search",
                                  params={"exchange": exch, "q": query}, timeout=8).json().get("results", [])
        except Exception:
            return []
        out = []
        for r in rows:
            instr = (r.get("instrumenttype") or "").upper()
            is_opt = "OPT" in instr
            tsym = r.get("tsym") or ""
            token = str(r.get("token") or "")
            # Expired contracts are untradable and carry no live price — offering
            # one in the picker is how a dead JUN future gets added by mistake.
            expd = _parse_expiry(r.get("expd") or "")
            if expd is not None and expd < _today_ist():
                continue
            # warm the resolve cache so an Add can price/trade it immediately
            if tsym and token:
                self._scrip.setdefault(tsym, {
                    "tsym": tsym, "token": token, "lot_size": int(r.get("lotsize") or 1),
                    "expiry": r.get("expd") or "", "exch": r.get("exch") or exch})
                self._token_to_symbol[f"{r.get('exch') or exch}|{token}"] = tsym
            out.append({
                "symbol": tsym, "tsym": tsym, "token": token,
                "exch": r.get("exch") or exch,
                "instr_type": "OPT" if is_opt else "FUT",
                "expiry": r.get("expd") or "",
                "lot_size": int(r.get("lotsize") or 1),
                "strike": str(r.get("strikeprice") or ""),
                "opttype": r.get("opttype") or "",
                "ltp": 0.0,
            })
        return out[:50]


class _SyncWS:
    """Lets synchronous code send on an asyncio websocket from another thread."""
    def __init__(self, ws, loop):
        self._ws = ws
        self._loop = loop

    def send(self, text: str) -> None:
        import asyncio
        asyncio.run_coroutine_threadsafe(self._ws.send(text), self._loop)


def build_gateway():
    """Factory: returns the configured gateway based on env."""
    from app.core.config import BROKER_MODE
    if BROKER_MODE == "mock":
        from app.brokers.mock_gateway import MockShoonyaGateway
        return MockShoonyaGateway()
    return ShoonyaGateway()
