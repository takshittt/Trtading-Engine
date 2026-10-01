"""Single sign-on against the shared Gateway — local JWT verification.

The Gateway is the single identity issuer; Swing never mints tokens. It verifies
the human *user* token presented to its dashboard LOCALLY, using the SAME HS256
secret as the Gateway (JWT_SECRET, identical value) — no database or network hop
per request. This mirrors grid_strategy's core/security.py.

Service tokens (typ=service) are rejected here: those authorize Swing→Gateway
broker calls, not the human dashboard. SSO is ALWAYS enforced (no off switch);
the Amibroker webhook is gated separately by AMI_WEBHOOK_SECRET.
"""
from __future__ import annotations

import os
from typing import Optional

import jwt
from fastapi import Header, HTTPException

_SECRET = os.getenv("JWT_SECRET", "")
if not _SECRET:
    raise RuntimeError(
        "JWT_SECRET is not set. It must be identical to the Gateway's JWT_SECRET "
        "so Swing can verify Gateway user tokens. Add it to backend/.env."
    )

_ALGO = "HS256"


def _decode_user_token(token: str) -> int:
    """Verify a Gateway *user* JWT and return the user id. Raises jwt exceptions.
    A service token is rejected — it's for broker calls, not the dashboard."""
    payload = jwt.decode(token, _SECRET, algorithms=[_ALGO])
    if payload.get("typ") != "user":
        raise jwt.InvalidTokenError("not a user token")
    return int(payload["sub"])


def require_user(authorization: Optional[str] = Header(default=None)) -> int:
    """FastAPI dependency: require a valid Gateway user Bearer token, or 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return _decode_user_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def user_from_token(token: str) -> Optional[int]:
    """Non-raising resolver for the WebSocket query-param path. Returns id or None.
    (ExpiredSignatureError subclasses InvalidTokenError, so expiry is covered.)"""
    try:
        return _decode_user_token(token)
    except jwt.InvalidTokenError:
        return None
