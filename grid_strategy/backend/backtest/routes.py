"""Backtest API — fully self-contained. main.py mounts this router inside a
try/except so the live engine runs identically whether or not yfinance is
installed. Nothing here reads or writes live engine state or the live DB."""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backtest.data import INTERVALS, PRESETS, clamp_days, fetch_bars
from backtest.simulator import BtConfig, run_backtest

router = APIRouter(prefix="/api/backtest")


class BacktestRequest(BaseModel):
    # data
    preset: str = "NATURALGAS"          # key from PRESETS, or "CUSTOM"
    ticker: str = ""                     # used when preset == CUSTOM
    interval: str = "1d"                 # 5m | 15m | 30m | 60m | 1d
    days: int = 365
    lot_size: Optional[int] = None       # default from preset
    # signal
    rsi_period: int = 14
    rsi_level: float = 30.0
    # ladder config (same fields the live watchlist config exposes)
    target_mode: str = "points"
    target_value: float = 0.0
    target_chain_pct: float = 100.0
    sl_mode: str = "points"
    sl_value: float = 0.0
    sizing_mode: str = "vol_target"
    vol_formula: str = "notional"
    risk_per_rung: float = 10000.0
    fixed_lots: int = 1
    atr_period: int = 14
    min_lots: int = 1
    max_lots_per_rung: int = 10
    max_rungs: int = 8
    min_gap_points: float = 0.0
    slippage_points: float = 0.0
    cb_threshold: float = 0.0
    # rollover simulation (daily futures)
    roll_enabled: bool = True
    start_capital: float = 0.0


@router.get("/presets")
def presets():
    return {"presets": PRESETS, "intervals": INTERVALS}


@router.post("/run")
def run(req: BacktestRequest):
    """Sync endpoint on purpose — FastAPI runs it in a worker thread, so the
    blocking yfinance download never stalls the live engine's event loop."""
    preset = next((p for p in PRESETS if p["key"] == req.preset), None)
    ticker = (req.ticker or "").strip() or (preset["ticker"] if preset else "")
    if not ticker:
        raise HTTPException(status_code=400, detail="pick a preset or give a yfinance ticker")
    lot_size = req.lot_size or (preset["lot_size"] if preset else 1)

    if req.target_value <= 0 or req.sl_value <= 0:
        raise HTTPException(status_code=400, detail="target_value and sl_value must be > 0")
    if req.interval not in INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {INTERVALS}")
    roll = req.roll_enabled and req.interval == "1d"   # rolls are a daily-futures concept here

    try:
        bars = fetch_bars(ticker, req.interval, req.days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"yfinance fetch failed: {e}")

    cfg = BtConfig(
        rsi_period=req.rsi_period, rsi_level=req.rsi_level,
        target_mode=req.target_mode, target_value=req.target_value,
        target_chain_pct=req.target_chain_pct,
        sl_mode=req.sl_mode, sl_value=req.sl_value,
        sizing_mode=req.sizing_mode, vol_formula=req.vol_formula,
        risk_per_rung=req.risk_per_rung, fixed_lots=req.fixed_lots,
        atr_period=req.atr_period, min_lots=req.min_lots,
        max_lots_per_rung=req.max_lots_per_rung, max_rungs=req.max_rungs,
        min_gap_points=req.min_gap_points, lot_size=lot_size,
        slippage_points=req.slippage_points, cb_threshold=req.cb_threshold,
        roll_enabled=roll, start_capital=req.start_capital,
    )
    result = run_backtest(bars, cfg)
    result["meta"] = {
        "ticker": ticker, "interval": req.interval,
        "days_requested": req.days, "days_used": clamp_days(req.interval, req.days),
        "lot_size": lot_size, "roll_enabled": roll,
        "note": ("Rollover simulated at each NYMEX contract switch (last trade = 3 business days "
                 "before the delivery month): basis = switch-bar open − previous close, applied to "
                 "entry/target/SL exactly like the live engine. Live trading rolls ~2 days earlier "
                 "inside the liquidity window.") if roll else
                ("Rollover simulation off" + ("" if req.interval == "1d" else " (needs 1d interval)")),
    }
    return result
