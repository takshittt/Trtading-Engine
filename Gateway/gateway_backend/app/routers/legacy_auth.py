import asyncio
import time as _time

import requests
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core import auth as _auth_module
from app.core import state
from app.core.deps import _require_legacy_auth
from app.core.security import Principal, require_scope
from app.services.targets import _load_target_positions, load_exchange_targets
from brokers.base import BrokerError, SessionToken
from brokers.registry import get_broker
from brokers.shoonya import _token_cache
from brokers.shoonya.authenticator import ShonyaAuthenticator
from brokers.shoonya.logout import logout as shoonya_logout
from brokers.shoonya.user_details import get_user_details
from crypto import decrypt_json
from db.engine import get_db
from db.models import Account, User
from ticker_manager import ticker_manager

router = APIRouter()


async def _reauth_shoonya(user: User, db: Session) -> bool:
    """Re-login the connected Shoonya session using the user's stored credentials.

    Called when a broker call rejects the in-memory session token (expired
    overnight or invalidated by an app login). Invalidates the stale cached
    token, performs a fresh OAuth login, and swaps the `_auth` singleton for the
    new session. Returns True if a fresh token was obtained.
    """
    account = (
        db.query(Account).filter_by(user_id=user.id)
        .order_by(Account.is_active.desc(), Account.id.asc()).first()
    )
    if account is None or account.broker.name != "shoonya":
        return False
    try:
        credentials = decrypt_json(account.credentials_enc)
        broker = get_broker(account.broker.name)
    except Exception:
        return False

    user_id = credentials.get("user_id")

    # A different session key may already sit in the DB (e.g. the ticker's
    # own relogin refreshed it) even though this in-memory token just failed.
    # Try that before paying for a fresh OAuth login.
    cached = _token_cache.get_cached_session(user_id, account.id)
    if cached:
        cached_token = SessionToken(
            token=cached["susertoken"],
            broker_uid=user_id,
            issued_at=cached["susertoken_issued_at"],
            broker_name="shoonya",
        )
        try:
            if await broker.validate_token(cached_token, credentials):
                _auth_module._auth = ShonyaAuthenticator.from_session(cached_token.token, cached_token.broker_uid)
                return True
        except Exception:
            pass

    stale = _auth_module._auth
    stub = SessionToken(
        token=stale.auth_token if stale else "",
        broker_uid=(stale.user_id if stale else user_id),
        issued_at="",
        broker_name="shoonya",
    )
    try:
        fresh = await broker.refreshSession(stub, credentials)
    except Exception:
        return False

    _auth_module._auth = ShonyaAuthenticator.from_session(fresh.token, fresh.broker_uid)
    return True


def _is_session_error(emsg: str) -> bool:
    """True if a Shoonya `stat != Ok` message looks like an expired/invalid session."""
    m = (emsg or "").lower()
    return any(k in m for k in ("session", "expired", "invalid token", "unauthor"))


@router.post("/api/connect")
async def connect_legacy(
    principal: Principal = Depends(require_scope("connect", user_needs_form=True)),
    db: Session = Depends(get_db),
):
    """Connect to the broker.

    A human user connects with their OWN stored credentials and always forces a
    fresh login — that is how a human recovers a session the broker killed
    mid-day (the token still reads "issued today", so only a forced re-login
    replaces it).

    A service (holding the `connect` scope) can trigger the same login on the
    single shared broker account, but a liveness guard short-circuits first:
    when the broker is already genuinely connected, a redundant service Connect
    is a no-op reply rather than a slow forced re-login on a healthy session.
    """
    if principal.kind == "service":
        # Idempotency guard: probe the live session with the SAME cheap check
        # /api/status uses, so "connected" means the same thing in both places.
        # Only a real outage should cost the ~180s headless re-login below.
        auth = _auth_module._auth
        if (auth is not None and auth.is_authenticated()
                and await _session_answers(auth.user_id, auth.auth_token)):
            return {"connected": True, "already_connected": True, "method": "rest",
                    "message": "Broker already connected — no action taken."}
        # A service token carries no human user, so it can't scope by user_id.
        # The live engine trades a single broker account (orders/positions all
        # ride one shared _auth singleton), so use whichever account a human has
        # marked active on the credentials page.
        account = db.query(Account).order_by(Account.is_active.desc(), Account.id.asc()).first()
    else:
        account = (
            db.query(Account).filter_by(user_id=principal.user.id)
            .order_by(Account.is_active.desc(), Account.id.asc()).first()
        )

    if account is None:
        raise HTTPException(status_code=400, detail="No broker configured")

    try:
        credentials = decrypt_json(account.credentials_enc)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Credential decryption failed: {e}")

    broker_name = account.broker.name
    try:
        broker = get_broker(broker_name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {broker_name}")

    try:
        credentials["_account_id"] = account.id
        token = await broker.login(credentials)
    except BrokerError as e:
        raise HTTPException(status_code=401, detail=f"Broker login failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Broker login error: {e}")

    if broker_name == "shoonya":
        # Populate the singleton the whole live engine expects, using the token
        # we just obtained from the user's stored credentials.
        _auth_module._auth = ShonyaAuthenticator.from_session(token.token, token.broker_uid)
        # We just logged in — make /api/status say so immediately for every other
        # polling service, rather than letting the stale liveness cache hide it.
        _mark_session_liveness(True)
        if not ticker_manager.is_running:
            ticker_manager.start(broker, token.token, token.broker_uid, asyncio.get_running_loop())
        load_exchange_targets()
        asyncio.create_task(_load_target_positions())
        return {"connected": True, "method": "rest", "broker": broker_name}

    raise HTTPException(status_code=400, detail=f"Unsupported broker: {broker_name}")


@router.post("/api/disconnect")
def disconnect_legacy(_=Depends(require_scope("connect", user_needs_form=False))):
    # Stop the live feed first, flagged intentional so the ticker's auto-reconnect
    # doesn't resurrect the socket we're about to log out from.
    ticker_manager.stop()
    if _auth_module._auth is not None and _auth_module._auth.is_authenticated():
        try:
            shoonya_logout(_auth_module._auth.user_id, _auth_module._auth.auth_token)
        except Exception:
            pass
    _auth_module._auth = None
    # Reflect the teardown in the cache so a stale "ok" can't briefly report the
    # broker as still up, and so the next service connect guard doesn't short-circuit.
    _mark_session_liveness(False)
    # Drop this account's in-memory target/position caches — otherwise a
    # different account connecting next in this same process would inherit
    # stale arm state (e.g. an auto-exited-token latch) for keys it never armed.
    state._target_positions.clear()
    state._symbol_targets.clear()
    state._auto_exited_tokens.clear()
    state._exchange_targets.clear()
    state._exchange_target_exited.clear()
    state._lot_targets.clear()
    state._lot_target_exited.clear()
    return {"connected": False}


# Holding a token is not the same as the broker still honouring it: Shoonya
# invalidates a session overnight, or the moment the user logs into its app, and
# then answers every call with an empty 200. This flag is the engine's entire
# trading gate, so "connected" has to mean the broker actually answered us.
# This REST probe is now only the FALLBACK — /api/status trusts the live
# WebSocket first (see status_legacy) and probes here only when the socket can't
# answer (mid-reconnect, or market-hours silence).
#
# Successes are cached _LIVENESS_TTL_OK (the engine polls every 15s, so this
# keeps the probe off the hot path). FAILURES get a much shorter TTL: a single
# transient network blip must NOT pin "disconnected" for 45s across every
# dashboard — the next poll re-checks almost immediately.
_LIVENESS_TTL_OK = 45.0
_LIVENESS_TTL_FAIL = 5.0
_liveness = {"at": 0.0, "ok": False}


def _mark_session_liveness(ok: bool) -> None:
    """Overwrite the cached liveness after a connect/disconnect we just performed.

    /api/status trusts `_liveness` for up to _LIVENESS_TTL_OK, and OTHER services
    learn the broker state only by polling /api/status. Without this, a connect
    or disconnect we just did stays invisible to them until the cache expires —
    up to 45s of a wrong badge across every other dashboard. We changed the state
    deliberately, so the truth is known: write it so the next poll from anyone is
    correct at once, without shortening the TTL (which would re-probe the broker
    far more often)."""
    _liveness.update(at=_time.monotonic(), ok=ok)


async def _session_answers(user_id: str, auth_token: str) -> bool:
    """Cheap, cached proof that the broker still accepts our session.

    A cached success is trusted for _LIVENESS_TTL_OK; a cached failure only for
    _LIVENESS_TTL_FAIL, so a transient probe error re-checks fast instead of
    flapping the broker badge to "disconnected" for the full success TTL."""
    now = _time.monotonic()
    ttl = _LIVENESS_TTL_OK if _liveness["ok"] else _LIVENESS_TTL_FAIL
    if now - _liveness["at"] < ttl:
        return _liveness["ok"]
    ok = False
    try:
        data = await asyncio.to_thread(get_user_details, user_id, auth_token)
        ok = isinstance(data, dict) and data.get("stat") == "Ok"
    except Exception:
        ok = False
    _liveness.update(at=now, ok=ok)
    return ok


@router.get("/api/status")
async def status_legacy(_=Depends(require_scope("status", user_needs_form=False))):
    """Broker-liveness for every strategy — WebSocket-primary, REST-fallback.

    The live Shoonya WebSocket (owned by the connection manager / TickerManager)
    reacts to a drop within seconds, so we trust it first: a healthy, authenticated
    feed means connected WITHOUT a REST round-trip. We fall back to the cached REST
    probe only when the socket can't answer — mid-(re)connect, market-hours silence,
    or the feed simply isn't running — and hard-fail on SESSION_EXPIRED. This
    replaces the old "REST probe cached 45s" path that turned any transient blip
    into up to 45s of false "disconnected" across every dashboard."""
    auth = _auth_module._auth
    has_token = auth is not None and auth.is_authenticated()
    live = ticker_manager.liveness()

    if not has_token:
        connected, method = False, "none"
    elif live["feed_healthy"]:
        # WS-primary: an authenticated, healthy socket is proof enough.
        connected, method = True, "ws"
    elif live["session_expired"]:
        # Reconnect exhausted → token is dead; a REST probe can't revive it.
        connected, method = False, "none"
    else:
        # Ambiguous (connecting, stale during market hours, or feed not running):
        # a held token that the broker no longer accepts is worse than no token, so
        # confirm against the broker over REST.
        connected = await _session_answers(auth.user_id, auth.auth_token)
        method = "rest" if connected else "none"

    resp = {
        "connected": connected,
        "method": method,
        "conn_state": live["state"],
        "subscribed_count": live["subscribed_count"],
    }
    try:
        from brokers.shoonya import netproxy
        resp["egress"] = netproxy.status()
    except Exception:
        pass
    return resp


@router.get("/api/status/subscribed")
async def status_subscribed(_=Depends(require_scope("status", user_needs_form=False))):
    """Every token currently held on the live WebSocket feed, with a readable
    symbol where the scripmaster knows it — backs the "Subscribed" detail view
    (the count alone, from /api/status, doesn't say which tokens they are)."""
    from brokers.shoonya.scripmaster import get_scripmaster

    sm = get_scripmaster()
    out = []
    for key in ticker_manager.subscribed_symbols():
        exch, _, token = key.partition("|")
        row = sm.lookup_by_token(exch, token) if token else None
        cached = ticker_manager.get_cached_quote(key) or {}
        out.append({
            "exch": exch,
            "token": token,
            "tsym": (row or {}).get("tsym", ""),
            "lp": str(cached.get("lp", "") or ""),
            "bp1": str(cached.get("bp1", "") or ""),
            "sp1": str(cached.get("sp1", "") or ""),
        })
    return {"symbols": out}


@router.post("/api/status/unsubscribe_all")
async def status_unsubscribe_all(_=Depends(require_scope("connect", user_needs_form=False))):
    """Drop every token currently held on the broker feed. Whatever is still
    needed (open positions, active lots/targets, an open option chain) gets
    re-subscribed automatically on the pollers' next cycle — nothing further
    to do here beyond clearing the feed."""
    count = ticker_manager.unsubscribe_all()
    return {"unsubscribed": count}


@router.get("/api/user")
async def user_details_legacy(
    principal: Principal = Depends(require_scope("profile", user_needs_form=False)),
    db: Session = Depends(get_db),
):
    # Try once with the current session; on an expired/invalid token, transparently
    # re-authenticate using the user's stored credentials and retry once. Only a
    # human caller can be re-authed (we need their stored broker credentials).
    can_reauth = principal.kind == "user"
    data: dict = {}
    for attempt in (1, 2):
        auth = _require_legacy_auth()
        try:
            data = await asyncio.to_thread(get_user_details, auth.user_id, auth.auth_token)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (401, 403) and attempt == 1 and can_reauth and await _reauth_shoonya(principal.user, db):
                continue
            raise HTTPException(status_code=502, detail=f"Shoonya API error: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Shoonya API error: {e}")

        if data.get("stat") != "Ok":
            emsg = data.get("emsg", "Failed to fetch user details")
            if attempt == 1 and can_reauth and _is_session_error(emsg) and await _reauth_shoonya(principal.user, db):
                continue
            raise HTTPException(status_code=400, detail=emsg)
        break

    return {
        "uid": data.get("uid", ""),
        "actid": data.get("actid", ""),
        "email": data.get("email", ""),
        "m_num": data.get("m_num", ""),
        "brkname": data.get("brkname", ""),
        "brnchid": data.get("brnchid", ""),
        "uprev": data.get("uprev", ""),
        "exarr": data.get("exarr", []),
        "orarr": data.get("orarr", []),
        "prarr": data.get("prarr", []),
        "request_time": data.get("request_time", ""),
    }
