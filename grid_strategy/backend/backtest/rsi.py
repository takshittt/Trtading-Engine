"""Wilder RSI — used only by the backtest signal generator."""

from typing import Sequence


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    """Returns a list aligned to `closes`; None until enough data."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_val(avg_gain, avg_loss)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = _rsi_val(avg_gain, avg_loss)
    return out


def _rsi_val(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)
