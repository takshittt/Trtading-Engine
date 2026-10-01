"""Reversal Strategy — FastAPI server.

Isolated strategy backend: Signal Dashboard + Automated Exit Manager.
Completely decoupled from the main Gateway; runs its own DB, its own dedicated
Shoonya order gateway, and its own background workers.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import background
from app.core.market_hours import refresh_from_config
from app.core.sso import require_user
from app.core.state import STATE
from app.routers import account, paper, positions, signals, user_auth, ws
from db.engine import get_config, init_db, session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reversal_strategy")

app = FastAPI(title="Reversal Strategy API", version="1.0.0")

_ALLOWED = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _ALLOWED.split(",") if o.strip()],
    allow_methods=["GET", "POST", "DELETE", "PUT"],
    allow_headers=["*"],
)

# Human dashboard routers always sit behind Gateway SSO. The login proxy and the
# dashboard WS must NOT: login is how you obtain a token in the first place, and
# the WS validates its token from the query param (browsers can't set an
# Authorization header on a WebSocket). Both are gated inside their routers.
_sso = [Depends(require_user)]
app.include_router(signals.router, dependencies=_sso)
app.include_router(positions.router, dependencies=_sso)
app.include_router(account.router, dependencies=_sso)
app.include_router(paper.router, dependencies=_sso)
app.include_router(user_auth.router)
app.include_router(ws.router)


@app.on_event("startup")
async def on_startup():
    init_db()
    # Load the market session into the cache before any worker can ask whether
    # the market is open — they'd otherwise run on the seed hours until the
    # first config save.
    with session() as db:
        refresh_from_config(get_config(db))
    STATE.hub.bind_loop(asyncio.get_running_loop())
    # Launch price feed, mock/live signal source, reconciliation + EOD loops.
    background.launch()
    logger.info("Reversal Strategy started (broker + signal workers live).")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "reversal-strategy"}
