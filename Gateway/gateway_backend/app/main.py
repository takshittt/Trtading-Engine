"""FastAPI server for multi-broker, multi-account trading gateway."""
import tls_compat  # noqa: F401  # force TLS 1.2 (Shoonya rejects TLS 1.3) — must import first
import logging
import os

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.background import startup as background_startup
from app.core.security import require_form_filled, require_scope
from app.services.market_hours import load_market_hours_config
from app.services.targets import load_exchange_targets
from app.routers import (
    accounts,
    credentials,
    funds,
    health,
    legacy_auth,
    lots,
    market,
    market_hours,
    order_margin,
    orders,
    persistent_orders,
    positions,
    service_auth,
    targets,
    user_auth,
    watchlist,
    ws,
)
from db.engine import init_db

logger = logging.getLogger(__name__)

app = FastAPI(title="Gateway API")

_ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _ALLOWED_ORIGINS.split(",") if o.strip()],
    allow_methods=["GET", "POST", "DELETE", "PUT"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    init_db()
    # No account is connected yet at process startup — this just clears the
    # cache; the real (account-scoped) load happens on /api/connect.
    load_exchange_targets()
    # Load per-exchange market-hours overrides into session_windows before the
    # tick callbacks (auto-exit gate) start firing.
    load_market_hours_config()
    background_startup.register_ticker_callbacks()
    background_startup.launch_tasks()


# Open routers: user auth (signup/signin), service token grant, and health need no token.
app.include_router(user_auth.router)
app.include_router(service_auth.router)
app.include_router(health.router)

# legacy_auth guards its own endpoints per-route (connect requires form_filled;
# disconnect/status/user require a logged-in user).
app.include_router(legacy_auth.router)

# credentials form requires a logged-in user (guarded inside the router).
app.include_router(credentials.router)

# Trading/data routers require either a form-filled human user (full UI access) or
# a service whose token carries the router's scope. `require_scope` layers
# get_principal + the user form-gate / service scope-check.
app.include_router(accounts.router, dependencies=[Depends(require_scope("funds"))])
app.include_router(funds.router, dependencies=[Depends(require_scope("funds"))])
app.include_router(positions.router, dependencies=[Depends(require_scope("positions"))])
app.include_router(orders.router, dependencies=[Depends(require_scope("orders"))])
app.include_router(order_margin.router, dependencies=[Depends(require_scope("orders"))])
app.include_router(lots.router, dependencies=[Depends(require_scope("orders"))])
app.include_router(persistent_orders.router, dependencies=[Depends(require_scope("orders"))])
app.include_router(market.router, dependencies=[Depends(require_scope("market"))])
app.include_router(market_hours.router, dependencies=[Depends(require_scope("market"))])
# targets/watchlist are UI-only config — humans only (no service uses them).
_user_only = [Depends(require_form_filled)]
app.include_router(targets.router, dependencies=_user_only)
app.include_router(watchlist.router, dependencies=_user_only)

# WebSocket ticker authenticates via ?token= query param (see ws.py).
app.include_router(ws.router)
