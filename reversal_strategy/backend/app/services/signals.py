"""Signal ingestion pipeline (Amibroker -> validated dashboard signal).

Amibroker is configured to send signals on CLOSED candles only (see
swing_scan.afl). That is verified rather than assumed: `candle_closed` records
whether the bar had actually finished when the signal arrived, so a scanner
reading a forming bar is visible in the data instead of passing as a real
entry. Checks:
  dedup -> market-hours/holiday -> blacklist -> averaging tag
Then enrich with LTP, lot size, expiry, ATR & resistance, and suggested Target/SL.
There is no post-SL cooldown.

A signal does NOT expire at the end of its day: the user may act on a previous
day's BUY at any time. `refresh()` therefore re-prices and re-gates stored
signals on every read — see its docstring.
"""
from __future__ import annotations

import threading
import time as _time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.market_hours import is_market_open
from app.core.state import STATE, audit
from app.core.timeutil import as_utc, iso_utc
from app.services import gateway_service as gw
from app.services import targets as target_svc
from db.engine import get_config
from db.models import Blacklist, Position, Signal


def _candle_id(symbol: str, timeframe: str, signal_time: datetime) -> str:
    if timeframe == "1H":
        bucket = signal_time.replace(minute=0, second=0, microsecond=0)
    elif timeframe == "4H":
        hour = (signal_time.hour // 4) * 4
        bucket = signal_time.replace(hour=hour, minute=0, second=0, microsecond=0)
    else:  # 1D
        bucket = signal_time.replace(hour=0, minute=0, second=0, microsecond=0)
    return f"{symbol}|{timeframe}|{bucket.isoformat()}"


_TF_MINUTES = {"1H": 60, "4H": 240, "1D": 1440}


def _candle_still_open(timeframe: str, signal_time: datetime) -> bool:
    """True when the signal's own candle has not finished yet.

    A rule evaluated on a forming bar can flip and flip back, so a signal from
    one is provisional — it may not exist by the time the candle closes. The
    scanner is supposed to emit only closed candles; this records whether it
    actually did, so a repainting setup shows up in the data instead of looking
    like a genuine entry.
    """
    minutes = _TF_MINUTES.get(timeframe, 60)
    closes_at = as_utc(signal_time) + timedelta(minutes=minutes)
    return closes_at > datetime.now(timezone.utc)


def ingest(db: Session, payload: dict) -> Signal | None:
    """Validate + persist one incoming Amibroker signal. Returns the stored row
    or None if it was dropped as a duplicate within the same candle."""
    cfg = get_config(db)
    symbol = payload["symbol"].strip().upper()
    signal_type = payload.get("signal_type", "BUY").upper()
    timeframe = payload.get("timeframe", "1H").upper()

    st = payload.get("signal_time")
    signal_time = datetime.fromisoformat(st) if st else datetime.now(timezone.utc)
    cid = _candle_id(symbol, timeframe, signal_time)

    # 1) Deduplication — same stock + same candle + same type already seen.
    if db.scalar(select(Signal).where(Signal.candle_id == cid, Signal.signal_type == signal_type)):
        return None

    ltp = STATE.prices.get(symbol) or gw.gateway().get_ltp(symbol) or float(payload.get("ltp", 0.0))
    atr = float(payload.get("atr", 0.0))
    resistance = float(payload.get("resistance", 0.0))
    support = float(payload.get("support", 0.0))
    closed = not _candle_still_open(timeframe, signal_time)
    sig = Signal(
        symbol=symbol, signal_type=signal_type, timeframe=timeframe,
        signal_time=signal_time, candle_id=cid, candle_closed=closed,
        ltp=ltp, signal_ltp=ltp, atr=atr, resistance=resistance, support=support,
        lot_size=gw.lot_size(symbol) or int(payload.get("lot_size", 1)),
        expiry=gw.expiry_of(symbol) or payload.get("expiry", ""),
        status="NEW",
    )

    # 2) Market hours / holiday -> STALE.
    if not is_market_open():
        sig.status = "STALE"
        sig.note = "Received outside market hours"

    # 3) Blacklist.
    if sig.status == "NEW" and db.scalar(select(Blacklist).where(Blacklist.symbol == symbol)):
        sig.status = "BLACKLISTED"
        sig.note = "Symbol blacklisted"

    # 4) Global halt (kill switch / global SL).
    if sig.status == "NEW" and cfg.signals_halted:
        sig.status = "STALE"
        sig.note = "Signal processing halted"

    # 5) Averaging tag — stock already held in the LIVE book (see refresh()).
    if sig.status == "NEW" and signal_type == "BUY":
        held = db.scalar(select(Position).where(
            Position.symbol == symbol, Position.status == "OPEN",
            Position.is_paper.is_(False)))
        if held is not None:
            sig.status = "AVERAGING"
            sig.note = _averaging_note(held, cfg)

    # 6) Target & SL for actionable BUY signals, per the GLOBAL config mode.
    #    SELL is an exit signal here — never a short entry (see paper.execute and
    #    the dashboard's Sell action). Long-only levels put a target *above* and a
    #    stop *below* the price, so attaching them to a SELL produced a "Profit @
    #    Target" that described a trade the system will never take.
    if sig.status in ("NEW", "AVERAGING") and ltp > 0 and signal_type == "BUY":
        tp = float(payload.get("target_points") or 0.0)
        sp = float(payload.get("sl_points") or 0.0)
        sig.target, sig.stop_loss = target_svc.signal_levels(
            ltp, cfg, atr=atr, resistance=resistance, support=support,
            target_points=tp, sl_points=sp
        )
        # 7) Reward:risk gate. Measured once, at the signal price, against the
        #    levels the trade would actually be taken on. Below the bar the
        #    signal is still stored and still re-priced on the board — it is
        #    just not actionable, so what was filtered and why stays visible
        #    instead of vanishing between Amibroker and the dashboard.
        sig.signal_rr = target_svc.reward_risk(ltp, sig.target, sig.stop_loss)
        if cfg.min_rr > 0 and sig.signal_rr < cfg.min_rr:
            sig.status = "LOW_RR"
            sig.note = f"R:R {sig.signal_rr:.2f} below minimum {cfg.min_rr:.2f}"

    db.add(sig)
    db.commit()
    db.refresh(sig)

    # Start streaming this symbol's live price so LTP/Target/SL stay current.
    if sig.status in ("NEW", "AVERAGING"):
        gw.ensure_subscribed(symbol)

    audit(db, "SIGNAL_RECEIVED", symbol,
          f"{signal_type} {timeframe} status={sig.status} ltp={ltp} "
          f"bar={signal_time:%H:%M}{'' if closed else ' INTRA-CANDLE(may repaint)'}",
          level="INFO" if closed else "WARN")
    STATE.hub.broadcast("signal", serialize(sig))
    _auto_paper(db, sig)
    _auto_execute(db, sig)
    return sig


def _auto_paper(db: Session, sig: Signal) -> None:
    """Hand an actionable signal to the paper trader when the mode is on.

    Imported lazily: paper -> positions -> signals, so a module-level import
    here would close the cycle. Execution is dispatched to its own thread so a
    slow broker quote never stalls the Amibroker webhook.
    """
    if sig.status not in ACTIONABLE:
        return
    if not get_config(db).paper_trading:
        return
    from app.services import paper
    paper.execute_async(sig.id)


def _auto_execute(db: Session, sig: Signal) -> None:
    """Hand an actionable signal to automated execution — the REAL book.

    Independent of the paper trader above rather than an else-branch of it: the
    two books are deliberately separate, so running both simply gives a live
    trade and its simulated twin. Lazily imported and dispatched off-thread for
    the same reasons paper is.
    """
    if sig.status not in ACTIONABLE:
        return
    from app.services import auto_exec
    if not auto_exec.armed(get_config(db))[0]:
        return
    auto_exec.execute_async(sig.id)


def manual_add(db: Session, symbol: str, signal_type: str = "BUY", timeframe: str = "1H",
               lot_size: int | None = None, expiry: str | None = None) -> Signal:
    """User searched a stock/fut/option and added it to the signals table even
    without an Amibroker signal. Action is user-chosen (BUY or SELL)."""
    symbol = symbol.strip().upper()
    signal_type = signal_type.upper() if signal_type.upper() in ("BUY", "SELL") else "BUY"
    cfg = get_config(db)
    ltp = STATE.prices.get(symbol) or gw.gateway().get_ltp(symbol)
    sig = Signal(
        symbol=symbol, signal_type=signal_type, timeframe=timeframe,
        signal_time=datetime.now(timezone.utc),
        candle_id=f"{symbol}|MANUAL|{datetime.now(timezone.utc).isoformat()}",
        candle_closed=True, ltp=ltp, signal_ltp=ltp,
        lot_size=lot_size or gw.lot_size(symbol) or 1,
        expiry=expiry or gw.expiry_of(symbol), status="NEW", manual_add=True, note="Manually added",
    )
    if db.scalar(select(Blacklist).where(Blacklist.symbol == symbol)):
        sig.status = "BLACKLISTED"; sig.note = "Symbol blacklisted"
    elif ltp > 0 and signal_type == "BUY":      # long-only levels — see ingest()
        sig.target, sig.stop_loss = target_svc.signal_levels(ltp, cfg)
    db.add(sig)
    db.commit()
    db.refresh(sig)
    gw.ensure_subscribed(symbol)
    audit(db, "SIGNAL_MANUAL_ADD", symbol, f"{timeframe} ltp={ltp}")
    STATE.hub.broadcast("signal", serialize(sig))
    _auto_paper(db, sig)
    # A hand-added signal is an actionable signal like any other, so automated
    # execution takes it too. Searching a symbol and adding it while Execution is
    # on Auto IS an instruction to trade it — the same way it already is for the
    # paper trader.
    _auto_execute(db, sig)
    return sig


ACTIONABLE = ("NEW", "AVERAGING")


def open_symbols(db: Session, *, live_only: bool = False) -> set[str]:
    """Symbols currently held. Both books by default.

    Which book a holding sits in says nothing about whether its exit signal is
    worth *reading*, so the dashboard asks for both. Automated execution asks
    for `live_only`, because it can only ever square off real money.
    """
    stmt = select(Position.symbol).where(Position.status == "OPEN")
    if live_only:
        stmt = stmt.where(Position.is_paper.is_(False))
    return set(db.scalars(stmt).all())


def is_essential(sig: Signal, held: set[str]) -> bool:
    """Is this signal something that can actually be acted on right now?

    The single definition of "Essential" — the dashboard's ⭐ filter and
    automated execution both read it, so the list the user is looking at is
    exactly the list the machine would trade. They were two separate predicates,
    one in SignalPanel.tsx and one implied by auto_exec's gates; they agreed, but
    nothing kept them agreeing, and "what will this thing buy" is not a question
    to answer from two places.

    A BUY that failed the reward:risk gate is not tradable. A SELL on a stock
    that is not held has nothing to close — this is a long-only strategy, so an
    exit signal for something never bought is noise.
    """
    if sig.signal_type == "SELL":
        return sig.symbol in held
    return sig.status != "LOW_RR"
# Terminal states — the signal has been decided, by the user or by the paper
# auto-trader. `refresh()` never re-gates or re-prices these, so the record of
# what happened stays exactly as it was at execution time.
_SETTLED = ("ACTED", "IGNORED", "EXECUTED", "AVERAGED", "SQUARED_OFF", "EXEC_FAILED")


def _averaging_note(held: Position, cfg) -> str:
    if held.averaging_count >= cfg.max_averaging_buys:
        return "Averaging limit reached"
    return f"Already held · averaging buy #{held.averaging_count + 1}"


def _gate(sig: Signal, cfg, held: Position | None, blacklisted: bool,
          market_open: bool) -> tuple[str, str]:
    """The status a stored signal deserves *right now* (status, note).

    Promotion only where time is the blocker: a signal parked as STALE because
    it arrived after hours (or during a halt) becomes tradable again once the
    market reopens. A NEW signal is never demoted at close — the user can still
    queue up on it.
    """
    if blacklisted:
        return "BLACKLISTED", "Symbol blacklisted"
    if cfg.signals_halted:
        return "STALE", "Signal processing halted"
    # Reward:risk, re-evaluated on every read rather than latched at ingest, so
    # changing the minimum releases signals already on the board instead of only
    # applying to the next one Amibroker sends. Judged on the R:R at the signal
    # price, which never moves — unlike STALE, no amount of waiting fixes it.
    if sig.signal_rr and cfg.min_rr > 0 and sig.signal_rr < cfg.min_rr:
        return "LOW_RR", f"R:R {sig.signal_rr:.2f} below minimum {cfg.min_rr:.2f}"
    if sig.status == "STALE" and not market_open:
        return sig.status, sig.note
    if sig.signal_type == "BUY" and held is not None:
        return "AVERAGING", _averaging_note(held, cfg)
    return "NEW", ("Manually added" if sig.manual_add else "")


_WARM_TTL = 300.0                       # re-subscribe a quiet symbol at most this often
_warmed: dict[str, float] = {}
_warm_lock = threading.Lock()


def warm_feed(symbols) -> None:
    """Subscribe symbols to the live price feed *off* the request path.

    Subscribing costs a scripmaster lookup and a websocket send against the
    broker gateway — one to two HTTP round-trips per symbol. Doing that inline
    while serving a signals read meant a slow gateway (a quote call currently
    takes ~9s when the feed is dry) turned a 47-row list into a 40s+ request
    that the dashboard's 5s poll never waited for. Reads now answer from stored
    state instantly and the feed catches up behind them.
    """
    now = _time.monotonic()
    with _warm_lock:
        fresh = [s for s in symbols if now - _warmed.get(s, 0.0) > _WARM_TTL]
        for s in fresh:
            _warmed[s] = now
    if not fresh:
        return

    def run() -> None:
        for s in fresh:
            gw.ensure_subscribed(s)     # already swallows gateway errors

    threading.Thread(target=run, daemon=True).start()


def refresh(db: Session, sigs: list[Signal]) -> list[Signal]:
    """Bring a batch of stored signals up to date with the live market.

    Signals outlive the day they fired — the user routinely buys a previous
    day's BUY — so a stored row must never be served with the price it carried
    when Amibroker sent it. On every read we:

      * re-price the signal off the current LTP and recompute Target / SL,
      * re-run the gates that were only momentarily true at ingest time
        (market closed, processing halted, blacklisted),
      * re-tag it AVERAGING when the stock has since entered the open book, so
        a repeat BUY on a stock the user already owns is offered as an
        averaging buy instead of a fresh entry.

    This is a hot read path — the dashboard polls it every 5s — so it does no
    network I/O at all. Prices come from the in-memory tick feed; symbols not
    ticking yet are handed to `warm_feed()` and keep their stored price until
    the feed delivers one.
    """
    if not sigs:
        return sigs
    cfg = get_config(db)
    symbols = {s.symbol for s in sigs}
    # Live book only. The AVERAGING tag drives the dashboard's Buy button, which
    # trades the real book — a simulated holding must not relabel it "Average"
    # when a click would in fact open a fresh live position.
    held = {p.symbol: p for p in db.scalars(
        select(Position).where(Position.symbol.in_(symbols), Position.status == "OPEN",
                               Position.is_paper.is_(False))
    ).all()}
    blacklisted = set(db.scalars(select(Blacklist.symbol).where(Blacklist.symbol.in_(symbols))).all())
    market_open = is_market_open()
    unpriced: list[str] = []
    dirty = False

    for sig in sigs:
        settled = sig.status in _SETTLED
        if not settled:
            status, note = _gate(sig, cfg, held.get(sig.symbol), sig.symbol in blacklisted,
                                 market_open)
            if (status, note) != (sig.status, sig.note):
                sig.status, sig.note = status, note
                dirty = True

        # LTP is live for EVERY row on the board, settled or not. A row the
        # paper trader has already acted on still needs to show what the stock
        # is doing now — freezing it left the whole executed half of the table
        # quoting its entry price with a Δ of exactly 0.00 forever. The frozen
        # record lives in signal_ltp / exec_price, which are never touched here.
        ltp = STATE.prices.get(sig.symbol) or 0.0
        if ltp <= 0:
            unpriced.append(sig.symbol)             # start it streaming again
            continue
        if abs(ltp - sig.ltp) >= 0.01:
            sig.ltp = ltp
            dirty = True

        # Target / SL are re-derived only while the signal is still actionable.
        # Once it is settled they are the levels the trade was taken on, and
        # re-pricing them would rewrite history under the user.
        # Anchored to the price the signal fired at, not the live price. The
        # levels are support and resistance — fixed places on the chart — so
        # re-deriving them from a moving LTP walked the target up the chart as
        # the stock rose, and the trade never reached a target that kept
        # retreating. The ATR fallback is entry-relative for the same reason.
        if not settled and sig.status in ACTIONABLE and sig.signal_type == "BUY":
            anchor = sig.signal_ltp or ltp
            target, sl = target_svc.signal_levels(
                anchor, cfg, atr=sig.atr, resistance=sig.resistance, support=sig.support)
            rr_at_signal = target_svc.reward_risk(anchor, target, sl)
            if (target, sl, rr_at_signal) != (sig.target, sig.stop_loss, sig.signal_rr):
                sig.target, sig.stop_loss, sig.signal_rr = target, sl, rr_at_signal
                dirty = True

    if dirty:
        db.commit()
    warm_feed(unpriced)
    return sigs


def serialize(sig: Signal, held: set[str] | None = None) -> dict:
    # Movement since the setup fired — the number that says whether a stored
    # signal is still near its trigger or has already run away from it.
    base = sig.signal_ltp or 0.0
    move_pct = round((sig.ltp / base - 1) * 100, 2) if base and sig.ltp else 0.0
    # Reward:risk on the *current* price, using the levels as displayed. Only
    # meaningful for BUY — a SELL carries no entry levels at all.
    reward = sig.target - sig.ltp
    risk = sig.ltp - sig.stop_loss
    rr = round(reward / risk, 2) if (
        sig.signal_type == "BUY" and risk > 0 and reward > 0) else 0.0
    return {
        "id": sig.id, "symbol": sig.symbol, "signal_type": sig.signal_type,
        "timeframe": sig.timeframe, "signal_time": iso_utc(sig.signal_time),
        "ltp": sig.ltp, "signal_ltp": sig.signal_ltp, "move_pct": move_pct, "rr": rr,
        "signal_rr": sig.signal_rr,
        "atr": sig.atr, "resistance": sig.resistance, "support": sig.support,
        "target": sig.target, "stop_loss": sig.stop_loss,
        "lot_size": sig.lot_size, "expiry": sig.expiry, "status": sig.status,
        "manual_add": sig.manual_add, "note": sig.note, "candle_closed": sig.candle_closed,
        # Only computed where the caller knows the open book — a single-row
        # broadcast has no cheap way to. null means "not evaluated here", and the
        # dashboard falls back to deciding locally rather than treating an
        # unknown as "not essential" and hiding a tradable signal.
        "essential": (None if held is None else is_essential(sig, held)),
        "paper": sig.paper, "auto": sig.auto, "position_id": sig.position_id,
        "executed_at": iso_utc(sig.executed_at),
        "exec_price": sig.exec_price, "exec_latency_ms": sig.exec_latency_ms,
        "created_at": iso_utc(sig.created_at),
    }
