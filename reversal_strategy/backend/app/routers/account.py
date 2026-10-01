"""Account, config, blacklist, journal, market-status, kill-switch, reconcile, margin."""
from __future__ import annotations

import time as _time
from datetime import datetime, time, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import IST
from app.core.market_hours import (
    is_forced_open,
    market_status,
    now_ist,
    refresh_from_config,
)
from app.core.state import STATE, audit
from app.core.timeutil import iso_utc
from app.services import budget as budget_svc
from app.services import exit_engine
from app.services import gateway_service as gw
from app.services import journal as journal_svc
from app.services import positions as pos_svc
from app.services.margin import MARGIN
from app.services.reconciliation import reconcile_once
from db.engine import get_config, get_db
from db.models import AuditLog, Blacklist, Position

router = APIRouter(prefix="/api", tags=["account"])


def _auto_exec_state(cfg) -> dict:
    """Is automated execution armed, and if not, why not."""
    from app.services import auto_exec
    armed, why = auto_exec.armed(cfg)
    return {
        "mode": cfg.execution_mode,
        "armed": armed,
        "blocked_by": why,
        "risk_pct": cfg.auto_risk_pct,
        "max_lots": cfg.auto_max_lots,
        "max_positions": cfg.auto_max_positions,
        "averaging_mode": cfg.averaging_mode,
    }


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    cfg = get_config(db)
    snap = budget_svc.snapshot(db, cfg)
    all_open = db.scalars(select(Position).where(Position.status == "OPEN")).all()
    # Real money and simulated money are reported separately — a paper trade
    # must never move the P&L the account is actually carrying.
    live_open = [p for p in all_open if not p.is_paper]
    paper_open = [p for p in all_open if p.is_paper]

    def _unreal(rows):
        return round(sum((STATE.prices.get(p.symbol, p.ltp) - p.avg_price) * p.qty for p in rows), 2)

    # "Today's P&L" is open MTM *plus* anything booked today. It previously
    # counted only open positions, so closing a winner made the number go down.
    # `journal.realized_today` is the single definition of "today" — the exit
    # engine's portfolio stop reads the same one.
    today_pnl = round(_unreal(live_open) + journal_svc.realized_today(db, paper=False), 2)
    paper_today = round(_unreal(paper_open) + journal_svc.realized_today(db, paper=True), 2)
    stats = journal_svc.stats(db, paper=False)
    paper_stats = journal_svc.stats(db, paper=True)
    try:
        funds = gw.gateway().get_funds()
    except Exception:
        funds = {"cash": 0.0}
    return {
        "shoonya_cash": round(funds.get("cash", 0.0), 2),   # total money in Shoonya
        "budget": snap,                                      # allocated to this strategy
        "today_pnl": today_pnl,
        # Open MTM + all realized ever. Built from _unreal, not today_pnl, or
        # today's booked trades would be counted twice.
        "overall_pnl": round(_unreal(live_open) + stats["total_pnl"], 2),
        "realized_pnl": stats["total_pnl"],
        "open_positions": len(live_open),
        "paper": {
            "enabled": cfg.paper_trading,
            "lots": cfg.paper_lots,
            "open_positions": len(paper_open),
            "unrealized_pnl": _unreal(paper_open),
            "realized_pnl": paper_stats["total_pnl"],
            "today_pnl": paper_today,
            "overall_pnl": round(_unreal(paper_open) + paper_stats["total_pnl"], 2),
            "trades": paper_stats["trades"],
            "win_rate": paper_stats["win_rate"],
        },
        "market_status": market_status(),
        "signals_halted": cfg.signals_halted,
        "stock_selection_mode": cfg.stock_selection_mode,
        "execution_mode": cfg.execution_mode,
        "exit_mode": cfg.exit_mode,
        # Whether automated execution will actually act, and if not, which switch
        # is holding it back. The mode alone does not answer that — it stands
        # down while Stoploss is manual or signals are halted, and a toggle that
        # reads "Auto" while nothing trades is the worst thing this panel could say.
        "auto_exec": _auto_exec_state(cfg),
        "connection": {"broker": STATE.broker_connected},
        # The broker flag above is a handshake; this is a heartbeat. Read
        # price_feed.stalled before believing a green broker badge — the two
        # disagree in exactly the case that matters.
        "price_feed": {**STATE.feed.as_dict(), "symbols_priced": len(STATE.prices)},
        "last_reconcile": STATE.last_reconcile,
        "margin_as_of": MARGIN.as_of(),
    }


# ---- Config ---------------------------------------------------------------
class ConfigReq(BaseModel):
    total_budget: Optional[float] = None
    soft_cap_pct: Optional[float] = None
    hard_cap_pct: Optional[float] = None
    max_averaging_buys: Optional[int] = None
    atr_period: Optional[int] = None
    atr_target_mult: Optional[float] = None
    atr_sl_mult: Optional[float] = None
    target_mode: Optional[str] = None
    sl_mode: Optional[str] = None
    default_target_pct: Optional[float] = None
    default_sl_pct: Optional[float] = None
    stock_selection_mode: Optional[str] = None
    execution_mode: Optional[str] = None
    auto_risk_pct: Optional[float] = None
    auto_max_lots: Optional[int] = None
    auto_max_positions: Optional[int] = None
    exit_mode: Optional[str] = None
    trailing_buffer_pct: Optional[float] = None
    global_sl_pct: Optional[float] = None
    averaging_mode: Optional[str] = None
    order_retries: Optional[int] = None
    slippage_ticks: Optional[int] = None
    reconcile_seconds: Optional[int] = None
    signals_halted: Optional[bool] = None
    paper_trading: Optional[bool] = None
    paper_lots: Optional[int] = None
    auto_rollover: Optional[bool] = None
    auto_rollover_days: Optional[int] = None
    min_rr: Optional[float] = None
    market_open_time: Optional[str] = None
    market_close_time: Optional[str] = None
    market_holidays: Optional[str] = None
    ignore_market_hours: Optional[bool] = None


def _config_dict(cfg) -> dict:
    d = {c.name: getattr(cfg, c.name) for c in cfg.__table__.columns}
    d["reserve_pct"] = budget_svc.reserve_pct(cfg)   # derived, shown read-only in UI
    # The env var can force the market open on its own, and then the toggle in
    # the UI reads "off" while hours are being ignored. Report what is actually
    # in force, not just what is stored.
    d["market_hours_bypassed"] = is_forced_open()
    return d


def _validated_hhmm(raw: str, field: str) -> str:
    """Reject a malformed session time at the API rather than in the tick path.

    market_hours falls back on unparseable input so a bad value can never stop
    ticks being classified — but silently ignoring what the user just typed is
    its own kind of wrong, so refuse it here where there's someone to tell.
    """
    try:
        hh, mm = raw.strip().split(":")
        parsed = time(int(hh), int(mm))
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(400, f"{field} must be HH:MM in 24-hour IST, got {raw!r}")
    return parsed.strftime("%H:%M")


@router.get("/config")
def get_cfg(db: Session = Depends(get_db)):
    return _config_dict(get_config(db))


@router.post("/config")
def set_cfg(req: ConfigReq, db: Session = Depends(get_db)):
    cfg = get_config(db)
    updates = req.model_dump(exclude_none=True)

    for field in ("market_open_time", "market_close_time"):
        if field in updates:
            updates[field] = _validated_hhmm(updates[field], field)
    open_t = updates.get("market_open_time", cfg.market_open_time)
    close_t = updates.get("market_close_time", cfg.market_close_time)
    if open_t >= close_t:                       # zero-length session accepts nothing
        raise HTTPException(400, f"Market open ({open_t}) must be before close ({close_t})")

    resuming_auto = (updates.get("exit_mode") == "auto" and cfg.exit_mode != "auto")
    arming_exec = (updates.get("execution_mode") == "automated"
                   and cfg.execution_mode != "automated")

    changed = {}
    for k, v in updates.items():
        setattr(cfg, k, v)
        changed[k] = v
    db.commit()

    # Handing the book to the machine is the single most consequential switch on
    # this panel, so it gets its own record rather than being one key inside a
    # CONFIG_UPDATED blob. Signals already on the board are deliberately NOT
    # swept up — arming applies from the next signal onward, or flipping the
    # toggle on a busy morning would buy the whole feed in one go.
    if arming_exec:
        from app.services import auto_exec
        armed_now, why = auto_exec.armed(cfg)
        audit(db, "AUTO_EXEC_ARMED", "",
              (f"automated execution ON — risk {cfg.auto_risk_pct:.2f}%/trade, "
               f"max {cfg.auto_max_lots} lots, max {cfg.auto_max_positions} positions, "
               f"averaging {cfg.averaging_mode}"
               + ("" if armed_now else f" — INACTIVE: {why}")),
              level="WARN")
    # Push the new session into the cache the tick path reads, or the change
    # would not take effect until the next restart.
    refresh_from_config(cfg)
    audit(db, "CONFIG_UPDATED", "", str(changed))

    # Handing exits back to the engine has to act on what manual mode held back,
    # here and not on the next tick — after the close there is no next tick, and
    # the breached positions would sit unsold until the morning.
    swept = exit_engine.sweep_manual_holds(db) if resuming_auto else 0
    if swept:
        audit(db, "AUTO_EXIT_RESUMED", "", f"squared off {swept} position(s) still below stop")

    STATE.hub.broadcast("config", _config_dict(cfg))
    return {"ok": True, "config": _config_dict(cfg), "auto_exited": swept}


# ---- Blacklist ------------------------------------------------------------
class BlacklistReq(BaseModel):
    symbol: str
    reason: str = "manual"


@router.get("/blacklist")
def list_blacklist(db: Session = Depends(get_db)):
    rows = db.scalars(select(Blacklist).order_by(Blacklist.created_at.desc())).all()
    return [{"symbol": b.symbol, "reason": b.reason, "created_at": iso_utc(b.created_at)} for b in rows]


@router.post("/blacklist")
def add_blacklist(req: BlacklistReq, db: Session = Depends(get_db)):
    sym = req.symbol.upper()
    if not db.scalar(select(Blacklist).where(Blacklist.symbol == sym)):
        db.add(Blacklist(symbol=sym, reason=req.reason))
        db.commit()
        audit(db, "BLACKLIST_ADD", sym, req.reason)
    return {"ok": True}


@router.delete("/blacklist/{symbol}")
def remove_blacklist(symbol: str, db: Session = Depends(get_db)):
    row = db.scalar(select(Blacklist).where(Blacklist.symbol == symbol.upper()))
    if row:
        db.delete(row)
        db.commit()
        audit(db, "BLACKLIST_REMOVE", symbol.upper())
    return {"ok": True}


# ---- Stock info (per-stock config modal) ----------------------------------
@router.get("/stock/{symbol}")
def stock_info(symbol: str, db: Session = Depends(get_db)):
    sym = symbol.upper()
    uni = gw.universe().get(sym)
    blacklisted = bool(db.scalar(select(Blacklist).where(Blacklist.symbol == sym)))
    held = db.scalar(select(Position).where(Position.symbol == sym, Position.status == "OPEN"))
    return {
        "symbol": sym,
        "ltp": STATE.prices.get(sym, uni[0] if uni else 0.0),
        "lot_size": gw.lot_size(sym),
        "margin_per_lot": MARGIN.margin_per_lot(sym),
        "expiry": gw.expiry_of(sym),
        "sector": "NSE F&O",
        "blacklisted": blacklisted,
        "held": pos_svc.serialize(held) if held else None,
    }


# ---- Journal / logs -------------------------------------------------------
@router.get("/journal")
def journal(book: str = "all", db: Session = Depends(get_db)):
    """Closed trades plus stats. `book` = all | live | paper.

    Stats for the two books are always returned separately — a simulated win
    rate mixed into the real one would be worse than no number at all.
    """
    paper = {"live": False, "paper": True}.get(book)
    return {
        "trades": journal_svc.closed_trades(db, paper=paper),
        "stats": journal_svc.stats(db, paper=False),
        "paper_stats": journal_svc.stats(db, paper=True),
    }


@router.get("/logs")
def logs(action: Optional[str] = None, symbol: Optional[str] = None,
         limit: int = 300, db: Session = Depends(get_db)):
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if symbol:
        stmt = stmt.where(AuditLog.symbol == symbol.upper())
    rows = db.scalars(stmt).all()
    return [{"action": r.action, "symbol": r.symbol, "detail": r.detail,
             "level": r.level, "created_at": iso_utc(r.created_at)} for r in rows]


# ---- Scripmaster ----------------------------------------------------------
@router.get("/scripmaster")
def scripmaster_status():
    from app.services.scripmaster import SCRIPMASTER
    return {"as_of": SCRIPMASTER.as_of(), "contracts": SCRIPMASTER.count(),
            "stale": SCRIPMASTER.is_stale(), "exchange": SCRIPMASTER.exchange}


@router.post("/scripmaster/refresh")
def scripmaster_refresh():
    """Re-download the contract master from Shoonya (daily otherwise)."""
    from app.services.scripmaster import SCRIPMASTER
    return SCRIPMASTER.refresh(force=True)


# ---- Margin ---------------------------------------------------------------
@router.get("/margin")
def margin_status():
    return {"as_of": MARGIN.as_of(), "stale": MARGIN.is_stale()}


@router.post("/margin/refresh")
def margin_refresh():
    return MARGIN.refresh()


# ---- Reconcile / kill / resume --------------------------------------------
@router.post("/reconcile")
def reconcile_now(db: Session = Depends(get_db)):
    return reconcile_once(db, source="MANUAL")


@router.post("/kill-switch")
def kill_switch(db: Session = Depends(get_db)):
    cfg = get_config(db)
    # Real money only. Sweeping simulated positions into an emergency square-off
    # would also misreport the count as real exits. Halting signals below stops
    # the paper trader from taking anything new regardless.
    open_positions = db.scalars(select(Position).where(
        Position.status == "OPEN", Position.is_paper.is_(False))).all()
    for p in open_positions:
        pos_svc.exit_full(db, p.id, reason="KILL")
    cfg.signals_halted = True
    db.commit()
    audit(db, "KILL_SWITCH", "", f"squared off {len(open_positions)} positions; signals halted", level="ERROR")
    STATE.hub.broadcast("alert", {"type": "KILL_SWITCH",
                                  "message": f"Kill switch activated — {len(open_positions)} positions squared off."})
    return {"ok": True, "closed": len(open_positions)}


@router.post("/resume")
def resume(db: Session = Depends(get_db)):
    cfg = get_config(db)
    cfg.signals_halted = False
    db.commit()
    audit(db, "SIGNALS_RESUMED", "", "signal processing resumed")
    return {"ok": True}


# ---- Broker session -------------------------------------------------------
# The broker login lives in a separate gateway process that this strategy shares
# with another system. These endpoints proxy it so the dashboard can show WHICH
# account is trading and hand back control of the session.
#
# All three write STATE.broker_connected from the action's own result rather
# than re-reading the gateway's /api/status, because that endpoint caches its
# liveness probe for 20 seconds: polling right after a connect or disconnect
# returns the previous answer, and the dashboard would show a session that had
# just been torn down (or miss one that had just come up) for the whole window.

@router.get("/broker")
def broker_status():
    # Identity is only reported while connected. Showing the last-known account
    # next to a disconnected indicator reads as "logged in as X" and is exactly
    # the wrong impression to leave before someone presses a button that trades.
    user = gw.broker_user() if STATE.broker_connected else {}
    return {
        "connected": STATE.broker_connected,
        "uid": user.get("uid", ""),
        "account_id": user.get("account_id", ""),
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "broker": user.get("broker", "Shoonya"),
        # A connected flag with no identity behind it means the gateway answered
        # its status check but would not say who it is — usually a session that
        # is alive enough to respond and dead enough to reject real calls.
        "identified": bool(user.get("uid")),
    }


@router.post("/broker/disconnect")
def broker_disconnect(db: Session = Depends(get_db)):
    open_live = db.scalars(select(Position).where(
        Position.status == "OPEN", Position.is_paper.is_(False))).all()

    res = gw.disconnect()
    STATE.broker_action_wall = _time.time()
    STATE.broker_connected = False
    STATE.feed.ticker_connected = False
    STATE.hub.broadcast("connection", {"broker": False})

    # Said plainly, because disconnecting is not a neutral act while positions
    # are open: no prices means no stop-loss monitoring, and the exit engine is
    # driven entirely by ticks that will stop arriving.
    warn = ""
    if open_live:
        warn = (f"{len(open_live)} live position(s) are still open — stops are NOT "
                f"being monitored while disconnected.")
    audit(db, "BROKER_DISCONNECTED", "",
          f"user-initiated disconnect. {warn}".strip(),
          level="ERROR" if open_live else "INFO")
    if warn:
        STATE.hub.broadcast("alert", {"type": "BROKER_DISCONNECTED", "message": warn})
    return {"ok": res.get("ok", False), "connected": False,
            "detail": res.get("detail", ""), "warning": warn}


@router.post("/broker/connect")
def broker_connect(db: Session = Depends(get_db)):
    """Force a genuine re-login rather than just re-reading the status.

    A plain status check reports on a session; it cannot create one. The gateway
    reuses any token issued the same day, so a session the broker killed
    mid-session survives a restart untouched — this is the only path that
    actually replaces it.
    """
    res = gw.connect_force()
    already = bool(res.get("already_connected"))
    STATE.broker_action_wall = _time.time()
    STATE.broker_connected = bool(res.get("connected"))
    STATE.hub.broadcast("connection", {"broker": STATE.broker_connected})
    user = gw.broker_user() if STATE.broker_connected else {}
    audit(db, "BROKER_CONNECTED" if STATE.broker_connected else "BROKER_CONNECT_FAILED", "",
          (("already connected — no action taken" if already
            else f"logged in as {user.get('uid') or 'unknown'}") if STATE.broker_connected
           else f"connect failed: {res.get('detail') or 'no detail'}"),
          level="INFO" if STATE.broker_connected else "ERROR")
    return {"ok": res.get("ok", False), "connected": STATE.broker_connected,
            "already_connected": already,
            "detail": res.get("detail", ""), "uid": user.get("uid", ""),
            "name": user.get("name", ""), "broker": user.get("broker", "Shoonya")}
