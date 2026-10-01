"""ATR (Wilder) + 1-minute→N-minute resampling so the strategy can size
positions on whatever timeframe AmiBroker signalled from."""

from typing import Sequence


def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    n = min(len(highs), len(lows), len(closes))
    if n < 2:
        return []
    tr: list[float] = []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return tr


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[float]:
    """Wilder-smoothed ATR series. First value is SMA of first `period` TRs."""
    tr = true_ranges(highs, lows, closes)
    if len(tr) < period:
        return []
    out: list[float] = []
    first = sum(tr[:period]) / period
    out.append(first)
    prev = first
    for i in range(period, len(tr)):
        cur = (prev * (period - 1) + tr[i]) / period
        out.append(cur)
        prev = cur
    return out


def atr_last(highs, lows, closes, period: int = 14) -> float | None:
    series = atr(highs, lows, closes, period)
    return series[-1] if series else None


# Broker candle timestamps are epoch seconds; the exchanges we trade are all
# IST. Bucketing on raw epoch (`ts // span`) aligns to UTC midnight, so a 60m
# bar starts at IST 05:30 and a 4h bar at IST 01:30/05:30/09:30 — neither is a
# clock boundary a chart would draw, so the live sizing ATR was computed on bars
# phased differently from the AmiBroker bars the signal came from.
IST_OFFSET_S = 19800   # +05:30


def resample(candles: list[dict], tf_min: int, *, drop_partial: bool = True) -> list[dict]:
    """Aggregate 1-minute candles (oldest-first, each with epoch `ts` seconds)
    into tf_min buckets.

    Buckets align to IST clock boundaries (see IST_OFFSET_S), so 15m bars start
    at :00/:15/:30/:45 IST and 60m bars on the IST hour — the same grid a chart
    package draws.

    `drop_partial` (default) discards a trailing bucket that is still forming.
    An in-progress bar has only seen part of its range, so its true range is
    understated; feeding it to Wilder ATR biases ATR DOWN, and a too-small ATR
    makes the sizing formula buy MORE lots — the error points the dangerous way.
    """
    if tf_min <= 1 or not candles:
        return candles
    out: list[dict] = []
    bucket_key = None
    cur: dict | None = None
    span = tf_min * 60
    for c in candles:
        ts = c.get("ts", 0)
        key = (ts + IST_OFFSET_S) // span      # IST-aligned bucket index
        if key != bucket_key:
            if cur is not None:
                out.append(cur)
            bucket_key = key
            cur = {"ts": key * span - IST_OFFSET_S, "open": c["open"], "high": c["high"],
                   "low": c["low"], "close": c["close"], "volume": c.get("volume", 0),
                   "last_ts": ts}
        else:
            cur["high"] = max(cur["high"], c["high"])
            cur["low"] = min(cur["low"], c["low"])
            cur["close"] = c["close"]
            cur["volume"] = cur.get("volume", 0) + c.get("volume", 0)
            cur["last_ts"] = ts
    if cur is not None:
        out.append(cur)

    if drop_partial and out:
        # The final bucket is complete only if its last 1m candle sits in the
        # bucket's last minute. One minute of slack absorbs a missing print on
        # an illiquid contract; anything shorter is genuinely still forming.
        last = out[-1]
        if last["last_ts"] < last["ts"] + span - 120:
            out.pop()
    for b in out:
        b.pop("last_ts", None)
    return out
