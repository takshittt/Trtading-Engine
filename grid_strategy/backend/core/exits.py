"""Per-lot target / stop-loss math.

The ladder rule from the spec (image 2):

  TARGET  — chained to the rung ABOVE. With buys b1, b2, b3 (each lower than
            the last) and target offset X:
                target(b1) = entry(b1) + X          (no rung above — anchor self)
                target(b2) = entry(b1) + X
                target(b3) = entry(b2) + X
            i.e. a rung exits when price climbs back X beyond the PREVIOUS
            buy's entry — the colour-coded "T3 = target for B2 (above previous
            buy)" in the sketch.

  STOP    — each lot protects its own entry:
                sl(bN) = entry(bN) − X

  X is configurable per instrument, in absolute points or percent.
  In percent mode, X is computed on the anchor price.

Targets/stops are FIXED at entry time from ACTUAL fill prices (post-execution
math), and later shifted only by rollover basis so their economics never drift.
"""

from db.models import Instrument, Lot


def offset_of(mode: str, value: float, anchor_price: float) -> float:
    if mode == "percent":
        return anchor_price * (value / 100.0)
    return value


def compute_target(inst: Instrument, entry_price: float,
                   prev_open_entry: float | None) -> tuple[float, float]:
    """Returns (target_price, anchor_price_used).

      FIRST rung (no rung above): target = own entry + FULL offset.
      CHAINED rung (a rung above exists): target = previous rung's entry +
          (target_chain_pct% × offset). A lower rung can therefore aim for a
          smaller pop above the previous buy instead of the whole offset.

    target_chain_pct defaults to 100 (→ previous behaviour: full offset chained),
    so existing instruments are unchanged until the field is set.
    """
    anchor = prev_open_entry if prev_open_entry is not None else entry_price
    offset = offset_of(inst.target_mode, inst.target_value, anchor)
    if prev_open_entry is not None:
        pct = getattr(inst, "target_chain_pct", None)
        pct = 100.0 if pct is None else float(pct)
        offset = offset * (pct / 100.0)
    tgt = anchor + offset
    # SAFETY CLAMP: a chained target must sit ABOVE this rung's own entry, or the
    # rung would "hit target" instantly for ~zero P&L (possible when the anchor
    # rung sits below this entry — e.g. a recycled level refiring above still-open
    # lower rungs). Fall back to the self-anchored full offset in that case.
    if prev_open_entry is not None and tgt <= entry_price:
        anchor = entry_price
        tgt = entry_price + offset_of(inst.target_mode, inst.target_value, entry_price)
    return round(tgt, 4), anchor


def compute_sl(inst: Instrument, entry_price: float) -> float:
    """sl_price = 0 means "no stop" everywhere in the engine — when the user
    explicitly disabled stop-losses for this instrument, return exactly that."""
    if getattr(inst, "sl_enabled", False) is False:
        return 0.0
    sl = entry_price - offset_of(inst.sl_mode, inst.sl_value, entry_price)
    return round(sl, 4)


def exit_edit_error(*, target: float | None, sl: float | None,
                    entry: float, ltp: float, live: bool,
                    tick: float = 0.05, ref: str = "the entry"
                    ) -> tuple[str, list[str]]:
    """(refusal, warnings) for a HAND-SET absolute target/stop price.

    compute_target/compute_sl derive exits from the instrument's OFFSETS, and
    Engine._exit_config_error guards those before a rung is ever opened. A price
    the operator types bypasses both: it is written straight onto the lot and
    the tick loop acts on it on the very next tick. This is that missing guard.

    The distinction that matters is whether the rung HOLDS UNITS yet:

    live=True   the rung is OPEN. A stop is armed on `price <= sl`, so a stop at
                or above the LAST TRADED PRICE fires a market sell immediately —
                refused. It is measured against LTP and not against the entry on
                purpose: a stop above entry but below the market is a trailing
                stop on a winner, which is ordinary risk management and must
                stay allowed.
    live=False  the buy has not filled (RESTING/UNCONFIRMED rung, ladder level).
                The edit is a plan applied at fill time, so it is judged against
                the price it will fill at; LTP is irrelevant, because a buy
                resting far below the market is the normal case for a dip ladder.

    Warnings are for choices that are legal but probably not what was meant.
    They never block — an operator who wants to exit into the book is entitled to.
    """
    warns: list[str] = []

    if target is not None:
        if target <= 0:
            return (f"a target of {target:g} switches the target exit OFF for this rung — it would then "
                    f"only ever leave on its stop. Set a price above {ref} {entry:g}."), warns
        if live:
            if target <= entry:
                warns.append(f"target {target:g} is at/below {ref} {entry:g} — this rung will be closed "
                             f"for a LOSS of about {entry - target:g} points a unit.")
            elif ltp > 0 and target <= ltp:
                warns.append(f"target {target:g} is at/below the last price {ltp:g} — this rung exits as "
                             f"soon as the sell fills; it is not a resting target.")
        elif target <= entry:
            return (f"target {target:g} is at/below {ref} {entry:g} — the rung would round-trip "
                    f"instantly for a loss of {entry - target:g} points."), warns

    if sl is not None:
        if sl < 0:
            return f"stop-loss {sl:g} is negative.", warns
        if sl == 0:
            warns.append("stop cleared — this rung now has NO stop; the exit gate reads sl_price <= 0 "
                         "as 'no stop' and these units run unprotected until you set one.")
        elif live:
            if ltp <= 0:
                warns.append(f"no live tick for this contract, so stop {sl:g} could not be checked against "
                             "the market — it fires on the first tick at/below it.")
            elif sl >= ltp:
                return (f"stop-loss {sl:g} is at/above the last price {ltp:g} — a stop is armed on "
                        f"price <= stop, so this fires a MARKET sell on the very next tick. Set it below "
                        f"{ltp:g}, or use Exit if you want to sell now."), warns
            elif sl >= ltp - max(tick, 0.0):
                warns.append(f"stop {sl:g} is within one tick of the last price {ltp:g} — expect it to "
                             "fire almost immediately.")
        elif sl >= entry:
            return (f"stop-loss {sl:g} is at/above {ref} {entry:g} — the rung would be stopped out at "
                    f"market the moment the buy fills."), warns

    return "", warns


def level_override_error(price: float, target_override: float | None,
                         sl_override: float | None) -> str:
    """Ladder-level wrapper. On those routes a value <= 0 means "clear back to
    auto", so it is normalised to None before being judged. A level has bought
    nothing yet, hence live=False."""
    t = target_override if (target_override or 0) > 0 else None
    s = sl_override if (sl_override or 0) > 0 else None
    err, _ = exit_edit_error(target=t, sl=s, entry=price, ltp=0.0, live=False,
                             ref="the level price")
    return err


def lot_pnl(entry_price: float, current_price: float, qty_units: int) -> float:
    """₹ P&L of a long lot of qty_units at current_price."""
    return (current_price - entry_price) * qty_units


def open_lot_pnl(lot: Lot, price: float) -> float:
    """₹ P&L of one live rung marked at `price`, prior realized P&L included.

    An OPEN rung marks against `price`. A paused TEMP_EXITED rung is FLAT at the
    broker, so its P&L is FROZEN at the temp-exit fill and `price` is ignored —
    on re-enter the basis shift makes live P&L resume from exactly this number.
    Returns 0.0 when the mark is unusable (price <= 0, or a temp-exited rung with
    no recorded fill) so a caller never books a fake full-loss against a 0 price.

    This is the single source of truth for open-rung P&L: the circuit-breaker
    valuation, the dashboard snapshot and the per-lot API row all go through it.
    """
    realized = lot.realized_pnl or 0.0
    if lot.status == "TEMP_EXITED":
        if lot.temp_exit_price and lot.temp_exit_price > 0:
            return (lot.temp_exit_price - lot.entry_price) * lot.qty + realized
        return 0.0
    if price and price > 0:
        return lot_pnl(lot.entry_price, price, lot.qty) + realized
    return 0.0
