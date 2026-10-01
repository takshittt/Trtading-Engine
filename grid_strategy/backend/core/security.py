"""Local verification of the *human user* JWT for grid's own dashboard.

The Gateway is the single identity issuer. grid never mints tokens — it only
verifies them, locally, using the SAME HS256 secret (JWT_SECRET, identical to the
Gateway's). A valid Gateway user signature is sufficient to admit a request; no
database or network hop is needed. Service tokens (typ=service) are rejected here
— those are for grid→Gateway calls, not for driving grid's UI.
"""
from typing import Optional

import jwt
from fastapi import Header, HTTPException

from config.settings import settings  # imports load_dotenv() → JWT_SECRET is available

_SECRET = settings.jwt_secret
if not _SECRET:
    raise RuntimeError(
        "JWT_SECRET is not set. It must match the Gateway's JWT_SECRET so grid "
        "can verify user tokens. Add it to grid's .env."
    )

_ALGO = "HS256"


def decode_user_token(token: str) -> int:
    """Verify a Gateway *user* JWT and return the user id. Raises jwt exceptions."""
    payload = jwt.decode(token, _SECRET, algorithms=[_ALGO])
    if payload.get("typ") != "user":
        raise jwt.InvalidTokenError("not a user token")
    return int(payload["sub"])


def get_current_user_id(authorization: Optional[str] = Header(default=None)) -> int:
    """FastAPI dependency: require a valid Gateway user Bearer token, or 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return decode_user_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def user_id_from_token(token: str) -> Optional[int]:
    """Non-raising resolver for the WebSocket query-param path. Returns id or None."""
    try:
        return decode_user_token(token)
    except jwt.InvalidTokenError:
        return None
