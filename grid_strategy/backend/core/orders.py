"""Order execution with explicit fill verification.

Golden rule: NEVER assume an order filled because the API accepted it. Every
order goes through:

    place → poll the broker order book (fast backoff) until a terminal
    status → report actual fillshares + avgprc

The gateway's ticker WebSocket also pushes order_update events; the engine
calls `notify_order_update()` on those, which wakes the poller immediately
instead of waiting for the next scheduled poll.

Outcomes:
  COMPLETE  — verified fill, with actual avg price and quantity
  PARTIAL   — terminal but only part filled (rest cancelled/rejected)
  REJECTED / CANCELLED — nothing filled
  TIMEOUT   — book never showed a terminal status; caller must treat the
              position as UNCONFIRMED and let the reconciler resolve it.
"""

import asyncio
import logging
from dataclasses import dataclass

from config.settings import settings
from gateway_client.rest import GatewayRest

logger = logging.getLogger(__name__)

TERMINAL = {"COMPLETE", "REJECTED", "CANCELED", "CANCELLED", "INVALID"}


@dataclass
class FillReport:
    order_id: str
    status: str            # COMPLETE | PARTIAL | REJECTED | CANCELLED | TIMEOUT | ERROR
    filled_qty: int = 0    # units actually filled
    avg_price: float = 0.0
    requested_qty: int = 0
    raw_status: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("COMPLETE", "PARTIAL") and self.filled_qty > 0


class OrderManager:
    def __init__(self, rest: GatewayRest):
        self.rest = rest
        self._wakeups: dict[str, asyncio.Event] = {}   # order_id → poke from ws order_update

    def notify_order_update(self, order_id: str) -> None:
        ev = self._wakeups.get(order_id)
        if ev is not None:
            ev.set()

    async def execute(self, *, side: str, exchange: str, tradingsymbol: str,
                      qty_units: int, remarks: str, price_type: str = "MKT",
                      price: float = 0.0, product_type: str | None = None) -> FillReport:
        """Place + verify one order. qty_units is in exchange units (lots × lot_size
        for MCX where lotsize>1 quantity is still submitted in units — matches the
        existing gateway/UI convention). product_type defaults to the engine-wide
        setting; pass "M" (NRML/delivery) or "I" (MIS/intraday) to override per
        instrument — exits MUST use the same product type the entry used."""
        try:
            order_id = await self.rest.place_order(
                side=side, exchange=exchange, tradingsymbol=tradingsymbol,
                quantity=qty_units, price_type=price_type, price=price,
                remarks=remarks, product_type=product_type or settings.product_type,
            )
        except Exception as e:
            return FillReport(order_id="", status="ERROR", requested_qty=qty_units, error=str(e))
        if not order_id:
            return FillReport(order_id="", status="ERROR", requested_qty=qty_units,
                              error="gateway returned no order id")
        report = await self.verify(order_id, qty_units)
        return report

    async def verify(self, order_id: str, requested_qty: int) -> FillReport:
        """Poll the order book until terminal status or timeout."""
        ev = asyncio.Event()
        self._wakeups[order_id] = ev
        waited = 0.0
        last: dict | None = None
        try:
            for delay in settings.order_poll_schedule:
                # wake early if the broker pushed an order_update for this id
                try:
                    await asyncio.wait_for(ev.wait(), timeout=delay)
                    ev.clear()
                except asyncio.TimeoutError:
                    pass
                waited += delay
                try:
                    last = await self.rest.find_order(order_id)
                except Exception as e:
                    logger.warning("order book fetch failed while verifying %s: %s", order_id, e)
                    continue
                if last is None:
                    continue
                status = (last.get("status") or "").upper()
                if status in TERMINAL:
                    return self._report_from_row(order_id, last, requested_qty)
                if waited >= settings.order_verify_timeout:
                    break
            # timed out — one last look
            if last is not None and (last.get("status") or "").upper() in TERMINAL:
                return self._report_from_row(order_id, last, requested_qty)
            return FillReport(order_id=order_id, status="TIMEOUT", requested_qty=requested_qty,
                              raw_status=(last or {}).get("status", ""),
                              filled_qty=_fillshares(last), avg_price=_avgprc(last))
        finally:
            self._wakeups.pop(order_id, None)

    def _report_from_row(self, order_id: str, row: dict, requested_qty: int) -> FillReport:
        status = (row.get("status") or "").upper()
        filled = _fillshares(row)
        avg = _avgprc(row)
        if status == "COMPLETE" and filled >= requested_qty:
            return FillReport(order_id=order_id, status="COMPLETE", filled_qty=filled,
                              avg_price=avg, requested_qty=requested_qty, raw_status=status)
        if status == "COMPLETE":
            # Broker says COMPLETE (it FILLED) but the fill qty is missing/short.
            # NEVER treat this as CANCELLED — that re-arms the level and double-buys
            # a position that already exists. Partial fill → PARTIAL; zero/absent
            # fill data → TIMEOUT so the reconciler confirms it against the broker
            # (and the caller marks the lot UNCONFIRMED, which does NOT re-arm).
            if filled > 0:
                return FillReport(order_id=order_id, status="PARTIAL", filled_qty=filled,
                                  avg_price=avg, requested_qty=requested_qty, raw_status=status)
            return FillReport(order_id=order_id, status="TIMEOUT", filled_qty=0, avg_price=avg,
                              requested_qty=requested_qty, raw_status=status,
                              error="COMPLETE but no fill qty reported — reconciler will confirm")
        if filled > 0:
            # partial fill: broker filled some units then rejected/cancelled the rest
            return FillReport(order_id=order_id, status="PARTIAL", filled_qty=filled,
                              avg_price=avg, requested_qty=requested_qty, raw_status=status)
        final = "REJECTED" if status == "REJECTED" else "CANCELLED"
        return FillReport(order_id=order_id, status=final, filled_qty=0, avg_price=0.0,
                          requested_qty=requested_qty, raw_status=status,
                          error=row.get("rejreason", "") or row.get("emsg", ""))


def _fillshares(row: dict | None) -> int:
    if not row:
        return 0
    try:
        return int(float(row.get("fillshares", "0") or "0"))
    except (TypeError, ValueError):
        return 0


def _avgprc(row: dict | None) -> float:
    if not row:
        return 0.0
    try:
        return float(row.get("avgprc", "0") or "0")
    except (TypeError, ValueError):
        return 0.0
