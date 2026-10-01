"""Amibroker payload normalisation, shared by both delivery paths.

Signals reach this backend two ways — an HTTP webhook, and rows the Amibroker
box INSERTs into `ami_signal_inbox` — and both must produce byte-identical
`signals` rows. That only holds if there is exactly one copy of the timeframe
mapping and the IST->UTC conversion, which is this module. It lived in
routers/amibroker.py until the DB path needed it too.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import IST

# Amibroker timeframe (minutes) -> our timeframe label.
#
# Only these three. Anything else used to fall through a .get(tf_min, "1H")
# default, so a 30-minute scan was stored, bucketed and displayed as 1H — and
# because candle_id rounds a 1H signal down to the hour, the 11:30 bar collided
# with the 11:00 bar and was dropped as a duplicate. Silently losing half a
# scan's signals is worse than refusing the scan, so unknown timeframes are
# rejected where the error is visible on the feed-status panel.
TF_MAP = {60: "1H", 240: "4H", 1440: "1D"}


def bar_time_utc(bar_time: str | None) -> str:
    """Candle time as UTC.

    The AFL builds bar_time from Amibroker's Year()/Hour()/… — the Windows box's
    local clock, which for NSE data is IST — and sends it with no offset. Every
    other stored timestamp is UTC, so a naive value is read as IST and converted.

    Measured against the live feed rather than assumed: the scanner posted
    bar_time 13:00 while this server's IST clock read 13:16, so the stamp is the
    13:00 IST candle. Stored raw it would sit 5h30m ahead of `created_at` in the
    same row and bucket the candle into the wrong hour.
    """
    if not bar_time:
        return datetime.now(timezone.utc).isoformat()
    try:
        dt = datetime.fromisoformat(bar_time)
    except Exception:
        return datetime.now(timezone.utc).isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def tf_label(body: dict) -> str | None:
    """Timeframe label for this payload, or None if it isn't one we support."""
    try:
        tf_min = int(float(body.get("timeframe_min") or 60))
    except (TypeError, ValueError):
        return None
    return TF_MAP.get(tf_min)


def to_our_payload(body: dict) -> dict:
    signal_time = bar_time_utc(body.get("bar_time") or body.get("signal_time"))
    return {
        "symbol": body["symbol"],
        "signal_type": (body.get("action") or body.get("signal_type") or "BUY").upper(),
        "timeframe": tf_label(body) or "1H",
        "candle_closed": True,
        "signal_time": signal_time,
        "ltp": float(body.get("price") or body.get("ltp") or 0.0),
        "atr": float(body.get("atr") or 0.0),
        "resistance": float(body.get("resistance") or 0.0),
        "support": float(body.get("support") or 0.0),
        "target_points": float(body.get("target_points") or 0.0),
        "sl_points": float(body.get("sl_points") or 0.0),
    }
