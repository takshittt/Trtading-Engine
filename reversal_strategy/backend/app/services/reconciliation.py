"""Reconciliation Engine.

Every minute (while positions are open) and again at EOD, compares the strategy's
internal position book against the broker's actual positions/tradebook to remove
inconsistencies in trade, price and quantity. Broker is the source of truth: on a
mismatch we alert the user AND auto-correct internal state.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.state import STATE, audit
from app.services import gateway_service as gw
from app.services import positions as pos_svc
from db.models import Position

# Symbols already reported as belonging to something else on this broker account.
# The Shoonya login is shared with the Grid strategy, so its book shows up in
# every position fetch. Warning about it once is information; warning every
# reconcile cycle is a permanent alert that trains you to ignore the channel —
# and this is the one alert that must stay meaningful, because it is what would
# otherwise tell you about a genuinely unexpected fill.
_foreign_reported: set[str] = set()


def reconcile_once(db: Session, source: str = "MINUTE") -> dict:
    """Run one reconciliation pass. Returns a summary of mismatches found."""
    gateway = gw.gateway()
    try:
        broker_positions = {p["symbol"]: p for p in gateway.get_positions()}
    except Exception as exc:
        audit(db, "RECONCILE_ERROR", "", f"broker fetch failed: {exc}", level="ERROR")
        return {"ok": False, "error": str(exc)}

    # Paper positions are deliberately invisible to the broker, so reconciling
    # them against it would flag every one as a GHOST_OPEN and close it within
    # the minute. Only the real book is reconciled.
    internal = db.scalars(select(Position).where(
        Position.status == "OPEN", Position.is_paper.is_(False))).all()
    mismatches: list[dict] = []

    # An EMPTY broker book while we believe we hold positions is far more often a
    # parse or session failure than a genuinely flat account — get_positions()
    # swallows its own errors and returns [], and it silently did exactly that
    # for every call while it was reading the wrong key out of the payload.
    # Acting on it closes the entire book in one pass, and a position closed here
    # stops being managed by the exit engine while the broker still holds it.
    # Refusing to reconcile is the recoverable failure; sweeping is not.
    if internal and not broker_positions:
        audit(db, "RECONCILE_ABORTED", "",
              f"[{source}] broker returned an empty position book while {len(internal)} "
              f"position(s) are open internally — treating as a broker/parse failure "
              f"and leaving internal state alone. Verify the book by hand.",
              level="ERROR")
        STATE.hub.broadcast("alert", {
            "type": "RECONCILE_ABORTED",
            "message": "Broker returned no positions while the system holds some — "
                       "reconciliation skipped. Check the broker session."})
        return {"ok": False, "error": "empty broker book with open internal positions"}

    # 1) Positions the system thinks are open.
    for pos in internal:
        bp = broker_positions.get(pos.symbol)
        if bp is None or bp["qty"] == 0:
            # The exit price here is the last LTP we happened to see, NOT what
            # the broker actually got — that fill happened outside this system
            # and there is no order row for it, so the journal's leg view will
            # show an entry with no matching exit. Realized P&L on this position
            # is therefore an estimate, and it is labelled as one rather than
            # being presented alongside genuinely reconciled numbers.
            est_price = STATE.prices.get(pos.symbol, pos.ltp)
            est_pnl = round((est_price - pos.avg_price) * pos.qty, 2)
            mismatches.append({
                "symbol": pos.symbol, "type": "GHOST_OPEN",
                "detail": (f"System shows OPEN but broker shows FLAT — closing internally at "
                           f"last known {est_price} (ESTIMATE, no broker fill recorded). "
                           f"Verify the real exit price in the broker's tradebook."),
            })
            pos.status = "CLOSED"
            pos.exit_reason = "RECON_ESTIMATED"
            pos.exit_price = est_price
            pos.realized_pnl += est_pnl
            pos.exit_qty += pos.qty
            pos.qty = 0
            pos.closed_at = datetime.now(timezone.utc)
            pos.margin_used = 0.0
            continue
        if bp["qty"] != pos.qty:
            mismatches.append({
                "symbol": pos.symbol, "type": "QTY_MISMATCH",
                "detail": f"system_qty={pos.qty} broker_qty={bp['qty']} — syncing to broker.",
            })
            pos.qty = bp["qty"]
            pos.lots = max(1, bp["qty"] // (pos.lot_size or 1))
        if bp.get("avg_price") and abs(bp["avg_price"] - pos.avg_price) > 0.05:
            mismatches.append({
                "symbol": pos.symbol, "type": "PRICE_MISMATCH",
                "detail": f"system_avg={pos.avg_price} broker_avg={bp['avg_price']} — syncing to broker.",
            })
            pos.avg_price = bp["avg_price"]

    # 2) Positions the broker has that this strategy did not open.
    #
    # This strategy owns only what IT executed. The Shoonya account is shared —
    # another bot trades on it and the user trades manually on it — so anything
    # not in our own Position table is somebody else's and is NOT an anomaly.
    #
    # This used to be scoped by exchange, which only ever caught the other
    # system's MCX trades. The other bot trades NFO, the same exchange as this
    # one, so every one of its ~16 futures positions fell through and re-alerted
    # once a minute forever — which is precisely how a real unexpected fill ends
    # up unnoticed among the noise.
    #
    # So ownership, not exchange, is the test now. Each unrecognised symbol is
    # recorded ONCE per process: silence would be its own trap (a position we
    # genuinely lost track of would vanish from view), but a recurring WARN for
    # normal activity on a shared account is worse than useless.
    internal_symbols = {p.symbol for p in internal}
    foreign = 0
    for sym, bp in broker_positions.items():
        if bp["qty"] == 0 or sym in internal_symbols:
            continue
        foreign += 1
        if sym not in _foreign_reported:
            _foreign_reported.add(sym)
            exch = (bp.get("exchange") or "").upper() or "?"
            audit(db, "FOREIGN_BROKER_POSITION", sym,
                  f"Broker holds {bp['qty']} of {sym} on {exch}, not opened by this "
                  f"strategy — another system or a manual trade on the same "
                  f"account. Ignored by reconciliation.")

    db.commit()
    STATE.last_reconcile = datetime.now(timezone.utc).isoformat()

    if mismatches:
        for m in mismatches:
            audit(db, "RECONCILE_MISMATCH", m["symbol"], f"[{source}] {m['type']}: {m['detail']}",
                  level="WARN")
        STATE.hub.broadcast("alert", {
            "type": "RECONCILE",
            "message": f"{len(mismatches)} inconsistency(ies) found & corrected ({source}).",
            "mismatches": mismatches,
        })
        for pos in internal:
            STATE.hub.broadcast("position", pos_svc.serialize(pos))
    else:
        audit(db, "RECONCILE_OK", "", f"[{source}] internal state matches broker.")

    # `foreign` is surfaced rather than dropped: ignoring another system's
    # positions silently is its own trap, so the count travels with every pass.
    STATE.hub.broadcast("reconcile", {
        "at": STATE.last_reconcile, "source": source, "mismatches": len(mismatches),
        "foreign": foreign,
    })
    return {"ok": True, "mismatches": mismatches, "foreign": foreign,
            "at": STATE.last_reconcile}
