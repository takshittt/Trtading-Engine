"""DB-backed structured event log with a live push hook for the UI.

Every decision the engine makes — signal received, each math step, execution,
skips and their reasons — lands here with an IST timestamp. Retention is one
month; `purge_old()` runs daily from the engine.

Persistence runs on a BACKGROUND WRITER THREAD: `write()` only enqueues (a
non-blocking, microsecond operation) and returns immediately. This matters
because the engine is fully async and SQLite allows a single writer with a
long busy-timeout — doing the commit inline would block the event loop (ticks,
exits, heartbeat) for as long as the write is contended, precisely during the
volatile moments when logging is heaviest. The UI broadcast still happens
inline on the event loop (it needs the running loop) and never touches the DB.
"""

import asyncio
import logging
import queue
import threading
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from db.engine import db_session
from db.models import LogEntry
from config.settings import settings

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("grid.events")

_QUEUE_MAX = 10000          # cap so a stuck writer can't grow memory without bound


class EventLog:
    def __init__(self):
        self.on_event: Optional[Callable[[dict], Awaitable[None]]] = None  # UI broadcast hook
        self._q: "queue.Queue[dict | None]" = queue.Queue(maxsize=_QUEUE_MAX)
        self._worker: Optional[threading.Thread] = None
        self._dropped = 0

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._drain, name="event-log-writer", daemon=True)
            self._worker.start()

    def _drain(self) -> None:
        """Background thread: persist queued rows, batching whatever is ready."""
        while True:
            item = self._q.get()
            if item is None:      # shutdown sentinel
                return
            batch = [item]
            # opportunistically coalesce a burst into one transaction
            while len(batch) < 200:
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    batch.append(None)
                    break
                batch.append(nxt)
            rows = [r for r in batch if r is not None]
            if rows:
                try:
                    with db_session() as db:
                        db.add_all([LogEntry(**r) for r in rows])
                        db.commit()
                except Exception:
                    logger.exception("event log batch write failed (%d rows dropped)", len(rows))
            if any(r is None for r in batch):   # sentinel seen inside the batch
                return

    def write(self, category: str, message: str, *, level: str = "info",
              sym: str = "", data: dict | None = None) -> None:
        ts = datetime.now(IST).replace(tzinfo=None)
        self._ensure_worker()
        row = {"ts": ts, "level": level, "category": category, "sym": sym,
               "message": message, "data": data or {}}
        try:
            self._q.put_nowait(row)
        except queue.Full:
            # never block the caller (the event loop); drop and count instead
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.error("event log queue full — dropped %d rows so far", self._dropped)
        logger.info("[%s] %s %s", category, sym, message)
        if self.on_event is not None:
            payload = {"ts": ts.isoformat(timespec="seconds"), "level": level,
                       "category": category, "sym": sym, "message": message,
                       "data": data or {}}
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.on_event(payload))
            except RuntimeError:
                pass  # no loop (unit tests / worker thread)

    # convenience levels
    def math(self, message: str, *, sym: str = "", data: dict | None = None) -> None:
        self.write("MATH", message, level="math", sym=sym, data=data)

    def trade(self, category: str, message: str, *, sym: str = "", data: dict | None = None) -> None:
        self.write(category, message, level="trade", sym=sym, data=data)

    def warn(self, category: str, message: str, *, sym: str = "", data: dict | None = None) -> None:
        self.write(category, message, level="warn", sym=sym, data=data)

    def error(self, category: str, message: str, *, sym: str = "", data: dict | None = None) -> None:
        self.write(category, message, level="error", sym=sym, data=data)

    def flush(self, timeout: float = 2.0) -> None:
        """Best-effort drain — call on shutdown so queued rows aren't lost."""
        import time as _t
        start = _t.monotonic()
        while not self._q.empty() and _t.monotonic() - start < timeout:
            _t.sleep(0.02)

    @staticmethod
    def purge_old() -> int:
        cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(days=settings.log_retention_days)
        with db_session() as db:
            n = db.query(LogEntry).filter(LogEntry.ts < cutoff).delete()
            db.commit()
        return n


event_log = EventLog()
