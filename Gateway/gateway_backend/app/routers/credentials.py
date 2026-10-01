"""Per-user broker credential accounts.

A user may save several broker credential profiles (e.g. two Shoonya
logins) and mark exactly one as *active*. The active
account is the one `/api/connect` and the trading engine log in with — saving
or editing another account never touches whichever session is currently live.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import get_current_user, verify_password
from app.schemas import (
    ConfiguredBrokerResponse,
    CredentialRevealRequest,
    CredentialRevealResponse,
    CredentialsSubmit,
    CredentialTestRequest,
    CredentialTestResponse,
    StoredCredentialTestRequest,
)
from brokers.base import BrokerError
from brokers.registry import get_broker
from crypto import decrypt_json, encrypt_json
from db.engine import get_db
from db.models import Account, Broker, User
from db.models import Session as DbSession
from db.models import StrategyRun

router = APIRouter()

# Required credential keys per broker (must be present and non-empty).
# Field names match what the broker adapters / auth-code helpers read.
REQUIRED_FIELDS: dict[str, list[str]] = {
    "shoonya": ["user_id", "password", "totp_secret", "vendor_code", "api_secret", "imei"],
}


def _to_response(account: Account) -> ConfiguredBrokerResponse:
    return ConfiguredBrokerResponse(
        id=account.id,
        broker_name=account.broker.name,
        label=account.label,
        is_active=account.is_active,
        fields_present=REQUIRED_FIELDS.get(account.broker.name, []),
    )


def _validate(broker_name: str, credentials: dict) -> None:
    missing = [
        f for f in REQUIRED_FIELDS[broker_name]
        if not str(credentials.get(f, "")).strip()
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required {broker_name} fields: {', '.join(missing)}",
        )


def _get_owned_account(account_id: int, user: User, db: Session) -> Account:
    account = db.query(Account).filter_by(id=account_id, user_id=user.id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@router.get("/api/credentials", response_model=list[ConfiguredBrokerResponse])
def list_credentials(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List every broker account this user has saved (field NAMES only, never values)."""
    accounts = db.query(Account).filter_by(user_id=user.id).order_by(Account.id).all()
    return [_to_response(a) for a in accounts]


@router.post("/api/credentials/test", response_model=CredentialTestResponse)
async def test_credentials(req: CredentialTestRequest, user: User = Depends(get_current_user)):
    """Test broker credentials as currently filled in the form — nothing is saved.

    Performs a real login against the broker and logs out again immediately.
    For Shoonya this drives a headless browser login and can take up to ~2
    minutes; the caller should show that expectation while waiting.
    """
    broker_name = req.broker_name.strip().lower()
    if broker_name not in REQUIRED_FIELDS:
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {req.broker_name}")
    _validate(broker_name, req.credentials)

    try:
        broker = get_broker(broker_name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {broker_name}")

    try:
        token = await broker.login(req.credentials)
    except BrokerError as e:
        return CredentialTestResponse(success=False, message=str(e))
    except Exception as e:
        return CredentialTestResponse(success=False, message=f"Unexpected error: {e}")

    try:
        await broker.logout(token, req.credentials)
    except Exception:
        pass  # best-effort cleanup — the login already proved the credentials work

    return CredentialTestResponse(success=True, message="Login succeeded.")


@router.post("/api/credentials", response_model=ConfiguredBrokerResponse)
def save_credentials(
    req: CredentialsSubmit,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save a NEW broker account. The first account a user saves becomes active
    automatically; later ones are saved inactive until explicitly activated."""
    broker_name = req.broker_name.strip().lower()
    if broker_name not in REQUIRED_FIELDS:
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {req.broker_name}")

    broker = db.query(Broker).filter_by(name=broker_name).first()
    if broker is None:
        raise HTTPException(status_code=400, detail=f"Unknown broker: {broker_name}")

    _validate(broker_name, req.credentials)

    try:
        credentials_enc = encrypt_json(req.credentials)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Encryption failed: {e}")

    existing_count = db.query(Account).filter_by(user_id=user.id).count()
    label = (req.label or "").strip() or f"{broker_name.capitalize()} #{existing_count + 1}"

    account = Account(
        user_id=user.id,
        broker_id=broker.id,
        label=label,
        credentials_enc=credentials_enc,
        is_active=(existing_count == 0),  # first account for this user goes live by default
    )
    db.add(account)
    user.form_filled = True
    db.commit()
    db.refresh(account)

    return _to_response(account)


@router.put("/api/credentials/{account_id}", response_model=ConfiguredBrokerResponse)
def update_credentials(
    account_id: int,
    req: CredentialsSubmit,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace an existing account's label/credentials. Does not change which
    account is active."""
    account = _get_owned_account(account_id, user, db)
    broker_name = req.broker_name.strip().lower()
    if broker_name not in REQUIRED_FIELDS:
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {req.broker_name}")

    broker = db.query(Broker).filter_by(name=broker_name).first()
    if broker is None:
        raise HTTPException(status_code=400, detail=f"Unknown broker: {broker_name}")

    _validate(broker_name, req.credentials)

    try:
        credentials_enc = encrypt_json(req.credentials)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Encryption failed: {e}")

    account.broker_id = broker.id
    account.credentials_enc = credentials_enc
    if req.label and req.label.strip():
        account.label = req.label.strip()
    account.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(account)

    return _to_response(account)


@router.post("/api/credentials/{account_id}/activate", response_model=ConfiguredBrokerResponse)
def activate_credentials(
    account_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark this account as the one /api/connect and the trading engine use next.

    Does not touch any session that is currently live — reconnect afterwards to
    actually switch the broker the engine is talking to.
    """
    account = _get_owned_account(account_id, user, db)
    db.query(Account).filter_by(user_id=user.id).update({"is_active": False})
    account.is_active = True
    account.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(account)
    return _to_response(account)


@router.delete("/api/credentials/{account_id}")
def delete_credentials(
    account_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    account = _get_owned_account(account_id, user, db)
    was_active = account.is_active

    # Clear rows that hard-FK to this account — Account.session/runs carry no
    # cascade, so leaving these in place makes db.delete(account) below fail
    # with an IntegrityError (surfaced to the client as a bare 500).
    db.query(DbSession).filter_by(account_id=account.id).delete()
    db.query(StrategyRun).filter_by(account_id=account.id).delete()

    db.delete(account)
    db.commit()

    # If exactly one account remains, it's an unambiguous replacement — activate it.
    remaining = db.query(Account).filter_by(user_id=user.id).all()
    if was_active and len(remaining) == 1 and not remaining[0].is_active:
        remaining[0].is_active = True
        db.commit()

    return {"deleted": True}


@router.post("/api/credentials/{account_id}/reveal", response_model=CredentialRevealResponse)
def reveal_credentials(
    account_id: int,
    req: CredentialRevealRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return one account's DECRYPTED credential values, gated by the app password.

    Password-in-body POST (never a query param) so the secret stays out of URLs/logs.
    """
    if not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect password")

    account = _get_owned_account(account_id, user, db)

    try:
        credentials = decrypt_json(account.credentials_enc)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt stored credentials")

    return CredentialRevealResponse(
        id=account.id,
        broker_name=account.broker.name,
        label=account.label,
        credentials=credentials,
    )


@router.post("/api/credentials/{account_id}/test", response_model=CredentialTestResponse)
async def test_stored_credentials(
    account_id: int,
    req: StoredCredentialTestRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Test a saved account's stored credentials without revealing them to the client."""
    if not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect password")

    account = _get_owned_account(account_id, user, db)

    try:
        credentials = decrypt_json(account.credentials_enc)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt stored credentials")

    try:
        broker = get_broker(account.broker.name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {account.broker.name}")

    try:
        credentials["_account_id"] = account.id
        token = await broker.login(credentials)
    except BrokerError as e:
        return CredentialTestResponse(success=False, message=str(e))
    except Exception as e:
        return CredentialTestResponse(success=False, message=f"Unexpected error: {e}")

    try:
        await broker.logout(token, credentials)
    except Exception:
        pass

    return CredentialTestResponse(success=True, message="Login succeeded.")
