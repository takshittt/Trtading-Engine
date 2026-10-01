"""Automated execution — the strategy takes its own entries on the REAL book.

This is `paper.py`'s twin, pointed at real money, and it is deliberately built
as a thin layer over the exact path a manual dashboard Buy already takes
(`positions.open_position` -> `execution.place`). Nothing here reimplements
ordering, averaging, locking or exits: an auto position is indistinguishable
from a hand-placed one the moment it exists, and the exit engine manages its
Target / SL / trailing stop the same way.

Flow, driven entirely by incoming signals:

    BUY  + nothing held  -> risk-sized LIMIT entry
    BUY  + already held  -> averaging buy, ONLY when averaging_mode is "auto"
    SELL + held          -> complete square-off
    SELL + nothing held  -> recorded as a no-op (this strategy never goes short)

THE STOPLOSS INTERLOCK
----------------------
Automated execution does nothing at all while `exit_mode` is "manual". That
toggle (the Stoploss switch on the Open Positions panel) means the user has
taken personal ownership of every exit: a breached stop is flagged and waits for
them rather than being sold. Opening fresh positions into that state would pile
up trades whose stops nothing is acting on automatically — the system would be
entering on its own while refusing to leave on its own. So the two are locked
together: automated entries require automated exits.

SIZING
------
Risk-based, off the signal's own stop:

    per_lot_risk = (entry - stop) * lot_size
    lots         = floor(total_budget * auto_risk_pct/100 / per_lot_risk)

capped by `auto_max_lots`. A signal whose stop is missing, or is not below the
entry, cannot be sized and is skipped rather than guessed at — sizing off a
default stop would silently risk an unknown amount.

Every decision writes a reason onto the signal and into the audit log, including
the refusals. An automated system that declines to trade and says nothing is
indistinguishable from one that is broken.
"""
from __future__ import annotations

import math
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.market_hours import is_market_open
from app.core.state import STATE, audit
from app.services import positions as pos_svc
from app.services import signals as signal_svc
from db.engine import get_config, session
from db.models import Position, Signal

# One live auto trade at a time, process-wide. Two signals for the same symbol
# arriving together must not both open a fresh position; `open_position` already
# holds a per-symbol lock that turns the second into an averaging buy, but
# serialising here also keeps the position-count cap honest — two entries racing
# past a `>= auto_max_positions` check would both read the pre-trade count.
_lock = threading.Lock()


def armed(cfg) -> tuple[bool, str]:
    """Is automated execution actually going to act? (armed, why-not).

    Returned as a reason rather than a bare bool so the dashboard and the audit
    log can both say *which* switch is holding it back.
    """
    if cfg.execution_mode != "automated":
        return False, "execution mode is manual"
    if cfg.exit_mode == "manual":
        return False, ("Stoploss is set to Manual — automated execution needs automated "
                       "exits, so it will not open or close anything")
    # Automated execution is a real-money path and nothing else. Paper mode
    # exists to prove the strategy WITHOUT spending, so leaving both armed would
    # have every signal simulate a trade and buy the same thing for real — two
    # positions per signal, only one of them pretend. They are mutually exclusive.
    if cfg.paper_trading:
        return False, ("Paper Trading is on — automated execution only trades real money, "
                       "so it stands down until paper mode is stopped")
    if cfg.signals_halted:
        return False, "signal processing is halted (kill switch or global SL)"
    return True, ""


def _held(db: Session, symbol: str) -> Position | None:
    """The LIVE position on this symbol, if any. Paper rows are a separate book."""
    return db.scalar(select(Position).where(
        Position.symbol == symbol, Position.status == "OPEN",
        Position.is_paper.is_(False)))


def _open_count(db: Session) -> int:
    return len(db.scalars(select(Position).where(
        Position.status == "OPEN", Position.is_paper.is_(False))).all())


def _settle(db: Session, sig: Signal, status: str, note: str) -> None:
    sig.status = status
    sig.note = note[:120]
    # Marks the signal as handled by automated execution against REAL money —
    # the counterpart of paper's `paper` flag, so the board can tell the two
    # apart instead of leaving real fills as the untagged default.
    sig.auto = True
    db.commit()
    STATE.hub.broadcast("signal", signal_svc.serialize(sig))


def _skip(db: Session, sig: Signal, reason: str) -> dict:
    """Decline to trade, loudly. The signal stays where it is on the board.

    Deliberately does NOT settle the signal: a refusal is this engine's decision,
    not the signal's outcome, and the user must still be able to take the trade
    by hand from the dashboard.
    """
    audit(db, "AUTO_EXEC_SKIPPED", sig.symbol, f"{sig.signal_type} — {reason}")
    return {"ok": False, "skipped": reason}


def size_lots(cfg, entry: float, stop: float, lot_size: int) -> tuple[int, str]:
    """Lots to buy for this entry/stop, or (0, why-not).

    Pure and side-effect free so the sizing rule can be tested and previewed
    without placing anything.
    """
    if entry <= 0:
        return 0, "no live price to size against"
    if stop <= 0:
        return 0, "signal carries no stop-loss — cannot size the risk"
    if stop >= entry:
        return 0, f"stop {stop} is not below the entry {entry}"
    if lot_size <= 0:
        return 0, "lot size unknown"

    per_lot_risk = (entry - stop) * lot_size
    budget_risk = cfg.total_budget * cfg.auto_risk_pct / 100.0
    if per_lot_risk <= 0:
        return 0, "risk per lot computed as zero"
    lots = int(math.floor(budget_risk / per_lot_risk))
    if lots < 1:
        return 0, (f"one lot risks {per_lot_risk:,.0f} which is over the "
                   f"{cfg.auto_risk_pct:.2f}% budget risk of {budget_risk:,.0f}")
    return min(lots, cfg.auto_max_lots), ""


def execute(db: Session, signal_id: int) -> dict:
    """Act on one signal against the real book. Safe to call for any signal —
    it self-filters on every gate below."""
    cfg = get_config(db)
    ok, why = armed(cfg)
    if not ok:
        return {"ok": False, "skipped": why}

    sig = db.get(Signal, signal_id)
    if sig is None:
        return {"ok": False, "skipped": "signal gone"}
    if sig.status not in signal_svc.ACTIONABLE:
        return {"ok": False, "skipped": f"status {sig.status}"}
    # Trade exactly the ⭐ Essential set the dashboard shows, off the same
    # predicate — so "what the machine will buy" and "what the board says is
    # actionable" cannot drift into two different answers. Scoped to the LIVE
    # book: a SELL whose only holding is a paper position has nothing real to
    # close, even though the board rightly still shows it.
    #
    # BUY only. For a SELL the Essential rule *is* "is it held", which the SELL
    # branch below already evaluates — and it settles the signal with a reason
    # instead of skipping it, which is the better outcome for a signal that will
    # never become tradable.
    live_held = signal_svc.open_symbols(db, live_only=True)
    if sig.signal_type == "BUY" and not signal_svc.is_essential(sig, live_held):
        return _skip(db, sig, "not Essential — failed the reward:risk gate")
    # Re-checked here and not just at ingest: this can be reached from a path
    # that revives an older signal, and an entry placed after the close would
    # rest unfilled overnight with no tick to manage it.
    if not is_market_open():
        return _skip(db, sig, "market is closed")

    held = _held(db, sig.symbol)

    if sig.signal_type == "SELL":
        if held is None:
            _settle(db, sig, "IGNORED", "Auto: no open position to square off")
            return {"ok": True, "action": "none"}
        res = pos_svc.exit_full(db, held.id, reason="SIGNAL", exit_signal=sig)
        if not res.get("ok"):
            return _fail(db, sig, f"Auto sell failed — {res.get('error', 'unknown')}")
        _settle(db, sig, "SQUARED_OFF",
                f"Auto SELL @ {res['exit_price']} · P&L ₹{res['position']['realized_pnl']}")
        audit(db, "AUTO_EXEC_EXIT", sig.symbol,
              f"squared off on SELL signal @ {res['exit_price']} "
              f"pnl={res['position']['realized_pnl']}")
        STATE.hub.broadcast("alert", {
            "type": "AUTO_EXIT",
            "message": (f"{sig.symbol} auto squared off @ ₹{res['exit_price']} "
                        f"· P&L ₹{res['position']['realized_pnl']}")})
        return res

    # ---- BUY ------------------------------------------------------------
    if held is not None:
        # Averaging is its own decision, with its own switch. Left alone when
        # that switch is manual, so the signal stays actionable and the user can
        # still average by hand from the dashboard.
        if cfg.averaging_mode != "auto":
            return _skip(db, sig, "already held and averaging mode is manual")
        if held.averaging_count >= cfg.max_averaging_buys:
            _settle(db, sig, "IGNORED",
                    f"Auto: averaging limit reached ({cfg.max_averaging_buys})")
            return {"ok": True, "action": "none"}
        lots = 1        # one lot per pyramid step; the entry already carries the risk
        res = pos_svc.average_position(db, held.id, lots, signal_id=sig.id)
        if not res.get("ok"):
            return _fail(db, sig, f"Auto averaging failed — {res.get('error', 'unknown')}")
        audit(db, "AUTO_EXEC_AVERAGED", sig.symbol,
              f"#{res['position']['averaging_count']} +{lots} lot → avg {res['position']['avg_price']}")
        STATE.hub.broadcast("alert", {
            "type": "AUTO_AVERAGED",
            "message": f"{sig.symbol} auto averaged +{lots} lot → avg ₹{res['position']['avg_price']}"})
        return res

    # Fresh entry.
    if _open_count(db) >= cfg.auto_max_positions:
        return _skip(db, sig, f"at the {cfg.auto_max_positions}-position limit")

    entry = sig.ltp or sig.signal_ltp
    lots, why = size_lots(cfg, entry, sig.stop_loss, sig.lot_size)
    if lots < 1:
        return _skip(db, sig, f"not sized — {why}")

    res = pos_svc.open_position(
        db, symbol=sig.symbol, lots=lots,
        target_mode=cfg.target_mode, target_method="atr", target_value=sig.target,
        sl_mode=cfg.sl_mode, sl_method="atr", sl_value=sig.stop_loss,
        atr=sig.atr, resistance=sig.resistance, support=sig.support,
        signal_id=sig.id, is_paper=False, opened_by="auto")

    if not res.get("ok"):
        return _fail(db, sig, f"Auto buy failed — {res.get('error', 'unknown')}")

    pos = res["position"]
    risked = round((pos["avg_price"] - pos["stop_loss"]) * pos["qty"], 2) if pos["stop_loss"] else 0.0
    audit(db, "AUTO_EXEC_ENTRY", sig.symbol,
          f"{lots} lot @ {pos['avg_price']} tgt={pos['target']} sl={pos['stop_loss']} "
          f"risking ₹{risked:,.0f} ({cfg.auto_risk_pct:.2f}% of budget)")
    STATE.hub.broadcast("alert", {
        "type": "AUTO_ENTRY",
        "message": (f"{sig.symbol} auto bought {lots} lot @ ₹{pos['avg_price']} "
                    f"· risking ₹{risked:,.0f}")})
    return res


def _fail(db: Session, sig: Signal, message: str) -> dict:
    """A real order attempt that did not result in a fill.

    Unlike a skip this IS the signal's outcome, so it is settled — and it raises
    an alert, because with nobody clicking Buy there is no one to see the error
    a manual attempt would have returned to the browser. Nothing retries it: a
    re-entry after the fact would be at a price the strategy never chose.
    """
    _settle(db, sig, "EXEC_FAILED", message)
    audit(db, "AUTO_EXEC_FAILED", sig.symbol, message, level="ERROR")
    STATE.hub.broadcast("alert", {
        "type": "AUTO_EXEC_FAILED",
        "message": f"{sig.symbol}: {message} — no position was opened. Review by hand."})
    return {"ok": False, "error": message}


def execute_async(signal_id: int) -> None:
    """Fire-and-forget execution on its own session, off the webhook thread.

    `execution.place()` blocks for up to five seconds per attempt polling the
    broker, and the Amibroker bridge posts signals in bursts — running this
    inline would stall the webhook behind every entry.
    """
    def run() -> None:
        with _lock:
            db = session()
            try:
                execute(db, signal_id)
            except Exception as exc:
                try:
                    audit(db, "AUTO_EXEC_ERROR", "", f"signal={signal_id}: {exc}", level="ERROR")
                    STATE.hub.broadcast("alert", {
                        "type": "AUTO_EXEC_ERROR",
                        "message": f"Automated execution crashed on signal {signal_id}: {exc}"})
                except Exception:
                    pass
            finally:
                db.close()

    threading.Thread(target=run, daemon=True).start()
