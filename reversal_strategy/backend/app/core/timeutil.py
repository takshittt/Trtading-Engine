"""UTC-correct timestamps on the wire.

Every timestamp column is a plain `DateTime`, so SQLAlchemy stores it in SQLite
without a timezone and hands it back naive. Calling `.isoformat()` on that
produces "2026-07-30T05:58:12" — no offset — and JavaScript's `new Date()`
reads an offset-less date-time as *local* time. The dashboard therefore printed
UTC digits as if they were IST and every time in the UI ran 5h30m slow: an
11:28 IST fill was logged as 05:58 am.

Worse, it was inconsistent. A row still attached to the session held the aware
value it was created with and serialized *with* an offset, so the same field
could be right or wrong depending on whether it had been round-tripped through
the database.

So nothing calls `.isoformat()` on a model timestamp directly. `iso_utc()` is
the single way a stored time reaches a client, and it always carries +00:00.
"""
from __future__ import annotations

from datetime import datetime, timezone


def as_utc(dt: datetime | None) -> datetime | None:
    """Attach UTC to a naive timestamp; leave an aware one alone.

    Stored times are UTC by construction (`db.models.utcnow`), so a naive value
    is UTC that merely lost its label on the way through SQLite.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 with an explicit UTC offset, or None."""
    aware = as_utc(dt)
    return aware.isoformat() if aware else None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
