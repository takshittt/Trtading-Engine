"""Per-symbol locks shared by every path that mutates a position.

The exit engine, the manual buy path and the paper trader all write the same
Position rows, and each used to hold its own module-level lock — which is no
mutual exclusion at all: the exit engine could be squaring a position off at the
exact moment the paper trader was adding to it.

Worse, a single process-wide lock is not just insufficient, it is actively
harmful on the tick path. `execution.place()` blocks the caller while it polls
the broker for up to five seconds, so one symbol's exit held the lock for every
other symbol's ticks. Those ticks were dropped, not queued — a stop breach on
another position simply went unseen until the exit finished.

So: one lock per symbol. Two symbols never wait on each other, and everything
touching a given symbol serialises properly regardless of which subsystem it
came from.

Paper and live are deliberately the SAME lock for a symbol. They are separate
rows, but they share the running-average and target recomputation code, and the
cost of over-serialising two books on one symbol is a few milliseconds.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

_registry_lock = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def lock_for(symbol: str) -> threading.RLock:
    """The lock for `symbol`, created on first use.

    Re-entrant, because these paths nest: open_position() takes the symbol lock
    and then calls average_position(), which takes it again. A plain Lock would
    deadlock the first time a repeat buy arrived on a held symbol.
    """
    key = (symbol or "").upper()
    with _registry_lock:
        lk = _locks.get(key)
        if lk is None:
            lk = threading.RLock()
            _locks[key] = lk
        return lk


@contextmanager
def hold(symbol: str):
    """Block until the symbol is free. For request paths, where correctness
    matters more than latency and the caller can afford to wait."""
    lk = lock_for(symbol)
    lk.acquire()
    try:
        yield True
    finally:
        lk.release()


@contextmanager
def try_hold(symbol: str):
    """Yields False instead of waiting. For the tick path.

    Dropping a tick for a symbol that is *already being processed* is correct —
    the in-flight pass is working from a price at most a moment older, and
    queueing ticks behind a five-second broker poll would only replay stale
    prices. What was wrong before was dropping ticks for OTHER symbols.
    """
    lk = lock_for(symbol)
    got = lk.acquire(blocking=False)
    try:
        yield got
    finally:
        if got:
            lk.release()
