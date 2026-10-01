"""Futures rollover: WHEN to roll (dates + intraday liquidity windows) and
HOW to roll (per-lot, basis-adjusted, verified on both legs).

WHY the timing rules (shown to the user in the ⓘ tooltip):

MCX energy (NATURALGAS, CRUDEOIL…) shadows NYMEX. Indian daytime books are
thin; both the expiring and next-month books are deepest while the US morning
session runs — 19:30–21:30 IST — which pins the calendar spread to its
tightest (often 10–20 paise on NG). Exception: on Thursdays the EIA Natural
Gas Storage Report lands in that window (~20:00 IST) and the spread whips
violently — wait until 21:00 IST for the market to digest it.

MCX metals (COPPER, GOLD, SILVER, ZINC…) key off COMEX/LME; 18:30–21:00 IST
has both US metals and late-LME flow. Also fine: 16:00–17:00 (LME official
prices set).

NSE index futures (NIFTY, BANKNIFTY): roll volume peaks on expiry-1; the
calendar spread is most stable mid-morning 10:15–11:30 (after the opening
auction noise dies) and early afternoon 14:15–15:00 (before expiry-day close
games). Avoid 09:15–09:45 entirely.

Roll dates default to: energy = expiry−2 (MCX NG enters delivery-intention
period and day volume migrates ~2 days out), metals = expiry−3 (tender period
starts earlier), index = expiry−1 (that IS the liquidity peak). Weekend-aware:
dates snap backwards onto a weekday. Override per instrument via
`rollover_days_before`.

BASIS MATH (the part that keeps P&L / target / SL exact):

    basis = new_contract_fill − old_contract_exit      (the cost of the roll)

    entry'  = entry  + basis
    target' = target + basis
    sl'     = sl     + basis

Proof of continuity: after rolling, lot P&L = (ltp_new − entry')
  = (ltp_new − new_fill) + (old_exit − entry)
  = P&L locked on the old leg + live P&L on the new leg.  Nothing gained,
nothing lost, and the target still pays out exactly the originally intended
₹ distance. Same argument for the stop.
"""

import calendar
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_HOLIDAY_FILE = Path(__file__).resolve().parent.parent / "config" / "holidays.json"
_holiday_cache: dict[str, set[date]] | None = None


def _load_holidays() -> dict[str, set[date]]:
    """config/holidays.json → {'common': {dates}, 'NFO': {...}, 'MCX': {...}}.
    Missing/broken file degrades to weekend-only handling (never crashes)."""
    global _holiday_cache
    if _holiday_cache is not None:
        return _holiday_cache
    out: dict[str, set[date]] = {"common": set(), "NFO": set(), "MCX": set()}
    try:
        raw = json.loads(_HOLIDAY_FILE.read_text())
        for key in out:
            for s in raw.get(key, []):
                try:
                    out[key].add(date.fromisoformat(s))
                except ValueError:
                    continue
    except Exception as e:  # noqa: BLE001
        logging.getLogger("grid.rollover").warning("holiday calendar unavailable: %s", e)
    _holiday_cache = out
    return out


def is_trading_holiday(d: date, exch: str = "") -> bool:
    h = _load_holidays()
    return d in h["common"] or (exch and d in h.get(exch, set()))


def is_trading_day(d: date, exch: str = "") -> bool:
    return d.weekday() < 5 and not is_trading_holiday(d, exch)

ENERGY = {"NATURALGAS", "NATGASMINI", "CRUDEOIL", "CRUDEOILM"}
METALS = {"GOLD", "GOLDM", "GOLDPETAL", "SILVER", "SILVERM", "SILVERMIC",
          "COPPER", "ZINC", "ZINCMINI", "LEAD", "LEADMINI", "ALUMINIUM", "ALUMINI", "NICKEL"}


def instrument_class(exch: str, sym: str) -> str:
    s = (sym or "").upper()
    if exch == "MCX":
        if s in ENERGY:
            return "energy"
        if s in METALS:
            return "metal"
        return "mcx_other"
    return "index"


def default_days_before(cls: str) -> int:
    return {"energy": 2, "metal": 3, "mcx_other": 2, "index": 1}[cls]


def default_slippage_threshold(cls: str) -> float:
    """Max acceptable TOTAL roll slippage in price points — (ask−bid)_old +
    (ask−bid)_new. The Sniper waits inside the liquidity window until the
    combined spread is at/below this before firing the roll. Per class because
    tick sizes / price scales differ (NG trades ~₹200 at 0.10 tick, GOLD
    ~₹70,000, NIFTY in index points)."""
    return {"energy": 0.20, "metal": 0.60, "mcx_other": 0.40, "index": 1.00}.get(cls, 0.40)


def parse_expiry(expd: str) -> date | None:
    """Broker format '28-JUL-2025' (case-insensitive month)."""
    if not expd:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(expd.strip().title(), fmt).date()
        except ValueError:
            continue
    return None


def rollover_date(expiry: date, days_before: int, exch: str = "") -> date:
    """Weekend- AND holiday-aware: lands on the previous trading day when the
    scheduled date is a Sat/Sun or an exchange holiday."""
    d = expiry - timedelta(days=days_before)
    while not is_trading_day(d, exch):
        d -= timedelta(days=1)
    return d


@dataclass
class WindowCheck:
    open_now: bool
    reason: str
    windows_text: str      # human description for tooltip/UI
    end: time | None = None  # end-of-day time of the CURRENTLY-OPEN window (for the sniper failsafe); None when closed


def window_for(cls: str, now: datetime) -> WindowCheck:
    """Is `now` (IST) inside the preferred roll window for this class? When open,
    `end` is the closing time of that window so the Sniper knows its deadline."""
    t = now.time()
    dow = now.weekday()  # 0=Mon … 3=Thu

    def within(a: time, b: time) -> bool:
        return a <= t <= b

    if cls == "energy":
        text = ("Best window 19:30–21:30 IST (US morning session — both contract books "
                "deepest, spread tightest). Thursday: EIA gas storage report ≈20:00 IST — "
                "wait until 21:00, roll 21:00–22:00.")
        if dow == 3:  # Thursday — EIA storage day
            if within(time(21, 0), time(22, 0)):
                return WindowCheck(True, "Thu post-EIA window (21:00–22:00 IST)", text, end=time(22, 0))
            if within(time(19, 30), time(21, 0)):
                return WindowCheck(False, "blocked: EIA storage report window (Thu 19:30–21:00 IST)", text)
            return WindowCheck(False, "outside Thu roll window (21:00–22:00 IST)", text)
        if within(time(19, 30), time(21, 30)):
            return WindowCheck(True, "US-morning liquidity window (19:30–21:30 IST)", text, end=time(21, 30))
        return WindowCheck(False, "outside 19:30–21:30 IST liquidity window", text)

    if cls in ("metal", "mcx_other"):
        text = ("Best window 18:30–21:00 IST (COMEX open + late LME — deepest MCX metal "
                "books). Secondary: 16:00–17:00 IST after LME official prices.")
        if within(time(18, 30), time(21, 0)):
            return WindowCheck(True, "COMEX/LME overlap window (18:30–21:00 IST)", text, end=time(21, 0))
        if within(time(16, 0), time(17, 0)):
            return WindowCheck(True, "post-LME-officials window (16:00–17:00 IST)", text, end=time(17, 0))
        return WindowCheck(False, "outside metal liquidity windows (18:30–21:00 / 16:00–17:00 IST)", text)

    # index futures
    text = ("Best windows 10:15–11:30 IST (post-open, calendar spread stable) and "
            "14:15–15:00 IST. Avoid 09:15–09:45 opening noise and the expiry-day "
            "close. Roll volume peaks on expiry−1.")
    if within(time(10, 15), time(11, 30)):
        return WindowCheck(True, "mid-morning window (10:15–11:30 IST)", text, end=time(11, 30))
    if within(time(14, 15), time(15, 0)):
        return WindowCheck(True, "early-afternoon window (14:15–15:00 IST)", text, end=time(15, 0))
    return WindowCheck(False, "outside index roll windows (10:15–11:30 / 14:15–15:00 IST)", text)


def tooltip_for(exch: str, sym: str, expiry_str: str, days_before: int) -> str:
    cls = instrument_class(exch, sym)
    exp = parse_expiry(expiry_str)
    rd = rollover_date(exp, days_before, exch) if exp else None
    why_date = {
        "energy": f"expiry−{days_before}: MCX energy day-volume migrates to the next contract "
                  "~2 sessions before expiry as delivery intentions open; holding later pays a "
                  "widening, illiquid spread.",
        "metal": f"expiry−{days_before}: metal tender/delivery period starts earlier — front-month "
                 "depth decays ~3 sessions out.",
        "mcx_other": f"expiry−{days_before} trading days, weekend/holiday-adjusted.",
        "index": f"expiry−{days_before}: index roll volume and tightest calendar spreads peak the "
                 "day before expiry.",
    }[cls]
    win = window_for(cls, datetime.now(IST))
    parts = [f"Roll date {rd.strftime('%d-%b-%Y') if rd else '—'} — {why_date}", win.windows_text]
    return "\n".join(parts)


def month_expiry_guess(exch: str, year: int, month: int) -> date:
    """Fallback only (real expiry always comes from the scripmaster):
    NSE index monthlies expire on the last Tuesday (post Sep-2025 regime);
    MCX varies by commodity so use month-end minus buffer."""
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    if exch != "MCX":
        while d.weekday() != 1:  # Tuesday
            d -= timedelta(days=1)
    return d
