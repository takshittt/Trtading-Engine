"""One-time repair for the pre-fix "opposite-side order created a new lot
instead of closing the existing one" bug (see app/routers/orders.py place_order).

Before that fix, a closing order placed via plain /api/orders (not through
POST /api/lots/{id}/exit) opened its own independent OrderLot. The FIFO
safety net (_offset_opposing_lots in app/services/reconciliation.py) later
netted the two opposite-side lots together, but it credits the FULL realized
PnL onto the OLDER lot and leaves the NEWER lot's realized_pnl at 0 — so one
real round-trip trade shows up in Order History as two rows: a correct one
with the PnL, and a phantom duplicate with ₹0.00.

This script finds exactly that signature and merges each pair back into a
single row:
  - within the same owner/exchange/symbol/product
  - opposite side
  - both status CLOSED
  - same entry_qty
  - closed within CLOSE_WINDOW_SECONDS of each other (both stamped by the
    same _offset_opposing_lots pass)
  - the newer lot's realized_pnl is exactly 0.0 (the tell-tale asymmetry)

For each matched pair, oldest lot is kept as the single surviving row:
  - closed_at is corrected to the newer lot's opened_at (the real fill time
    of the closing order, not the moment the offset routine happened to run)
  - a synthetic FILLED LotExit is attached so the Order History "Exit" column
    shows the real exit price instead of "—"
  - the phantom newer lot row is deleted (its own RolloverIntent references,
    if any, are removed first to satisfy the FK)

realized_pnl on the surviving lot is NOT recomputed — _offset_opposing_lots
already computed it correctly at the time; only the duplicate row and the
missing exit price are fixed here.

Dry-run by default — lists what would change. Pass --execute to commit.

Usage (from gateway_backend/):
    python -m scripts.repair_offset_split_lots            # dry run
    python -m scripts.repair_offset_split_lots --execute   # actually merge
"""
import argparse
import sys
import uuid
from collections import defaultdict
from datetime import timedelta

sys.path.insert(0, ".")

from db.engine import SessionLocal
from db.models import LotExit, OrderLot, RolloverIntent

CLOSE_WINDOW_SECONDS = 120


def _norm_prd(value) -> str:
    return (value or "").strip().upper() or "M"


def find_pairs(db):
    lots = (
        db.query(OrderLot)
        .filter(OrderLot.status == "CLOSED", OrderLot.closed_at.isnot(None))
        .order_by(OrderLot.opened_at.asc())
        .all()
    )

    groups: dict[tuple, list[OrderLot]] = defaultdict(list)
    for lot in lots:
        groups[(lot.owner_uid, lot.exch, lot.tsym, _norm_prd(lot.product_type))].append(lot)

    pairs = []
    for _key, grp in groups.items():
        used = set()
        for i, older in enumerate(grp):
            if older.id in used:
                continue
            for newer in grp[i + 1:]:
                if newer.id in used:
                    continue
                if newer.side == older.side:
                    continue
                if newer.entry_qty != older.entry_qty:
                    continue
                if newer.realized_pnl not in (0.0, None):
                    continue
                delta = abs((newer.closed_at - older.closed_at).total_seconds())
                if delta > CLOSE_WINDOW_SECONDS:
                    continue
                pairs.append((older, newer))
                used.add(older.id)
                used.add(newer.id)
                break
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Actually merge rows (default is dry-run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        pairs = find_pairs(db)
        if not pairs:
            print("No offset-split lot pairs found.")
            return

        print(f"Found {len(pairs)} pair(s) to merge:\n")
        for older, newer in pairs:
            print(
                f"  KEEP   id={older.id} {older.side} {older.tsym} qty={older.entry_qty} "
                f"entry={older.avg_entry_price} pnl={older.realized_pnl} "
                f"opened={older.opened_at} closed={older.closed_at}"
            )
            print(
                f"  DELETE id={newer.id} {newer.side} {newer.tsym} qty={newer.entry_qty} "
                f"entry={newer.avg_entry_price} pnl={newer.realized_pnl} "
                f"opened={newer.opened_at} closed={newer.closed_at}"
            )
            print(
                f"         -> exit price for id={older.id} becomes {newer.avg_entry_price} "
                f"(from the closing order's own fill), closed_at corrected to {newer.opened_at}\n"
            )

        if not args.execute:
            print("Dry run only — no rows changed. Re-run with --execute to merge.")
            return

        for older, newer in pairs:
            db.query(RolloverIntent).filter_by(lot_id=newer.id).delete(synchronize_session=False)

            exit_ref = f"repair{uuid.uuid4().hex[:10]}"
            db.add(LotExit(
                lot_id=older.id,
                exit_qty=older.entry_qty,
                filled_qty=older.entry_qty,
                avg_exit_price=newer.avg_entry_price,
                status="FILLED",
                client_ref=exit_ref,
                filled_at=newer.opened_at,
            ))
            older.closed_at = newer.opened_at

            db.delete(newer)  # cascades any lot_exits it may have

        db.commit()
        print(f"\nMerged {len(pairs)} pair(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
