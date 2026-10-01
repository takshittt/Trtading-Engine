"""Position Manager — entries, averaging, exits, partial exits, rollover.

All broker interaction goes through the LIMIT-only smart order module
(execution.py); this layer owns strategy state: weighted-average price,
averaging counts, Target/SL (manual or auto method), trailing-SL state, and
P&L on close.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import symlock
from app.core.state import STATE, audit
from app.core.timeutil import as_utc, iso_utc
from app.services import budget as budget_svc
from app.services import execution
from app.services import gateway_service as gw
from app.services import signals as signal_svc
from app.services import targets as target_svc
from app.services.margin import MARGIN
from db.engine import get_config
from db.models import Position, Signal


def _broadcast_position(p: Position) -> None:
    STATE.hub.broadcast("position", serialize(p))


def _mark_acted(db: Session, signal_id: int | None, *, status: str = "ACTED",
                note: str | None = None, fill: dict | None = None,
                position_id: int | None = None, paper: bool = False,
                auto: bool = False) -> None:
    """Settle the signal that produced a trade, recording how it was executed."""
    if not signal_id:
        return
    sig = db.get(Signal, signal_id)
    if not sig:
        return
    sig.status = status
    if note is not None:
        sig.note = note[:120]
    if paper:
        sig.paper = True
    # Real money, placed by the machine. Set here rather than only in auto_exec's
    # own settle path, which a successful BUY never reaches — the entry settles
    # through this function, so without it every auto entry looked hand-placed.
    if auto:
        sig.auto = True
    if position_id is not None:
        sig.position_id = position_id
    if fill:
        sig.executed_at = datetime.now(timezone.utc)
        sig.exec_price = fill.get("filled_price", 0.0)
        sig.exec_latency_ms = fill.get("latency_ms", 0)
    db.commit()
    STATE.hub.broadcast("signal", signal_svc.serialize(sig))


def _stamp_provenance(pos: Position, sig: Signal | None) -> None:
    """Copy the originating signal's identity and timings onto the position."""
    if sig is None:
        return
    pos.signal_id = sig.id
    pos.timeframe = sig.timeframe
    pos.signal_price = sig.signal_ltp or sig.ltp
    pos.signal_time = sig.signal_time
    pos.signal_received_at = sig.created_at


def open_position(db: Session, *, symbol: str, lots: int,
                  target_mode: str, target_method: str, target_value: float,
                  sl_mode: str, sl_method: str, sl_value: float,
                  atr: float = 0.0, resistance: float = 0.0, support: float = 0.0,
                  signal_id: int | None = None, is_paper: bool = False,
                  opened_by: str = "manual") -> dict:
    """Manual buy entry. Target/SL each resolved as manual price or auto method.

    A repeat BUY on a stock that is already in the open book is *always* an
    averaging buy — never a second fresh entry — no matter how the request got
    here (a re-signal today, a previous day's signal, or a stale dashboard).
    """
    # Everything from the held-check to db.add() runs under the symbol's lock.
    # It is a check-then-create, and two near-simultaneous buys — a dashboard
    # double-click, or paper auto-execute racing a manual buy — both saw
    # `held is None` and both inserted a Position. Two open rows for one symbol
    # breaks every assumption downstream: the exit engine exits them separately,
    # averaging attaches to whichever it finds first, and margin is double
    # counted. The same lock also excludes the exit engine, which previously
    # held a different lock entirely and so could square a position off midway
    # through an averaging buy on it.
    with symlock.hold(symbol):
        return _open_position_locked(
            db, symbol=symbol, lots=lots, target_mode=target_mode,
            target_method=target_method, target_value=target_value, sl_mode=sl_mode,
            sl_method=sl_method, sl_value=sl_value, atr=atr, resistance=resistance,
            support=support, signal_id=signal_id, is_paper=is_paper,
            opened_by=opened_by)


def _open_position_locked(db: Session, *, symbol: str, lots: int,
                          target_mode: str, target_method: str, target_value: float,
                          sl_mode: str, sl_method: str, sl_value: float,
                          atr: float, resistance: float, support: float,
                          signal_id: int | None, is_paper: bool,
                          opened_by: str = "manual") -> dict:
    cfg = get_config(db)
    # Paper and live are separate books: a simulated buy must never attach
    # itself to a real position, and vice versa.
    held = db.scalar(select(Position).where(
        Position.symbol == symbol, Position.status == "OPEN",
        Position.is_paper.is_(is_paper)))
    if held is not None:
        return {**average_position(db, held.id, lots, signal_id=signal_id), "averaged": True}

    sig = db.get(Signal, signal_id) if signal_id else None
    gw.ensure_subscribed(symbol)          # stream this symbol's price for exit monitoring
    lot_size = gw.lot_size(symbol) or 1
    qty = lots * lot_size
    # Paper locks no margin, so don't pay for a SPAN lookup it will never use.
    margin = 0.0 if is_paper else MARGIN.margin_per_lot(symbol) * lots

    # Paper entries are simulated, so they lock no real margin and are not
    # gated by the live budget — the ₹ budget belongs to the real book.
    if not is_paper:
        allowed, reason, _ = budget_svc.can_open_new(db, cfg, margin, is_averaging=False)
        if not allowed:
            return {"ok": False, "error": reason}

    fill = execution.place(db, symbol=symbol, side="BUY", qty=qty, intent="ENTRY",
                           paper=is_paper, signal_id=signal_id,
                           received_at=sig.created_at if sig else None)
    if fill["status"] not in ("FILLED", "PARTIAL"):
        # Carry the reason up. "Order REJECTED" on its own tells the user
        # nothing about why the entry never happened.
        return {"ok": False, "error": fill.get("reason") or f"Order {fill['status']}",
                "order": fill}

    price = fill["filled_price"]
    actual_qty = fill.get("qty_filled", qty)
    actual_lots = max(1, actual_qty // lot_size) if lot_size else 1
    actual_margin = 0.0 if is_paper else MARGIN.margin_per_lot(symbol) * actual_lots

    tgt = target_svc.resolve_target(price, cfg, mode=target_mode, method=target_method,
                                    manual_value=target_value, atr=atr, resistance=resistance)
    sl = target_svc.resolve_sl(price, cfg, mode=sl_mode, method=sl_method,
                               manual_value=sl_value, atr=atr, support=support)

    # Checked against the FILL price, not the signal price, because that is what
    # the exit engine compares against. A manual stop typed above the entry used
    # to be accepted and then square the position off on the next tick.
    #
    # The entry has already filled by this point, so refusing outright would
    # leave a real position with no row to manage it. Instead the offending
    # level is dropped and the position opens unprotected-but-visible, with a
    # loud audit — a position the user must set a level on beats one that exits
    # itself for no reason, and beats an untracked fill.
    level_err = _validate_levels(price, tgt, sl)
    if level_err:
        if sl and sl >= price:
            sl = 0.0
        if tgt and tgt <= price:
            tgt = 0.0
        audit(db, "LEVELS_REJECTED", symbol,
              f"{level_err} Level dropped — position opened without it, set one manually.",
              level="ERROR")
        STATE.hub.broadcast("alert", {
            "type": "LEVELS_REJECTED",
            "message": f"{symbol}: {level_err} The level was not applied — set it manually."})

    pos = Position(
        symbol=symbol, status="OPEN", avg_price=price, qty=actual_qty, lots=actual_lots,
        lot_size=lot_size, target=tgt, stop_loss=sl,
        target_method=(target_method if target_mode == "auto" else "manual"),
        sl_method=(sl_method if sl_mode == "auto" else "manual"),
        atr=atr, resistance=resistance, support=support,
        averaging_count=0, ltp=price, expiry=gw.expiry_of(symbol),
        margin_used=actual_margin, used_reserve=False,
        is_paper=is_paper, opened_by=opened_by,
        executed_at=datetime.now(timezone.utc), exec_latency_ms=fill.get("latency_ms", 0),
        # Frozen record of the entry leg. `avg_price` moves with every averaging
        # buy; these do not, so entry slippage stays a statement about the entry.
        entry_price=price, entry_ltp=fill.get("ltp", 0.0),
        entry_bid=fill.get("bid", 0.0), entry_ask=fill.get("ask", 0.0),
        peak_price=price, trough_price=price,
    )
    _stamp_provenance(pos, sig)
    db.add(pos)
    db.commit()
    db.refresh(pos)
    execution.attach_position(db, fill["order_id"], pos.id)

    kind = "Paper BUY" if is_paper else "BUY"
    _mark_acted(db, signal_id, status="EXECUTED" if is_paper else "ACTED",
                note=f"{kind} {actual_lots} lot @ {price}" if is_paper else None,
                fill=fill, position_id=pos.id, paper=is_paper,
                auto=(opened_by == "auto" and not is_paper))

    audit(db, "PAPER_POSITION_OPENED" if is_paper else "POSITION_OPENED", symbol,
          f"{lots} lot(s) @ {price} tgt={tgt} sl={sl} lat={fill.get('latency_ms', 0)}ms")
    _broadcast_position(pos)
    return {"ok": True, "position": serialize(pos)}


def average_position(db: Session, position_id: int, lots: int,
                     signal_id: int | None = None) -> dict:
    """Pyramiding buy on a held stock. Recomputes weighted avg + Target/SL."""
    pos = db.get(Position, position_id)
    if not pos or pos.status != "OPEN":
        return {"ok": False, "error": "Position not open"}
    # Re-entrant, so the nested call from open_position() is free; a direct call
    # from the API still serialises against the exit engine and the paper trader.
    with symlock.hold(pos.symbol):
        return _average_position_locked(db, pos, lots, signal_id)


def _average_position_locked(db: Session, pos: Position, lots: int,
                             signal_id: int | None) -> dict:
    cfg = get_config(db)
    if pos.status != "OPEN":
        return {"ok": False, "error": "Position not open"}   # re-checked under the lock
    if pos.averaging_count >= cfg.max_averaging_buys:
        return {"ok": False, "error": "Averaging limit reached"}

    sig = db.get(Signal, signal_id) if signal_id else None
    add_qty = lots * pos.lot_size
    add_margin = 0.0 if pos.is_paper else MARGIN.margin_per_lot(pos.symbol) * lots
    uses_reserve = False
    if not pos.is_paper:
        allowed, reason, uses_reserve = budget_svc.can_open_new(db, cfg, add_margin, is_averaging=True)
        if not allowed:
            return {"ok": False, "error": reason}

    fill = execution.place(db, symbol=pos.symbol, side="BUY", qty=add_qty, intent="AVERAGING",
                           paper=pos.is_paper, signal_id=signal_id,
                           received_at=sig.created_at if sig else None)
    if fill["status"] not in ("FILLED", "PARTIAL"):
        return {"ok": False, "error": fill.get("reason") or f"Order {fill['status']}"}

    price = fill["filled_price"]
    actual_add_qty = fill.get("qty_filled", add_qty)
    actual_add_lots = max(1, actual_add_qty // pos.lot_size) if pos.lot_size else 1
    actual_add_margin = 0.0 if pos.is_paper else MARGIN.margin_per_lot(pos.symbol) * actual_add_lots

    new_qty = pos.qty + actual_add_qty
    pos.avg_price = round((pos.avg_price * pos.qty + price * actual_add_qty) / new_qty, 2)
    pos.qty = new_qty
    pos.lots += actual_add_lots
    pos.averaging_count += 1
    if not pos.is_paper:
        pos.margin_used += actual_add_margin
    pos.used_reserve = pos.used_reserve or uses_reserve
    pos.target, pos.stop_loss = target_svc.recompute_for_position(
        pos.avg_price, cfg, target_method=pos.target_method, sl_method=pos.sl_method,
        atr=pos.atr, resistance=pos.resistance, support=pos.support,
        current_target=pos.target, current_sl=pos.stop_loss)
    # a fresh averaging buy resets any trailing phase (target moved up)
    pos.target_hit = False
    pos.trailing_active = False
    pos.trail_peak = 0.0
    pos.trailing_sl = 0.0
    db.commit()
    execution.attach_position(db, fill["order_id"], pos.id)
    _mark_acted(db, signal_id, status="AVERAGED" if pos.is_paper else "ACTED",
                note=(f"Paper averaging buy #{pos.averaging_count} @ {price} "
                      f"→ avg {pos.avg_price}") if pos.is_paper else None,
                fill=fill, position_id=pos.id, paper=pos.is_paper,
                auto=(pos.opened_by == "auto" and not pos.is_paper))

    audit(db, "PAPER_AVERAGING_BUY" if pos.is_paper else "AVERAGING_BUY", pos.symbol,
          f"#{pos.averaging_count} +{lots} lot @ {price} new_avg={pos.avg_price} "
          f"lat={fill.get('latency_ms', 0)}ms")
    _broadcast_position(pos)
    return {"ok": True, "position": serialize(pos)}


def _safe_lot_size(pos: Position) -> int:
    """Never 0 — used wherever qty is divided by it.

    Re-resolves from the gateway rather than just defaulting to 1, so a position
    created on a day the scripmaster was empty gets its real lot size back
    instead of silently reporting the wrong number of lots forever.
    """
    if pos.lot_size and pos.lot_size > 0:
        return pos.lot_size
    try:
        resolved = int(gw.lot_size(pos.symbol) or 0)
    except Exception:
        resolved = 0
    return resolved if resolved > 0 else 1


def _validate_levels(price: float, tgt: float, sl: float) -> str:
    """Reject levels that are on the wrong side of the entry.

    A stop above the entry fires on the very next tick — the position is bought
    and instantly squared off at a loss, which looks like a broker problem and
    is not. A target below the entry arms the trailing stop immediately for the
    same reason. Both are reachable from the dashboard by typing a manual level
    into the wrong box, and nothing checked either.

    0 is allowed and means "not set" — the exit engine already treats a falsy
    level as absent.
    """
    if sl and sl >= price:
        return (f"Stop-loss {sl} is at or above the entry price {price} — it would "
                f"trigger immediately. Set a stop below the entry.")
    if tgt and tgt <= price:
        return (f"Target {tgt} is at or below the entry price {price} — it would "
                f"arm the trailing stop immediately. Set a target above the entry.")
    if tgt and sl and tgt <= sl:
        return f"Target {tgt} is not above the stop-loss {sl} — reward:risk would be negative."
    return ""


def _close_qty(db: Session, pos: Position, qty: int, reason: str,
               exit_signal: Signal | None = None) -> dict:
    fill = execution.place(db, symbol=pos.symbol, side="SELL", qty=qty, intent=reason,
                           paper=pos.is_paper,
                           signal_id=exit_signal.id if exit_signal else None,
                           received_at=exit_signal.created_at if exit_signal else None)
    if fill["status"] not in ("FILLED", "PARTIAL"):
        return {"ok": False, "error": fill.get("reason") or f"Exit order {fill['status']}"}
    
    exit_price = fill["filled_price"]
    actual_qty = fill.get("qty_filled", qty)
    
    realized = round((exit_price - pos.avg_price) * actual_qty, 2)
    pos.realized_pnl += realized
    pos.exit_qty += actual_qty          # survives qty being drawn down to zero
    pos.qty -= actual_qty
    # lot_size is written once at creation and never revisited, so a scripmaster
    # that returned nothing that day leaves a 0 on the row forever — and this
    # line is on the EXIT path, meaning the crash lands when you are trying to
    # get out. Re-resolve first, fall back to treating the position as one lot.
    pos.lot_size = _safe_lot_size(pos)
    pos.lots = max(0, pos.qty // pos.lot_size)
    execution.attach_position(db, fill["order_id"], pos.id)

    if pos.qty <= 0:
        pos.status = "CLOSED"
        pos.exit_price = exit_price
        pos.exit_reason = reason
        pos.closed_at = datetime.now(timezone.utc)
        pos.margin_used = 0.0
        # The breach is resolved once the position is gone — leaving the flag set
        # would mark a closed trade as still waiting on the user.
        pos.sl_breached = False
        pos.sl_breached_at = None
        pos.exit_executed_at = pos.closed_at
        pos.exit_latency_ms = fill.get("latency_ms", 0)
        pos.exit_bid = fill.get("bid", 0.0)
        pos.exit_ask = fill.get("ask", 0.0)
        if exit_signal is not None:
            pos.exit_signal_at = exit_signal.created_at
    elif not pos.is_paper:
        pos.margin_used = round(MARGIN.margin_per_lot(pos.symbol) * pos.lots, 2)
    db.commit()
    _broadcast_position(pos)
    return {"ok": True, "realized": realized, "exit_price": exit_price,
            "latency_ms": fill.get("latency_ms", 0), "position": serialize(pos)}


def exit_full(db: Session, position_id: int, reason: str = "MANUAL",
              exit_signal: Signal | None = None) -> dict:
    pos = db.get(Position, position_id)
    if not pos or pos.status != "OPEN":
        return {"ok": False, "error": "Position not open"}
    paper = pos.is_paper
    res = _close_qty(db, pos, pos.qty, reason, exit_signal=exit_signal)
    if res["ok"]:
        audit(db, "PAPER_POSITION_CLOSED" if paper else "POSITION_CLOSED", pos.symbol,
              f"reason={reason} exit={res['exit_price']} pnl={pos.realized_pnl} "
              f"lat={res.get('latency_ms', 0)}ms")
    return res


def exit_partial(db: Session, position_id: int, lots: int) -> dict:
    """Exit a specific NUMBER OF LOTS (user-typed). Requires >1 lot held."""
    pos = db.get(Position, position_id)
    if not pos or pos.status != "OPEN":
        return {"ok": False, "error": "Position not open"}
    if pos.lots <= 1:
        return {"ok": False, "error": "Only 1 lot — use Complete Exit"}
    lots = max(1, min(lots, pos.lots - 1))   # keep at least 1 lot open for a partial
    qty = lots * pos.lot_size
    res = _close_qty(db, pos, qty, "PARTIAL")
    if res["ok"]:
        audit(db, "PARTIAL_EXIT", pos.symbol, f"-{lots} lot @ {res['exit_price']} ({pos.lots} left)")
    return res


def edit_targets(db: Session, position_id: int, target: float | None,
                 stop_loss: float | None) -> dict:
    pos = db.get(Position, position_id)
    if not pos or pos.status != "OPEN":
        return {"ok": False, "error": "Position not open"}
    if target is not None:
        pos.target = target
        pos.target_method = "manual"
    if stop_loss is not None:
        pos.stop_loss = stop_loss
        pos.sl_method = "manual"
    db.commit()
    audit(db, "TARGET_EDITED", pos.symbol, f"tgt={pos.target} sl={pos.stop_loss}")
    _broadcast_position(pos)
    return {"ok": True, "position": serialize(pos)}


def roll_targets(db: Session, position_id: int) -> list[dict]:
    """Contracts this position can roll into, nearest expiry first, each priced."""
    pos = db.get(Position, position_id)
    if not pos or pos.status != "OPEN":
        return []
    try:
        rows = gw.gateway().next_expiries(pos.symbol)
    except Exception:
        return []
    out = []
    for r in rows:
        q = {}
        try:
            q = gw.gateway().quote_token(r.get("exch", ""), r.get("token", ""))
        except Exception:
            pass
        out.append({**r, "ltp": q.get("ltp", 0.0),
                    "bid": q.get("bid", 0.0), "ask": q.get("ask", 0.0)})
    return out


def roll_position(db: Session, position_id: int, *, target_tsym: str, target_expiry: str,
                  qty: int = 0, price_type: str = "LMT",
                  exit_price: float = 0.0, entry_price: float = 0.0,
                  carry_pnl: bool = True, carry_target: bool = True,
                  reason: str = "MANUAL") -> dict:
    """Close the near contract and open the far one, carrying the position.

    Two real orders are placed — this is not a bookkeeping shift. The near leg
    is sold and the far leg bought, each through the same execution path (and
    the same paper simulation) as any other order, so a rolled paper position
    keeps paying the spread it would really pay.

    `carry_pnl` decides what the far contract inherits. Carried, the near leg's
    result is folded into the new average so the position continues as one
    trade and its target still means what it meant before. Not carried, the
    near leg's P&L is booked and the far contract starts clean at its fill.

    `carry_target` shifts Target and SL by the roll basis (far entry − near
    exit), which preserves both distances in rupees across the contract change.
    """
    from db.models import Rollover

    pos = db.get(Position, position_id)
    if not pos or pos.status != "OPEN":
        return {"ok": False, "error": "Position not open"}
    if not target_tsym:
        return {"ok": False, "error": "No target contract selected"}

    qty = pos.qty if qty <= 0 else min(qty, pos.qty)
    if qty <= 0:
        return {"ok": False, "error": "Nothing to roll"}
    market = (price_type or "LMT").upper() == "MKT"

    # 1) Close the near leg.
    near = execution.place(db, symbol=pos.symbol, side="SELL", qty=qty, intent="ROLLOVER",
                           paper=pos.is_paper)
    if near["status"] not in ("FILLED", "PARTIAL"):
        return {"ok": False, "error": f"Near-leg exit {near['status']} — position untouched"}
    near_px = near["filled_price"] if market else (exit_price or near["filled_price"])
    actual_near_qty = near.get("qty_filled", qty)

    # 2) Open the far leg. The near leg is already closed, so a failure here
    #    leaves the position flat rather than silently half-rolled — say so.
    far = execution.place(db, symbol=target_tsym, side="BUY", qty=actual_near_qty, intent="ROLLOVER",
                          paper=pos.is_paper)
    if far["status"] not in ("FILLED", "PARTIAL"):
        realized = round((near_px - pos.avg_price) * actual_near_qty, 2)
        pos.realized_pnl += realized
        pos.exit_qty += actual_near_qty
        pos.qty -= actual_near_qty
        pos.lots = max(0, pos.qty // pos.lot_size)
        if pos.qty <= 0:
            pos.status = "CLOSED"
            pos.exit_price = near_px
            pos.exit_reason = "ROLL_FAILED"
            pos.closed_at = datetime.now(timezone.utc)
            pos.margin_used = 0.0
        db.commit()
        audit(db, "ROLLOVER_HALF_DONE", pos.symbol,
              f"near leg closed @ {near_px} but {target_tsym} entry {far['status']} — "
              f"position is FLAT, re-enter manually", level="ERROR")
        STATE.hub.broadcast("alert", {
            "type": "ROLLOVER_FAILED",
            "message": (f"{pos.symbol}: near leg closed but {target_tsym} entry failed. "
                        f"You are FLAT — re-enter manually.")})
        _broadcast_position(pos)
        return {"ok": False, "error": f"Far-leg entry {far['status']}. Position is flat.",
                "flat": True}

    actual_far_qty = far.get("qty_filled", actual_near_qty)
    orphaned_qty = actual_near_qty - actual_far_qty
    
    far_px = far["filled_price"] if market else (entry_price or far["filled_price"])
    basis = round(far_px - near_px, 2)
    
    # Realize PnL on the orphaned shares that failed to roll. Quantity is not
    # adjusted here — pos.qty is assigned outright below from what the far leg
    # actually took on. Doing both subtracted the shortfall twice: a 100-lot roll
    # whose far leg filled 80 ended up holding 60.
    if orphaned_qty > 0:
        orphan_pnl = round((near_px - pos.avg_price) * orphaned_qty, 2)
        pos.realized_pnl += orphan_pnl
        pos.exit_qty += orphaned_qty
        # Said out loud. The position silently shrinks — 100 lots go in, 80 come
        # out — and booking the difference as realized P&L made it look like a
        # deliberate partial exit. Nothing re-enters the shortfall, so this is a
        # decision the user has to make, and they can only make it if they know.
        audit(db, "ROLLOVER_PARTIAL", pos.symbol,
              f"far leg filled {actual_far_qty} of {actual_near_qty} — {orphaned_qty} "
              f"closed at {near_px} for {orphan_pnl:+.2f} and NOT rolled. Position is "
              f"now {actual_far_qty}; re-enter manually if you want the size back.",
              level="ERROR")
        STATE.hub.broadcast("alert", {
            "type": "ROLLOVER_PARTIAL",
            "message": f"{pos.symbol}: only {actual_far_qty}/{actual_near_qty} rolled — "
                       f"{orphaned_qty} closed at {near_px}. Position size reduced."})

    # The leg PnL for the shares that successfully rolled
    leg_pnl = round((near_px - pos.avg_price) * actual_far_qty, 2)

    old = {"expiry": pos.expiry, "target": pos.target, "sl": pos.stop_loss,
           "avg": pos.avg_price, "symbol": pos.symbol}

    # 3) Carry the position onto the far contract.
    if carry_pnl:
        # Fold the near leg's result into the new cost basis so the roll is P&L
        # neutral and the position reads as one continuous trade.
        pos.avg_price = round(far_px - (leg_pnl / actual_far_qty), 2) if actual_far_qty else pos.avg_price
    else:
        pos.realized_pnl += leg_pnl
        pos.avg_price = far_px

    pos.symbol = target_tsym
    pos.expiry = target_expiry or pos.expiry
    pos.lot_size = gw.lot_size(target_tsym) or pos.lot_size
    # What the far leg actually took on — the position now lives entirely in the
    # new contract, so this is its whole size.
    pos.qty = actual_far_qty
    pos.lots = max(1, pos.qty // pos.lot_size) if pos.lot_size else pos.lots
    pos.ltp = far_px

    # Excursion across a roll.
    #
    # These used to be reset to the far price outright, which threw away
    # everything the trade did on the near contract — a position rolled after
    # running 8% in your favour reported its MFE as whatever it managed on the
    # far leg alone, and the journal's avg_mfe/avg_mae were quietly wrong for
    # every rolled trade.
    #
    # When the P&L is carried, the roll is deliberately made continuous: the
    # cost basis is rewritten so the position reads as ONE trade. The excursion
    # has to follow the same rule, shifted into the far contract's price terms
    # by the basis — exactly what already happens to the target and the stop
    # just below. Otherwise the high-water mark is measured in near-contract
    # prices against a far-contract average.
    #
    # When the P&L is NOT carried, the near leg is booked and closed off and the
    # far leg genuinely starts fresh at far_px, so resetting is right there.
    if carry_pnl and pos.peak_price and pos.trough_price:
        pos.peak_price = round(max(pos.peak_price + basis, far_px), 2)
        pos.trough_price = round(min(pos.trough_price + basis, far_px), 2)
    else:
        pos.peak_price = far_px
        pos.trough_price = far_px

    if carry_target:
        pos.target = round(pos.target + basis, 2) if pos.target else 0.0
        pos.stop_loss = round(pos.stop_loss + basis, 2) if pos.stop_loss else 0.0
        if pos.trailing_active:
            pos.trailing_sl = round(pos.trailing_sl + basis, 2)
            pos.trail_peak = round(pos.trail_peak + basis, 2)
    else:
        cfg = get_config(db)
        pos.target, pos.stop_loss = target_svc.recompute_for_position(
            pos.avg_price, cfg, target_method=pos.target_method, sl_method=pos.sl_method,
            atr=pos.atr, resistance=pos.resistance,
            current_target=pos.target, current_sl=pos.stop_loss)
        pos.target_hit = False
        pos.trailing_active = False

    db.add(Rollover(position_id=pos.id, symbol=pos.symbol, old_expiry=old["expiry"],
                    new_expiry=pos.expiry, old_target=old["target"], new_target=pos.target,
                    old_sl=old["sl"], new_sl=pos.stop_loss))
    db.commit()
    execution.attach_position(db, near["order_id"], pos.id)
    execution.attach_position(db, far["order_id"], pos.id)
    gw.ensure_subscribed(target_tsym)

    audit(db, "ROLLOVER", old["symbol"],
          f"[{reason}] {old['expiry']} -> {pos.expiry} qty={qty} near={near_px} far={far_px} "
          f"basis={basis} carry_pnl={carry_pnl} avg {old['avg']} -> {pos.avg_price}")
    STATE.hub.broadcast("alert", {
        "type": "ROLLOVER",
        "message": f"{old['symbol']} rolled to {target_tsym} · basis {basis:+.2f}"})
    _broadcast_position(pos)
    return {"ok": True, "basis": basis, "near_price": near_px, "far_price": far_px,
            "leg_pnl": leg_pnl, "position": serialize(pos)}


def rollover(db: Session, position_id: int, new_expiry: str, basis: float = 0.0) -> dict:
    """Basis-adjusted rollover (Grid math): entry/target/SL all shift by the
    roll basis = new_contract_fill − old_contract_exit, preserving exact ₹ P&L,
    target and stop distances across the contract change."""
    from db.models import Rollover
    pos = db.get(Position, position_id)
    if not pos or pos.status != "OPEN":
        return {"ok": False, "error": "Position not open"}
    old_expiry, old_t, old_sl = pos.expiry, pos.target, pos.stop_loss
    pos.expiry = new_expiry
    pos.avg_price = round(pos.avg_price + basis, 2)
    pos.target = round(pos.target + basis, 2)
    pos.stop_loss = round(pos.stop_loss + basis, 2)
    if pos.trailing_active:
        pos.trailing_sl = round(pos.trailing_sl + basis, 2)
        pos.trail_peak = round(pos.trail_peak + basis, 2)
    db.add(Rollover(position_id=pos.id, symbol=pos.symbol, old_expiry=old_expiry,
                    new_expiry=new_expiry, old_target=old_t, new_target=pos.target,
                    old_sl=old_sl, new_sl=pos.stop_loss))
    db.commit()
    audit(db, "ROLLOVER", pos.symbol,
          f"{old_expiry}->{new_expiry} basis={basis} entry'={pos.avg_price} tgt'={pos.target}")
    _broadcast_position(pos)
    return {"ok": True, "position": serialize(pos)}


def legs(db: Session, position_id: int) -> dict:
    """Every filled leg of one position, with the average price after each.

    This is what the ×N averaging badge opens. `avg_price` only ever shows the
    end state, so a position averaged three times says nothing about *where* it
    was averaged — which is exactly the question when a trade is sitting on a
    loss. Each leg carries its own time, fill, the book it crossed, and the
    running average it produced.

    The averages are recomputed from the orders rather than stored per leg:
    the order rows are the record of what actually happened, so a replay of
    them cannot disagree with the position it built.
    """
    from db.models import Order

    pos = db.get(Position, position_id)
    if pos is None:
        return {"ok": False, "error": "Position not found"}

    orders = db.scalars(
        select(Order).where(Order.position_id == position_id, Order.status == "FILLED")
        .order_by(Order.created_at, Order.id)).all()

    run_qty, run_cost, buy_n = 0, 0.0, 0
    rows: list[dict] = []
    for o in orders:
        price = o.filled_price or o.price
        avg_before = round(run_cost / run_qty, 2) if run_qty else 0.0
        if o.side == "BUY":
            buy_n += 1
            run_cost += price * o.qty
            run_qty += o.qty
        else:
            # A sell takes quantity out at the running cost basis; the per-unit
            # average of what is left is unchanged.
            run_qty = max(0, run_qty - o.qty)
            run_cost = avg_before * run_qty
        avg_after = round(run_cost / run_qty, 2) if run_qty else 0.0
        rows.append({
            "order_id": o.id, "broker_order_id": o.broker_order_id,
            "n": buy_n if o.side == "BUY" else 0,
            "label": ("Entry" if (o.side == "BUY" and buy_n == 1) else
                      f"Avg #{buy_n - 1}" if o.side == "BUY" else "Exit"),
            "side": o.side, "intent": o.intent, "qty": o.qty,
            "lots": round(o.qty / pos.lot_size, 2) if pos.lot_size else 0,
            "price": price, "limit_price": o.price, "order_type": o.order_type,
            "bid": o.bid, "ask": o.ask, "ltp_at_order": o.ltp_at_order,
            "spread_paid": round(price - o.ltp_at_order, 2) if o.ltp_at_order else 0.0,
            "value": round(price * o.qty, 2),
            "retries": o.retries, "latency_ms": o.latency_ms,
            "at": iso_utc(o.created_at),
            "qty_after": run_qty,
            "avg_before": avg_before, "avg_after": avg_after,
            # what this leg did to the cost basis — the number the popup is for
            "avg_delta": round(avg_after - avg_before, 2) if avg_before else 0.0,
        })

    ltp = STATE.prices.get(pos.symbol, pos.ltp)
    return {"ok": True, "position": serialize(pos), "legs": rows,
            "averaging_count": pos.averaging_count, "ltp": ltp}


def _iso(dt: datetime | None) -> str | None:
    return iso_utc(dt)


def serialize(p: Position) -> dict:
    ltp = STATE.prices.get(p.symbol, p.ltp)
    is_open = p.status == "OPEN"
    unreal = round((ltp - p.avg_price) * p.qty, 2) if is_open else 0.0
    # Gated on OPEN like the rupee figure. Ungated, a closed trade showed
    # unrealized_pct of +20% next to an unrealized_pnl of 0.00 — the two numbers
    # contradicting each other on the same row, both computed off a live LTP the
    # position no longer has any exposure to.
    unreal_pct = round((ltp / p.avg_price - 1) * 100, 2) if (is_open and p.avg_price) else 0.0
    opened, closed = as_utc(p.opened_at), as_utc(p.closed_at)
    days_held = (datetime.now(timezone.utc) - opened).days

    # Best/worst the trade ever got to. `p.qty or p.exit_qty` used the REMAINING
    # quantity for an open position that had been partially exited, so a trade
    # scaled out of reported an excursion on a fraction of the size it actually
    # held at the peak. Total ever held is remaining + already sold.
    #
    # Still an approximation when a position was averaged INTO after its peak,
    # because the size at the moment of the peak is not recorded — which is why
    # the per-unit figures below are also published: those are exact, whatever
    # the quantity did.
    size = (p.qty or 0) + (p.exit_qty or 0) or p.qty
    mfe = round((p.peak_price - p.avg_price) * size, 2) if p.peak_price else 0.0
    mae = round((p.trough_price - p.avg_price) * size, 2) if p.trough_price else 0.0
    mfe_pts = round(p.peak_price - p.avg_price, 2) if p.peak_price else 0.0
    mae_pts = round(p.trough_price - p.avg_price, 2) if p.trough_price else 0.0

    held_secs = int((closed - opened).total_seconds()) if closed else None
    # Derived from the money actually booked, over what that quantity cost.
    # exit_price/avg_price only described the FINAL leg, so a position scaled out
    # at +10% and closed at −2% reported −2% and called a winning trade a loser.
    cost_basis = (p.avg_price or 0.0) * (p.exit_qty or 0)
    realized_pct = round(p.realized_pnl / cost_basis * 100, 2) if cost_basis else 0.0
    # What the round trip paid in spread, per unit, versus the mid it crossed.
    entry_spread = round(p.entry_ask - p.entry_bid, 2) if (p.entry_ask and p.entry_bid) else 0.0
    exit_spread = round(p.exit_ask - p.exit_bid, 2) if (p.exit_ask and p.exit_bid) else 0.0

    # Entry slippage is about the ENTRY leg, so it is measured on that leg's own
    # fill — not on avg_price, which every averaging buy rewrites. Positions
    # opened before entry_price existed fall back to avg_price, which is exact
    # for the ones that were never averaged.
    entry_px = p.entry_price or p.avg_price
    slip = round(entry_px - p.signal_price, 2) if p.signal_price else 0.0
    # …and it splits into two very different things: the market moving between
    # the signal and the order (nobody's fault) and the spread the fill crossed.
    slip_market = round(p.entry_ltp - p.signal_price, 2) if (p.entry_ltp and p.signal_price) else 0.0
    slip_spread = round(entry_px - p.entry_ltp, 2) if p.entry_ltp else 0.0

    return {
        "id": p.id, "symbol": p.symbol, "status": p.status,
        "avg_price": p.avg_price, "qty": p.qty, "lots": p.lots, "lot_size": p.lot_size,
        "target": p.target, "stop_loss": p.stop_loss,
        "target_method": p.target_method, "sl_method": p.sl_method,
        "target_hit": p.target_hit, "trailing_active": p.trailing_active,
        "trailing_sl": p.trailing_sl, "trail_peak": p.trail_peak,
        # Stop breached while exits were manual — nothing was sold, the position
        # is waiting on the user.
        "sl_breached": p.sl_breached, "sl_breached_at": _iso(p.sl_breached_at),
        "averaging_count": p.averaging_count, "ltp": ltp,
        "unrealized_pnl": unreal, "unrealized_pct": unreal_pct,
        "realized_pnl": p.realized_pnl, "realized_pct": realized_pct, "days_held": days_held,
        "expiry": p.expiry, "margin_used": p.margin_used, "used_reserve": p.used_reserve,
        "exit_price": p.exit_price, "exit_reason": p.exit_reason,
        "opened_at": iso_utc(p.opened_at),
        "closed_at": _iso(p.closed_at),
        # -- paper / provenance / timing --------------------------------------
        "is_paper": p.is_paper, "opened_by": p.opened_by,
        "signal_id": p.signal_id, "timeframe": p.timeframe,
        "signal_price": p.signal_price,
        "signal_time": _iso(p.signal_time),
        "signal_received_at": _iso(p.signal_received_at),
        "executed_at": _iso(p.executed_at),
        "exec_latency_ms": p.exec_latency_ms,
        "entry_price": entry_px, "entry_ltp": p.entry_ltp,
        "entry_bid": p.entry_bid, "entry_ask": p.entry_ask, "entry_spread": entry_spread,
        "exit_signal_at": _iso(p.exit_signal_at),
        "exit_executed_at": _iso(p.exit_executed_at),
        "exit_latency_ms": p.exit_latency_ms,
        "exit_bid": p.exit_bid, "exit_ask": p.exit_ask, "exit_spread": exit_spread,
        # slippage of the actual entry fill versus where the signal fired
        "entry_slippage": slip,
        "slip_market": slip_market, "slip_spread": slip_spread,
        # how far the *running average* has drifted from the signal — averaging,
        # not execution quality. Kept separate so the two never get confused.
        "avg_drift": round(p.avg_price - p.signal_price, 2) if p.signal_price else 0.0,
        "peak_price": p.peak_price, "trough_price": p.trough_price,
        "mfe": mfe, "mae": mae, "exit_qty": p.exit_qty,
        # Per-unit excursion — exact regardless of how the quantity changed.
        "mfe_points": mfe_pts, "mae_points": mae_pts,
        "held_seconds": held_secs,
    }
