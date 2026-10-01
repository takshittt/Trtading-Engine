"""Grid Strategy Engine — FastAPI entry point (port 8010).

Run:  uvicorn main:app --port 8010
Env:  GATEWAY_BASE_URL / GATEWAY_WS_URL / GRID_DB_URL
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from api.routes import build_router, build_ws_router
from api.user_auth import build_user_auth_router
from core.engine import GridEngine
from core.event_log import event_log
from core.security import get_current_user_id
from db.engine import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("grid")


class Runtime:
    def __init__(self):
        self.engine = GridEngine()
        self._ws_clients: set[WebSocket] = set()
        self.engine.on_snapshot = self._broadcast_snapshot
        event_log.on_event = self._broadcast_log

    async def start(self) -> None:
        init_db()
        self.engine._seed_econ_events()
        await self.engine.start()

    async def stop(self) -> None:
        await self.engine.stop()

    # ---- UI websocket fanout ----

    def add_ws(self, ws: WebSocket) -> None:
        self._ws_clients.add(ws)

    def remove_ws(self, ws: WebSocket) -> None:
        self._ws_clients.discard(ws)

    async def _broadcast(self, payload: dict) -> None:
        if not self._ws_clients:
            return
        msg = json.dumps(payload, default=str)
        dead = []
        for client in list(self._ws_clients):
            try:
                await client.send_text(msg)
            except Exception:
                dead.append(client)
        for d in dead:
            self._ws_clients.discard(d)

    async def _broadcast_snapshot(self, snap: dict) -> None:
        await self._broadcast({"type": "snapshot", "data": snap})

    async def _broadcast_log(self, entry: dict) -> None:
        await self._broadcast({"type": "log", "data": entry})


runtime = Runtime()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime.start()
    yield
    await runtime.stop()


app = FastAPI(title="Grid Strategy Engine", lifespan=lifespan)


# Catch-all JSON error middleware. MUST be registered BEFORE CORSMiddleware so
# it sits INSIDE it: an unhandled exception then comes back as a JSON 500 that
# still passes through CORS on the way out. Without this, the browser gets a
# bare 500 with no CORS headers and reports the useless "Failed to fetch".
@app.middleware("http")
async def _json_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500,
                            content={"detail": f"internal error: {type(exc).__name__}: {exc}"})


_ALLOWED = os.getenv("ALLOWED_ORIGINS",
                     "http://localhost:5175,http://localhost:5173,http://localhost:4173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _ALLOWED.split(",")],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


def _get_engine() -> "GridEngine":
    return runtime.engine


# Login proxy is OPEN (it's how you obtain a token). Verifies against the Gateway.
app.include_router(build_user_auth_router())
# Dashboard API requires a valid Gateway *user* JWT (verified locally).
app.include_router(build_router(_get_engine), dependencies=[Depends(get_current_user_id)])
# UI WebSocket validates the user token via ?token= inside the router.
app.include_router(build_ws_router(_get_engine, runtime.add_ws, runtime.remove_ws))

# Optional, fully isolated backtest module (yfinance) — the live engine is
# unaffected whether or not this import succeeds.
try:
    from backtest.routes import router as backtest_router
    app.include_router(backtest_router)
    logger.info("backtest module loaded")
except ImportError as _bt_err:
    logger.info("backtest module disabled (%s) — run `poetry install --with backtest` to enable", _bt_err)


@app.get("/health")
async def health():
    eng = runtime.engine
    return {
        "status": "ok",
        "engine_on": eng.engine_on(),
        "broker_connected": eng.broker_connected,
        "feed": eng.feed_state,
    }
