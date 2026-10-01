"""Volatility-targeting position sizing (the Anti-Martingale).

Each rung of the ladder must represent the SAME monetary risk, adjusted for
current volatility. When the market dips and ATR/IV spike, the engine buys
FEWER lots instead of doubling into a market that is moving twice as fast.

Two formulas are supported (per-instrument `vol_formula`):

  "atr_rupee"  (DEFAULT — the dimensionally correct one):
      Quantity = Target_Risk_Per_Rung_₹ / (ATR_points × lot_size_units)
      ATR × lot_size is the exact ₹ P&L of one lot over a one-ATR move, so the
      result is directly "how many lots does my risk budget afford".

  "notional"  (legacy — Framework 1 as literally written in the spec):
      Quantity = Target_Risk_Per_Rung_₹ / (Entry_Price × Current_ATR)
      This only coincides with constant-₹-risk when price ≈ lot_size. It happens
      to look right for Nifty (it always floors to 1) but grossly OVER-sizes
      instruments whose lot_size dwarfs their price: NATURALGAS (lot 1250,
      px ~200, ATR 2.5) → 20 lots = ₹31,250 risk per ATR against a ₹10,000
      budget, i.e. 3.1× over. Kept only so existing configs keep evaluating,
      and it is BOUND by the rupee-risk ceiling below so it can never exceed
      the budget in practice.

RUPEE-RISK CEILING (applies to BOTH formulas): whatever the chosen formula
returns, the size is clamped so that `lots × ATR × lot_size ≤ risk_per_rung`.
That is the definition of the risk budget, so no formula choice — and no
mis-set field — can put more than one rung's budget at risk over a 1-ATR move.

ATR is computed on the SAME timeframe the AmiBroker signal came from: the
engine pulls 1-minute candles from the gateway and resamples to N minutes, so
any timeframe works — 3m, 7m, 15m, 4h. If AmiBroker includes `atr` in the
webhook payload, that value wins (it is the exact series the signal used).

Both formulas are evaluated and written to the math log so the numbers can
always be audited side by side.
"""

from dataclasses import dataclass, field

from config.settings import settings
from db.models import Instrument
from indicators.atr import atr_last, resample


@dataclass
class SizingResult:
    lots: int = 0
    atr: float = 0.0
    atr_source: str = ""          # "amibroker" | "computed(1m→Nm)" | "fallback"
    timeframe_min: int = 0
    risk_per_rung: float = 0.0
    formula: str = ""
    raw_qty: float = 0.0          # pre-floor value from the chosen formula
    reason: str = ""
    steps: list = field(default_factory=list)   # human-readable math trace for the log


def _hard_cap() -> int:
    """ABSOLUTE lots ceiling — must bound EVERY sizing path (vol-target, fixed,
    ATR-fallback), otherwise the 'no config can emit a runaway order' guarantee
    is false."""
    return max(int(getattr(settings, "max_lots_hard_cap", 50)), 1)


async def compute_size(rest, inst: Instrument, *, timeframe_min: int,
                       signal_atr: float | None, entry_price: float) -> SizingResult:
    res = SizingResult(timeframe_min=timeframe_min, risk_per_rung=inst.risk_per_rung,
                       formula=inst.vol_formula or "atr_rupee")

    if inst.sizing_mode == "fixed":
        res.lots = max(int(inst.fixed_lots), 1)
        if res.lots > _hard_cap():
            res.steps.append(f"HARD CAP: fixed {res.lots} → {_hard_cap()} lots (MAX_LOTS_HARD_CAP)")
            res.lots = _hard_cap()
        res.atr_source = "n/a (fixed sizing)"
        res.steps.append(f"Sizing mode fixed → {res.lots} lot(s)")
        return res

    # ---- 1) establish ATR on the signal's timeframe ----
    atr_val: float | None = None
    if signal_atr and signal_atr > 0:
        atr_val = float(signal_atr)
        res.atr_source = "amibroker"
        res.steps.append(f"ATR {atr_val:.4f} taken from AmiBroker payload")
    else:
        tf = max(int(timeframe_min) or 15, 1)
        lookback = tf * (inst.atr_period * 4 + 10)      # enough bars for a stable Wilder ATR
        lookback = min(lookback, 6 * 24 * 60)           # cap ≈ 6 days of minutes
        try:
            one_min = await rest.get_candles_1m(inst.exch, inst.token, lookback)
            bars = resample(one_min, tf)
            highs = [b["high"] for b in bars]
            lows = [b["low"] for b in bars]
            closes = [b["close"] for b in bars]
            atr_val = atr_last(highs, lows, closes, inst.atr_period)
            if atr_val:
                res.atr_source = f"computed(1m→{tf}m, {len(bars)} bars)"
                res.steps.append(
                    f"ATR({inst.atr_period}) on {tf}m = {atr_val:.4f} (resampled from {len(one_min)} 1m candles)")
        except Exception as e:
            res.steps.append(f"candle fetch failed: {e}")

    if not atr_val or atr_val <= 0:
        # a data hiccup must not brick the strategy — clearly flagged fallback
        res.lots = max(int(inst.fixed_lots), 1)
        if res.lots > _hard_cap():
            res.steps.append(f"HARD CAP: fallback {res.lots} → {_hard_cap()} lots (MAX_LOTS_HARD_CAP)")
            res.lots = _hard_cap()
        res.atr_source = "fallback"
        res.reason = "ATR unavailable → fell back to fixed lots"
        res.steps.append(f"ATR unavailable → fallback to fixed {res.lots} lot(s)")
        return res

    res.atr = round(atr_val, 4)

    # ---- 2) both formulas, side by side, for the audit trail ----
    lot_size = max(inst.lot_size, 1)
    notional_qty = (inst.risk_per_rung / (entry_price * atr_val)) if entry_price > 0 else 0.0
    rupee_per_lot = atr_val * lot_size
    atr_rupee_qty = inst.risk_per_rung / rupee_per_lot if rupee_per_lot > 0 else 0.0
    res.steps.append(
        f"[notional]  qty = ₹{inst.risk_per_rung:,.0f} ÷ (price {entry_price:,.2f} × ATR {atr_val:.4f}) "
        f"= {notional_qty:.4f} lots")
    res.steps.append(
        f"[atr_rupee] qty = ₹{inst.risk_per_rung:,.0f} ÷ (ATR {atr_val:.4f} × lot_size {lot_size}) "
        f"= {atr_rupee_qty:.3f} lots  (₹{rupee_per_lot:,.0f} risk/lot/ATR)")

    raw = notional_qty if res.formula == "notional" else atr_rupee_qty
    res.raw_qty = round(raw, 4)
    lots = int(raw)  # floor — never round risk UP
    res.steps.append(f"chosen formula '{res.formula}' → floor({raw:.4f}) = {lots}")

    # ---- 3) RUPEE-RISK CEILING — binds every formula ----
    # `lots × ATR × lot_size` is the ₹ at risk over a one-ATR move; it may never
    # exceed risk_per_rung. For "atr_rupee" this is a no-op (same expression);
    # for the legacy "notional" formula it is what stops the dimensional error
    # from over-sizing lot_size-heavy contracts (NG et al) by several multiples.
    risk_cap = int(atr_rupee_qty)
    if lots > risk_cap:
        res.steps.append(
            f"RISK CEILING: {lots} → {risk_cap} lots — {lots} lot(s) would risk "
            f"₹{lots * rupee_per_lot:,.0f} per 1×ATR move against a ₹{inst.risk_per_rung:,.0f} "
            f"budget (formula '{res.formula}' over-sized by {lots / max(risk_cap, 1):.1f}×)")
        lots = risk_cap

    # ---- 4) floors and caps ----
    if lots < 1:
        if inst.min_lots >= 1:
            lots = inst.min_lots
            res.steps.append(
                f"volatility too high for risk budget → keep flat at min_lots={inst.min_lots} "
                f"(institutional action: buy less, never more, into a vol spike)")
        else:
            res.lots = 0
            res.reason = "risk budget cannot afford 1 lot at current volatility"
            res.steps.append("min_lots=0 (strict) → SKIP entry")
            return res
    if inst.max_lots_per_rung > 0 and lots > inst.max_lots_per_rung:
        res.steps.append(f"capped {lots} → max_lots_per_rung={inst.max_lots_per_rung}")
        lots = inst.max_lots_per_rung

    # ABSOLUTE backstop — applies even when max_lots_per_rung is 0 ("off") or
    # mis-set, so no config combination can ever emit a runaway order size.
    hard_cap = max(int(getattr(settings, "max_lots_hard_cap", 50)), 1)
    if lots > hard_cap:
        res.steps.append(
            f"HARD CAP hit: {lots} → {hard_cap} lots (system ceiling MAX_LOTS_HARD_CAP; "
            f"check risk_per_rung/max_lots_per_rung — sizing wanted {lots})")
        lots = hard_cap

    res.lots = lots
    return res
