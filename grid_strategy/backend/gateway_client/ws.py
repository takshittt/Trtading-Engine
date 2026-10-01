"""WebSocket subscriber for the Gateway ticker stream.

Carries two frame types from the gateway:
  - price ticks:    {"e": "MCX", "tk": "432556", "lp": "...", "bp1": "...", "sp1": "..."}
  - order updates:  {"type": "order_update", "norenordno": "...", "status": "...", ...}

Also owns the heartbeat bookkeeping: `last_rx_monotonic` is updated on every
frame; the engine's watchdog reads it to decide FEED_STALE.
"""

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from config.settings import settings
from gateway_client.auth import fetch_service_token

logger = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[None]]


class GatewayWS:
    def __init__(self, on_tick: Handler, on_order_update: Handler):
        self.on_tick = on_tick
        self.on_order_update = on_order_update
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._subscribed: set[str] = set()
        self._ws = None
        self.connected: bool = False
        self.last_rx_monotonic: float = 0.0     # any frame
        self.reconnect_count: int = 0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()

    def seconds_since_rx(self) -> float:
        if self.last_rx_monotonic == 0.0:
            return 1e9
        return time.monotonic() - self.last_rx_monotonic

    async def subscribe(self, symbol_key: str) -> None:
        """symbol_key: 'EXCH|TOKEN' e.g. 'MCX|432556'."""
        self._subscribed.add(symbol_key)
        await self._send({"action": "subscribe", "symbols": [symbol_key]})

    def is_subscribed(self, symbol_key: str) -> bool:
        """The gateway broadcasts every token ANY client subscribed — this tells
        the engine whether a frame is for a token it actually asked for."""
        return symbol_key in self._subscribed

    async def unsubscribe(self, symbol_key: str) -> None:
        self._subscribed.discard(symbol_key)
        await self._send({"action": "unsubscribe", "symbols": [symbol_key]})

    async def _send(self, msg: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(msg))
        except Exception as e:
            logger.warning("WS send failed: %s", e)

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                # The Gateway ticker WS authenticates via ?token=<service JWT>
                # (WS can't send headers). Fetch a fresh token each (re)connect so
                # the ~1h service-token expiry is handled by the reconnect loop.
                token = await fetch_service_token()
                url = f"{settings.gateway_ws_url}?token={token}"
                async with websockets.connect(url, ping_interval=10, ping_timeout=10) as ws:
                    self._ws = ws
                    self.connected = True
                    self.last_rx_monotonic = time.monotonic()
                    backoff = 1.0
                    if self._subscribed:
                        await ws.send(json.dumps({"action": "subscribe", "symbols": list(self._subscribed)}))
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        try:
                            if data.get("type") == "order_update":
                                await self.on_order_update(data)
                            elif "tk" in data:
                                # The heartbeat counts PRICE frames only. Status
                                # and alert frames also arrive on this socket, and
                                # counting those made the feed flap
                                # LIVE→STALE→LIVE every ~30s outside market hours
                                # — each "recovery" kicking a reconcile, which is
                                # how a position-closing guard meant to need
                                # minutes of agreement got through in one minute.
                                self.last_rx_monotonic = time.monotonic()
                                await self.on_tick(data)
                        except Exception:
                            logger.exception("WS handler error")
            except ConnectionClosed:
                logger.warning("Gateway WS closed; reconnect in %.1fs", backoff)
            except Exception as e:
                logger.warning("Gateway WS error: %s; reconnect in %.1fs", e, backoff)
            finally:
                self._ws = None
                self.connected = False
            if self._stop.is_set():
                break
            self.reconnect_count += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)
