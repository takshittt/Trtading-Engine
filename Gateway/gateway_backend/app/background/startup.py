"""Startup wiring: register ticker callbacks and launch long-running tasks.

Called from the FastAPI `@app.on_event("startup")` handler in `app.main`.
Order preserved from the original monolithic server.py.
"""
import asyncio
import logging

from app.background.persistent_sweeper import _persistent_sweeper_loop
from app.background.position_sync import _sync_positions_to_lots_on_startup
from app.core import auth as _auth_module
from app.services.reconciliation import _on_order_update_event, _short_poll_reconcile
from app.services.targets import _on_target_tick
from ticker_manager import ticker_manager

_log = logging.getLogger(__name__)


async def _auto_reconnect_shoonya() -> None:
    """Silently resume the live session across a backend restart — but ONLY
    when a same-day cached broker token already exists.

    A restart (crash, deploy, or `--reload` picking up a code change) used to
    always drop back to "the human must click Connect", even when today's
    session token was still perfectly valid — disruptive mid-session, and
    actively annoying while developing, where every saved file restarts the
    process. This deliberately does NOT perform a fresh login: that can mean a
    ~2min headless-browser run, which nobody wants kicked off silently in the
    background on every process boot. It only rehydrates the `_auth` singleton
    and the ticker WebSocket from a token Shoonya would still accept anyway —
    the exact same cache `_login_sync` already checks first on a human Connect.
    """
    # Local imports: keeps this module's import graph from ballooning on every
    # startup path that doesn't need the broker stack (most of them).
    from brokers.registry import get_broker
    from brokers.shoonya import _token_cache
    from brokers.shoonya.authenticator import ShonyaAuthenticator
    from crypto import decrypt_json
    from db.engine import SessionLocal
    from db.models import Account, Broker

    db = SessionLocal()
    try:
        account = (
            db.query(Account).join(Broker)
            .filter(Broker.name == "shoonya")
            .order_by(Account.is_active.desc(), Account.id.asc())
            .first()
        )
        if account is None:
            return
        try:
            credentials = decrypt_json(account.credentials_enc)
        except Exception:
            return

        broker_uid = credentials.get("user_id")
        if not broker_uid or _token_cache.get_cached_session(broker_uid, account.id) is None:
            return  # no valid same-day cached session in the DB — a human must Connect

        broker = get_broker("shoonya")
        try:
            credentials["_account_id"] = account.id
            token = await broker.login(credentials)  # cache hit inside — no Selenium
        except Exception as e:
            _log.warning("auto-reconnect: cached-session login failed: %s", e)
            return

        _auth_module._auth = ShonyaAuthenticator.from_session(token.token, token.broker_uid)
        # Import here too — app.routers.legacy_auth doesn't import this module,
        # so this stays a one-way dependency (no import cycle).
        from app.routers.legacy_auth import _mark_session_liveness
        _mark_session_liveness(True)
        if not ticker_manager.is_running:
            ticker_manager.start(broker, token.token, token.broker_uid, asyncio.get_running_loop())
        _log.info("auto-reconnect: resumed Shoonya session for %s from cache", token.broker_uid)
    finally:
        db.close()


async def _relogin_shoonya() -> str:
    """Get a brand-new Shoonya session key when the cached/reopened one no
    longer works, and resume the ticker on it.

    Called by TickerManager (`on_relogin_callback`) once reopening the OLD
    session is exhausted. Returns "ok" (ticker already restarted), "retry"
    (transient failure — TickerManager will try again on backoff), or
    "credential_error" (broker rejected the credentials/OTP themselves —
    TickerManager stops retrying so we don't risk the broker's account
    lockout on repeated bad logins).
    """
    from brokers.base import BrokerError, SessionToken
    from brokers.registry import get_broker
    from brokers.shoonya import _token_cache
    from brokers.shoonya.authenticator import ShonyaAuthenticator
    from crypto import decrypt_json
    from db.engine import SessionLocal
    from db.models import Account, Broker

    db = SessionLocal()
    try:
        account = (
            db.query(Account).join(Broker)
            .filter(Broker.name == "shoonya")
            .order_by(Account.is_active.desc(), Account.id.asc())
            .first()
        )
        if account is None:
            return "credential_error"  # nothing configured to log in with
        try:
            credentials = decrypt_json(account.credentials_enc)
        except Exception:
            return "credential_error"  # can't decrypt — won't fix itself by retrying

        user_id = credentials.get("user_id")
        credentials["_account_id"] = account.id
        broker = get_broker("shoonya")

        # Try the session key already cached in the DB before paying for a
        # fresh OAuth login: the WS session dropping doesn't necessarily mean
        # the broker revoked the token, so confirm it's actually dead first.
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
                    token = cached_token
                    _auth_module._auth = ShonyaAuthenticator.from_session(token.token, token.broker_uid)
                    from app.routers.legacy_auth import _mark_session_liveness
                    _mark_session_liveness(True)
                    ticker_manager.start(broker, token.token, token.broker_uid, asyncio.get_running_loop())
                    _log.info("relogin: resumed Shoonya session for %s from DB-cached key", token.broker_uid)
                    return "ok"
            except Exception as e:
                _log.warning("relogin: DB-cached key validation failed for %s: %s", user_id, e)

        # Cached key is missing or no longer valid — clear it so login()
        # performs a fresh OAuth round-trip instead of handing back that same
        # stale token, then save the newly issued key to the DB.
        _token_cache.invalidate_session(user_id, account.id)

        try:
            token = await broker.login(credentials)
        except BrokerError as e:
            if e.code == "INVALID_CREDENTIALS":
                _log.error("relogin: broker rejected credentials for %s: %s", user_id, e)
                return "credential_error"
            _log.warning("relogin: transient login failure for %s: %s", user_id, e)
            return "retry"
        except Exception as e:
            _log.warning("relogin: unexpected login failure for %s: %s", user_id, e)
            return "retry"

        _auth_module._auth = ShonyaAuthenticator.from_session(token.token, token.broker_uid)
        from app.routers.legacy_auth import _mark_session_liveness
        _mark_session_liveness(True)
        ticker_manager.start(broker, token.token, token.broker_uid, asyncio.get_running_loop())
        _log.info("relogin: resumed Shoonya session for %s on a fresh session key", token.broker_uid)
        return "ok"
    finally:
        db.close()


async def _verify_after_reconnect() -> None:
    """Verify step of the connection manager: after the feed auto-reconnects,
    re-poll the broker order book and re-sync lots/positions so any fills that
    happened while the socket was down are picked up (Shoonya doesn't replay
    order updates missed during the outage). Self-guards on a live session."""
    auth = _auth_module._auth
    if auth is None or not auth.is_authenticated():
        return
    await _short_poll_reconcile(auth.user_id)


def register_ticker_callbacks() -> None:
    ticker_manager.on_tick_callback = _on_target_tick
    ticker_manager.on_order_update_callback = _on_order_update_event
    ticker_manager.on_reconnect_callback = _verify_after_reconnect
    ticker_manager.on_relogin_callback = _relogin_shoonya


def launch_tasks() -> None:
    asyncio.create_task(_auto_reconnect_shoonya())
    asyncio.create_task(_persistent_sweeper_loop())
    asyncio.create_task(_sync_positions_to_lots_on_startup())
