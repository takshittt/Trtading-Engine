"""FastAPI dependency helpers."""
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.core import auth
from app.core.security import get_current_user
from brokers.shoonya.authenticator import ShonyaAuthenticator
from db.engine import get_db
from db.models import Account, User


def _require_legacy_auth() -> ShonyaAuthenticator:
    if auth._auth is None or not auth._auth.is_authenticated():
        raise HTTPException(status_code=401, detail="Not authenticated with Shoonya broker")
    return auth._auth


def resolve_owner_uid(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> str:
    """The broker account id (e.g. "FA29702") this user's saved credentials log
    in as — used to scope per-account config (watchlist, targets) that must
    stay readable/editable even while no broker session is live. This is the
    same identity `_require_legacy_auth().user_id` carries once connected, so
    rows saved while disconnected line up with rows read by the live engine.

    Uses whichever saved account is marked active (falling back to the oldest
    saved one), matching the account `/api/connect` would log in with.
    """
    account = (
        db.query(Account).filter_by(user_id=user.id)
        .order_by(Account.is_active.desc(), Account.id.asc()).first()
    )
    if account is None:
        raise HTTPException(status_code=400, detail="No broker account configured")
    from crypto import decrypt_json
    try:
        creds = decrypt_json(account.credentials_enc)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt stored credentials")
    owner_uid = (creds.get("user_id") or "").strip()
    if not owner_uid:
        raise HTTPException(status_code=400, detail="Saved broker account has no user_id")
    return owner_uid


def _active_broker_proxy(broker_name: str) -> dict:
    """The `proxy` / `proxy_mode` credential fields saved on whichever account
    is marked active for this broker — spread into a credentials stub, e.g.
    `{"user_id": ..., **_active_broker_proxy("shoonya")}`.

    The order-path routers (orders/lots/persistent_orders) build a bare
    `{"user_id", "account_id"}` credentials stub — they never decrypt the
    stored account row, since the live session is already authenticated via
    the `_auth` singleton. That stub reaches netproxy.post() too, though, so
    without this it never sees the account's proxy override (or its "Always
    route via proxy" checkbox) and orders sent while this host's IP is
    unregistered fail with ALGO_CHK: Invalid IP address instead of falling
    back to the proxy the way login does.
    """
    from crypto import decrypt_json
    from db.engine import SessionLocal
    from db.models import Account, Broker

    db = SessionLocal()
    try:
        account = (
            db.query(Account).join(Broker)
            .filter(Broker.name == broker_name)
            .order_by(Account.is_active.desc(), Account.id.asc())
            .first()
        )
        if account is None:
            return {}
        try:
            creds = decrypt_json(account.credentials_enc)
        except Exception:
            return {}
        return {"proxy": creds.get("proxy"), "proxy_mode": creds.get("proxy_mode")}
    finally:
        db.close()
