"""Account Manager — budget, margin and cap logic.

Utilised budget = sum of MARGIN locked in open positions (not notional; see
services/margin.py for why). Reserve is mathematically linked to the hard cap:

    reserve_pct = 100 - hard_cap_pct          (e.g. hard cap 90% -> 10% reserve)

The reserve is locked exclusively for averaging buys. Soft cap (e.g. 80%) shows
a yellow warning; hard cap (e.g. 90%) blocks new (non-averaging) entries.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Position, StrategyConfig


def reserve_pct(cfg: StrategyConfig) -> float:
    return max(0.0, 100.0 - cfg.hard_cap_pct)


def snapshot(db: Session, cfg: StrategyConfig) -> dict:
    # Paper positions lock no real margin and must not consume the live budget.
    open_positions = db.scalars(
        select(Position).where(Position.status == "OPEN", Position.is_paper.is_(False))
    ).all()
    utilised = sum(p.margin_used for p in open_positions)
    total = cfg.total_budget
    r_pct = reserve_pct(cfg)
    reserve = round(total * r_pct / 100.0, 2)
    investable = total - reserve                     # spendable outside the reserve
    available = round(total - utilised, 2)
    util_pct = round((utilised / total * 100.0), 2) if total else 0.0
    reserve_in_use = round(max(0.0, utilised - investable), 2)

    if util_pct >= cfg.hard_cap_pct:
        cap_state = "HARD"
    elif util_pct >= cfg.soft_cap_pct:
        cap_state = "SOFT"
    else:
        cap_state = "OK"

    # What a NEW entry can actually draw on. `available` is total minus used and
    # therefore includes the reserve — but the reserve is locked for averaging,
    # so a new buy is never judged against it. The dashboard was showing
    # `available` as "free", which overstates the spendable amount by the whole
    # reserve: it could read "Rs15,000 free" and refuse a Rs6,000 entry in the
    # same breath. Both numbers are published so neither has to be inferred.
    hard_cap_value = total * cfg.hard_cap_pct / 100.0
    available_for_new = round(max(0.0, hard_cap_value - utilised), 2)

    return {
        "total_budget": round(total, 2),
        "utilised": round(utilised, 2),
        "available": available,
        "available_for_new": available_for_new,
        "reserve": reserve,
        "reserve_pct": r_pct,
        "reserve_in_use": reserve_in_use,
        "utilisation_pct": util_pct,
        "cap_state": cap_state,          # OK / SOFT / HARD
        "open_positions": len(open_positions),
    }


def can_open_new(db: Session, cfg: StrategyConfig, margin_needed: float,
                 is_averaging: bool = False) -> tuple[bool, str, bool]:
    """Return (allowed, reason, uses_reserve).

    New entries are blocked at the hard cap and may NOT dip into the reserve.
    Averaging buys are the only thing allowed to consume the reserve.
    """
    snap = snapshot(db, cfg)
    total = cfg.total_budget
    investable = total - snap["reserve"]
    projected = snap["utilised"] + margin_needed
    projected_pct = (projected / total * 100.0) if total else 100.0

    if projected > total:
        return False, "Insufficient budget (would exceed 100%).", False

    if not is_averaging:
        if projected_pct >= cfg.hard_cap_pct:
            return False, f"Budget hard cap ({cfg.hard_cap_pct:.0f}%) reached — reserve is locked for averaging only.", False
        return True, "", False

    # averaging: may use the reserve, but never beyond 100%
    uses_reserve = projected > investable
    return True, "", uses_reserve
