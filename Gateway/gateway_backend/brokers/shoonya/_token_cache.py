"""Multi-account token cache for Shoonya sessions.

The session key (susertoken) is cached in the `sessions` DB table (keyed by
account_id) so a restart — including on a different machine pointed at the
same DATABASE_URL — can reuse it instead of forcing a fresh headless login.
The OAuth code (a same-day, single-use intermediate credential, not the
session key) stays in the local token_store.json file.
"""

import json
from pathlib import Path
from datetime import datetime, date
from typing import Optional

from db.engine import SessionLocal
from db.models import Session as SessionModel


_TOKEN_STORE_PATH = Path(__file__).parent.parent.parent / "token_store.json"


def _ensure_path():
    """Create token_store.json if it doesn't exist."""
    if not _TOKEN_STORE_PATH.exists():
        _TOKEN_STORE_PATH.write_text(json.dumps({}))


def load() -> dict:
    """
    Load and migrate token_store.json.

    Old format (flat): {"susertoken": "...", "susertoken_issued_at": "...", "user_id": "..."}
    New format (keyed): {"FN131640": {"susertoken": "...", "susertoken_issued_at": "...", ...}}

    Detects old format and auto-migrates on first read.
    """
    _ensure_path()
    try:
        data = json.loads(_TOKEN_STORE_PATH.read_text())
    except (json.JSONDecodeError, IOError):
        return {}

    if not data:
        return {}

    # Detect old flat format: has "susertoken" key at top level and "user_id" as string
    if "susertoken" in data and isinstance(data.get("user_id"), str):
        user_id = data.pop("user_id")
        migrated = {user_id: data}
        save(migrated)
        return migrated

    return data


def save(store: dict) -> None:
    """Write token store to disk."""
    _ensure_path()
    _TOKEN_STORE_PATH.write_text(json.dumps(store, indent=2))


def _issued_today(timestamp_str: Optional[str]) -> bool:
    """Check if a timestamp string (ISO format) is from today."""
    if not timestamp_str:
        return False
    try:
        issued_date = datetime.fromisoformat(timestamp_str).date()
        return issued_date == date.today()
    except (ValueError, AttributeError):
        return False


def get_cached_session(user_id: str, account_id: Optional[int] = None) -> Optional[dict]:
    """
    Return the cached session entry for account_id (from the `sessions` DB
    table) if it belongs to user_id and was issued today, else None.
    Entry shape: {"susertoken": str, "susertoken_issued_at": str}

    account_id is required — callers with no known DB account (e.g. testing
    unsaved form credentials) get no cache hit and always do a fresh login.
    """
    if account_id is None:
        return None
    db = SessionLocal()
    try:
        row = db.query(SessionModel).filter_by(account_id=account_id).first()
        if row and row.broker_uid == user_id and row.token and _issued_today(
            row.issued_at.isoformat() if row.issued_at else None
        ):
            return {"susertoken": row.token, "susertoken_issued_at": row.issued_at.isoformat()}
        return None
    finally:
        db.close()


def cache_session(user_id: str, token: str, issued_at: str, account_id: Optional[int] = None) -> None:
    """Write or update the session token row for account_id in the `sessions` DB table."""
    if account_id is None:
        return
    db = SessionLocal()
    try:
        row = db.query(SessionModel).filter_by(account_id=account_id).first()
        issued_dt = datetime.fromisoformat(issued_at)
        if row:
            row.token = token
            row.broker_uid = user_id
            row.issued_at = issued_dt
        else:
            db.add(SessionModel(account_id=account_id, token=token, broker_uid=user_id, issued_at=issued_dt))
        db.commit()
    finally:
        db.close()


def invalidate_session(user_id: str, account_id: Optional[int] = None) -> None:
    """Remove the session token row for account_id from the `sessions` DB table."""
    if account_id is None:
        return
    db = SessionLocal()
    try:
        row = db.query(SessionModel).filter_by(account_id=account_id).first()
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()


def get_cached_oauth_code(user_id: str) -> Optional[str]:
    """Return today's OAuth code for user_id if cached, else None."""
    store = load()
    entry = store.get(user_id, {})

    if entry.get("oauth_code") and _issued_today(entry.get("oauth_code_issued_at")):
        return entry["oauth_code"]
    return None


def cache_oauth_code(user_id: str, code: str) -> None:
    """Cache an OAuth code for user_id with today's timestamp."""
    store = load()
    if user_id not in store:
        store[user_id] = {}
    store[user_id]["oauth_code"] = code
    store[user_id]["oauth_code_issued_at"] = datetime.now().isoformat()
    save(store)


def invalidate_oauth_code(user_id: str) -> None:
    """Remove the OAuth code entry for user_id."""
    store = load()
    if user_id in store:
        store[user_id].pop("oauth_code", None)
        store[user_id].pop("oauth_code_issued_at", None)
        if not store[user_id]:
            store.pop(user_id)
    save(store)
