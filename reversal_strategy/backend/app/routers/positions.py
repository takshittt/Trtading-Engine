"""Position & order endpoints: manual buy, averaging, exits, edits, rollover."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services import positions as pos_svc
from db.engine import get_db
from db.models import Order, Position

router = APIRouter(prefix="/api/positions", tags=["positions"])


class BuyReq(BaseModel):
    symbol: str
    lots: int = 1
    # Target: mode manual|auto, method (atr|resistance), manual value
    target_mode: str = "auto"
    target_method: str = "atr"
    target_value: float = 0.0
    # SL: mode manual|auto, method (atr), manual value
    sl_mode: str = "auto"
    sl_method: str = "atr"
    sl_value: float = 0.0
    atr: float = 0.0
    resistance: float = 0.0
    support: float = 0.0
    signal_id: Optional[int] = None


class AverageReq(BaseModel):
    lots: int = 1
    signal_id: Optional[int] = None     # the signal that triggered it, if any


class EditReq(BaseModel):
    target: Optional[float] = None
    stop_loss: Optional[float] = None


class PartialReq(BaseModel):
    lots: int = 1


class RolloverReq(BaseModel):
    new_expiry: str
    basis: float = 0.0


class RollReq(BaseModel):
    target_tsym: str
    target_expiry: str = ""
    qty: int = 0                       # 0 = whole position
    price_type: str = "LMT"            # LMT | MKT
    exit_price: float = 0.0
    entry_price: float = 0.0
    carry_pnl: bool = True
    carry_target: bool = True


@router.get("")
def list_open(db: Session = Depends(get_db)):
    rows = db.scalars(select(Position).where(Position.status == "OPEN")
                      .order_by(Position.opened_at.desc())).all()
    return [pos_svc.serialize(p) for p in rows]


@router.post("/buy")
def buy(req: BuyReq, db: Session = Depends(get_db)):
    return pos_svc.open_position(
        db, symbol=req.symbol.upper(), lots=req.lots,
        target_mode=req.target_mode, target_method=req.target_method, target_value=req.target_value,
        sl_mode=req.sl_mode, sl_method=req.sl_method, sl_value=req.sl_value,
        atr=req.atr, resistance=req.resistance, support=req.support,
        signal_id=req.signal_id)


@router.post("/{position_id}/average")
def average(position_id: int, req: AverageReq, db: Session = Depends(get_db)):
    return pos_svc.average_position(db, position_id, req.lots, signal_id=req.signal_id)


@router.post("/{position_id}/edit")
def edit(position_id: int, req: EditReq, db: Session = Depends(get_db)):
    return pos_svc.edit_targets(db, position_id, req.target, req.stop_loss)


@router.post("/{position_id}/partial")
def partial(position_id: int, req: PartialReq, db: Session = Depends(get_db)):
    return pos_svc.exit_partial(db, position_id, req.lots)


@router.post("/{position_id}/exit")
def exit_full(position_id: int, db: Session = Depends(get_db)):
    return pos_svc.exit_full(db, position_id, reason="MANUAL")


@router.get("/{position_id}/legs")
def legs(position_id: int, db: Session = Depends(get_db)):
    """Every leg of the position — entry, each averaging buy, exits — with the
    average price each one produced. Backs the ×N averaging popup."""
    return pos_svc.legs(db, position_id)


@router.get("/{position_id}/roll-targets")
def roll_targets(position_id: int, db: Session = Depends(get_db)):
    """Later expiries of the same underlying, nearest first, each with a quote."""
    return pos_svc.roll_targets(db, position_id)


@router.post("/{position_id}/roll")
def roll(position_id: int, req: RollReq, db: Session = Depends(get_db)):
    """Two-leg rollover: close the near contract, open the far one."""
    return pos_svc.roll_position(
        db, position_id, target_tsym=req.target_tsym.upper(),
        target_expiry=req.target_expiry, qty=req.qty, price_type=req.price_type,
        exit_price=req.exit_price, entry_price=req.entry_price,
        carry_pnl=req.carry_pnl, carry_target=req.carry_target)


@router.post("/{position_id}/rollover")
def rollover(position_id: int, req: RolloverReq, db: Session = Depends(get_db)):
    """Legacy basis-only shift — kept for the older client contract."""
    return pos_svc.rollover(db, position_id, req.new_expiry, req.basis)


@router.get("/orders")
def orders(limit: int = 200, db: Session = Depends(get_db)):
    from app.services import execution
    rows = db.scalars(select(Order).order_by(Order.created_at.desc()).limit(limit)).all()
    return [execution.serialize(o) for o in rows]
