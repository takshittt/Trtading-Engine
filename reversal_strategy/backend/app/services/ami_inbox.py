"""Database-delivered Amibroker signals: poll `ami_signal_inbox` and ingest.

The Amibroker box INSERTs raw scan rows into `ami_signal_inbox` and never talks
to this process. This poller drains that table and feeds each row through the
same `signals.ingest()` the webhook uses, so both delivery paths produce
identical `signals` rows.

Enabled with SWING_SIGNAL_SOURCE=db. The webhook stays mounted either way —
running both is harmless, because ingest dedups on candle_id.

Why the queue is not the `signals` table itself: `signals` rows carry
candle_id, dedup state, target/stop, status and execution history, all computed
by ingest. A writer INSERTing there directly would invent those fields, skip
dedup, and never trigger auto-execute — the row would land and nothing would
happen. The raw table keeps the transport dumb and the processing in one place.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import SIGNAL_SOURCE
from app.core.state import STATE, audit
from app.services import signals as signal_svc
from app.services.ami_payload import tf_label, to_our_payload
from db.engine import IS_SQLITE, session
from db.models import AmiSignalInbox

POLL_SECONDS = float(os.getenv("SWING_INBOX_POLL_SEC", "5"))

# Rows per pass. Bounded so one backlogged batch (a bridge that was offline over
# a weekend) can't hold a single transaction open across thousands of ingests,
# each of which may fire an auto-execute.
BATCH = 200


def _claim(db):
    """Oldest unprocessed rows, locked so a second reader can't take them too.

    SKIP LOCKED means two backends pointed at the same database each get a
    disjoint batch instead of both processing the same row and racing on
    ingest's dedup. SQLite has no row locks and no second writer, so the plain
    query is correct there.
    """
    q = (select(AmiSignalInbox)
         .where(AmiSignalInbox.processed == False)   # noqa: E712 — SQL, not Python
         .order_by(AmiSignalInbox.id)
         .limit(BATCH))
    if not IS_SQLITE:
        q = q.with_for_update(skip_locked=True)
    return db.scalars(q).all()


def _process_row(db, row: AmiSignalInbox) -> None:
    """Ingest one inbox row. Always marks it processed — a row that cannot be
    ingested must not be retried forever, or a single malformed entry stalls
    every signal behind it."""
    row.processed = True
    row.processed_at = datetime.now(timezone.utc)

    body = {
        "symbol": row.symbol,
        "action": row.action,
        "timeframe_min": row.timeframe_min,
        "price": row.price,
        "atr": row.atr,
        "resistance": row.resistance,
        "support": row.support,
        "bar_time": row.bar_time,
    }
    # Only forward explicit levels when the scan actually supplied them; a
    # permanent 0 would read as "target is zero" rather than "not supplied".
    if row.target_points:
        body["target_points"] = row.target_points
    if row.sl_points:
        body["sl_points"] = row.sl_points

    if not str(row.symbol or "").strip():
        row.result = "missing symbol"
        STATE.ami.invalid += 1
        STATE.ami.last_error = row.result
        return

    if tf_label(body) is None:
        row.result = f"unsupported timeframe {row.timeframe_min}"
        STATE.ami.invalid += 1
        STATE.ami.last_error = row.result
        return

    payload = to_our_payload(body)
    STATE.ami.last_symbol = payload["symbol"]
    STATE.ami.last_bar_time = payload["signal_time"]

    sig = signal_svc.ingest(db, payload)
    if sig is None:
        # Same stock, same candle, same direction as one already stored. Normal
        # when the bridge replays a batch it could not confirm.
        row.result = "duplicate"
        STATE.ami.duplicate += 1
        return

    row.result = ""
    STATE.ami.accepted += 1
    STATE.ami.last_accepted_at = datetime.now(timezone.utc).isoformat()


def drain_once() -> int:
    """One polling pass. Returns how many rows were consumed."""
    db = session()
    try:
        rows = _claim(db)
        if not rows:
            return 0
        STATE.ami.stamp()
        if not STATE.amibroker_connected:
            STATE.amibroker_connected = True
            STATE.hub.broadcast("connection", {"amibroker": True})
        for row in rows:
            try:
                _process_row(db, row)
            except Exception as e:
                # Per-row isolation: one bad row is marked and skipped rather
                # than aborting the batch and blocking everything behind it.
                row.processed = True
                row.processed_at = datetime.now(timezone.utc)
                row.result = f"error: {e}"[:120]
                STATE.ami.invalid += 1
                STATE.ami.last_error = row.result
        db.commit()
        return len(rows)
    finally:
        db.close()


def _loop() -> None:
    while True:
        try:
            drain_once()
        except Exception as e:
            # The DB being briefly unreachable must not kill the only path
            # signals arrive by. Log once and keep polling.
            try:
                db = session()
                audit(db, "INBOX_ERROR", "", f"poll failed: {e}")
                db.commit()
                db.close()
            except Exception:
                pass
        time.sleep(POLL_SECONDS)


def start() -> None:
    """Start the poller if SWING_SIGNAL_SOURCE=db, otherwise do nothing."""
    if SIGNAL_SOURCE != "db":
        return
    threading.Thread(target=_loop, daemon=True).start()
