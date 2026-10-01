"""Paper trading session control — start, stop, reset.

A paper *run* is a test with a beginning and an end, so it gets explicit
controls rather than a buried config flag. Stopping matters more than it looks:
turning the switch off only stops NEW signals from being executed — positions
already open stay under the exit engine's management, exactly as a real one
would. Ending a run properly means deciding what happens to those, which is why
`stop` takes an explicit square_off choice instead of guessing.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.state import STATE, audit
from app.services import positions as pos_svc
from db.engine import get_config, get_db
from db.models import Order, Position, Signal

router = APIRouter(prefix="/api/paper", tags=["paper"])


def _open_paper(db: Session) -> list[Position]:
    return list(db.scalars(select(Position).where(
        Position.status == "OPEN", Position.is_paper.is_(True))).all())


@router.get("/status")
def status(db: Session = Depends(get_db)):
    cfg = get_config(db)
    open_rows = _open_paper(db)
    closed = db.scalars(select(Position).where(
        Position.status == "CLOSED", Position.is_paper.is_(True))).all()
    unreal = sum((STATE.prices.get(p.symbol, p.ltp) - p.avg_price) * p.qty for p in open_rows)
    return {
        "running": cfg.paper_trading,
        "lots": cfg.paper_lots,
        "open_positions": len(open_rows),
        "closed_trades": len(closed),
        "unrealized_pnl": round(unreal, 2),
        "realized_pnl": round(sum(p.realized_pnl for p in closed), 2),
    }


class StartReq(BaseModel):
    lots: int = 1


@router.post("/start")
def start(req: StartReq, db: Session = Depends(get_db)):
    cfg = get_config(db)
    cfg.paper_trading = True
    cfg.paper_lots = max(1, req.lots)
    db.commit()
    audit(db, "PAPER_START", "", f"paper trading started · {cfg.paper_lots} lot(s) per buy")
    STATE.hub.broadcast("config", {"paper_trading": True, "paper_lots": cfg.paper_lots})
    STATE.hub.broadcast("alert", {
        "type": "PAPER_START",
        "message": f"Paper trading ON — every signal auto-executes at {cfg.paper_lots} lot(s). No real orders."})
    return {"ok": True, **status(db)}


class StopReq(BaseModel):
    square_off: bool = True


@router.post("/stop")
def stop(req: StopReq, db: Session = Depends(get_db)):
    """Stop taking new signals. Optionally close whatever is still open.

    Left open, existing paper positions keep running under target/SL/trailing —
    useful to let a test finish naturally. Squared off, the run is sealed and
    every trade lands in the journal with a realized P&L.
    """
    cfg = get_config(db)
    cfg.paper_trading = False
    db.commit()

    closed = 0
    if req.square_off:
        for p in _open_paper(db):
            if pos_svc.exit_full(db, p.id, reason="PAPER_STOP").get("ok"):
                closed += 1

    audit(db, "PAPER_STOP", "",
          f"paper trading stopped · {closed} position(s) squared off"
          if req.square_off else "paper trading stopped · open positions left running")
    STATE.hub.broadcast("config", {"paper_trading": False})
    STATE.hub.broadcast("alert", {
        "type": "PAPER_STOP",
        "message": (f"Paper trading OFF — {closed} position(s) squared off."
                    if req.square_off else
                    "Paper trading OFF — open paper positions left running.")})
    return {"ok": True, "squared_off": closed, **status(db)}


@router.post("/reset")
def reset(db: Session = Depends(get_db)):
    """Wipe every paper trade so the next run starts from a clean sheet.

    Destructive and irreversible, but scoped: only rows flagged as paper are
    touched. The live book, its orders and its journal are never in range.
    """
    positions = db.scalars(select(Position).where(Position.is_paper.is_(True))).all()
    ids = [p.id for p in positions]
    orders = db.scalars(select(Order).where(Order.is_paper.is_(True))).all()

    # Un-settle the signals the paper trader consumed so they read as untouched
    # rather than pointing at positions that no longer exist.
    # Every row here was settled by the paper trader (paper=True is only ever
    # set by it), so IGNORED belongs in this list too — it is how a skipped
    # paper decision is recorded, not a choice the user made.
    settled = ("EXECUTED", "AVERAGED", "SQUARED_OFF", "EXEC_FAILED", "IGNORED")
    signals = db.scalars(select(Signal).where(Signal.paper.is_(True))).all()
    for s in signals:
        s.status = "NEW" if s.status in settled else s.status
        s.paper = False
        s.position_id = None
        s.executed_at = None
        s.exec_price = 0.0
        s.exec_latency_ms = 0
        s.note = ""

    for o in orders:
        db.delete(o)
    for p in positions:
        db.delete(p)
    db.commit()

    audit(db, "PAPER_RESET", "",
          f"cleared {len(ids)} paper position(s), {len(orders)} order(s), "
          f"{len(signals)} signal(s) reset", level="WARN")
    STATE.hub.broadcast("alert", {
        "type": "PAPER_RESET",
        "message": f"Paper data cleared — {len(ids)} trade(s) removed."})
    return {"ok": True, "positions_removed": len(ids), "orders_removed": len(orders),
            "signals_reset": len(signals)}
