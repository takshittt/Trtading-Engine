"""Trade Journal & P&L stats over closed positions.

Paper and live trades are kept apart everywhere: `paper=False` is the real book
(what the account actually did), `paper=True` is the simulation. Mixing them
would make both numbers meaningless, so every caller states which one it wants.

Beyond P&L, the stats cover the two things a strategy test is actually for:
how fast a signal turned into a fill, and how far each trade ran in both
directions before it closed.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import IST
from app.core.market_hours import now_ist
from app.services import positions as pos_svc
from app.core.timeutil import as_utc
from db.models import Position


def _closed(db: Session, paper: bool | None) -> list[Position]:
    stmt = select(Position).where(Position.status == "CLOSED")
    if paper is not None:
        stmt = stmt.where(Position.is_paper.is_(paper))
    return list(db.scalars(stmt.order_by(Position.closed_at.desc())).all())


def closed_trades(db: Session, paper: bool | None = None) -> list[dict]:
    return [pos_svc.serialize(p) for p in _closed(db, paper)]


def day_start_utc() -> datetime:
    """Naive-UTC midnight of the current IST day.

    Naive on purpose: SQLite stores and returns these columns without a tzinfo,
    so a comparison against an aware datetime raises TypeError. Returning the
    same shape the database holds lets the filter run in SQL instead of pulling
    every closed row into Python to compare one by one.
    """
    start_ist = datetime.combine(now_ist().date(), time.min, tzinfo=IST)
    return start_ist.astimezone(timezone.utc).replace(tzinfo=None)


def realized_today(db: Session, paper: bool = False) -> float:
    """P&L booked since midnight IST, for one book.

    Single definition on purpose. This existed twice — once here in the summary
    and once in the exit engine's portfolio stop — with different
    implementations of the same idea: one filtered in SQL, the other loaded
    every closed position and compared in Python. They happened to agree, but
    nothing kept them in step, and the two callers are the daily P&L the user
    reads and the circuit breaker that squares the book off. Those two must
    never be able to disagree about what "today" means.
    """
    rows = db.scalars(select(Position).where(
        Position.status == "CLOSED",
        Position.is_paper.is_(paper),
        Position.closed_at.isnot(None),
        Position.closed_at >= day_start_utc())).all()
    return round(sum(p.realized_pnl or 0.0 for p in rows), 2)


def _avg(vals: list[float]) -> float:
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def _empty() -> dict:
    return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "avg_pnl": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "best": 0.0, "worst": 0.0,
            "profit_factor": 0.0, "max_drawdown": 0.0, "total_pnl": 0.0,
            "avg_exec_latency_ms": 0, "avg_exit_latency_ms": 0,
            "avg_hold_minutes": 0.0, "avg_mfe": 0.0, "avg_mae": 0.0,
            "avg_mfe_points": 0.0, "avg_mae_points": 0.0,
            "by_exit_reason": {}}


def stats(db: Session, paper: bool | None = None) -> dict:
    rows = _closed(db, paper)
    if not rows:
        return _empty()

    pnls = [p.realized_pnl for p in rows]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    # Max drawdown over the equity curve of realized P&L. Oldest first, or the
    # curve is walked backwards and the drawdown is not the one that happened.
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in reversed(rows):
        equity += p.realized_pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    by_reason: dict[str, dict] = {}
    for p in rows:
        slot = by_reason.setdefault(p.exit_reason or "—", {"trades": 0, "pnl": 0.0})
        slot["trades"] += 1
        slot["pnl"] = round(slot["pnl"] + p.realized_pnl, 2)

    holds = [(as_utc(p.closed_at) - as_utc(p.opened_at)).total_seconds() / 60.0
             for p in rows if p.closed_at and p.opened_at]
    entry_lat = [p.exec_latency_ms for p in rows if p.exec_latency_ms]
    exit_lat = [p.exit_latency_ms for p in rows if p.exit_latency_ms]

    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(rows) * 100, 2),
        "avg_pnl": _avg(pnls),
        "avg_win": _avg(wins),
        "avg_loss": _avg(losses),
        "best": round(max(pnls), 2),
        "worst": round(min(pnls), 2),
        # None, not 0.0. A run with wins and no losses has an UNDEFINED profit
        # factor (division by zero) — reporting 0.0 made a flawless stretch read
        # as the worst possible score, which is the opposite of the truth. The
        # UI renders None as an infinity sign; 0.0 is reserved for "no trades".
        "profit_factor": (round(gross_profit / gross_loss, 2) if gross_loss
                          else (None if gross_profit > 0 else 0.0)),
        "max_drawdown": round(max_dd, 2),
        "total_pnl": round(sum(pnls), 2),
        # Execution quality — the reason the timestamps are captured at all.
        "avg_exec_latency_ms": int(_avg(entry_lat)) if entry_lat else 0,
        "avg_exit_latency_ms": int(_avg(exit_lat)) if exit_lat else 0,
        "avg_hold_minutes": _avg(holds),
        # Total size ever held (remaining + already sold), not the remaining
        # quantity. `p.qty or p.exit_qty` understated every position that was
        # scaled out of, and it did so systematically — partial exits are
        # exactly the trades whose excursion you most want to read.
        "avg_mfe": _avg([(p.peak_price - p.avg_price) * (((p.qty or 0) + (p.exit_qty or 0)) or p.qty)
                         for p in rows if p.peak_price]),
        "avg_mae": _avg([(p.trough_price - p.avg_price) * (((p.qty or 0) + (p.exit_qty or 0)) or p.qty)
                         for p in rows if p.trough_price]),
        # Per-unit, so the averages are comparable across position sizes.
        "avg_mfe_points": _avg([p.peak_price - p.avg_price for p in rows if p.peak_price]),
        "avg_mae_points": _avg([p.trough_price - p.avg_price for p in rows if p.trough_price]),
        "by_exit_reason": by_reason,
    }
