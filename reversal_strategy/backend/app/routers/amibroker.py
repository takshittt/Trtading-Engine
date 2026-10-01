"""Amibroker webhook — the live signal source.

A CSV-watcher bridge on the Amibroker Windows box (192.168.29.51) POSTs each
closed-candle scan signal here. Payload (Grid-compatible):

    {"symbol":"RELIANCE-FUT","action":"BUY","timeframe_min":60,
     "atr":31.5,"resistance":3120.0,"price":2958.0,
     "target_points":20.5,"sl_points":10.2,
     "bar_time":"2026-07-25T10:15:00+05:30","secret":"..."}

Amibroker computes indicators, so it sends ATR (and optionally the nearest
resistance) with the signal — that powers the AUTO Target/SL methods. Signals
fire on CLOSED candles only.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.state import STATE, audit
from app.services import signals as signal_svc
# Shared with the DB-inbox path (services/ami_inbox.py) so a signal delivered
# through the database and one delivered through this webhook normalise
# identically. Do not re-implement either helper here.
from app.services.ami_payload import tf_label as _tf_label
from app.services.ami_payload import to_our_payload as _to_our_payload
from db.engine import get_db

router = APIRouter(prefix="/api/amibroker", tags=["amibroker"])

WEBHOOK_SECRET = os.getenv("AMI_WEBHOOK_SECRET", "")

async def _parse_body(request: Request) -> dict:
    """Accept JSON or form-encoded (Amibroker InternetPostRequest sends form).
    Form parsing is done without python-multipart to keep deps minimal."""
    raw = await request.body()
    text = raw.decode("utf-8", "ignore").strip()
    if not text:
        return {}
    if text[0] in "{[":
        try:
            return __import__("json").loads(text)
        except Exception:
            pass
    from urllib.parse import parse_qs
    return {k: v[0] for k, v in parse_qs(text).items()}


@router.get("/status")
def feed_status():
    """What the webhook has been receiving — see AmibrokerFeedStats."""
    return {"connected": STATE.amibroker_connected, **STATE.ami.as_dict()}


def _reject(reason: str) -> dict:
    STATE.ami.invalid += 1
    STATE.ami.last_error = reason
    return {"ok": False, "error": reason}


def _ingest_body(db: Session, body: dict) -> tuple[str, str]:
    """Validate → normalise → ingest one signal, updating the feed-status stats.

    The single per-row path shared by the webhook and its batch variant, so both
    delivery shapes count, dedup and stamp a signal identically. Returns
    (result, detail) where result is "accepted" | "duplicate" | "invalid".
    """
    if not str(body.get("symbol") or "").strip():
        STATE.ami.invalid += 1
        STATE.ami.last_error = "missing symbol"
        return "invalid", "missing symbol"

    if _tf_label(body) is None:
        reason = (f"unsupported timeframe {body.get('timeframe_min')!r} "
                  f"(expected 60, 240 or 1440)")
        STATE.ami.invalid += 1
        STATE.ami.last_error = reason
        return "invalid", reason

    payload = _to_our_payload(body)
    STATE.ami.last_symbol = payload["symbol"]
    STATE.ami.last_bar_time = payload["signal_time"]

    sig = signal_svc.ingest(db, payload)
    if sig is None:
        # Same stock, same candle, same direction as one already stored. Normal
        # when a scan re-runs; a symptom when the scan's newest bar stops moving.
        STATE.ami.duplicate += 1
        return "duplicate", payload["signal_time"]

    STATE.ami.accepted += 1
    STATE.ami.last_accepted_at = datetime.now(timezone.utc).isoformat()
    return "accepted", sig.status


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    body = await _parse_body(request)
    STATE.ami.stamp()

    if WEBHOOK_SECRET and body.get("secret") != WEBHOOK_SECRET:
        return _reject("unauthorized")

    STATE.amibroker_connected = True
    STATE.hub.broadcast("connection", {"amibroker": True})

    result, detail = _ingest_body(db, body)
    if result == "invalid":
        return {"ok": False, "error": detail}
    if result == "duplicate":
        return {"ok": True, "duplicate": True, "bar_time": detail}
    return {"ok": True, "status": detail}


@router.post("/webhook/batch")
async def webhook_batch(request: Request, db: Session = Depends(get_db)):
    """Batch variant: {"secret":..., "signals":[ {..}, {..} ]}."""
    body = await _parse_body(request)
    if WEBHOOK_SECRET and body.get("secret") != WEBHOOK_SECRET:
        return {"ok": False, "error": "unauthorized"}
    STATE.amibroker_connected = True
    STATE.ami.stamp()
    rows = body.get("signals", [])
    count = sum(1 for s in rows if _ingest_body(db, s)[0] == "accepted")
    audit(db, "AMIBROKER_BATCH", "", f"{count} of {len(rows)} signals ingested")
    return {"ok": True, "ingested": count, "received": len(rows)}
