"""NSE market-hours + holiday calendar helpers (IST).

Segment: NSE Equity Derivatives (F&O).
  Open  : 09:15 IST
  Close : 15:40 IST  (extended from 15:30, effective 03-Aug-2026, CAS alignment)
Weekends + listed holidays are treated as closed.

The session and the holiday list are stored on the StrategyConfig row and
edited from the dashboard, because the exchange changes both: the 15:30 → 15:40
extension was a code change and a redeploy, and a hardcoded calendar silently
stops matching the moment the year rolls over.

These are read on the tick path, so the settings are cached in module globals
and refreshed at startup and on every config save — never queried per tick.

LTP ticks received outside these hours (e.g. Saturday mock trading sessions)
are silently discarded so the strategy's PnL never reflects fake prices.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, time

from app.core.config import (
    DEFAULT_MARKET_HOLIDAYS,
    IGNORE_MARKET_HOURS,
    IST,
    MARKET_CLOSE,
    MARKET_OPEN,
)

logger = logging.getLogger(__name__)

# Cached session settings. Seeded with the module defaults so the tick path is
# answerable before the first refresh_from_config() lands.
_lock = threading.Lock()
_open_t = time(*MARKET_OPEN)
_close_t = time(*MARKET_CLOSE)
_holidays: frozenset[str] = frozenset()
_force_open = False


def parse_hhmm(raw: str | None, fallback: time) -> time:
    """"HH:MM" -> time. Falls back rather than raising: this feeds the tick path."""
    try:
        hh, mm = str(raw).strip().split(":")
        return time(int(hh), int(mm))
    except (AttributeError, TypeError, ValueError):
        logger.warning("market_hours: bad time %r, using %s", raw, fallback)
        return fallback


def parse_holidays(raw: str | None) -> frozenset[str]:
    """Comma / whitespace separated YYYY-MM-DD list -> set of ISO dates.

    Unparseable entries are dropped, not fatal — one typo in the calendar box
    should cost that date, not halt the session check for every other day.
    """
    out: set[str] = set()
    for tok in re.split(r"[,;\s]+", raw or ""):
        if not tok:
            continue
        try:
            out.add(date.fromisoformat(tok).isoformat())
        except ValueError:
            logger.warning("market_hours: ignoring bad holiday %r", tok)
    return frozenset(out)


def refresh_from_config(cfg) -> None:
    """Re-read the session settings from a StrategyConfig row into the cache."""
    global _open_t, _close_t, _holidays, _force_open
    with _lock:
        _open_t = parse_hhmm(getattr(cfg, "market_open_time", None), time(*MARKET_OPEN))
        _close_t = parse_hhmm(getattr(cfg, "market_close_time", None), time(*MARKET_CLOSE))
        raw_days = getattr(cfg, "market_holidays", None)
        _holidays = parse_holidays(DEFAULT_MARKET_HOLIDAYS if raw_days is None else raw_days)
        _force_open = bool(getattr(cfg, "ignore_market_hours", False))
    logger.info(
        "market_hours: %s–%s IST, %d holidays%s",
        _open_t.strftime("%H:%M"), _close_t.strftime("%H:%M"),
        len(_holidays), "  [FORCED OPEN]" if _force_open else "",
    )


def session_hours() -> tuple[time, time]:
    return _open_t, _close_t


def is_forced_open() -> bool:
    """Is the hours check being bypassed — by env var or by the config toggle?"""
    return IGNORE_MARKET_HOURS or _force_open


def now_ist() -> datetime:
    return datetime.now(IST)


def is_holiday(d: date | None = None) -> bool:
    d = d or now_ist().date()
    return d.weekday() >= 5 or d.isoformat() in _holidays


def is_market_open(dt: datetime | None = None) -> bool:
    if is_forced_open():
        return True
    dt = dt or now_ist()
    if is_holiday(dt.date()):
        return False
    return _open_t <= dt.time() <= _close_t


def is_ltp_valid(dt: datetime | None = None) -> bool:
    """Should an LTP tick be accepted right now?

    Returns False on holidays, weekends, and outside the configured session.
    While the hours check is bypassed, always returns True.
    """
    return is_market_open(dt)


def market_status() -> str:
    now = now_ist()
    if is_forced_open():
        # Say so out loud. A green OPEN on a Sunday with no explanation is how
        # a forgotten override turns into trades against a closed market.
        return "OPEN (forced)" if not _within_session(now) else "OPEN"
    if is_holiday(now.date()):
        day = now.strftime("%A")
        return f"HOLIDAY ({day})" if now.date().weekday() >= 5 else "HOLIDAY"
    if _within_session(now):
        return "OPEN"
    return "PRE-MARKET" if now.time() < _open_t else "CLOSED"


def _within_session(dt: datetime) -> bool:
    """Real session check, ignoring any override."""
    return not is_holiday(dt.date()) and _open_t <= dt.time() <= _close_t
