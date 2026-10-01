"""REST + WebSocket API for the grid dashboard."""

import asyncio
import json
import time as _time
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import func

from config.settings import settings
from core.event_log import event_log
from core.exits import compute_sl, compute_target, exit_edit_error, level_override_error, open_lot_pnl
from core.rollover import IST, default_days_before, instrument_class, parse_expiry
from db.engine import db_session
from db.models import (
    EconEvent, Instrument, LadderLevel, LogEntry, Lot, RolloverEvent, Signal,
    UserSettings, now_ist,
)


# ============================ payload models ============================

class InstrumentCreate(BaseModel):
    sym: str
    exch: str
    tsym: str
    token: str
    lot_size: int = 1
    tick_size: float = 0.05
    expiry: str = ""
    mode: str = "auto"           # "auto" (webhook signals) | "ladder" (manual levels)
    instr_type: str = "FUT"      # "FUT" | "OPT"
    opttype: str = ""            # "" | "CE" | "PE" (options only)
    strike: float = 0.0          # option strike (0 for futures)


class InstrumentUpdate(BaseModel):
    enabled: Optional[bool] = None
    target_mode: Optional[str] = None
    target_value: Optional[float] = None
    target_chain_pct: Optional[float] = None   # chained rung target = prev entry + this % of offset
    sl_mode: Optional[str] = None
    sl_value: Optional[float] = None
    sizing_mode: Optional[str] = None
    vol_formula: Optional[str] = None
    risk_per_rung: Optional[float] = None
    fixed_lots: Optional[int] = None
    atr_period: Optional[int] = None
    min_lots: Optional[int] = None
    max_lots_per_rung: Optional[int] = None
    max_rungs: Optional[int] = None
    min_gap_points: Optional[float] = None
    max_spread_points: Optional[float] = None
    marketable_ticks: Optional[int] = None
    sell_signal_mode: Optional[str] = None
    cb_enabled: Optional[bool] = None
    cb_threshold: Optional[float] = None
    rollover_days_before: Optional[int] = None
    rollover_date_override: Optional[str] = None   # "" = automated | ISO "YYYY-MM-DD" = manual
    fallback_symbol: Optional[str] = None          # yfinance/http symbol for the feed-down fallback price
    product_type: Optional[str] = None
    buy_order_type: Optional[str] = None           # "LMT" (default) | "MKT" — entries
    sell_order_type: Optional[str] = None          # "LMT" (default) | "MKT" — TARGET exits (SL always MKT)
    ladder_basis: Optional[str] = None
    ladder_anchor_price: Optional[float] = None
    ladder_interval_points: Optional[float] = None
    ladder_num_levels: Optional[int] = None
    ladder_sr_lookback_days: Optional[int] = None
    ladder_rearm: Optional[bool] = None            # re-arm a triggered level on price recovery (range-bound re-buy)
    sl_enabled: Optional[bool] = None              # False = trade WITHOUT stop-losses (explicit user choice)


class RollRequest(BaseModel):
    """Optional chosen roll target — must be one of the LISTED later futures
    returned by /rollover/plan. Empty = nearest later contract."""
    target_tsym: Optional[str] = None
    target_token: Optional[str] = None


class LadderPreviewReq(BaseModel):
    basis: str = "fixed"                 # "fixed" | "support"
    anchor_price: float = 0.0            # 0 = use live price
    interval_points: float = 0.0
    num_levels: int = 5
    sr_lookback_days: int = 45


class LadderLevelIn(BaseModel):
    price: float
    lots_override: Optional[int] = None    # None = size with the strategy math
    target_override: Optional[float] = None  # None = auto (chained) target math
    sl_override: Optional[float] = None      # None = auto SL math (or SL disabled)


class LadderConfirmReq(LadderPreviewReq):
    levels: list[LadderLevelIn]
    # target & stop-loss are configured IN the levels view now: the first-buy
    # target/SL define the offsets the auto (chained) math uses for every level
    first_target: Optional[float] = None   # target price for B1; sets target_value = first_target − B1
    first_sl: Optional[float] = None       # stop price for B1; sets sl_value = B1 − first_sl
    sl_enabled: Optional[bool] = None      # toggle stop-losses for this instrument
    target_chain_pct: Optional[float] = None  # chained rung target = prev buy + this % of the offset


class LadderLevelUpdate(BaseModel):
    price: Optional[float] = None
    lots_override: Optional[int] = None    # send -1 to clear back to auto sizing
    target_override: Optional[float] = None  # send -1 to clear back to auto (chained) target
    sl_override: Optional[float] = None      # send -1 to clear back to auto SL


class LadderLevelCreate(BaseModel):
    instrument_id: int
    price: float
    lots_override: Optional[int] = None
    target_override: Optional[float] = None
    sl_override: Optional[float] = None


class LotUpdate(BaseModel):
    target_price: Optional[float] = None
    sl_price: Optional[float] = None


class TempActionReq(BaseModel):
    type: str = "market"           # "market" | "limit" | "cancel"
    price: Optional[float] = None  # required for "limit"


class EventCreate(BaseModel):
    dt: str          # ISO "2026-07-10T20:00"
    title: str
    impact: str = "medium"
    region: str = "IN"
    note: str = ""


class SettingsUpdate(BaseModel):
    whatsapp_enabled: Optional[bool] = None
    whatsapp_number: Optional[str] = None
    callmebot_apikey: Optional[str] = None
    alert_on_feed_down: Optional[bool] = None


def _validate_config(updates: dict) -> None:
    """Reject config values that would make the engine trade unsafely. Raises
    HTTPException(400) with a clear message. Only keys present are checked, so
    it works for both create (full) and PATCH-style update (partial)."""
    def num(key):
        return updates.get(key)

    # exit offsets: 0/negative → instant round-trip; percent must be < 100
    tmode, smode = updates.get("target_mode"), updates.get("sl_mode")
    tv, sv = num("target_value"), num("sl_value")
    if tv is not None and tv <= 0:
        raise HTTPException(400, "target_value must be > 0 (0 would exit every rung instantly)")
    if sv is not None and sv <= 0:
        raise HTTPException(400, "sl_value must be > 0 (0/blank leaves the rung with no stop-loss)")
    if tmode == "percent" and tv is not None and tv >= 100:
        raise HTTPException(400, "target_value percent must be < 100")
    tcp = num("target_chain_pct")
    if tcp is not None and (tcp < 0 or tcp > 1000):
        raise HTTPException(400, "target_chain_pct must be between 0 and 1000 (100 = full offset chained)")
    if smode == "percent" and sv is not None and sv >= 100:
        raise HTTPException(400, "sl_value percent must be < 100 (≥100 puts the stop at/below 0)")
    # sizing / risk
    if num("risk_per_rung") is not None and updates["risk_per_rung"] <= 0:
        raise HTTPException(400, "risk_per_rung must be > 0")
    if num("max_rungs") is not None and updates["max_rungs"] < 1:
        raise HTTPException(400, "max_rungs must be ≥ 1 (0 blocks all entries)")
    if num("atr_period") is not None and updates["atr_period"] < 1:
        raise HTTPException(400, "atr_period must be ≥ 1")
    if num("fixed_lots") is not None and updates["fixed_lots"] < 0:
        raise HTTPException(400, "fixed_lots cannot be negative")
    if num("min_lots") is not None and updates["min_lots"] < 0:
        raise HTTPException(400, "min_lots cannot be negative")
    if num("max_lots_per_rung") is not None and updates["max_lots_per_rung"] < 0:
        raise HTTPException(400, "max_lots_per_rung cannot be negative (use 0 for 'no per-rung cap')")
    # 0 here means "no per-rung cap", which is a legitimate choice — but it must
    # not read as "unlimited": the absolute MAX_LOTS_HARD_CAP still applies, and
    # anything above it is a typo, not a strategy.
    _hard = max(int(getattr(settings, "max_lots_hard_cap", 50)), 1)
    if num("max_lots_per_rung") is not None and updates["max_lots_per_rung"] > _hard:
        raise HTTPException(400, f"max_lots_per_rung {updates['max_lots_per_rung']} exceeds the system hard cap "
                                 f"of {_hard} lots — raise MAX_LOTS_HARD_CAP if that is really intended")
    if num("fixed_lots") is not None and updates["fixed_lots"] > _hard:
        raise HTTPException(400, f"fixed_lots {updates['fixed_lots']} exceeds the system hard cap of {_hard} lots")
    if num("min_lots") is not None and updates["min_lots"] > _hard:
        raise HTTPException(400, f"min_lots {updates['min_lots']} exceeds the system hard cap of {_hard} lots")
    # min_lots is a FLOOR: if it sits above the per-rung cap the two contradict
    # and the cap silently wins, sizing smaller than the user's stated minimum.
    mn, mx = num("min_lots"), num("max_lots_per_rung")
    if mn is not None and mx is not None and mx > 0 and mn > mx:
        raise HTTPException(400, f"min_lots ({mn}) cannot exceed max_lots_per_rung ({mx})")
    vf = updates.get("vol_formula")
    if vf is not None and str(vf) not in ("atr_rupee", "notional"):
        raise HTTPException(400, "vol_formula must be 'atr_rupee' (recommended) or 'notional' (legacy)")
    sm_ = updates.get("sizing_mode")
    if sm_ is not None and str(sm_) not in ("vol_target", "fixed"):
        raise HTTPException(400, "sizing_mode must be 'vol_target' or 'fixed'")
    for k in ("min_gap_points", "max_spread_points"):
        if num(k) is not None and updates[k] < 0:
            raise HTTPException(400, f"{k} cannot be negative (use 0 to disable)")
    if num("marketable_ticks") is not None and updates["marketable_ticks"] < 0:
        raise HTTPException(400, "marketable_ticks cannot be negative (use 0 for a strict limit at the touch)")
    # circuit breaker: if it's enabled it must have a positive threshold
    cb_en = updates.get("cb_enabled")
    cb_th = num("cb_threshold")
    if cb_th is not None and cb_th < 0:
        raise HTTPException(400, "cb_threshold cannot be negative")
    if cb_en is True and (cb_th is not None and cb_th <= 0):
        raise HTTPException(400, "cb_threshold must be > 0 when the circuit breaker is enabled")
    # order routing: only MARKET or LIMIT are valid
    for k in ("buy_order_type", "sell_order_type"):
        v = updates.get(k)
        if v is not None and str(v).upper() not in ("LMT", "MKT"):
            raise HTTPException(400, f"{k} must be 'LMT' or 'MKT'")


def _lot_dict(l: Lot, sym: str, price: float = 0.0,
              level: tuple[int, float] | None = None) -> dict:
    level_no = level[0] if level else None
    # what the broker's pending order actually says: RESTING lots store the limit
    # as entry_price; legacy UNCONFIRMED rows show their LEVEL price (the limit
    # the old flow placed), never the pre-trade estimate
    pending_at = l.entry_price
    if l.status == "UNCONFIRMED" and level is not None:
        pending_at = level[1]
    # OPEN with no known price stays None (UI shows a dash, not ₹0). A paused
    # TEMP_EXITED rung shows its FROZEN temp-exit P&L (open_lot_pnl ignores the
    # live price for it) so the displayed value stops ticking.
    live = None
    if (l.status == "OPEN" and price > 0) or l.status == "TEMP_EXITED":
        live = round(open_lot_pnl(l, price), 2)
    return {
        "id": l.id, "instrument_id": l.instrument_id, "sym": sym, "seq": l.seq,
        "label": f"{sym[:1].upper()}{l.seq}",
        "exch": l.exch, "contract_tsym": l.contract_tsym, "contract_token": l.contract_token,
        "lots": l.lots, "qty": l.qty, "lot_size": l.lot_size,
        "entry_price": l.entry_price, "raw_entry_price": l.raw_entry_price,
        "entry_time": l.entry_time.isoformat(timespec="seconds") if l.entry_time else "",
        "target_price": l.target_price, "sl_price": l.sl_price,
        "status": l.status, "exit_reason": l.exit_reason, "exit_price": l.exit_price,
        "exit_time": l.exit_time.isoformat(timespec="seconds") if l.exit_time else "",
        "exit_pending": l.exit_pending,
        "realized_pnl": round(l.realized_pnl or 0.0, 2),
        "live_pnl": live,
        "temp_exit_price": round(l.temp_exit_price or 0.0, 4),
        "temp_exit_loss": round(l.temp_exit_loss or 0.0, 4),
        "temp_exit_time": l.temp_exit_time.isoformat(timespec="seconds") if l.temp_exit_time else "",
        "temp_exit_trigger_price": round(l.temp_exit_trigger_price or 0.0, 4),
        "reenter_trigger_price": round(l.reenter_trigger_price or 0.0, 4),
        "carry_recovery": round(l.carry_recovery or 0.0, 4),
        "roll_count": l.roll_count, "total_basis": round(l.total_basis or 0.0, 4),
        "atr_at_entry": l.atr_at_entry, "timeframe_min": l.timeframe_min,
        "source": l.source or "automated",
        "notes": l.notes or "",
        "level_no": level_no,
        "level_price": (level[1] if level else None),
        "pending_order_at": round(pending_at, 4) if pending_at else None,
        "has_resting_target": bool(l.target_order_id or ""),
    }


# All-time realised P&L: one WAN query to an off-site database, for a number
# that only moves when a trade closes. Held briefly so the once-a-second panel
# poll doesn't pay for it every time.
_realized_all_cache: dict = {"at": 0.0, "value": 0.0}


def build_router(get_engine) -> APIRouter:
    # NOTE ON `def` vs `async def` BELOW — this is a latency decision, not style.
    #
    # FastAPI runs an `async def` handler ON the event loop and a plain `def`
    # handler in a threadpool. Every handler here talks to SQLAlchemy
    # synchronously, and the database is ~36ms away, so an `async def` handler
    # blocks the entire engine — ticks, exits, the snapshot push and every other
    # request — for the whole query. That is why /health, /api/lots, /api/pnl and
    # /api/logs all used to spike to ~900ms together: one stall, seen from four
    # endpoints.
    #
    # So: a handler that does DB work and never awaits MUST be plain `def`.
    # The exceptions are handlers that call asyncio.create_task (engine_start,
    # global_cb_reset) — those need a running loop and must stay `async def`.
    router = APIRouter(prefix="/api")

    # ----------------- broker session -----------------

    @router.post("/broker/connect")
    async def broker_connect():
        """Ask the shared Gateway to (re)login to the broker.

        async def (not plain def) because it awaits network I/O, not a blocking
        DB query — the await yields the loop, so it doesn't stall ticks/exits."""
        return await get_engine().rest.connect_broker()

    @router.post("/broker/disconnect")
    async def broker_disconnect():
        """Log the shared Gateway out of the broker (affects every strategy on it)."""
        return await get_engine().rest.disconnect_broker()

    # ----------------- state / master switch -----------------

    @router.get("/state")
    def state():
        return get_engine().snapshot()

    @router.post("/engine/start")
    async def engine_start():
        eng = get_engine()
        eng.set_engine_on(True)
        # re-place resting ladder entry orders now that buying is allowed again
        asyncio.create_task(eng.sync_resting_orders(force=True, clear_holds=True))
        return {"engine_on": True}

    @router.post("/engine/stop")
    async def engine_stop():
        eng = get_engine()
        eng.set_engine_on(False)
        # resting entry orders are pre-authorized buys — pull them from the broker.
        # Resting TARGETS stay: exits keep protecting open rungs while stopped.
        await eng.cancel_resting_entries(None, "strategy master switch OFF")
        return {"engine_on": False}

    @router.post("/engine/global-cb/reset")
    async def global_cb_reset():
        """Clear the GLOBAL circuit breaker.

        It is an in-memory, set-only flag: before this endpoint existed the only
        way to resume trading after a global trip was restarting the engine
        process — unacceptable on a live book. Per-instrument breakers already
        had a reset (the watchlist CB switch); this is the missing global one.
        """
        return get_engine().reset_global_cb()

    # ----------------- user settings (alerts) -----------------

    def _settings_dict(s: UserSettings) -> dict:
        # never echo the full API key back to the UI — just whether one is set
        return {
            "whatsapp_enabled": bool(s.whatsapp_enabled),
            "whatsapp_number": s.whatsapp_number or "",
            "callmebot_apikey_set": bool(s.callmebot_apikey),
            "alert_on_feed_down": bool(s.alert_on_feed_down),
        }

    @router.get("/settings")
    def get_settings():
        with db_session() as db:
            s = db.get(UserSettings, 1) or UserSettings(id=1)
            return _settings_dict(s)

    @router.put("/settings")
    def update_settings(req: SettingsUpdate):
        updates = req.model_dump(exclude_unset=True)
        num = (updates.get("whatsapp_number") or "").strip()
        if num and not (num.startswith("+") and num[1:].isdigit() and 8 <= len(num[1:]) <= 15):
            raise HTTPException(400, "whatsapp_number must be E.164, e.g. +9198XXXXXXXX (country code, digits only)")
        with db_session() as db:
            s = db.get(UserSettings, 1)
            if s is None:
                s = UserSettings(id=1)
                db.add(s)
            for k, v in updates.items():
                if k == "callmebot_apikey" and v == "":
                    continue   # blank = "leave unchanged"; a real clear can be a separate action
                setattr(s, k, v.strip() if isinstance(v, str) else v)
            db.commit()
            out = _settings_dict(s)
        event_log.write("SYSTEM", "Alert settings updated", level="info")
        return out

    # ----------------- instrument search (futures only, monthly) -----------------

    @router.get("/search")
    async def search(q: str = "", exch: str = "ALL"):
        """Tokenized, Zerodha-style search across FUTURES only (front month
        first). Type words in any order — "nifty aug", "crudeoil jul". Options
        are intentionally excluded — the grid trades futures."""
        if len(q.strip()) < 2:
            return {"results": []}
        eng = get_engine()
        exchanges = ["MCX", "NFO"] if exch == "ALL" else [exch]
        out = []
        seen: set = set()
        for e in exchanges:
            try:
                rows = await eng.rest.search(e, q)
            except Exception:
                rows = []
            for r in rows:
                itype = (r.get("instrumenttype") or "").upper()
                opttype = (r.get("opttype") or "").upper()
                # exclude options — "OPTFUT"/"OPTIDX" contain "FUT" but are
                # options; only a plain futures type with no opttype is a future.
                is_opt = itype.startswith("OPT") or opttype in ("CE", "PE")
                is_fut = (not is_opt) and ("FUT" in itype or itype in ("F", "FUTSTK"))
                if not is_fut:
                    continue    # futures only
                key = (r.get("exch", e), r.get("token", ""))
                if not key[1] or key in seen:
                    continue
                seen.add(key)
                out.append({
                    "sym": r.get("sym") or r.get("tsym", ""),
                    "exch": r.get("exch", e), "tsym": r.get("tsym", ""),
                    "token": r.get("token", ""),
                    "instrumenttype": "FUT", "opttype": "", "strikeprice": 0.0,
                    "expd": r.get("expd", ""), "lotsize": r.get("lotsize", "1"),
                })
        # nearest expiry first, so the front month is the natural pick
        def k(r):
            return (parse_expiry(r["expd"]) or datetime(9999, 1, 1).date(), r["sym"])
        out.sort(key=k)
        return {"results": out[:40]}

    # ----------------- watchlist -----------------

    @router.get("/instruments")
    def list_instruments():
        eng = get_engine()
        snap = eng.snapshot()
        return snap["instruments"]

    @router.post("/instruments", status_code=201)
    async def add_instrument(req: InstrumentCreate):
        eng = get_engine()
        underlying = req.sym.split(" ")[0].upper()
        mode = "ladder" if req.mode == "ladder" else "auto"
        which = "ladder" if mode == "ladder" else "automated-signal"
        opttype = (req.opttype or "").upper()
        is_opt = (req.instr_type or "FUT").upper() == "OPT" or opttype in ("CE", "PE")
        if is_opt:
            # a unique, readable watchlist name per option contract (strike+type)
            # so multiple strikes of the same underlying coexist in one watchlist
            strike = float(req.strike or 0)
            root = f"{underlying}{strike:g}{opttype}".upper() if strike else underlying + opttype
        else:
            root = underlying
        with db_session() as db:
            existing = db.query(Instrument).filter_by(exch=req.exch, sym=root, mode=mode).first()
            if existing:
                raise HTTPException(status_code=409,
                                    detail=f"{root} is already in the {which} watchlist")
            cls = "option" if is_opt else instrument_class(req.exch, underlying)
            inst = Instrument(
                sym=root, exch=req.exch, tsym=req.tsym, token=req.token,
                lot_size=max(int(req.lot_size or 1), 1), tick_size=req.tick_size,
                expiry=req.expiry, rollover_days_before=-1, mode=mode,
                instr_type="OPT" if is_opt else "FUT",
                opt_type=opttype if is_opt else "",
                strike=float(req.strike or 0) if is_opt else 0.0,
                underlying=underlying if is_opt else "",
            )
            db.add(inst)
            db.commit()
            inst_id = inst.id
        await eng.ws.subscribe(f"{req.exch}|{req.token}")
        await eng.prime_quote(req.exch, req.token)   # show a price immediately
        if is_opt:
            event_log.write("SYSTEM", f"{root} added to the {which} watchlist ({req.tsym}, OPTION, "
                                      f"expiry {req.expiry}). Options are NOT auto-rolled — they expire.",
                            sym=root)
        else:
            event_log.write("SYSTEM", f"{root} added to the {which} watchlist ({req.tsym}, expiry {req.expiry}, "
                                      f"class {cls}, default roll offset −{default_days_before(cls)}d)", sym=root)
        return {"id": inst_id}

    @router.put("/instruments/{inst_id}")
    async def update_instrument(inst_id: int, req: InstrumentUpdate):
        updates = req.model_dump(exclude_unset=True)
        _validate_config(updates)
        for k in ("buy_order_type", "sell_order_type"):
            if updates.get(k) is not None:
                updates[k] = str(updates[k]).upper()
        # manual rollover date must be blank (= automated) or a valid ISO date
        ov = updates.get("rollover_date_override")
        if ov:
            try:
                date.fromisoformat(ov.strip())
                updates["rollover_date_override"] = ov.strip()
            except ValueError:
                raise HTTPException(status_code=400,
                                    detail="rollover_date_override must be YYYY-MM-DD or empty")
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
            if inst is None:
                raise HTTPException(status_code=404, detail="instrument not found")
            for k, v in updates.items():
                setattr(inst, k, v)
            # the SL master toggle applies to the rungs that ALREADY exist too:
            # off → every open rung's stop is removed (sl_price=0 = "no stop");
            # on → stops recomputed from entry (per-lot override wins)
            if "sl_enabled" in updates:
                open_lots = db.query(Lot).filter(Lot.instrument_id == inst_id,
                                                 Lot.status.in_(["OPEN", "TEMP_EXITED"])).all()
                for l in open_lots:
                    if updates["sl_enabled"] is False:
                        l.sl_price = 0.0
                    else:
                        sl = compute_sl(inst, l.entry_price)
                        if l.sl_override and l.sl_override > 0:
                            sl = round(float(l.sl_override), 4)
                        l.sl_price = sl
            db.commit()
            sym = inst.sym
            mode = inst.mode
        if updates:
            event_log.write("SYSTEM", f"{sym} config updated: {updates}", sym=sym)
        if updates.get("sl_enabled") is False:
            event_log.warn("RISK", f"{sym}: STOP-LOSSES DISABLED by user — open rungs now trade "
                                   "WITHOUT a stop", sym=sym)
        elif updates.get("sl_enabled") is True:
            event_log.write("RISK", f"{sym}: stop-losses re-enabled — stops recomputed on open rungs",
                            sym=sym)
        if mode == "ladder":
            eng = get_engine()
            if updates.get("enabled") is False or updates.get("buy_order_type") == "MKT":
                await eng.cancel_resting_entries(inst_id, "instrument disabled / order type changed")
            else:
                asyncio.create_task(eng.sync_resting_orders(inst_id, force=True, clear_holds=True))
        return {"ok": True}

    @router.delete("/instruments/{inst_id}")
    def delete_instrument(inst_id: int):
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
            if inst is None:
                raise HTTPException(status_code=404, detail="instrument not found")
            open_count = db.query(Lot).filter_by(instrument_id=inst_id, status="OPEN").count()
            if open_count:
                raise HTTPException(status_code=400,
                                    detail=f"{inst.sym} has {open_count} open rung(s) — flatten first")
            resting_count = db.query(Lot).filter_by(instrument_id=inst_id, status="RESTING").count()
            if resting_count:
                raise HTTPException(status_code=400,
                                    detail=f"{inst.sym} has {resting_count} pending order(s) at the broker — "
                                           "cancel them first (Pending tab)")
            unconfirmed_count = db.query(Lot).filter_by(instrument_id=inst_id, status="UNCONFIRMED").count()
            if unconfirmed_count:
                raise HTTPException(status_code=400,
                                    detail=f"{inst.sym} has {unconfirmed_count} unconfirmed order(s) — the "
                                           "reconciler settles these against the broker within a minute or "
                                           "two; try again after they clear (deleting now could orphan a "
                                           "live position)")
            temp_count = db.query(Lot).filter_by(instrument_id=inst_id, status="TEMP_EXITED").count()
            if temp_count:
                raise HTTPException(status_code=400,
                                    detail=f"{inst.sym} has {temp_count} paused (temp-exited) rung(s) — "
                                           "re-enter or close them from the Trades tab first")
            sym = inst.sym
            # Remove the instrument's dependent rows explicitly. Without this,
            # SQLAlchemy tries to NULL lots.instrument_id (no delete cascade on
            # the relationship) and the NOT NULL constraint aborts the delete.
            # Every remaining lot is settled history (the guards above block
            # OPEN/RESTING/UNCONFIRMED/TEMP_EXITED) and orphaned history would
            # be invisible anyway — /api/lots inner-joins instruments.
            lots_gone = db.query(Lot).filter_by(instrument_id=inst_id).delete(synchronize_session=False)
            db.query(RolloverEvent).filter_by(instrument_id=inst_id).delete(synchronize_session=False)
            db.query(LadderLevel).filter_by(instrument_id=inst_id).delete(synchronize_session=False)
            db.delete(inst)
            db.commit()
        event_log.write("SYSTEM", f"{sym} removed from watchlist"
                                  + (f" (with {lots_gone} settled/cancelled historical lot(s))" if lots_gone else ""),
                        sym=sym)
        return {"ok": True}

    @router.post("/instruments/{inst_id}/cb")
    async def set_circuit_breaker(inst_id: int, on: bool = Query(...)):
        """on=true re-arms trading (green). on=false manually trips it (red)."""
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
            if inst is None:
                raise HTTPException(status_code=404, detail="instrument not found")
            inst.cb_tripped = not on
            inst.cb_reason = "" if on else "manually switched off"
            db.commit()
            sym = inst.sym
        event_log.write("CB", f"{sym} circuit breaker manually set to {'ON (trading allowed)' if on else 'OFF (buys blocked)'}",
                        level="warn", sym=sym)
        eng = get_engine()
        if on:
            asyncio.create_task(eng.sync_resting_orders(inst_id, force=True, clear_holds=True))
        else:
            await eng.cancel_resting_entries(inst_id, "circuit breaker switched off by user")
        return {"ok": True, "tripped": not on}

    @router.post("/instruments/{inst_id}/flatten")
    async def flatten(inst_id: int):
        eng = get_engine()
        n = await eng.flatten_instrument(inst_id, "MANUAL")
        return {"closed": n}

    @router.post("/instruments/{inst_id}/rollover")
    async def manual_rollover(inst_id: int, req: RollRequest | None = None):
        """Manual 'Roll now'. Without a chosen contract → queue for the Smart
        Sniper (spread-scan when the liquidity window opens). WITH a chosen
        contract (target_tsym/target_token from the roll-targets list) → roll
        every open rung into it immediately, one rung at a time."""
        eng = get_engine()
        if req is not None and (req.target_token or req.target_tsym):
            res = await eng.roll_all_now(inst_id, req.target_tsym or "", req.target_token or "")
        else:
            res = await eng.request_manual_rollover(inst_id)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("reason", "rollover request failed"))
        return res

    @router.get("/instruments/{inst_id}/rollover/plan")
    async def rollover_plan(inst_id: int, target_token: str = ""):
        """Data for the rollover drawer: listed roll targets + per-rung money
        math (basis, leg P&L, roll cost, entry/target/SL after the roll)."""
        res = await get_engine().rollover_plan(inst_id, target_token)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("reason", "no rollover plan available"))
        return res

    @router.post("/lots/{lot_id}/roll")
    async def quick_roll_lot(lot_id: int, req: RollRequest | None = None):
        """Quick roll: sell + re-buy ONE open rung now (optionally into a chosen
        listed contract). Legs are verified sequentially; basis math identical
        to the full roll."""
        res = await get_engine().roll_single_lot(
            lot_id,
            target_tsym=(req.target_tsym if req else "") or "",
            target_token=(req.target_token if req else "") or "")
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("reason", "quick roll failed"))
        return res

    # ----------------- manual ladder -----------------

    @router.post("/instruments/{inst_id}/ladder/preview")
    async def ladder_preview(inst_id: int, req: LadderPreviewReq):
        """Compute levels (support-based or fixed-interval) WITHOUT saving —
        the user reviews/edits them in the UI before Confirm."""
        eng = get_engine()
        try:
            return await eng.ladder_preview(inst_id, req.basis, req.anchor_price,
                                            req.interval_points, req.num_levels,
                                            req.sr_lookback_days)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"level computation failed: {e}")

    @router.post("/instruments/{inst_id}/ladder/confirm")
    async def ladder_confirm(inst_id: int, req: LadderConfirmReq):
        """Save the (possibly hand-edited) levels + ladder config and ARM the
        ladder. Replaces PENDING levels; TRIGGERED history is kept."""
        if not req.levels:
            raise HTTPException(status_code=400, detail="at least one level is required")
        prices = [l.price for l in req.levels]
        if any(p <= 0 for p in prices):
            raise HTTPException(status_code=400, detail="level prices must be > 0")
        # reject duplicate prices — they'd create multiple rungs at one level
        if len({round(p, 2) for p in prices}) != len(prices):
            raise HTTPException(status_code=400, detail="duplicate level prices are not allowed")
        # NOTE: levels ABOVE the live price are allowed — they simply fire at market
        # (marketable order) on arm. The UI warns the user per-level; no hard block.
        ordered = sorted(req.levels, key=lambda l: l.price, reverse=True)  # B1 = highest
        # Per-level overrides are validated HERE, above the session below, because
        # that block commits target_value/sl_value/sl_enabled onto the instrument
        # before it ever reaches the levels — raising later left the instrument
        # half-reconfigured while the ladder was rejected. A stop at/above its own
        # level stops the rung out the moment it fills; a target at/below it
        # round-trips for a loss.
        for _l in ordered:
            _err = level_override_error(_l.price, _l.target_override, _l.sl_override)
            if _err:
                raise HTTPException(status_code=400,
                                    detail=f"level @ {_l.price:g}: {_err}")
        # target & SL live IN the levels view: the first-buy target/SL define
        # the offsets the chained auto-math uses for every level below
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
            if inst is None:
                raise HTTPException(status_code=404, detail="instrument not found")
            # A ladder whose levels sit closer together than min_gap_points can
            # only ever fire ONCE. The gap guard refuses to open a rung within
            # min_gap of an existing one, so the moment B1 fills it blocks every
            # level inside that distance — with levels 20 apart and a 200-point
            # gap, that is all of them. The engine logs the refusal on every
            # attempt, but by then the user has armed what looks like a working
            # ladder and is watching price trade through levels that will never
            # buy. Catch it here, while it is still a configuration question
            # rather than a missed entry.
            gap = float(inst.min_gap_points or 0)
            if gap > 0 and len(prices) > 1:
                ps = sorted(prices)
                tightest = min(b - a for a, b in zip(ps, ps[1:]))
                if tightest < gap:
                    raise HTTPException(
                        status_code=400,
                        detail=(f"these levels are {tightest:g} points apart but the minimum gap between "
                                f"rungs is set to {gap:g}. Once the first level fills, every level within "
                                f"{gap:g} points of it is refused — so the levels below it would never "
                                f"buy. Lower 'min gap between rungs' to {tightest:g} or less in ⚙ Config "
                                f"(0 disables it), or space the levels further apart."))
            # B1 is the TOP OF THE WHOLE LADDER, not the top of the payload.
            #
            # `first_target` arrives as a PRICE and is turned into an offset by
            # subtracting B1, so the two sides have to agree on which level B1
            # is. The dialog measures from the highest level it displays —
            # already-executed rungs included — while this used only the levels
            # being posted, which excludes them. Once the top level had filled,
            # the same screen produced a much larger offset than it showed:
            # B1 15390 with a 15590 target is 200 points, but measured against a
            # posted-only top of 14990 it silently became 600, and every rung
            # below inherited that target.
            live_lvl_max = (db.query(func.max(LadderLevel.price))
                            .filter(LadderLevel.instrument_id == inst_id,
                                    LadderLevel.status.notin_(["PENDING", "CANCELLED"]))
                            .scalar())
            b1 = max([ordered[0].price] + ([float(live_lvl_max)] if live_lvl_max else []))
            if req.sl_enabled is not None:
                inst.sl_enabled = bool(req.sl_enabled)
            if req.target_chain_pct is not None:
                if req.target_chain_pct < 0:
                    raise HTTPException(status_code=400,
                                        detail="chained-target % cannot be negative")
                inst.target_chain_pct = round(float(req.target_chain_pct), 4)
            if req.first_target is not None and req.first_target > 0:
                if req.first_target <= b1:
                    raise HTTPException(status_code=400,
                                        detail=f"first-buy target {req.first_target:g} must be ABOVE the "
                                               f"first buy level {b1:g}")
                inst.target_mode = "points"
                inst.target_value = round(req.first_target - b1, 4)
            sl_on = inst.sl_enabled is not False
            if sl_on and req.first_sl is not None and req.first_sl > 0:
                if req.first_sl >= b1:
                    raise HTTPException(status_code=400,
                                        detail=f"first-buy stop-loss {req.first_sl:g} must be BELOW the "
                                               f"first buy level {b1:g}")
                inst.sl_mode = "points"
                inst.sl_value = round(b1 - req.first_sl, 4)
            # arming without a usable target/SL is refused HERE (this is where
            # they are configured now), not in the instrument-config dialog
            if (inst.target_value or 0) <= 0:
                raise HTTPException(status_code=400,
                                    detail='set "Target — first buy exits at" before arming — without it '
                                           "the target would be 0 and every rung would exit instantly")
            if sl_on and (inst.sl_value or 0) <= 0:
                raise HTTPException(status_code=400,
                                    detail='set "Stop-loss — first buy stops at" (or disable stop-losses) '
                                           "before arming — without it the rungs would be unprotected")
            db.commit()

        # the whole replace runs UNDER THE INSTRUMENT LOCK inside the engine —
        # resting entries are cancelled (confirmed) inline, stale
        # SKIPPED/AWAIT_RECOVERY rows die with the old ladder, and no placement
        # can interleave with the row swap
        res = await get_engine().confirm_ladder(
            inst_id, req.basis, req.anchor_price, req.interval_points,
            req.num_levels, req.sr_lookback_days,
            [{"price": l.price,
              "lots_override": (l.lots_override if l.lots_override and l.lots_override > 0 else None),
              "target_override": (l.target_override if l.target_override and l.target_override > 0 else None),
              "sl_override": (l.sl_override if l.sl_override and l.sl_override > 0 else None),
              } for l in ordered])
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("reason", "confirm failed"))
        return {"ok": True, "levels_saved": res.get("levels_saved", len(ordered))}

    @router.post("/instruments/{inst_id}/ladder/arm")
    async def ladder_arm(inst_id: int, on: bool = Query(...)):
        """Pause / resume the ladder without touching its levels."""
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
            if inst is None:
                raise HTTPException(status_code=404, detail="instrument not found")
            inst.ladder_armed = on
            db.commit()
            sym = inst.sym
        event_log.write("LADDER", f"{sym} ladder {'ARMED — levels are live' if on else 'PAUSED — no level will fire'}",
                        level="warn", sym=sym)
        eng = get_engine()
        if on:
            asyncio.create_task(eng.sync_resting_orders(inst_id, force=True, clear_holds=True))
        else:
            await eng.cancel_resting_entries(inst_id, "ladder paused by user")
        return {"ok": True, "armed": on}

    @router.put("/ladder/levels/{level_id}")
    async def edit_ladder_level(level_id: int, req: LadderLevelUpdate):
        """Edit a PENDING level: price and/or per-level lots (−1 clears the
        override back to strategy sizing)."""
        with db_session() as db:
            lvl = db.get(LadderLevel, level_id)
            if lvl is None:
                raise HTTPException(status_code=404, detail="level not found")
            lvl_status = lvl.status
        if lvl_status == "PLACED":
            # price edit on a level whose order RESTS at the broker → cancel,
            # update, re-place (race-fill safe)
            if req.price is None:
                raise HTTPException(status_code=400,
                                    detail="only the price of a PLACED level can be edited")
            if req.price <= 0:
                raise HTTPException(status_code=400, detail="price must be > 0")
            res = await get_engine().move_placed_level(level_id, req.price)
            if not res.get("ok"):
                raise HTTPException(status_code=400, detail=res.get("reason", "edit failed"))
            return {"ok": True}
        with db_session() as db:
            lvl = db.get(LadderLevel, level_id)
            if lvl.status != "PENDING":
                raise HTTPException(status_code=400, detail=f"level is {lvl.status} — only PENDING levels are editable")
            # Judge the EFFECTIVE level after this edit, and only when the edit
            # actually moves one of the three. add_ladder_level never validated,
            # so bad rows already exist in live databases and must stay editable
            # — changing lots_override alone must not be refused.
            if (req.price is not None or req.target_override is not None
                    or req.sl_override is not None):
                _price = round(req.price, 4) if req.price is not None else lvl.price
                _tgt = req.target_override if req.target_override is not None else lvl.target_override
                _sl = req.sl_override if req.sl_override is not None else lvl.sl_override
                if _price > 0:
                    _err = level_override_error(_price, _tgt, _sl)
                    if _err:
                        raise HTTPException(status_code=400, detail=_err)
            changes = {}
            if req.price is not None:
                if req.price <= 0:
                    raise HTTPException(status_code=400, detail="price must be > 0")
                changes["price"] = (lvl.price, req.price)
                lvl.price = round(req.price, 4)
                lvl.source = "manual"
                lvl.note = "user-edited"
            if req.lots_override is not None:
                changes["lots"] = (lvl.lots_override, req.lots_override)
                lvl.lots_override = req.lots_override if req.lots_override > 0 else None
            if req.target_override is not None:
                changes["target"] = (lvl.target_override, req.target_override)
                lvl.target_override = round(req.target_override, 4) if req.target_override > 0 else None
            if req.sl_override is not None:
                changes["sl"] = (lvl.sl_override, req.sl_override)
                lvl.sl_override = round(req.sl_override, 4) if req.sl_override > 0 else None
            db.commit()
            sym = db.get(Instrument, lvl.instrument_id).sym
            no = lvl.level_no
        event_log.write("LADDER", f"{sym} level B{no} edited: {changes}", sym=sym)
        return {"ok": True}

    @router.delete("/ladder/levels/{level_id}")
    async def delete_ladder_level(level_id: int):
        with db_session() as db:
            lvl = db.get(LadderLevel, level_id)
            if lvl is None:
                raise HTTPException(status_code=404, detail="level not found")
            lvl_status = lvl.status
        if lvl_status == "PLACED":
            res = await get_engine().delete_placed_level(level_id)
            if not res.get("ok"):
                raise HTTPException(status_code=400, detail=res.get("reason", "delete failed"))
            return {"ok": True}
        if lvl_status in ("FILLED", "TRIGGERED", "CANCELLED"):
            # executed / retired: drop the level row only. An open rung keeps its
            # position and exits — this removes it from the ladder, not the market.
            res = await get_engine().detach_ladder_level(level_id)
            if not res.get("ok"):
                raise HTTPException(status_code=400, detail=res.get("reason", "remove failed"))
            return res
        with db_session() as db:
            lvl = db.get(LadderLevel, level_id)
            if lvl.status not in ("PENDING", "SKIPPED", "AWAIT_RECOVERY"):
                raise HTTPException(status_code=400, detail=f"level is {lvl.status} — cannot remove")
            sym = db.get(Instrument, lvl.instrument_id).sym
            no = lvl.level_no
            db.delete(lvl)
            db.commit()
        event_log.write("LADDER", f"{sym} level B{no} removed", sym=sym)
        return {"ok": True}

    @router.delete("/instruments/{inst_id}/ladder/levels")
    async def clear_ladder_levels(inst_id: int):
        """Clear the whole armed ladder: remove all PENDING/SKIPPED/AWAIT_RECOVERY
        levels and cancel any PLACED resting broker orders. Open rungs
        (FILLED/TRIGGERED) and history (CANCELLED) are kept."""
        res = await get_engine().clear_ladder_levels(inst_id)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("reason", "clear failed"))
        return res

    @router.post("/ladder/levels", status_code=201)
    def add_ladder_level(req: LadderLevelCreate):
        if req.price <= 0:
            raise HTTPException(status_code=400, detail="price must be > 0")
        _err = level_override_error(req.price, req.target_override, req.sl_override)
        if _err:
            raise HTTPException(status_code=400, detail=_err)
        with db_session() as db:
            inst = db.get(Instrument, req.instrument_id)
            if inst is None or inst.mode != "ladder":
                raise HTTPException(status_code=404, detail="ladder instrument not found")
            no = (db.query(func.coalesce(func.max(LadderLevel.level_no), 0))
                  .filter(LadderLevel.instrument_id == req.instrument_id).scalar() or 0) + 1
            # GRID: freeze a self-anchored target (own price + offset) when the
            # user didn't type one, so a manually-added level behaves like the rest
            # of the ladder — target fixed, buy price free to shift down on a fill.
            frozen_tgt = (round(req.target_override, 4)
                          if req.target_override and req.target_override > 0
                          else compute_target(inst, round(req.price, 4), None)[0])
            db.add(LadderLevel(
                instrument_id=req.instrument_id, level_no=no, price=round(req.price, 4),
                lots_override=(req.lots_override if req.lots_override and req.lots_override > 0 else None),
                target_override=frozen_tgt,
                sl_override=(round(req.sl_override, 4)
                             if req.sl_override and req.sl_override > 0 else None),
                status="PENDING", source="manual", note="user-added"))
            db.commit()
            sym = inst.sym
        event_log.write("LADDER", f"{sym} level B{no} added @ {req.price:g}", sym=sym)
        return {"ok": True, "level_no": no}

    # ----------------- lots (open / closed trades) -----------------

    @router.get("/lots")
    def lots(status: str = "open", sym: str = "", sort: str = "latest",
                   source: str = "", limit: int = 200, offset: int = 0):
        eng = get_engine()
        with db_session() as db:
            q = db.query(Lot, Instrument.sym).join(Instrument, Lot.instrument_id == Instrument.id)
            if status == "open":
                q = q.filter(Lot.status.in_(["OPEN", "PENDING", "TEMP_EXITED"]))
            elif status == "pending":
                # orders at the broker, no confirmed position: RESTING (ladder
                # LMT flow) + UNCONFIRMED (verify timed out, reconciler owns it)
                q = q.filter(Lot.status.in_(["RESTING", "UNCONFIRMED"]))
            elif status == "closed":
                q = q.filter(Lot.status.in_(["CLOSED", "EXTERNAL_CLOSED", "CANCELLED"]))
            if sym:
                q = q.filter(func.upper(Instrument.sym) == sym.upper())
            if source in ("automated", "ladder"):
                q = q.filter(func.coalesce(Lot.source, "automated") == source)
            rows = q.all()
            level_by_lot = {lv.lot_id: (lv.level_no, lv.price)
                            for lv in db.query(LadderLevel)
                            .filter(LadderLevel.lot_id.in_([l.id for l, _ in rows])).all()
                            } if rows else {}

        def price_for(l: Lot) -> float:
            # Live tick when there is one, else the broker's last reported price,
            # so an open rung still shows its LTP and P&L outside market hours.
            return eng.display_price(l.exch, l.contract_token)

        items = [_lot_dict(l, s, price_for(l), level_by_lot.get(l.id)) for l, s in rows]
        if sort == "latest":
            items.sort(key=lambda x: x["exit_time"] if status == "closed" and x["exit_time"] else x["entry_time"],
                       reverse=True)
        elif sort == "symbol":
            # group by stock name; inside each group latest → oldest (n1,n2,n3, m1,m2…)
            items.sort(key=lambda x: (x["sym"], _neg_time(x["entry_time"])))
        elif sort == "pnl":
            items.sort(key=lambda x: (x["live_pnl"] if x["live_pnl"] is not None else x["realized_pnl"]),
                       reverse=True)
        elif sort == "entry_price":
            items.sort(key=lambda x: x["entry_price"], reverse=True)
        total = len(items)
        return {"total": total, "lots": items[offset:offset + limit]}

    @router.put("/lots/{lot_id}")
    async def edit_lot(lot_id: int, req: LotUpdate):
        with db_session() as db:
            lot = db.get(Lot, lot_id)
            if lot is None:
                raise HTTPException(status_code=404, detail="lot not found")
            if lot.status not in ("OPEN", "RESTING", "UNCONFIRMED"):
                raise HTTPException(status_code=400, detail=f"lot is {lot.status}")
            is_resting = lot.status in ("RESTING", "UNCONFIRMED")
            # ---- validate BEFORE mutating anything ----
            # A typed price bypasses every offset-based guard: it is written
            # onto the lot and the tick loop acts on it next tick. A stop above
            # the market fires a MARKET sell immediately; a target under the
            # entry round-trips the rung for a loss.
            #
            # NOTE: there must be no `await` between the db.get above and the
            # db.commit below — this route takes no instrument lock and is safe
            # only because that window is synchronous. engine.prices is a plain
            # dict read. Do not make this an awaited call.
            _inst = db.get(Instrument, lot.instrument_id)
            _ps = get_engine().prices.get(f"{lot.exch}|{lot.contract_token}")
            _ltp = _ps.lp if (_ps is not None and _ps.lp > 0) else 0.0
            # Judge only a field this call actually MOVED: the edit modal
            # re-posts both, so a pre-existing bad stop must not block fixing
            # the target.
            _new_tgt = (req.target_price if (req.target_price is not None
                        and req.target_price != lot.target_price) else None)
            _new_sl = (req.sl_price if (req.sl_price is not None
                       and req.sl_price != lot.sl_price) else None)
            _err, _warns = exit_edit_error(
                target=_new_tgt, sl=_new_sl, entry=lot.entry_price, ltp=_ltp,
                live=not is_resting,
                tick=getattr(_inst, "tick_size", None) or 0.05,
                ref=("the buy price" if is_resting else "the entry"))
            if _err:
                raise HTTPException(status_code=400, detail=_err)
            target_changed = False
            changes = {}
            lvl = (db.query(LadderLevel).filter(LadderLevel.lot_id == lot.id).first()
                   if is_resting else None)
            if req.target_price is not None:
                changes["target"] = (lot.target_price, req.target_price)
                target_changed = req.target_price != lot.target_price
                lot.target_price = req.target_price
                if is_resting:
                    # persist on BOTH the lot and its level, so the edit survives
                    # any system cancel/re-place cycle (day expiry, pause, CB…)
                    lot.target_override = req.target_price
                    if lvl is not None:
                        lvl.target_override = req.target_price
            if req.sl_price is not None:
                changes["sl"] = (lot.sl_price, req.sl_price)
                lot.sl_price = req.sl_price
                if is_resting:
                    lot.sl_override = req.sl_price
                    if lvl is not None:
                        lvl.sl_override = req.sl_price
            has_resting_target = bool(lot.target_order_id or "")
            db.commit()
            sym = db.get(Instrument, lot.instrument_id).sym
        event_log.write("EXIT", f"rung #{lot_id} target/SL manually edited: {changes}", sym=sym)
        for _w in _warns:
            event_log.warn("EXIT", f"rung #{lot_id}: {_w}", sym=sym)
        if not is_resting and target_changed and has_resting_target:
            # move the broker-resting target order to the new price
            await get_engine().refresh_target_order(lot_id)
        return {"ok": True, "warnings": _warns}

    @router.post("/lots/{lot_id}/skip")
    async def skip_pending_order(lot_id: int):
        """"Skip to next level": cancel this pending order and place the buy one
        level lower (the ladder keeps buying, one notch down)."""
        res = await get_engine().skip_pending_lot(lot_id)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("reason", "skip failed"))
        return res

    @router.post("/lots/{lot_id}/cancel")
    async def cancel_pending_order(lot_id: int):
        """"Cancel & stop buying": cancel this pending order and PAUSE the ladder —
        no lower buy is placed. Open rungs keep their targets/stop-losses."""
        res = await get_engine().cancel_and_pause_pending(lot_id)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("reason", "cancel failed"))
        return res

    @router.post("/ladder/levels/{level_id}/reestablish")
    async def reestablish_level(level_id: int):
        """Bring a SKIPPED level back: places its BUY LIMIT now if price is above
        the level, else waits for price to recover above it first."""
        res = await get_engine().reestablish_level(level_id)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("reason", "re-establish failed"))
        return res

    @router.post("/lots/{lot_id}/exit")
    async def manual_exit(lot_id: int):
        eng = get_engine()
        ok = await eng.close_lot(lot_id, "MANUAL")
        if not ok:
            raise HTTPException(status_code=400, detail="exit did not complete — check logs")
        return {"ok": True}

    @router.post("/lots/{lot_id}/temp-exit")
    async def temp_exit(lot_id: int, req: TempActionReq):
        """Pause an OPEN rung. type=market → sell now; type=limit → arm a trigger
        that fires the pause when price falls to `price`; type=cancel → disarm."""
        eng = get_engine()
        if req.type == "cancel":
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                if lot is None:
                    raise HTTPException(404, "lot not found")
                lot.temp_exit_trigger_price = 0.0
                db.commit()
            return {"ok": True, "armed": False}
        if req.type == "limit":
            if not req.price or req.price <= 0:
                raise HTTPException(400, "limit temp-exit needs a positive price")
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                if lot is None:
                    raise HTTPException(404, "lot not found")
                if lot.status != "OPEN":
                    raise HTTPException(400, f"lot is {lot.status}, not OPEN")
                lot.temp_exit_trigger_price = float(req.price)
                db.commit()
                sym = lot.instrument.sym if lot.instrument else ""
            event_log.write("EXIT", f"⏸ limit temp-exit armed @ {req.price:g} for rung #{lot_id}", level="warn", sym=sym)
            return {"ok": True, "armed": True, "trigger_price": req.price}
        res = await eng.temp_exit_now(lot_id)
        if not res.get("ok"):
            raise HTTPException(400, res.get("reason", "temp-exit failed"))
        return res

    @router.post("/lots/{lot_id}/re-enter")
    async def re_enter(lot_id: int, req: TempActionReq):
        """Resume a TEMP_EXITED rung. type=market → buy now; type=limit → arm a
        trigger that re-enters when price falls to `price`; type=cancel → disarm."""
        eng = get_engine()
        if req.type == "cancel":
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                if lot is None:
                    raise HTTPException(404, "lot not found")
                lot.reenter_trigger_price = 0.0
                db.commit()
            return {"ok": True, "armed": False}
        if req.type == "limit":
            if not req.price or req.price <= 0:
                raise HTTPException(400, "limit re-enter needs a positive price")
            with db_session() as db:
                lot = db.get(Lot, lot_id)
                if lot is None:
                    raise HTTPException(404, "lot not found")
                if lot.status != "TEMP_EXITED":
                    raise HTTPException(400, f"lot is {lot.status}, not TEMP_EXITED")
                lot.reenter_trigger_price = float(req.price)
                db.commit()
                sym = lot.instrument.sym if lot.instrument else ""
            event_log.write("ORDER", f"▶ limit re-enter armed @ {req.price:g} for rung #{lot_id}", level="warn", sym=sym)
            return {"ok": True, "armed": True, "trigger_price": req.price}
        res = await eng.reenter_now(lot_id)
        if not res.get("ok"):
            raise HTTPException(400, res.get("reason", "re-enter failed"))
        return res

    # ----------------- chart data (candles + strategy markers) -----------------

    @router.get("/chart/{inst_id}")
    async def chart(inst_id: int, tf: int = 15, bars: int = 240):
        eng = get_engine()
        with db_session() as db:
            inst = db.get(Instrument, inst_id)
            if inst is None:
                raise HTTPException(status_code=404, detail="instrument not found")
            lots_rows = (db.query(Lot).filter(Lot.instrument_id == inst_id)
                         .filter(Lot.status.in_(["OPEN", "CLOSED", "EXTERNAL_CLOSED"]))
                         .order_by(Lot.entry_time.asc()).all())
        lookback = max(tf * bars, 60)
        try:
            candles = await eng.rest.get_candles(inst.exch, inst.token, min(tf, 240), min(lookback, 7 * 24 * 60))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"candle fetch failed: {e}")

        markers = []
        for l in lots_rows:
            if l.entry_time:
                markers.append({
                    "time": int(l.entry_time.replace(tzinfo=IST).timestamp()),
                    "position": "belowBar", "shape": "arrowUp", "color": "#10b981",
                    "text": f"buy {l.seq} ({l.lots} lot{'s' if l.lots != 1 else ''})",
                    "price": l.raw_entry_price or l.entry_price, "lot_id": l.id, "kind": "entry",
                })
            if l.status in ("CLOSED", "EXTERNAL_CLOSED") and l.exit_time:
                reason = (l.exit_reason or "exit").lower()
                label = {"target": "target", "stoploss": "SL", "ami_sell": "sell-signal",
                         "manual": "manual", "external": "external", "cb": "CB"}.get(reason, reason)
                color = "#f59e0b" if reason == "target" else ("#ef4444" if reason == "stoploss" else "#94a3b8")
                markers.append({
                    "time": int(l.exit_time.replace(tzinfo=IST).timestamp()),
                    "position": "aboveBar", "shape": "arrowDown", "color": color,
                    "text": f"{label} {l.seq} (buy {l.seq} closed)",
                    "price": l.exit_price, "lot_id": l.id, "kind": "exit",
                })
        markers.sort(key=lambda m: m["time"])
        open_levels = [
            {"lot_id": l.id, "seq": l.seq, "entry": l.entry_price,
             "target": l.target_price, "sl": l.sl_price}
            for l in lots_rows if l.status == "OPEN"
        ]
        return {"tsym": inst.tsym, "tf": tf, "candles": candles,
                "markers": markers, "open_levels": open_levels}

    # ----------------- account / P&L -----------------

    @router.get("/pnl")
    async def pnl():
        """Everything about the account & P&L for the P&L panel:
        - engine: our independent-lot ladder ledger (open + realized today/all-time)
        - broker: Shoonya's own settlement-aware P&L (per symbol + total), funds
          and margin, and the account profile — proxied through the gateway."""
        eng = get_engine()
        snap = eng.snapshot()
        # The database is off-site, so every query here is a WAN round trip and
        # the panel polls once a second. Today's realised figure and the open
        # rung count are already in the snapshot that was just built, so take
        # them from there; only the all-time total needs its own query, and it
        # moves rarely enough to hold briefly.
        realized_today = sum(float(i.get("realized_today") or 0.0) for i in snap["instruments"])
        open_rungs = sum(int(i.get("open_rungs") or 0) for i in snap["instruments"])
        now_m = _time.monotonic()
        if now_m - _realized_all_cache["at"] > 30.0:
            with db_session() as db:
                _realized_all_cache["value"] = float(
                    (db.query(func.coalesce(func.sum(Lot.realized_pnl), 0.0))
                       .filter(Lot.status.in_(["CLOSED", "EXTERNAL_CLOSED"])).scalar()) or 0.0)
            _realized_all_cache["at"] = now_m
        realized_all = _realized_all_cache["value"]
        engine_block = {
            "total_open_pnl": snap["total_open_pnl"],
            "realized_today": round(float(realized_today), 2),
            "realized_all_time": round(float(realized_all), 2),
            "net_today": round(snap["total_open_pnl"] + float(realized_today), 2),
            "open_rungs": open_rungs,
            "per_instrument": [{
                "sym": i["sym"], "exch": i["exch"], "tsym": i["tsym"],
                "mode": i["mode"], "instr_type": i.get("instr_type", "FUT"),
                "open_pnl": i["open_pnl"], "realized_today": i["realized_today"],
                "open_rungs": i["open_rungs"],
            } for i in snap["instruments"]],
        }

        # Served from the engine's cached account snapshot and refreshed behind
        # this call. Fetching inline meant three sequential broker round trips
        # per poll, so the panel sat frozen for seconds while the rest of the
        # dashboard stayed live. `age_seconds` travels with the data so the UI
        # can show how fresh it is instead of implying it is current.
        broker: dict = {"connected": snap["broker_connected"], "error": ""}
        bs = await eng.broker_snapshot()
        broker["positions"] = bs.get("positions") or {"symbol_groups": [], "total_pnl": 0.0}
        broker["funds"] = bs.get("funds")
        broker["account"] = bs.get("account")
        broker["error"] = bs.get("error") or ""
        broker["age_seconds"] = (round(_time.monotonic() - bs["at"], 1) if bs.get("at") else None)
        return {"engine": engine_block, "broker": broker}

    # ----------------- logs / signals / rollovers -----------------

    @router.get("/logs")
    def logs(limit: int = 5, offset: int = 0, sym: str = "", level: str = "",
                   category: str = ""):
        with db_session() as db:
            q = db.query(LogEntry)
            if sym:
                q = q.filter(func.upper(LogEntry.sym) == sym.upper())
            if level:
                q = q.filter(LogEntry.level == level)
            if category:
                q = q.filter(LogEntry.category == category)
            total = q.count()
            rows = q.order_by(LogEntry.ts.desc()).offset(offset).limit(min(limit, 500)).all()
        return {"total": total, "logs": [
            {"id": r.id, "ts": r.ts.isoformat(timespec="seconds") if r.ts else "",
             "level": r.level, "category": r.category, "sym": r.sym,
             "message": r.message, "data": r.data or {}} for r in rows]}

    @router.get("/signals")
    def signals(limit: int = 50):
        with db_session() as db:
            rows = db.query(Signal).order_by(Signal.ts.desc()).limit(min(limit, 200)).all()
        return [{"id": s.id, "ts": s.ts.isoformat(timespec="seconds") if s.ts else "",
                 "sym": s.sym, "action": s.action, "timeframe_min": s.timeframe_min,
                 "source": s.source, "status": s.status, "reason": s.reason,
                 "lot_id": s.lot_id} for s in rows]

    @router.get("/rollovers")
    def rollovers(limit: int = 50):
        with db_session() as db:
            rows = (db.query(RolloverEvent, Instrument.sym)
                    .join(Instrument, RolloverEvent.instrument_id == Instrument.id)
                    .order_by(RolloverEvent.ts.desc()).limit(limit).all())
        return [{"id": r.id, "ts": r.ts.isoformat(timespec="seconds") if r.ts else "", "sym": s,
                 "lot_id": r.lot_id, "old_tsym": r.old_tsym, "new_tsym": r.new_tsym,
                 "old_exit": r.old_exit_price, "new_entry": r.new_entry_price,
                 "basis": r.basis, "qty": r.qty, "status": r.status} for r, s in rows]

    # ----------------- econ events -----------------

    @router.get("/events")
    def events(days: int = 14):
        now = datetime.now(IST).replace(tzinfo=None)
        with db_session() as db:
            rows = (db.query(EconEvent)
                    .filter(EconEvent.dt >= now - timedelta(hours=6),
                            EconEvent.dt <= now + timedelta(days=days))
                    .order_by(EconEvent.dt.asc()).all())
        return [{"id": e.id, "dt": e.dt.isoformat(timespec="minutes"), "title": e.title,
                 "impact": e.impact, "region": e.region, "note": e.note, "auto": e.auto}
                for e in rows]

    @router.post("/events", status_code=201)
    def add_event(req: EventCreate):
        try:
            dt = datetime.fromisoformat(req.dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="dt must be ISO format")
        with db_session() as db:
            db.add(EconEvent(dt=dt, title=req.title, impact=req.impact,
                             region=req.region, note=req.note, auto=False))
            db.commit()
        return {"ok": True}

    @router.delete("/events/{event_id}")
    def delete_event(event_id: int):
        with db_session() as db:
            e = db.get(EconEvent, event_id)
            if e:
                db.delete(e)
                db.commit()
        return {"ok": True}

    return router


def _neg_time(iso: str) -> str:
    """Descending-time sort key inside an ascending group sort."""
    if not iso:
        return "0"
    return "".join(chr(255 - ord(c)) if c.isdigit() else c for c in iso)


def build_ws_router(get_engine, register_ws, unregister_ws) -> APIRouter:
    wsr = APIRouter()

    @wsr.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        # Browsers can't set Authorization headers on a WebSocket, so the Gateway
        # user JWT is passed as ?token=. Reject before accepting if it's invalid.
        from core.security import user_id_from_token
        if user_id_from_token(websocket.query_params.get("token") or "") is None:
            await websocket.close(code=1008)  # policy violation
            return
        await websocket.accept()
        register_ws(websocket)
        try:
            await websocket.send_text(json.dumps({"type": "snapshot",
                                                  "data": get_engine().snapshot()}))
            while True:
                await websocket.receive_text()   # keepalive pings from the UI
        except WebSocketDisconnect:
            pass
        finally:
            unregister_ws(websocket)

    return wsr
