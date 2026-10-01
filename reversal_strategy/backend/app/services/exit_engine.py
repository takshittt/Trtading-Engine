"""Automated Exit Engine — Phase-1 automation, runs on every price tick.

  - SL hit (before target)  -> auto square-off completely. No cooldown.
  - Target hit              -> DO NOT sell. Activate a Trailing Stop:
        trailing_sl = peak × (1 − buffer%);  peak trails up with price.
        Exit only when price falls back to the trailing_sl.
  - Global MTM loss beyond threshold -> square off ALL + halt signals.

Only exits are automated; entries stay manual.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone

from sqlalchemy import select

from app.core import symlock
from app.core.state import STATE, audit
from app.core.market_hours import is_ltp_valid
from app.core.timeutil import as_utc
from app.services import positions as pos_svc
from db.engine import get_config, session
from db.models import Position

_log = logging.getLogger(__name__)

# The portfolio stop reads the whole book, so it needs its own lock rather than
# a per-symbol one — but it must never make a symbol wait, hence non-blocking.
_global_lock = threading.Lock()
_global_last_run = 0.0
GLOBAL_SL_INTERVAL_SEC = 2.0

# Consecutive failures per symbol. A tick handler that swallows its exception
# fails identically on every subsequent tick, so the position never exits and
# nothing anywhere says so. Counting them is what turns a permanent silent
# failure into something the dashboard can shout about.
_fail_counts: dict[str, int] = {}
_FAIL_ALERT_AT = 3


def on_tick(symbol: str, ltp: float) -> None:
    # Reject ticks outside NSE F&O hours (09:15–15:40 Mon–Fri).
    # Saturday mock-trading sessions push fake prices that would corrupt PnL.
    if not is_ltp_valid():
        return
    STATE.set_price(symbol, ltp)

    # Per-symbol, not process-wide. execution.place() polls the broker for up to
    # five seconds while holding this, and under a single global lock that
    # blocked every other symbol's ticks — a stop breach elsewhere went unseen
    # for the whole window. Now only repeat ticks for THIS symbol are skipped,
    # which is the case where skipping is right.
    with symlock.try_hold(symbol) as got:
        if not got:
            return
        db = session()
        try:
            _check_symbol(db, symbol, ltp)
            _fail_counts.pop(symbol, None)
        except Exception as exc:
            _note_failure(db, symbol, exc)
        finally:
            db.close()

    _maybe_check_global(symbol)


def _note_failure(db, symbol: str, exc: Exception) -> None:
    """Record a tick-handler failure loudly enough to be found.

    The old handler wrote one audit row and swallowed even that if the write
    itself failed — so a broken DB session took the exit engine down in total
    silence, and every later tick died at the same line. The stack goes to the
    log (journalctl) where it survives a dead DB, and repeated failures raise a
    dashboard alert, because "this position can no longer exit" is not something
    to leave sitting in a table nobody is reading.
    """
    n = _fail_counts.get(symbol, 0) + 1
    _fail_counts[symbol] = n
    _log.error("exit_engine: tick failed for %s (consecutive=%d): %s\n%s",
               symbol, n, exc, traceback.format_exc())
    try:
        audit(db, "EXIT_ENGINE_ERROR", symbol,
              f"tick handler failed (consecutive={n}): {exc}", level="ERROR")
    except Exception:
        db.rollback()          # a poisoned session must not block the alert below
    if n == _FAIL_ALERT_AT:
        try:
            STATE.hub.broadcast("alert", {
                "type": "EXIT_ENGINE_DOWN",
                "message": f"Exit engine has failed {n} ticks in a row on {symbol} — "
                           f"stops are NOT being monitored for it. Check the logs.",
            })
        except Exception:
            pass


def _maybe_check_global(symbol: str) -> None:
    """Portfolio stop, outside the symbol lock and throttled.

    Runs after the per-symbol pass rather than inside it so that a symbol
    holding its own lock cannot stop the portfolio check from ever running.
    Throttled because it reads the whole open book and ticks arrive far faster
    than the book changes.
    """
    global _global_last_run
    now = time.monotonic()
    if now - _global_last_run < GLOBAL_SL_INTERVAL_SEC:
        return
    if not _global_lock.acquire(blocking=False):
        return
    db = session()
    try:
        _global_last_run = time.monotonic()
        _check_global_sl(db)
    except Exception as exc:
        _note_failure(db, symbol, exc)
    finally:
        db.close()
        _global_lock.release()


def _hold_for_manual(db, pos: Position, ltp: float, stop: float, kind: str) -> None:
    """Flag a stop breach for the user instead of squaring off.

    Manual exit mode does not disarm the stop — it moves the decision. The
    position is marked so the dashboard can surface it, and nothing is sold.
    """
    if not pos.sl_breached:
        pos.sl_breached = True
        pos.sl_breached_at = datetime.now(timezone.utc)
        audit(db, "SL_HIT_HELD", pos.symbol,
              f"{kind} breached: ltp={ltp} <= {stop} — manual exit mode, awaiting user",
              level="WARN")
    db.commit()
    STATE.hub.broadcast("position", pos_svc.serialize(pos))


def _clear_breach(pos: Position) -> bool:
    """Price is back above the stop — drop the flag. True when it changed."""
    if not pos.sl_breached:
        return False
    pos.sl_breached = False
    pos.sl_breached_at = None
    return True


def sweep_manual_holds(db) -> int:
    """Act on everything manual mode held back. Returns how many were exited.

    Called when exit_mode flips back to auto. A flagged position still under
    water is squared off at once rather than waiting for the next tick — which
    may never come if the market has closed. One whose price recovered, or whose
    stop the user moved, simply loses the flag.
    """
    exited = 0
    rows = db.scalars(select(Position).where(
        Position.status == "OPEN", Position.sl_breached.is_(True))).all()
    # Locked per position rather than once around the whole sweep: holding one
    # lock across every symbol would reintroduce exactly the stall this engine
    # was just fixed for, since each exit_full() can block on the broker for
    # seconds. Each position is also wrapped individually so that one broker
    # failure cannot abandon the positions after it in the list.
    for pos in rows:
        with symlock.hold(pos.symbol):
            try:
                ltp = STATE.prices.get(pos.symbol) or pos.ltp
                trailing = bool(pos.trailing_active)
                stop = pos.trailing_sl if trailing else pos.stop_loss
                if stop and ltp and ltp <= stop:
                    audit(db, "SL_HIT", pos.symbol,
                          f"auto exit resumed — ltp={ltp} <= {stop}", level="WARN")
                    res = pos_svc.exit_full(db, pos.id, reason="TRAIL" if trailing else "SL")
                    if res.get("ok"):
                        exited += 1
                    else:
                        _record_exit_failure(db, pos)
                else:
                    _clear_breach(pos)
                    db.commit()
            except Exception as exc:
                db.rollback()
                _note_failure(db, pos.symbol, exc)
    return exited


def _check_symbol(db, symbol: str, ltp: float) -> None:
    cfg = get_config(db)
    manual_exit = cfg.exit_mode == "manual"
    buffer = cfg.trailing_buffer_pct / 100.0
    positions = db.scalars(
        select(Position).where(Position.symbol == symbol, Position.status == "OPEN")
    ).all()
    for pos in positions:
        pos.ltp = ltp
        # Excursion envelope — the best and worst this trade ever saw. Recorded
        # on every tick because it cannot be reconstructed after the exit, and
        # it is what tells you whether a target was unreachable or a stop was
        # merely grazed before the move worked.
        pos.peak_price = max(pos.peak_price or ltp, ltp)
        pos.trough_price = min(pos.trough_price or ltp, ltp)

        if pos.trailing_active:
            # trail the stop upward with new highs; exit if price falls to it
            if ltp > pos.trail_peak:
                pos.trail_peak = ltp
                pos.trailing_sl = round(ltp * (1 - buffer), 2)
            if ltp <= pos.trailing_sl:
                # Trailing SL ALWAYS auto-exits regardless of manual mode.
                # Only normal SL is subject to manual hold.
                # continue, not return: this loop covers every position on the
                # symbol, and paper and live are separate rows. Returning let one
                # book's cooldown stop the other book from ever being checked.
                if _is_cooling_down(pos):
                    continue
                audit(db, "TRAILING_SL_HIT", symbol,
                      f"ltp={ltp} <= trail_sl={pos.trailing_sl} (peak {pos.trail_peak})")
                res = pos_svc.exit_full(db, pos.id, reason="TRAIL")
                if not res.get("ok"):
                    _record_exit_failure(db, pos)
                continue
            _clear_breach(pos)          # trailed back above the stop
            db.commit()
            STATE.hub.broadcast("position", pos_svc.serialize(pos))
            continue

        if pos.target and ltp >= pos.target:
            # activate trailing phase — do NOT sell yet
            pos.target_hit = True
            pos.trailing_active = True
            pos.trail_peak = ltp
            pos.trailing_sl = round(max(pos.target, ltp) * (1 - buffer), 2)
            audit(db, "TARGET_HIT", symbol,
                  f"ltp={ltp} >= target={pos.target} — trailing SL armed @ {pos.trailing_sl}")
            db.commit()
            STATE.hub.broadcast("position", pos_svc.serialize(pos))
        elif pos.stop_loss and ltp <= pos.stop_loss:
            if manual_exit:
                _hold_for_manual(db, pos, ltp, pos.stop_loss, "SL")
                continue
            if _is_cooling_down(pos):
                continue
            audit(db, "SL_HIT", symbol, f"ltp={ltp} <= sl={pos.stop_loss}", level="WARN")
            res = pos_svc.exit_full(db, pos.id, reason="SL")
            if not res.get("ok"):
                _record_exit_failure(db, pos)
        else:
            # Above the stop and below the target: a held breach has recovered,
            # either because price came back or because the user moved the stop.
            _clear_breach(pos)
            db.commit()
            STATE.hub.broadcast("position", pos_svc.serialize(pos))


EXIT_COOLDOWN_SEC = 10.0


def _is_cooling_down(pos: Position) -> bool:
    """Is this position still inside the pause after a failed exit attempt?

    as_utc, not a raw subtraction: SQLite hands the stored timestamp back naive,
    so subtracting it from an aware now() raised TypeError on the very next tick.
    on_tick swallows that, which made it worse than a crash — every later tick
    for the symbol died at the same line, so the position could never exit and
    _check_global_sl stopped running for that symbol entirely. The failure was
    permanent and it started the moment an exit failed.
    """
    if not pos.exit_failed_at:
        return False
    elapsed = datetime.now(timezone.utc) - as_utc(pos.exit_failed_at)
    return elapsed.total_seconds() < EXIT_COOLDOWN_SEC


def _record_exit_failure(db, pos: Position) -> None:
    pos.exit_failed_at = datetime.now(timezone.utc)
    pos.exit_retries += 1
    db.commit()


def _check_global_sl(db) -> None:
    cfg = get_config(db)
    if cfg.signals_halted:
        return
    # Live book only: the portfolio stop squares off real money and halts all
    # signal processing, so a simulated drawdown must never be able to fire it.
    open_positions = db.scalars(select(Position).where(
        Position.status == "OPEN", Position.is_paper.is_(False))).all()

    unrealized = 0.0
    for p in open_positions:
        ltp = STATE.prices.get(p.symbol, p.ltp)
        unrealized += (ltp - p.avg_price) * p.qty

    # Today's BOOKED loss counts too. Measuring only open MTM meant a stop-out
    # removed its own loss from the portfolio stop's view the instant it closed:
    # the position left the open book and its loss moved to realized_pnl, which
    # nothing here was reading. A day that bled out through four separate
    # stop-outs could therefore never trip a 5% portfolio stop, because at no
    # single moment was 5% sitting in open positions. That is exactly the day
    # the stop exists for.
    # One shared definition of "today", in journal.realized_today — the
    # daily P&L the user reads and this circuit breaker must never be able
    # to disagree about where the day starts.
    from app.services import journal as journal_svc
    realized_today = journal_svc.realized_today(db, paper=False)
    drawdown = unrealized + realized_today

    # Still return early only when there is genuinely nothing to act on. A book
    # that is flat but deeply down on the day must be able to halt signals.
    if not open_positions and realized_today >= 0:
        return

    threshold = -cfg.total_budget * cfg.global_sl_pct / 100.0
    if drawdown <= threshold:
        audit(db, "GLOBAL_SL_TRIGGERED", "",
              f"drawdown={drawdown:.0f} (open {unrealized:.0f} + booked today "
              f"{realized_today:.0f}) <= threshold={threshold:.0f} — squaring off all",
              level="ERROR")
        for p in open_positions:
            pos_svc.exit_full(db, p.id, reason="GLOBAL_SL")
        cfg.signals_halted = True
        db.commit()
        STATE.hub.broadcast("alert", {
            "type": "GLOBAL_SL",
            "message": (f"Global strategy SL hit — drawdown {drawdown:,.0f} "
                        f"(open {unrealized:,.0f} + booked today {realized_today:,.0f}). "
                        f"All positions squared off, signals halted."),
        })
