"""Backtest simulator — replays history through the SAME ladder math as live.

Isolation contract: this module (and everything under backtest/) never touches
live state. It imports only PURE functions from the live code — compute_target
/ compute_sl (core.exits) and ATR (indicators.atr) — so the strategy math being
tested is literally the deployed math, while the live engine stays unmodified.

Signals (test stand-in for AmiBroker): RSI crossing UP through the oversold
level (default 30) at bar close → BUY at next bar open (+slippage).

Rollover (daily futures mode): the continuous series (e.g. NG=F) switches
front contract right after each NYMEX last-trade date (3 business days before
the delivery month). At that switch bar the sim applies

    basis = open(switch bar) − close(previous bar)

to every open lot — entry/target/SL all shift by basis, the live engine's
exact adjustment — so P&L and exit distances carry across the roll unchanged.
(Live rolls happen 2 days earlier inside a liquidity window; the continuous
data can only switch at expiry, which is close enough for config/UI testing.)

Circuit breaker mirrors live semantics: (realized-today + open P&L) below
−threshold blocks new buys; in the sim it auto re-arms once the book is flat.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from core.exits import compute_sl, compute_target          # live math, read-only
from indicators.atr import atr as atr_series               # live math, read-only
from backtest.data import Bar
from backtest.rsi import rsi as rsi_series


@dataclass
class BtConfig:
    # signal
    rsi_period: int = 14
    rsi_level: float = 30.0
    # exits (same semantics as live Instrument config)
    target_mode: str = "points"
    target_value: float = 0.0
    target_chain_pct: float = 100.0       # chained rung target = prev entry + this % of offset
    sl_mode: str = "points"
    sl_value: float = 0.0
    # sizing
    sizing_mode: str = "vol_target"       # vol_target | fixed
    vol_formula: str = "notional"         # notional | atr_rupee
    risk_per_rung: float = 10000.0
    fixed_lots: int = 1
    atr_period: int = 14
    min_lots: int = 1
    max_lots_per_rung: int = 10
    max_rungs: int = 8
    min_gap_points: float = 0.0
    # instrument
    lot_size: int = 1250
    slippage_points: float = 0.0
    # risk
    cb_threshold: float = 0.0             # ₹, 0 = off
    # rollover
    roll_enabled: bool = False
    # capital baseline for equity display only
    start_capital: float = 0.0


class _ExitsProxy:
    """Adapts BtConfig to the attribute names core.exits expects."""
    def __init__(self, c: BtConfig):
        self.target_mode = c.target_mode
        self.target_value = c.target_value
        self.target_chain_pct = c.target_chain_pct
        self.sl_mode = c.sl_mode
        self.sl_value = c.sl_value


@dataclass
class _Lot:
    seq: int
    lots: int
    qty: int
    entry: float
    raw_entry: float
    entry_ts: int
    target: float
    sl: float
    atr_at_entry: float
    rsi_at_signal: float
    anchor: float
    roll_count: int = 0
    total_basis: float = 0.0
    # exit
    exit_ts: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl: float = 0.0


# NYMEX NG: trading terminates 3 business days before the 1st of the delivery month
def _ng_last_trade(delivery_year: int, delivery_month: int) -> date:
    d = date(delivery_year, delivery_month, 1)
    biz = 0
    while biz < 3:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            biz += 1
    return d


def _roll_switch_dates(start: date, end: date) -> list[date]:
    """Dates after which the continuous series trades the next contract."""
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month + 2):
        out.append(_ng_last_trade(y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return [d for d in out if start <= d <= end]


def _size_lots(cfg: BtConfig, entry_price: float, atr_val: float | None) -> tuple[int, list[str]]:
    """Mirrors core/sizing.py exactly (kept local so live code stays untouched)."""
    from config.settings import settings as _settings
    hard_cap = max(int(getattr(_settings, "max_lots_hard_cap", 50)), 1)   # live parity
    steps: list[str] = []
    if cfg.sizing_mode == "fixed" or not atr_val or atr_val <= 0:
        lots = min(max(int(cfg.fixed_lots), 1), hard_cap)
        steps.append(f"fixed sizing → {lots} lot(s)" if cfg.sizing_mode == "fixed"
                     else f"ATR unavailable → fallback fixed {lots}")
        return lots, steps
    notional = cfg.risk_per_rung / (entry_price * atr_val) if entry_price > 0 else 0.0
    atr_rupee = cfg.risk_per_rung / (atr_val * max(cfg.lot_size, 1))
    steps.append(f"[notional] {cfg.risk_per_rung:,.0f}/({entry_price:,.2f}×{atr_val:.4f})={notional:.4f}")
    steps.append(f"[atr_rupee] {cfg.risk_per_rung:,.0f}/({atr_val:.4f}×{cfg.lot_size})={atr_rupee:.3f}")
    raw = notional if cfg.vol_formula == "notional" else atr_rupee
    lots = int(raw)
    if lots < 1:
        if cfg.min_lots >= 1:
            lots = cfg.min_lots
            steps.append(f"floored to min_lots={cfg.min_lots} (anti-martingale: flat, never more)")
        else:
            steps.append("strict min_lots=0 → skip")
            return 0, steps
    if cfg.max_lots_per_rung > 0 and lots > cfg.max_lots_per_rung:
        lots = cfg.max_lots_per_rung
        steps.append(f"capped to {lots}")
    if lots > hard_cap:
        lots = hard_cap
        steps.append(f"HARD CAP → {lots} (MAX_LOTS_HARD_CAP, same as live)")
    return lots, steps


def run_backtest(bars: list[Bar], cfg: BtConfig) -> dict:
    ex_proxy = _ExitsProxy(cfg)
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    rsi_vals = rsi_series(closes, cfg.rsi_period)

    # ATR aligned per-bar: series starts at index (atr_period) within TR space
    atr_vals: list[float | None] = [None] * len(bars)
    series = atr_series(highs, lows, closes, cfg.atr_period)
    offset = len(bars) - len(series)
    for i, v in enumerate(series):
        atr_vals[offset + i] = v

    # rollover switch bars: first bar strictly after each last-trade date
    roll_after: set[int] = set()
    if cfg.roll_enabled and bars:
        d0 = datetime.fromtimestamp(bars[0].ts, tz=timezone.utc).date()
        d1 = datetime.fromtimestamp(bars[-1].ts, tz=timezone.utc).date()
        switch_dates = _roll_switch_dates(d0, d1)
        di = 0
        for i, b in enumerate(bars):
            bd = datetime.fromtimestamp(b.ts, tz=timezone.utc).date()
            while di < len(switch_dates) and switch_dates[di] < bd:
                roll_after.add(i)      # bars[i] is the first bar of the new contract
                di += 1

    open_lots: list[_Lot] = []
    closed: list[_Lot] = []
    rolls: list[dict] = []
    markers: list[dict] = []
    equity: list[dict] = []
    math_log: list[str] = []
    seq = 0
    pending_entry: dict | None = None       # signal fired at close of bar i → fill at open of i+1
    cb_tripped = False
    cb_trips = 0
    realized_total = 0.0
    realized_by_day: dict[str, float] = {}
    peak = cfg.start_capital
    max_dd = 0.0

    def day_of(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    def close_lot(lot: _Lot, ts: int, price: float, reason: str) -> None:
        nonlocal realized_total
        lot.exit_ts = ts
        lot.exit_price = price
        lot.exit_reason = reason
        lot.pnl = (price - lot.entry) * lot.qty
        realized_total += lot.pnl
        realized_by_day[day_of(ts)] = realized_by_day.get(day_of(ts), 0.0) + lot.pnl
        closed.append(lot)
        label = {"TARGET": "target", "STOPLOSS": "SL"}.get(reason, reason.lower())
        markers.append({"time": ts, "position": "aboveBar", "shape": "arrowDown",
                        "color": "#f59e0b" if reason == "TARGET" else "#ef4444",
                        "text": f"{label} {lot.seq} (buy {lot.seq} closed)",
                        "price": round(price, 4), "lot_id": lot.seq, "kind": "exit"})

    for i, b in enumerate(bars):
        ts = b.ts

        # ---- 1) rollover at contract switch (live basis math) ----
        if i in roll_after and i > 0 and open_lots:
            basis = b.open - bars[i - 1].close
            for lot in open_lots:
                lot.entry += basis
                lot.target = lot.target + basis if lot.target > 0 else lot.target
                lot.sl = lot.sl + basis if lot.sl > 0 else lot.sl
                lot.roll_count += 1
                lot.total_basis += basis
            rolls.append({"ts": ts, "basis": round(basis, 4), "lots_rolled": len(open_lots),
                          "note": f"contract switch — entry/target/SL shifted {basis:+.4f}"})
            markers.append({"time": ts, "position": "belowBar", "shape": "arrowUp",
                            "color": "#38bdf8", "text": f"↻ roll ({basis:+.3f})",
                            "price": round(b.open, 4), "lot_id": 0, "kind": "roll"})
            math_log.append(f"{day_of(ts)} ROLLOVER: basis {basis:+.4f} applied to {len(open_lots)} lot(s) "
                            f"— P&L/target/SL continuity preserved")

        # ---- 2) exits (SL first — conservative when both hit inside one bar) ----
        still_open: list[_Lot] = []
        for lot in open_lots:
            hit = False
            if lot.sl > 0 and b.low <= lot.sl:
                px = b.open if b.open <= lot.sl else lot.sl
                close_lot(lot, ts, px - cfg.slippage_points, "STOPLOSS")
                hit = True
            elif lot.target > 0 and b.high >= lot.target:
                px = b.open if b.open >= lot.target else lot.target
                close_lot(lot, ts, px - cfg.slippage_points, "TARGET")
                hit = True
            if not hit:
                still_open.append(lot)
        open_lots = still_open

        # ---- 3) circuit breaker (live semantics; auto re-arm when flat) ----
        open_pnl = sum((b.close - l.entry) * l.qty for l in open_lots)
        if cfg.cb_threshold > 0:
            day_realized = realized_by_day.get(day_of(ts), 0.0)
            if not cb_tripped and (day_realized + open_pnl) <= -abs(cfg.cb_threshold):
                cb_tripped = True
                cb_trips += 1
                math_log.append(f"{day_of(ts)} CIRCUIT BREAKER tripped "
                                f"(day {day_realized:+,.0f} + open {open_pnl:+,.0f} ≤ -{cfg.cb_threshold:,.0f}) "
                                "— new buys blocked")
                markers.append({"time": ts, "position": "aboveBar", "shape": "arrowDown",
                                "color": "#dc2626", "text": "CB tripped", "price": round(b.close, 4),
                                "lot_id": 0, "kind": "cb"})
            elif cb_tripped and not open_lots:
                cb_tripped = False
                math_log.append(f"{day_of(ts)} circuit breaker re-armed (book flat)")

        # ---- 4) pending entry from last bar's signal → fill at this open ----
        if pending_entry is not None:
            fill = b.open + cfg.slippage_points
            gate = ""
            if cb_tripped:
                gate = "circuit breaker active"
            elif len(open_lots) >= cfg.max_rungs:
                gate = f"max rungs {cfg.max_rungs} reached"
            elif cfg.min_gap_points > 0 and open_lots and fill > open_lots[-1].entry - cfg.min_gap_points:
                gate = f"not ≥{cfg.min_gap_points:g} below last rung {open_lots[-1].entry:.2f}"
            if gate:
                math_log.append(f"{day_of(ts)} Don't execute buy signal (RSI {pending_entry['rsi']:.1f}): {gate}")
            else:
                lots_n, steps = _size_lots(cfg, fill, pending_entry["atr"])
                math_log.append(f"{day_of(ts)} Doing math for buy signal (RSI crossed {cfg.rsi_level:g} ↑, "
                                f"RSI={pending_entry['rsi']:.1f}): " + "; ".join(steps))
                if lots_n > 0:
                    seq += 1
                    qty = lots_n * max(cfg.lot_size, 1)
                    prev_entry = open_lots[-1].entry if open_lots else None
                    tgt, anchor = compute_target(ex_proxy, fill, prev_entry)
                    sl = compute_sl(ex_proxy, fill)
                    lot = _Lot(seq=seq, lots=lots_n, qty=qty, entry=fill, raw_entry=fill,
                               entry_ts=ts, target=tgt, sl=sl,
                               atr_at_entry=pending_entry["atr"] or 0.0,
                               rsi_at_signal=pending_entry["rsi"], anchor=anchor)
                    open_lots.append(lot)
                    markers.append({"time": ts, "position": "belowBar", "shape": "arrowUp",
                                    "color": "#10b981",
                                    "text": f"buy {seq} ({lots_n} lot{'s' if lots_n != 1 else ''})",
                                    "price": round(fill, 4), "lot_id": seq, "kind": "entry"})
                    math_log.append(f"{day_of(ts)} Buy {lots_n} lot(s) @ {fill:.4f} → rung #{seq} | "
                                    f"target {tgt:.4f} (anchor {anchor:.4f}), SL {sl:.4f}")
            pending_entry = None

        # ---- 5) signal detection at close: RSI crosses UP through level ----
        r_prev, r_now = rsi_vals[i - 1] if i > 0 else None, rsi_vals[i]
        if r_prev is not None and r_now is not None and r_prev < cfg.rsi_level <= r_now:
            pending_entry = {"rsi": r_now, "atr": atr_vals[i]}

        # ---- 6) equity curve ----
        open_pnl = sum((b.close - l.entry) * l.qty for l in open_lots)
        eq = cfg.start_capital + realized_total + open_pnl
        equity.append({"ts": ts, "value": round(eq, 2)})
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    # ---- stats ----
    last_close = bars[-1].close if bars else 0.0
    open_pnl_end = sum((last_close - l.entry) * l.qty for l in open_lots)
    wins = [l for l in closed if l.pnl > 0]
    losses = [l for l in closed if l.pnl <= 0]
    gross_win = sum(l.pnl for l in wins)
    gross_loss = -sum(l.pnl for l in losses)

    def lot_row(l: _Lot, status: str) -> dict:
        live = (last_close - l.entry) * l.qty if status == "OPEN" else None
        return {"seq": l.seq, "label": f"B{l.seq}", "lots": l.lots, "qty": l.qty,
                "entry_price": round(l.entry, 4), "raw_entry_price": round(l.raw_entry, 4),
                "entry_time": l.entry_ts, "target_price": round(l.target, 4),
                "sl_price": round(l.sl, 4), "status": status,
                "exit_time": l.exit_ts or None, "exit_price": round(l.exit_price, 4) if l.exit_ts else None,
                "exit_reason": l.exit_reason, "realized_pnl": round(l.pnl, 2),
                "live_pnl": round(live, 2) if live is not None else None,
                "roll_count": l.roll_count, "total_basis": round(l.total_basis, 4),
                "atr_at_entry": round(l.atr_at_entry, 4), "rsi_at_signal": round(l.rsi_at_signal, 2)}

    return {
        "stats": {
            "bars": len(bars),
            "from_ts": bars[0].ts if bars else 0,
            "to_ts": bars[-1].ts if bars else 0,
            "trades_closed": len(closed),
            "open_at_end": len(open_lots),
            "net_pnl": round(realized_total + open_pnl_end, 2),
            "realized_pnl": round(realized_total, 2),
            "open_pnl": round(open_pnl_end, 2),
            "win_rate": round(100 * len(wins) / len(closed), 1) if closed else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
            "max_drawdown": round(max_dd, 2),
            "rolls": len(rolls),
            "cb_trips": cb_trips,
            "target_exits": sum(1 for l in closed if l.exit_reason == "TARGET"),
            "sl_exits": sum(1 for l in closed if l.exit_reason == "STOPLOSS"),
        },
        "candles": [{"ts": b.ts, "open": b.open, "high": b.high, "low": b.low,
                     "close": b.close, "volume": b.volume} for b in bars],
        "markers": sorted(markers, key=lambda m: m["time"]),
        "equity": equity,
        "trades": [lot_row(l, "CLOSED") for l in closed] + [lot_row(l, "OPEN") for l in open_lots],
        "rolls": rolls,
        "math_log": math_log[-400:],
        "rsi": [{"ts": bars[i].ts, "value": round(v, 2)} for i, v in enumerate(rsi_vals) if v is not None],
    }
