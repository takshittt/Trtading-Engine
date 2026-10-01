"""Human login proxy for the Swing dashboard.

Swing has NO user store — the shared Gateway is the single identity provider.
The browser posts credentials HERE (never to the Gateway directly), and this
backend relays them to the Gateway's /api/auth/signin, returning the Gateway-
issued *user* JWT to the browser. Every subsequent dashboard request carries that
token and is verified locally by app/core/sso.py using the shared JWT_SECRET.

Why proxy instead of letting the frontend call the Gateway? So the browser only
ever talks to Swing's own origin: the Gateway can stay a private, backend-only
service (no public exposure, no per-frontend CORS allowlist), reachable on the
same private address Swing already uses for the broker REST/WS and service-token
calls. This mirrors grid_strategy/backend/api/user_auth.py — the two services
now share one login topology.
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Same host Swing fetches service tokens from (see shoonya_gateway.py): the
# identity Gateway and the broker Gateway are one service. Read at request time
# so .env (loaded by app.core.config) is guaranteed present.
def _gateway_base() -> str:
    return os.getenv("SWING_GATEWAY_BASE_URL", "http://localhost:8000").rstrip("/")


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(req: LoginRequest):
    """Verify credentials against the Gateway and relay the user JWT back."""
    async with httpx.AsyncClient(base_url=_gateway_base(), timeout=15.0) as c:
        try:
            resp = await c.post("/api/auth/signin",
                                json={"email": req.email, "password": req.password})
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Gateway unreachable: {e}")

    if resp.status_code != 200:
        detail = "Invalid email or password"
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            pass
        # Relay client errors (e.g. 401 bad password) as-is; treat a Gateway
        # server error as an auth failure so the browser shows a login error
        # rather than a 5xx it can't act on.
        raise HTTPException(status_code=resp.status_code if resp.status_code < 500 else 401,
                            detail=detail)

    data = resp.json()
    return {"token": data["token"], "user": data["user"]}
