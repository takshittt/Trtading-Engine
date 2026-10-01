"""Signal endpoints: live feed ingestion, list, history, search & manual add."""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import IST
from app.core.market_hours import now_ist
from app.core.state import STATE
from app.services import gateway_service as gw
from app.services import signals as signal_svc
from app.services import targets as target_svc
from db.engine import get_config, get_db
from db.models import Signal

router = APIRouter(prefix="/api/signals", tags=["signals"])


def _today_start_utc() -> datetime:
    """Naive UTC datetime for 00:00 IST today (signals store naive UTC)."""
    start_ist = datetime.combine(now_ist().date(), time.min, tzinfo=IST)
    return start_ist.astimezone(timezone.utc).replace(tzinfo=None)


def _ist_date(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).astimezone(IST).date().isoformat()


@router.get("")
def list_signals(
    actionable: bool = Query(True),
    timeframe: Optional[str] = None,
    signal_type: Optional[str] = None,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    stmt = select(Signal).order_by(Signal.created_at.desc()).limit(limit)
    if actionable:
        stmt = select(Signal).where(Signal.status.in_(["NEW", "AVERAGING"])) \
            .order_by(Signal.created_at.desc()).limit(limit)
    if timeframe:
        stmt = stmt.where(Signal.timeframe == timeframe)
    if signal_type:
        stmt = stmt.where(Signal.signal_type == signal_type)
    rows = signal_svc.refresh(db, list(db.scalars(stmt).all()))
    held = signal_svc.open_symbols(db)
    return [signal_svc.serialize(s, held) for s in rows]


@router.get("/history")
def history(symbol: Optional[str] = None, limit: int = 500, db: Session = Depends(get_db)):
    stmt = select(Signal).order_by(Signal.created_at.desc()).limit(limit)
    if symbol:
        stmt = stmt.where(Signal.symbol == symbol.upper())
    held = signal_svc.open_symbols(db)
    return [signal_svc.serialize(s, held) for s in db.scalars(stmt).all()]


@router.get("/today")
def today(db: Session = Depends(get_db)):
    """Today's signals (any status). In MANUAL stock-selection mode, only the
    user's manually-added signals are shown (Amibroker signals are hidden)."""
    start = _today_start_utc()
    stmt = select(Signal).where(Signal.created_at >= start)
    if get_config(db).stock_selection_mode == "manual":
        stmt = stmt.where(Signal.manual_add.is_(True))
    rows = signal_svc.refresh(db, list(db.scalars(stmt.order_by(Signal.created_at.desc())).all()))
    held = signal_svc.open_symbols(db)
    return [signal_svc.serialize(s, held) for s in rows]


@router.get("/previous")
def previous(limit: int = 1000, db: Session = Depends(get_db)):
    """Signals from prior days, grouped by IST date (newest day first).

    These stay actionable — the user can buy a previous day's signal — so they
    are re-priced and re-gated on the way out, exactly like today's feed, and
    obey the same MANUAL stock-selection filter.
    """
    start = _today_start_utc()
    stmt = select(Signal).where(Signal.created_at < start)
    if get_config(db).stock_selection_mode == "manual":
        stmt = stmt.where(Signal.manual_add.is_(True))
    rows = signal_svc.refresh(db, list(db.scalars(
        stmt.order_by(Signal.created_at.desc()).limit(limit)
    ).all()))
    held = signal_svc.open_symbols(db)
    groups: dict[str, list] = {}
    for s in rows:
        groups.setdefault(_ist_date(s.created_at), []).append(signal_svc.serialize(s, held))
    return [{"date": d, "count": len(sigs), "signals": sigs} for d, sigs in groups.items()]


@router.get("/search")
def search(q: str, exchange: str = "", db: Session = Depends(get_db)):
    """Search tradable futures & options (Shoonya) for the Search & Add box."""
    try:
        return gw.gateway().search_symbols(q, exchange)
    except Exception:
        return []


class ManualAddReq(BaseModel):
    symbol: str
    signal_type: str = "BUY"        # BUY or SELL — user picks
    timeframe: str = "1H"
    lot_size: Optional[int] = None
    expiry: Optional[str] = None


@router.post("/manual-add")
def manual_add(req: ManualAddReq, db: Session = Depends(get_db)):
    sig = signal_svc.manual_add(db, req.symbol, signal_type=req.signal_type,
                                timeframe=req.timeframe, lot_size=req.lot_size, expiry=req.expiry)
    return {"ok": True, "signal": signal_svc.serialize(sig)}


@router.get("/preview")
def preview(symbol: str, db: Session = Depends(get_db)):
    """Suggested AUTO Target/SL values + LTP for the Buy modal."""
    cfg = get_config(db)
    sym = symbol.upper()
    ltp = STATE.prices.get(sym) or gw.gateway().get_ltp(sym)
    return {"symbol": sym, "ltp": ltp, "lot_size": gw.lot_size(sym),
            "expiry": gw.expiry_of(sym), **target_svc.preview(ltp, cfg)}


@router.post("/{signal_id}/ignore")
def ignore(signal_id: int, db: Session = Depends(get_db)):
    sig = db.get(Signal, signal_id)
    if not sig:
        return {"ok": False, "error": "not found"}
    sig.status = "IGNORED"
    db.commit()
    return {"ok": True}
