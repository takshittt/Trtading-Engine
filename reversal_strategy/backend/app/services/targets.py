"""Target & Stop-Loss calculation.

A signal's levels come from Amibroker's support and resistance: the target is
the resistance above, the stop is the support below. That is the whole point of
computing S&R in the scan — they are the levels the market actually respects,
and they are ABSOLUTE prices, not offsets from wherever the stock happens to be
trading now.

There used to be a global "manual" mode that priced every signal at a flat
±default_pct instead. With both percentages set to 1.0 that made the target and
the stop exactly equidistant, so every single signal carried a 1:1 reward:risk
by construction and no reward:risk filter could ever pass. It is gone.

Fallback order per side, first that applies:
  1. explicit points sent with the signal (target_points / sl_points)
  2. the S&R level, when it is on the correct side of the entry
  3. entry ± ATR × multiplier
  4. entry ± default percentage — last resort, so a signal is never left at 0

Per-buy the user can still type an exact price in the Buy modal; that is one
trade's decision and is kept (see resolve_target / resolve_sl).
"""
from __future__ import annotations

import math

from db.models import StrategyConfig


def _round_resistance(price: float) -> float:
    """Nearest round level above price — used only when Amibroker sent no level."""
    if price <= 0:
        return price
    step = 10 ** max(0, int(math.log10(price)) - 1)
    level = math.ceil(price / step) * step
    if level <= price:          # already on a round level — take the next one up
        level += step
    return round(level, 2)


def auto_target(entry: float, cfg: StrategyConfig, *, atr: float = 0.0, resistance: float = 0.0,
                method: str = "atr", target_points: float = 0.0) -> float:
    if target_points > 0:
        return round(entry + target_points, 2)
    # Resistance is a real level, so use it whenever it is above the entry —
    # not only when the caller explicitly asked for the "resistance" method.
    if resistance > entry:
        return round(resistance, 2)
    if method == "resistance":
        return round(_round_resistance(entry), 2)
    if atr > 0:
        return round(entry + atr * cfg.atr_target_mult, 2)
    return round(entry * (1 + cfg.default_target_pct / 100.0), 2)


def auto_sl(entry: float, cfg: StrategyConfig, *, atr: float = 0.0, method: str = "atr",
            sl_points: float = 0.0, support: float = 0.0) -> float:
    if sl_points > 0:
        return round(entry - sl_points, 2)
    # Support below the entry is the level the stop belongs at. A support at or
    # above the entry is stale or wrong for this price, so it is ignored rather
    # than used to place a stop above the market.
    if 0 < support < entry:
        return round(support, 2)
    if atr > 0:
        return round(entry - atr * cfg.atr_sl_mult, 2)
    return round(entry * (1 - cfg.default_sl_pct / 100.0), 2)


def reward_risk(entry: float, target: float, stop: float) -> float:
    """Reward:risk for a long at `entry`, 0.0 when it cannot be measured.

    Judged at the entry price and nowhere else. The R:R shown live on the board
    is recomputed against the current price and so decays as the stock moves
    toward its target — useful to look at, but it would make a filter that
    accepts a signal at 09:15 reject the identical signal at 09:20.
    """
    reward = target - entry
    risk = entry - stop
    if reward <= 0 or risk <= 0:
        return 0.0
    return round(reward / risk, 2)


def resolve_target(entry: float, cfg: StrategyConfig, *, mode: str, method: str,
                   manual_value: float, atr: float, resistance: float, target_points: float = 0.0) -> float:
    if mode == "manual" and manual_value > 0:
        return round(manual_value, 2)
    return auto_target(entry, cfg, atr=atr, resistance=resistance, method=method or "atr", target_points=target_points)


def resolve_sl(entry: float, cfg: StrategyConfig, *, mode: str, method: str,
               manual_value: float, atr: float, sl_points: float = 0.0,
               support: float = 0.0) -> float:
    if mode == "manual" and manual_value > 0:
        return round(manual_value, 2)
    return auto_sl(entry, cfg, atr=atr, method=method or "atr", sl_points=sl_points,
                   support=support)


def signal_levels(entry: float, cfg: StrategyConfig, *, atr: float = 0.0, resistance: float = 0.0,
                  support: float = 0.0, target_points: float = 0.0,
                  sl_points: float = 0.0) -> tuple[float, float]:
    """Target & SL for a signal: Amibroker's resistance above, support below."""
    tgt = auto_target(entry, cfg, atr=atr, resistance=resistance, target_points=target_points)
    sl = auto_sl(entry, cfg, atr=atr, sl_points=sl_points, support=support)
    return tgt, sl


def preview(entry: float, cfg: StrategyConfig, *, atr: float = 0.0, resistance: float = 0.0,
            support: float = 0.0) -> dict:
    """Suggested AUTO values shown in the Buy modal before the user commits."""
    return {
        "atr_target": auto_target(entry, cfg, atr=atr, method="atr"),
        "atr_sl": auto_sl(entry, cfg, atr=atr, method="atr"),
        "resistance_target": auto_target(entry, cfg, resistance=resistance, method="resistance"),
        "support_sl": auto_sl(entry, cfg, atr=atr, support=support),
    }


def recompute_for_position(avg_price: float, cfg: StrategyConfig, *, target_method: str,
                           sl_method: str, atr: float = 0.0, resistance: float = 0.0,
                           support: float = 0.0, current_target: float = 0.0,
                           current_sl: float = 0.0) -> tuple[float, float]:
    """After an averaging buy, recompute Target & SL off the new average.

    Two rules that used to be silently broken:

    A level set by hand is a *price the user chose*, not a formula, so it is
    kept. Previously "manual" fell through to the ATR branch and was overwritten
    with a percentage of the new average, discarding the user's number while
    still reporting the method as manual.

    Auto levels recompute from the ATR and resistance captured at entry. Without
    them the ATR branch found atr=0 and quietly degraded to the percentage
    fallback, so a position labelled "atr" stopped being ATR-based the moment it
    was averaged.
    """
    tgt = current_target if target_method == "manual" else auto_target(
        avg_price, cfg, atr=atr, resistance=resistance, method=target_method or "atr")
    sl = current_sl if sl_method == "manual" else auto_sl(
        avg_price, cfg, atr=atr, method=sl_method or "atr", support=support)
    return tgt, sl
