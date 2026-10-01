"""Automated paper trader — runs the strategy end-to-end without a broker order.

Purpose is measurement, not money: it exists to answer whether the Amibroker
setups are worth trading, and how much the round trip costs in latency and
spread once it hits a real book.

Flow, driven entirely by incoming signals:

    BUY  + nothing held  -> simulated LIMIT entry, lifting the ASK
    BUY  + already held  -> averaging buy at the ASK (up to max_averaging_buys)
    SELL + held          -> complete square-off, hitting the BID
    SELL + nothing held  -> recorded as a no-op (we never go short)

Fills come from the live Shoonya top-of-book, capped at the limit price the live
path would have used, so a paper trade pays exactly the spread a real order
would have crossed — and skips the trade when the book is beyond that limit,
because live it would not have filled either. Every leg is stamped with the time
the signal was received and the time the fill landed, which is what makes
execution latency measurable after the fact.

Paper positions live in the same table as real ones, flagged `is_paper`, and are
kept strictly separate everywhere it matters: they lock no margin, are not gated
by (or counted against) the live budget, and the exit engine manages their
Target/SL/trailing exactly as it does a real position.

Execution runs on a background thread so a slow quote never blocks the Amibroker
webhook — the latency it would add is measured, not inflicted on the caller.
"""
from __future__ import annotations

import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.state import STATE, audit
from app.services import positions as pos_svc
from app.services import signals as signal_svc
from db.engine import get_config, session
from db.models import Position, Signal

# One trade at a time. Two signals for the same symbol arriving together must
# not both open a fresh position — the second has to see the first one's book.
_lock = threading.Lock()


def enabled(db: Session) -> bool:
    return bool(get_config(db).paper_trading)


def _held(db: Session, symbol: str) -> Position | None:
    return db.scalar(select(Position).where(
        Position.symbol == symbol, Position.status == "OPEN", Position.is_paper.is_(True)))


def _settle(db: Session, sig: Signal, status: str, note: str) -> None:
    sig.status = status
    sig.note = note[:120]
    sig.paper = True
    db.commit()
    STATE.hub.broadcast("signal", signal_svc.serialize(sig))


def execute(db: Session, signal_id: int) -> dict:
    """Act on one signal. Safe to call for any signal — it self-filters."""
    cfg = get_config(db)
    if not cfg.paper_trading:
        return {"ok": False, "skipped": "paper trading off"}

    sig = db.get(Signal, signal_id)
    if sig is None:
        return {"ok": False, "skipped": "signal gone"}
    if sig.status not in signal_svc.ACTIONABLE:
        return {"ok": False, "skipped": f"status {sig.status}"}

    lots = max(1, cfg.paper_lots)
    held = _held(db, sig.symbol)

    if sig.signal_type == "SELL":
        if held is None:
            _settle(db, sig, "IGNORED", "Paper: no open position to square off")
            return {"ok": True, "action": "none"}
        res = pos_svc.exit_full(db, held.id, reason="SIGNAL", exit_signal=sig)
        if not res.get("ok"):
            _settle(db, sig, "EXEC_FAILED", f"Paper sell failed — {res.get('error', 'unknown')}")
            return res
        _settle(db, sig, "SQUARED_OFF",
                f"Paper SELL @ {res['exit_price']} · P&L ₹{res['position']['realized_pnl']}")
        sig.executed_at = held.exit_executed_at
        sig.exec_price = res["exit_price"]
        sig.exec_latency_ms = res.get("latency_ms", 0)
        sig.position_id = held.id
        db.commit()
        STATE.hub.broadcast("alert", {
            "type": "PAPER_EXIT",
            "message": (f"{sig.symbol} paper squared off @ ₹{res['exit_price']} "
                        f"· P&L ₹{res['position']['realized_pnl']}")})
        return res

    # BUY — fresh entry, or an averaging buy when the stock is already held.
    if held is not None and held.averaging_count >= cfg.max_averaging_buys:
        _settle(db, sig, "IGNORED",
                f"Paper: averaging limit reached ({cfg.max_averaging_buys})")
        return {"ok": True, "action": "none"}

    res = pos_svc.open_position(
        db, symbol=sig.symbol, lots=lots,
        target_mode=cfg.target_mode, target_method="atr", target_value=sig.target,
        sl_mode=cfg.sl_mode, sl_method="atr", sl_value=sig.stop_loss,
        atr=sig.atr, resistance=sig.resistance, support=sig.support,
        signal_id=sig.id, is_paper=True, opened_by="auto")

    if not res.get("ok"):
        _settle(db, sig, "EXEC_FAILED", f"Paper buy failed — {res.get('error', 'unknown')}")
        return res

    pos = res["position"]
    STATE.hub.broadcast("alert", {
        "type": "PAPER_AVERAGED" if res.get("averaged") else "PAPER_ENTRY",
        "message": (f"{sig.symbol} paper "
                    f"{'averaged' if res.get('averaged') else 'bought'} "
                    f"{lots} lot @ ₹{pos['avg_price']} ({pos['exec_latency_ms']}ms)")})
    return res


def execute_async(signal_id: int) -> None:
    """Fire-and-forget execution on its own session, off the webhook thread."""
    def run() -> None:
        with _lock:
            db = session()
            try:
                execute(db, signal_id)
            except Exception as exc:
                try:
                    audit(db, "PAPER_EXEC_ERROR", "", f"signal={signal_id}: {exc}", level="ERROR")
                except Exception:
                    pass
            finally:
                db.close()

    threading.Thread(target=run, daemon=True).start()
