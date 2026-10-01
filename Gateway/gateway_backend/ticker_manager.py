"""TickerManager: bridges Shoonya NorenApi WebSocket to frontend FastAPI WebSocket clients."""

import asyncio
import json
import time
from typing import Callable, Optional

from fastapi import WebSocket

from session_windows import is_session_open

# --- self-healing feed ------------------------------------------------------
# A human establishes the broker session (POST /api/connect); the gateway is
# responsible for keeping the WebSocket alive after that. When the socket drops
# or goes silent we reopen it with the SAME session — no human, no restart. Only
# when the session itself has expired (reopen keeps failing) do we hand it back
# to the human with a "please reconnect" alert.
_STALE_SECONDS = 30.0          # no tick this long during an open session → force reconnect
_HEARTBEAT_INTERVAL = 5.0      # how often the connection-manager heartbeat checks the feed
_REOPEN_WAIT = 15.0            # wait this long for _on_open to fire after a reopen
_RECONNECT_BACKOFF = (2.0, 5.0, 10.0)   # per-attempt wait; exhausting these ⇒ try a fresh login
# Reopening the SAME session (_RECONNECT_BACKOFF above) failed → the broker
# has likely invalidated that session key itself. Rather than immediately
# handing the feed back to a human, keep retrying a FRESH login (new session
# key) on this backoff, uncapped, for as long as the failures look transient
# (network blip, broker outage). We only stop and alert the human when the
# broker explicitly rejects the credentials/OTP — see BrokerError code
# "INVALID_CREDENTIALS" — since retrying THAT would only risk tripping the
# broker's own account lockout for no benefit.
_RELOGIN_BACKOFF = (30.0, 60.0, 120.0, 300.0)   # last value repeats thereafter

# Connection-manager states. `/api/status` reports these (WS-primary) so every
# strategy sees one broker-liveness truth instead of each polling Shoonya over REST.
CONNECTED = "CONNECTED"            # socket open + Shoonya auth ack received
CONNECTING = "CONNECTING"         # starting up or mid-reconnect
DISCONNECTED = "DISCONNECTED"     # not running (never started / human disconnect)
SESSION_EXPIRED = "SESSION_EXPIRED"   # reconnect exhausted → token dead, human needed


class TickerManager:
    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._subscribed: set[str] = set()   # "NSE|22" / "MCX|432556" format
        # Last known quote per subscribed symbol, kept fresh by every tick — the
        # single shared cache every strategy's /api/quote & /api/option-chain call
        # reads from instead of each hitting the broker over REST independently.
        self._last_quote: dict[str, dict] = {}
        self._last_quote_mono: dict[str, float] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._broker = None
        self.is_running: bool = False    # True after start_websocket() is called
        self._ws_open: bool = False      # True only after _on_open fires (WS actually ready)
        self.on_tick_callback: Optional[Callable] = None             # async fn called on every tick
        self.on_order_update_callback: Optional[Callable] = None     # async fn called on every order update
        self.on_reconnect_callback: Optional[Callable] = None        # async fn called after a successful auto-reconnect (Verify step)
        # async fn () -> "ok" | "retry" | "credential_error", called when
        # reopening the OLD session is exhausted. On "ok" it has already
        # restarted the ticker itself (via start()) with the new session.
        self.on_relogin_callback: Optional[Callable] = None

        # connection-manager state (see /api/status). Event-driven from the
        # NorenApi callbacks below; the heartbeat loop only forces reconnects.
        self.conn_state: str = DISCONNECTED

        # session held from the human's connect, reused to reopen the socket
        self._auth_token: Optional[str] = None
        self._uid: Optional[str] = None

        # self-healing state
        self._last_tick_at: float = 0.0        # monotonic of the last tick seen
        self._intentional_stop: bool = False   # True after stop() — suppress auto-reconnect
        self._reconnecting: bool = False       # guard against overlapping reconnects
        self._session_expired: bool = False    # latched when reopen keeps failing
        self._watchdog_started: bool = False

    def start(self, broker, auth_token: str, uid: str, loop: asyncio.AbstractEventLoop) -> None:
        """
        Start the Shoonya WebSocket connection.
        `loop` must be the running event loop, passed from the async route handler
        via asyncio.get_running_loop().
        """
        if self.is_running:
            return
        self._loop = loop
        self._broker = broker
        self._auth_token = auth_token
        self._uid = uid
        # A fresh human connect clears any prior "expired" latch and stop flag.
        self._intentional_stop = False
        self._session_expired = False
        self.conn_state = CONNECTING   # flips to CONNECTED once _on_open fires
        broker._api.set_credentials(auth_token, uid, uid)
        broker.start_websocket(
            subscribe_callback=self._on_tick,
            socket_open_callback=self._on_open,
            socket_close_callback=self._on_close,
            order_update_callback=self._on_order_update,
            socket_error_callback=self._on_ws_error,
        )
        self.is_running = True
        self._ensure_watchdog()
        print(f"[TickerManager] start_websocket called, waiting for _on_open...")

    def stop(self) -> None:
        """Human-initiated disconnect (POST /api/disconnect).

        Marks the stop as intentional so the socket-close callback does NOT
        auto-reconnect, then tears the Shoonya WebSocket down.
        """
        self._intentional_stop = True
        self.is_running = False
        self._ws_open = False
        self.conn_state = DISCONNECTED
        if self._broker is not None:
            try:
                self._broker._stop_websocket()
            except Exception as e:
                print(f"[TickerManager] stop: teardown failed: {e}")

    def _ensure_watchdog(self) -> None:
        """Launch the stale-feed watchdog once, on the event loop."""
        if self._watchdog_started or self._loop is None:
            return
        self._watchdog_started = True
        self._loop.create_task(self._watchdog_loop())

    # ------------------------------------------------------------------
    # NorenApi callbacks (called from background thread)
    # ------------------------------------------------------------------

    def _on_open(self) -> None:
        self._ws_open = True
        self.conn_state = CONNECTED
        # A successful (re)open means the session is alive again — clear the
        # expired latch and give the watchdog a fresh grace period.
        self._session_expired = False
        self._last_tick_at = time.monotonic()
        print(f"[TickerManager] Shoonya WS connected. Subscribing {len(self._subscribed)} queued symbols: {self._subscribed}")
        if self._subscribed:
            self._broker.ws_subscribe(list(self._subscribed))
        try:
            self._broker.ws_subscribe_orders()
            print("[TickerManager] subscribed to broker order updates")
        except Exception as e:
            print(f"[TickerManager] order-update subscribe failed: {e}")

    def _on_ws_error(self, error) -> None:
        """NorenApi's socket_error_callback — the underlying websocket-client
        exception, if any, right before a disconnect. Logged only: `_on_close`
        still owns the reconnect decision. Purely diagnostic, so we can tell an
        IP-level rejection apart from a plain network drop the next time the
        feed dies instead of everyone doing that (see netproxy.py: the feed's
        socket isn't proxied, so if this ever turns out to be IP-related too,
        that fallback needs extending to cover it)."""
        print(f"[TickerManager] Shoonya WS error: {error!r}")

    def _on_close(self) -> None:
        # Fires from NorenApi's background thread. Don't drop is_running here —
        # the reconnect coroutine owns the session lifecycle now. Just kick a
        # reconnect, unless the human deliberately disconnected.
        print("[TickerManager] Shoonya WS disconnected.")
        self._ws_open = False
        if self._intentional_stop:
            self.is_running = False
            self.conn_state = DISCONNECTED
            return
        # Not a human disconnect → we're about to auto-reconnect.
        self.conn_state = CONNECTING
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._reconnect("socket closed"), self._loop)

    def _on_tick(self, tick: dict) -> None:
        if not self._loop:
            return
        # "tk" = touchline ack (initial price sent on subscribe)
        # "tf" = touchline feed (subsequent price updates)
        if tick.get("t") not in ("tf", "tk"):
            return
        self._last_tick_at = time.monotonic()   # heartbeat for the stale-feed watchdog
        key = f"{tick.get('e', '')}|{tick.get('tk', '')}"
        if key != "|":
            # "tf" frames carry only the fields that changed — merge onto
            # whatever's cached so a partial frame never blanks out fields
            # (e.g. lot size, tick size) an earlier REST seed or "tk" ack filled in.
            cur = self._last_quote.get(key, {})
            self._last_quote[key] = {**cur, **{k: v for k, v in tick.items() if v not in (None, "")}}
            self._last_quote_mono[key] = self._last_tick_at
        if self._clients:
            asyncio.run_coroutine_threadsafe(self._broadcast(tick), self._loop)
        if self.on_tick_callback:
            asyncio.run_coroutine_threadsafe(self.on_tick_callback(tick), self._loop)

    def _on_order_update(self, order: dict) -> None:
        """Fires on every broker order state change (placed/filled/cancelled/rejected)."""
        if not self._loop:
            return
        if self.on_order_update_callback:
            asyncio.run_coroutine_threadsafe(self.on_order_update_callback(order), self._loop)
        if self._clients:
            asyncio.run_coroutine_threadsafe(self._broadcast_order(order), self._loop)

    async def _broadcast_order(self, order: dict) -> None:
        payload = json.dumps({
            "type":       "order_update",
            "norenordno": order.get("norenordno", ""),
            "status":     order.get("status", ""),
            "remarks":    order.get("remarks", ""),
            "tsym":       order.get("tsym", ""),
            "exch":       order.get("exch", ""),
        })
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception as e:
                print(f"[TickerManager] order-update send failed: {e}")
                dead.add(ws)
        self._clients -= dead

    async def broadcast_alert(self, level: str, message: str) -> None:
        """Push a user-facing alert (e.g. an auto-rollover result) to all
        connected frontend clients. level is "success" | "error" | "info"."""
        await self._send_all({"type": "alert", "level": level, "message": message})

    async def broadcast_feed_status(self, state: str, message: str = "") -> None:
        """Push live-feed health to the UI on its own channel (separate from the
        rollover alert channel). state is "session_expired" (human must Connect)
        or "live" (feed healed — clear any warning)."""
        await self._send_all({"type": "feed_status", "state": state, "message": message})

    async def _send_all(self, payload_dict: dict) -> None:
        payload = json.dumps(payload_dict)
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception as e:
                print(f"[TickerManager] send failed: {e}")
                dead.add(ws)
        self._clients -= dead

    # ------------------------------------------------------------------
    # Async helpers (run on the event loop)
    # ------------------------------------------------------------------

    async def _broadcast(self, tick: dict) -> None:
        # "tf" frames carry only the fields that changed - broadcasting the raw
        # tick would send "" for lp/bp1/sp1/o/c/v whenever this particular frame
        # didn't touch them, blanking them out on every client even though
        # _on_tick already merged them into self._last_quote just above. Source
        # the broadcast from that merged cache instead so clients always see the
        # last known full quote, not just this frame's delta.
        key = f"{tick.get('e', '')}|{tick.get('tk', '')}"
        merged = self._last_quote.get(key, tick)
        payload = json.dumps({
            "e":   tick.get("e", ""),
            "tk":  tick.get("tk", ""),
            "lp":  merged.get("lp", ""),
            "bp1": merged.get("bp1", ""),
            "sp1": merged.get("sp1", ""),
            "o":   merged.get("o", ""),
            "c":   merged.get("c", ""),
            # last-trade qty is per-tick, not a persistent quote field - the
            # engines' ghost-tick guard reads it to mean "this frame was a real
            # trade", so it must reflect only THIS tick, not a merged/stale one.
            "ltq": tick.get("ltq", ""),
            "v":   merged.get("v", ""),
        })
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception as e:
                print(f"[TickerManager] client send failed: {e}")
                dead.add(ws)
        self._clients -= dead

    async def send_snapshot(self, ws: WebSocket, symbols: list[str]) -> None:
        """Replay the last known quote for each symbol directly to one client.

        subscribe() below dedups against the broker-wide _subscribed set, so a
        client that (re)connects and asks for tokens the broker is already
        streaming gets no reply from subscribe() itself — it would otherwise
        sit blank until the next organic tick, which may not come for a long
        time (e.g. outside market hours). This fills that gap without
        touching the broker-level dedup.
        """
        dead: set[WebSocket] = set()
        for sym in symbols:
            quote = self._last_quote.get(sym)
            if not quote:
                continue
            exch, _, tok = sym.partition("|")
            payload = json.dumps({
                "e": exch,
                "tk": tok,
                "lp": quote.get("lp", ""),
                "bp1": quote.get("bp1", ""),
                "sp1": quote.get("sp1", ""),
                "o": quote.get("o", ""),
                "c": quote.get("c", ""),
                "ltq": quote.get("ltq", ""),
                "v": quote.get("v", ""),
            })
            try:
                await ws.send_text(payload)
            except Exception as e:
                print(f"[TickerManager] snapshot send failed: {e}")
                dead.add(ws)
                break
        self._clients -= dead

    async def add_client(self, ws: WebSocket) -> None:
        self._clients.add(ws)
        print(f"[TickerManager] frontend client connected. total={len(self._clients)}")

    def remove_client(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        print(f"[TickerManager] frontend client disconnected. total={len(self._clients)}")

    # ------------------------------------------------------------------
    # Shared quote cache — every REST quote consumer (many strategies through
    # one gateway) reads the same cache instead of each hitting the broker.
    # ------------------------------------------------------------------

    def get_cached_quote(self, key: str) -> Optional[dict]:
        """Last known quote fields for "EXCH|TOKEN", or None if never seen."""
        return self._last_quote.get(key)

    def cache_age(self, key: str) -> Optional[float]:
        ts = self._last_quote_mono.get(key)
        return (time.monotonic() - ts) if ts is not None else None

    def seed_quote_cache(self, key: str, fields: dict) -> None:
        """Merge a REST quote response into the cache so static fields (lot
        size, tick size, previous close) that never arrive over the tick feed
        are still available to the next reader, WS-fed or not."""
        cur = self._last_quote.get(key, {})
        self._last_quote[key] = {**cur, **{k: v for k, v in fields.items() if v not in (None, "")}}
        self._last_quote_mono[key] = time.monotonic()

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    def subscribe(self, symbols: list[str]) -> None:
        # positions/orders/lots endpoints call this on every poll with the same
        # symbol set already held — only log and touch the broker for symbols
        # that are actually new, so a healthy feed doesn't spam the log.
        new_syms = [sym for sym in symbols if sym not in self._subscribed]
        if not new_syms:
            return
        self._subscribed.update(new_syms)
        print(f"[TickerManager] subscribe({new_syms}) — ws_open={self._ws_open}")
        if self._ws_open:
            # WS is ready — one batched frame for every new symbol at once
            # (NorenApi '#'-joins a list into a single subscribe frame)
            # instead of a separate socket call per symbol.
            self._broker.ws_subscribe(new_syms)
        # else: symbols are queued in _subscribed; _on_open will subscribe them

    def unsubscribe(self, symbols: list[str]) -> None:
        held = [sym for sym in symbols if sym in self._subscribed]
        for sym in held:
            self._subscribed.discard(sym)
            self._last_quote.pop(sym, None)
            self._last_quote_mono.pop(sym, None)
        if held and self._ws_open:
            self._broker.ws_unsubscribe(held)

    def unsubscribe_all(self) -> int:
        """Drop every token currently held (the "Unsubscribe All" button).

        Only clears the broker feed and this manager's own bookkeeping — the
        callers that populate _subscribed (positions/lots/targets pollers,
        market-data and option-chain routes, manual WS requests) keep calling
        subscribe() on their own schedule, so anything still relevant to an
        open position or an active watch re-subscribes on its own within one
        poll cycle without any extra wiring here.
        """
        held = list(self._subscribed)
        self._subscribed.clear()
        self._last_quote.clear()
        self._last_quote_mono.clear()
        if held and self._ws_open:
            self._broker.ws_unsubscribe(held)
        print(f"[TickerManager] unsubscribe_all: dropped {len(held)} symbols")
        return len(held)

    # ------------------------------------------------------------------
    # Self-healing: reconnect the socket without a human, unless the
    # broker SESSION itself has expired (then alert and wait for connect).
    # ------------------------------------------------------------------

    async def _reconnect(self, reason: str) -> None:
        """Reopen the Shoonya WebSocket with the session we already hold.

        Retries a few times with backoff. If every attempt fails, the session
        key itself is almost certainly dead (broker invalidated it — this is
        what a stored/cached session key surviving a restart still can't fix)
        — hand off to _attempt_relogin rather than giving up outright.
        """
        if self._reconnecting or self._intentional_stop:
            return
        self._reconnecting = True
        self.conn_state = CONNECTING
        try:
            for attempt, backoff in enumerate(_RECONNECT_BACKOFF, start=1):
                if self._intentional_stop:
                    return
                print(f"[TickerManager] reconnect attempt {attempt}/{len(_RECONNECT_BACKOFF)} ({reason})")
                if await self._reopen_once():
                    print("[TickerManager] reconnect succeeded — feed live again")
                    # Verify step: re-sync orders/positions for any fills missed
                    # while the feed was down. _on_open already re-subscribed.
                    await self._run_verify_hook()
                    return
                await asyncio.sleep(backoff)
            await self._attempt_relogin(reason)
        finally:
            self._reconnecting = False

    async def _attempt_relogin(self, reason: str) -> None:
        """The old session key won't reopen — get a NEW one and resume.

        Keeps retrying a fresh login on backoff for as long as failures look
        transient (network blip, broker outage). Stops and hands off to the
        human ONLY when on_relogin_callback reports "credential_error" (the
        broker itself rejected the credentials/OTP) — repeating that would
        only risk tripping the broker's own account lockout.
        """
        if self.on_relogin_callback is None:
            await self._declare_session_expired(reason)
            return

        self._session_expired = False   # not "expired, wait for human" yet — actively retrying
        self.is_running = False         # let a successful relogin's start() call rearm cleanly
        self._ws_open = False
        attempt = 0
        while True:
            if self._intentional_stop:
                return
            attempt += 1
            backoff = _RELOGIN_BACKOFF[min(attempt, len(_RELOGIN_BACKOFF)) - 1]
            print(f"[TickerManager] relogin attempt {attempt} ({reason})")
            try:
                result = await self.on_relogin_callback()
            except Exception as e:
                print(f"[TickerManager] relogin callback error: {e!r}")
                result = "retry"

            if result == "ok":
                print("[TickerManager] relogin succeeded — feed resumed on a fresh session")
                await self._run_verify_hook()
                return
            if result == "credential_error":
                print("[TickerManager] relogin stopped — broker rejected credentials")
                await self._declare_session_expired(f"{reason} (invalid credentials)")
                return

            # "retry": transient failure — wait and try again, uncapped.
            if self._intentional_stop:
                return
            await asyncio.sleep(backoff)

    async def _run_verify_hook(self) -> None:
        """Verify step of the connection manager. Runs after a successful
        auto-reconnect. A failure here must not break the reconnect — the feed is
        already live again — so it's caught and logged."""
        if self.on_reconnect_callback is None:
            return
        try:
            await self.on_reconnect_callback()
        except Exception as e:
            print(f"[TickerManager] verify-after-reconnect hook failed: {e}")

    async def _reopen_once(self) -> bool:
        """Tear down and reopen the socket. True if _on_open fired in time."""
        if self._broker is None or self._loop is None:
            return False
        self._ws_open = False

        def _do_reopen() -> None:
            # Credentials persist on _api from start(); re-set to be explicit.
            self._broker._api.set_credentials(self._auth_token, self._uid, self._uid)
            self._broker.start_websocket(
                subscribe_callback=self._on_tick,
                socket_open_callback=self._on_open,
                socket_close_callback=self._on_close,
                order_update_callback=self._on_order_update,
            )

        try:
            # start_websocket tears down the old thread (blocking join) then
            # spawns a new one — keep it off the event loop.
            await self._loop.run_in_executor(None, _do_reopen)
        except Exception as e:
            print(f"[TickerManager] reopen call failed: {e}")
            return False

        # _on_open (background thread) flips _ws_open once the socket is live.
        deadline = time.monotonic() + _REOPEN_WAIT
        while time.monotonic() < deadline:
            if self._ws_open:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _declare_session_expired(self, reason: str) -> None:
        """Reconnect exhausted: hand the session back to the human."""
        self._session_expired = True
        self._ws_open = False
        self.conn_state = SESSION_EXPIRED
        # Drop is_running so POST /api/connect (guarded on `not is_running`)
        # re-arms the feed with a freshly-issued token.
        self.is_running = False
        print(f"[TickerManager] session appears expired ({reason}); awaiting human reconnect")
        await self.broadcast_feed_status(
            "session_expired",
            "Live price feed disconnected — the broker session has expired. "
            "Please click Connect to log back in.",
        )

    def _any_subscribed_session_open(self) -> bool:
        """True if any subscribed exchange is inside its trading window — i.e.
        ticks are expected right now. Keeps the watchdog quiet overnight."""
        exchanges = {s.split("|", 1)[0] for s in self._subscribed if "|" in s}
        return any(is_session_open(e) for e in exchanges)

    def liveness(self) -> dict:
        """Snapshot the connection-manager health for `/api/status` (WS-primary).

        `feed_healthy` is the WS-primary "connected" signal: an authenticated
        socket that is EITHER idle in a closed market (silence is expected) OR
        delivering ticks within the stale window. When it's False the caller
        should fall back to the REST liveness probe, because the socket alone
        can't confirm the session then — we're mid-(re)connect, or the market is
        open but silent (which could be a half-dead feed the watchdog is about to
        reconnect). Shoonya sends no server-side heartbeat, so tick freshness is
        the only positive liveness signal during market hours."""
        now = time.monotonic()
        tick_age = (now - self._last_tick_at) if self._last_tick_at else None
        session_open = self._any_subscribed_session_open()
        feed_healthy = bool(
            self.conn_state == CONNECTED and self._ws_open
            and (not session_open or (tick_age is not None and tick_age < _STALE_SECONDS))
        )
        return {
            "state": self.conn_state,
            "ws_open": self._ws_open,
            "is_running": self.is_running,
            "reconnecting": self._reconnecting,
            "session_expired": self._session_expired,
            "session_open": session_open,
            "last_tick_age": round(tick_age, 1) if tick_age is not None else None,
            "feed_healthy": feed_healthy,
            "subscribed_count": len(self._subscribed),
        }

    def subscribed_symbols(self) -> list[str]:
        """Every "EXCH|TOKEN" currently held, for a detail view of what's live."""
        return sorted(self._subscribed)

    async def _watchdog_loop(self) -> None:
        """Force a reconnect if the socket looks open but has gone silent during
        an open session — catches half-dead connections that never fire close."""
        print("[TickerManager] stale-feed watchdog started")
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            try:
                if not (self.is_running and self._ws_open):
                    continue
                if self._reconnecting or self._intentional_stop or self._session_expired:
                    continue
                if not self._subscribed or self._last_tick_at == 0.0:
                    continue
                if not self._any_subscribed_session_open():
                    continue
                silent_for = time.monotonic() - self._last_tick_at
                if silent_for >= _STALE_SECONDS:
                    print(f"[TickerManager] no ticks for {silent_for:.0f}s during open session — forcing reconnect")
                    await self._reconnect(f"stale feed {silent_for:.0f}s")
            except Exception as e:
                print(f"[TickerManager] watchdog error: {e}")


# Module-level singleton imported by server.py
ticker_manager = TickerManager()
