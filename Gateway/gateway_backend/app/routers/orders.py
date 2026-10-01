import asyncio
import logging
import math
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.core import state
from app.core.deps import _active_broker_proxy, _require_legacy_auth
from app.core.security import Principal, get_principal
from app.schemas import OrderBookResponse, OrderItem, PlaceOrderRequest
from app.services.reconciliation import (
    _is_exit_remarks,
    _norm_prd,
    _reconcile_lots_from_orderbook,
    _short_poll_reconcile,
)
from brokers.base import BrokerError, SessionToken
from brokers.registry import get_broker
from brokers.shoonya.scripmaster import get_scripmaster
from db.engine import get_db
from db.models import ClosedTradeArchive, LotExit, OrderLot, Service, SymbolTarget

router = APIRouter()
logger = logging.getLogger(__name__)


# Shoonya's RMS blocks plain MARKET orders for API/algo flow (ALGO_CHK). We
# transparently convert MKT → a MARKETABLE LIMIT (a limit priced through the
# touch) which fills like a market order but passes the check. Toggle off with
# SHOONYA_MKT_TO_LIMIT=0 if the account later gets MKT-for-API enabled.
_MKT_TO_LIMIT = os.getenv("SHOONYA_MKT_TO_LIMIT", "1") not in ("0", "false", "False", "")
# How far THROUGH the touch a converted MKT order prices itself.
#
# A marketable limit only has to cross the spread; every point beyond that is
# pure permitted slippage, because the limit lets the order fill all the way
# down to it if the book is thin. A percentage does not express that: 0.3% is
# under a rupee on a ₹250 contract and ~₹47 on a ₹15,500 one, so the same
# setting was a reasonable buffer on natural gas and a 47-point giveaway on
# gold. Cross by a few TICKS, and keep the percentage only as a ceiling for
# instruments whose tick is coarse relative to their price.
_MARKETABLE_BUFFER_PCT = float(os.getenv("SHOONYA_MARKETABLE_BUFFER_PCT", "0.003") or 0.003)
_MARKETABLE_TICKS = max(int(os.getenv("SHOONYA_MARKETABLE_TICKS", "3") or 3), 0)


def _tick_of(exchange: str, tsym: str) -> float:
    """Instrument tick size from the scripmaster."""
    sm = get_scripmaster()
    scrip = sm.master.get(f"{exchange}|{tsym}", {}) if hasattr(sm, "master") else {}
    try:
        return float(scrip.get("ticksize") or 0) or 0.05
    except (TypeError, ValueError):
        return 0.05


def _snap_to_tick(price: float, exchange: str, tsym: str) -> float:
    """Snap a limit price onto the instrument's tick grid.

    Shoonya rejects off-grid limit prices outright, so a hand-typed 412.53 on a
    0.05-tick contract is a rejected order at exactly the moment the user
    wanted to trade. Nearest-tick keeps the user's intent (unlike the
    marketable path, which deliberately rounds through the touch).
    """
    if price <= 0:
        return price
    tick = _tick_of(exchange, tsym)
    return round(round(price / tick) * tick, 2) if tick > 0 else round(price, 2)


async def _marketable_limit_price(broker, token_obj, credentials, exchange: str,
                                  tsym: str, side: str):
    """Compute a marketable LIMIT price for a would-be MKT order: buy at/through
    the ask, sell at/through the bid, plus a small buffer, rounded to the tick.
    Returns (price, tick) or (None, None) if no live price is available."""
    sm = get_scripmaster()
    tok = (sm.get_token(exchange, tsym) or "") if hasattr(sm, "get_token") else ""
    bid = ask = ltp = tick_q = 0.0
    try:
        q = await broker.getQuote(token_obj, credentials, symbol=tok, exchange=exchange)
        bid = float(q.get("bp1") or 0)
        ask = float(q.get("sp1") or 0)
        ltp = float(q.get("lp") or 0)
        tick_q = float(q.get("ti") or 0) or 0.0
    except Exception:
        pass
    need_ask = side == "B"
    ref = (ask or ltp) if need_ask else (bid or ltp)
    if not ref or ref <= 0:
        return None, None
    scrip = sm.master.get(f"{exchange}|{tsym}", {}) if hasattr(sm, "master") else {}
    try:
        tick = float(scrip.get("ticksize") or 0) or tick_q or 0.05
    except (TypeError, ValueError):
        tick = tick_q or 0.05
    # Cross by whole ticks, never further than the percentage ceiling. `min` is
    # the point: on a coarse-tick instrument the ticks win, on a high-priced one
    # the percentage stops a few ticks becoming a large rupee concession.
    tick_buf = tick * _MARKETABLE_TICKS
    pct_buf = ref * _MARKETABLE_BUFFER_PCT
    buf = min(tick_buf, pct_buf) if tick_buf > 0 else pct_buf
    raw = ref + buf if need_ask else max(ref - buf, tick)
    # buy rounds UP to a tick, sell rounds DOWN → stays marketable after rounding
    steps = math.ceil(raw / tick) if need_ask else math.floor(raw / tick)
    return round(max(steps, 1) * tick, 2), tick


@router.post("/api/orders")
async def place_order(req: PlaceOrderRequest, principal: Principal = Depends(get_principal)):
    """Place a new order.

    For entry orders (remarks not flagged as exit), creates an OrderLot row and
    tags the broker order with `lot:<client_ref>` so fills can be reconciled
    back to the originating lot. Entry lots are stamped with `source_service`
    when placed by a machine service (e.g. grid) so the Gateway UI can badge
    where each order came from; human-placed orders leave it NULL.
    """
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    token = SessionToken(
        token=auth.auth_token,
        broker_uid=auth.user_id,
        issued_at="",
        broker_name="shoonya",
    )
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id,
                   **_active_broker_proxy("shoonya")}

    if req.quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")
    if req.price_type == "LMT":
        if req.price is None or req.price <= 0:
            raise HTTPException(status_code=400,
                                detail="a LMT order needs price > 0 (use MKT for a market order)")
        snapped = _snap_to_tick(req.price, req.exchange, req.tradingsymbol)
        if snapped != req.price:
            logger.info("tick-snapped LMT price for %s: %.4f → %.2f",
                        req.tradingsymbol, req.price, snapped)
            req.price = snapped

    # MKT is rejected by this account's RMS (ALGO_CHK) → convert to a marketable
    # LIMIT that fills like a market order but is accepted for API orders.
    if _MKT_TO_LIMIT and req.price_type == "MKT":
        px, tick = await _marketable_limit_price(broker, token, credentials,
                                                 req.exchange, req.tradingsymbol, req.buy_or_sell)
        if px and px > 0:
            logger.info("MKT→marketable LMT: %s %s → %.2f (tick %s, +/-%.2g%%)",
                        req.buy_or_sell, req.tradingsymbol, px, tick, _MARKETABLE_BUFFER_PCT * 100)
            req.price_type = "LMT"
            req.price = px
        else:
            logger.warning("MKT→LMT: no live price for %s — sending MKT (may hit ALGO_CHK)",
                           req.tradingsymbol)

    is_entry = not _is_exit_remarks(req.remarks)

    # A plain opposite-side order for a symbol that already has an open lot is,
    # at the broker, a close — MIS/NRML nets same-product positions automatically,
    # there is no such thing as simultaneously long AND short one symbol/product.
    # Without this lookup such an order fell through as a brand-new ENTRY lot: a
    # phantom lot until _offset_opposing_lots later netted it against the real
    # position, and even then only the older lot got the realized PnL while the
    # newer one closed at ₹0 — two rows in Order History for one round-trip
    # instead of the real exit being booked against the position it closed.
    matched_lot: OrderLot | None = None
    if is_entry and req.reentry_source_lot_id is None:
        db_lookup = next(get_db())
        try:
            opposite_side = "S" if req.buy_or_sell == "B" else "B"
            candidates = (
                db_lookup.query(OrderLot)
                .filter(
                    OrderLot.owner_uid == auth.user_id,
                    OrderLot.exch == req.exchange,
                    OrderLot.tsym == req.tradingsymbol,
                    OrderLot.side == opposite_side,
                    OrderLot.status.in_(["OPEN", "PARTIAL"]),
                    OrderLot.open_qty >= req.quantity,
                )
                .order_by(OrderLot.opened_at.asc())
                .all()
            )
            matched_lot = next(
                (c for c in candidates if _norm_prd(c.product_type) == _norm_prd(req.product_type)),
                None,
            )
        finally:
            db_lookup.close()
        if matched_lot is not None:
            is_entry = False

    lot: OrderLot | None = None
    exit_record: LotExit | None = None
    db = None
    # Snapshot of a recycled CLOSED lot's fields, so a broker failure can put
    # the row back the way it was instead of leaving a half-reopened lot.
    reentry_snapshot: dict | None = None
    # Denormalized snapshot of the finished round-trip a CLOSED re-entry is about
    # to overwrite. Built before the row is recycled; inserted into
    # ClosedTradeArchive only once the broker accepts the re-entry, so the
    # all-time Order History keeps the closed trade the recycle would otherwise
    # erase (see ClosedTradeArchive).
    archive_data: dict | None = None
    if is_entry:
        sm = get_scripmaster()
        tok = sm.get_token(req.exchange, req.tradingsymbol) or ""
        scrip = sm.master.get(f"{req.exchange}|{req.tradingsymbol}", {}) if hasattr(sm, "master") else {}
        try:
            lotsize = max(int(scrip.get("lotsize", "1") or "1"), 1)
        except Exception:
            lotsize = 1
        client_ref = f"lot{uuid.uuid4().hex[:12]}"
        db = next(get_db())

        # Stamp which service placed this order (human callers → NULL). The
        # service JWT's subject is its client_id; map it to the Service.name so
        # the UI badges by a stable, readable name ("grid" → GR).
        source_service: str | None = None
        if principal.kind == "service":
            svc = db.query(Service).filter_by(client_id=principal.client_id).first()
            source_service = svc.name if svc else (principal.client_id or "service")

        carried_pnl = 0.0
        is_reentry = False
        recycle_lot: OrderLot | None = None
        if req.reentry_source_lot_id is not None:
            source_lot = db.query(OrderLot).filter(
                OrderLot.id == req.reentry_source_lot_id,
                OrderLot.owner_uid == auth.user_id,
            ).first()
            if source_lot is not None:
                is_reentry = True
                if source_lot.status == "CLOSED":
                    # Re-entering a CLOSED position: reuse the existing row in
                    # place instead of adding a new one, so no duplicate lot
                    # appears in the UI. Its prior booked result rides forward as
                    # carried_pnl (same "continue from where the last leg left
                    # off" math as a rollover, pnl.py:_lot_live_pnl).
                    carried_pnl = (source_lot.realized_pnl or 0.0) + (source_lot.carried_pnl or 0.0)
                    recycle_lot = source_lot
                # Re-entering an OPEN/PARTIAL lot instead creates a fresh row
                # (that position is still live), and carries no P&L — its own
                # realized_pnl isn't "done" yet.
            else:
                logger.warning(
                    "place_order: reentry_source_lot_id %s not found for user %s; placing without P&L carry",
                    req.reentry_source_lot_id, auth.user_id,
                )

        if recycle_lot is not None:
            # Preserve the closed round-trip's state in case the broker rejects
            # this re-entry and we have to restore the row (see _fail_lot below).
            reentry_snapshot = {
                "exch": recycle_lot.exch, "tsym": recycle_lot.tsym, "token": recycle_lot.token,
                "lotsize": recycle_lot.lotsize, "product_type": recycle_lot.product_type,
                "side": recycle_lot.side, "entry_qty": recycle_lot.entry_qty,
                "open_qty": recycle_lot.open_qty, "avg_entry_price": recycle_lot.avg_entry_price,
                "client_ref": recycle_lot.client_ref, "status": recycle_lot.status,
                "broker_entry_orderid": recycle_lot.broker_entry_orderid,
                "opened_at": recycle_lot.opened_at, "closed_at": recycle_lot.closed_at,
                "description": recycle_lot.description,
                "target_enabled": recycle_lot.target_enabled, "target_value": recycle_lot.target_value,
                "realized_pnl": recycle_lot.realized_pnl, "carried_pnl": recycle_lot.carried_pnl,
                "is_reentry": recycle_lot.is_reentry,
                "is_temp_exit": recycle_lot.is_temp_exit,
                "reentry_source_lot_id": recycle_lot.reentry_source_lot_id,
            }
            # Snapshot the finished round-trip for the all-time history archive,
            # computed here while the original fields and exits are still intact
            # (both are about to be overwritten / deleted below). The weighted
            # avg exit price mirrors _serialize_lot's Exit-column math; it's 0
            # for an offset-closed lot that never had explicit LotExit rows.
            _ex_notional = 0.0
            _ex_filled = 0
            for ex in recycle_lot.exits:
                fq = int(ex.filled_qty or 0)
                if fq > 0 and (ex.avg_exit_price or 0.0) > 0:
                    _ex_notional += fq * float(ex.avg_exit_price)
                    _ex_filled += fq
            archive_data = {
                "owner_uid": recycle_lot.owner_uid,
                "source_lot_id": recycle_lot.id,
                "exch": recycle_lot.exch,
                "tsym": recycle_lot.tsym,
                "token": recycle_lot.token,
                "lotsize": recycle_lot.lotsize,
                "product_type": recycle_lot.product_type,
                "side": recycle_lot.side,
                "entry_qty": recycle_lot.entry_qty,
                "avg_entry_price": recycle_lot.avg_entry_price or 0.0,
                "avg_exit_price": (_ex_notional / _ex_filled) if _ex_filled > 0 else 0.0,
                "realized_pnl": recycle_lot.realized_pnl or 0.0,
                "carried_pnl": recycle_lot.carried_pnl or 0.0,
                "opened_at": recycle_lot.opened_at,
                "closed_at": recycle_lot.closed_at,
                "is_reentry": bool(recycle_lot.is_reentry),
                "is_rollover": bool(recycle_lot.is_rollover),
                "is_temp_exit": bool(recycle_lot.is_temp_exit),
                "source_service": recycle_lot.source_service,
                "description": recycle_lot.description or "",
            }
            # Prior per-exit records belong to the finished round-trip; their net
            # is already folded into carried_pnl. Drop them so the reopened leg
            # starts with a clean Exit column and no stale exit average.
            for ex in list(recycle_lot.exits):
                db.delete(ex)
            recycle_lot.exch = req.exchange
            recycle_lot.tsym = req.tradingsymbol
            recycle_lot.token = tok
            recycle_lot.lotsize = lotsize
            recycle_lot.product_type = req.product_type
            recycle_lot.side = req.buy_or_sell
            recycle_lot.entry_qty = req.quantity
            recycle_lot.open_qty = req.quantity
            recycle_lot.avg_entry_price = req.price if req.price_type == "LMT" else 0.0
            recycle_lot.client_ref = client_ref
            recycle_lot.status = "PENDING"
            recycle_lot.broker_entry_orderid = ""
            recycle_lot.opened_at = datetime.utcnow()
            recycle_lot.closed_at = None
            recycle_lot.description = (req.description or "").strip()
            recycle_lot.target_enabled = bool(req.target_enabled and req.target_value > 0)
            recycle_lot.target_value = req.target_value if req.target_enabled else 0.0
            recycle_lot.realized_pnl = 0.0
            recycle_lot.carried_pnl = carried_pnl
            recycle_lot.is_reentry = True
            # Re-entering consumes the TE tag: the row is live again, so it no
            # longer needs the closed-today-filter exemption (the tag "changes").
            recycle_lot.is_temp_exit = False
            recycle_lot.reentry_source_lot_id = recycle_lot.id
            recycle_lot.source_service = source_service
            lot = recycle_lot
        else:
            lot = OrderLot(
                owner_uid=auth.user_id,
                exch=req.exchange,
                tsym=req.tradingsymbol,
                token=tok,
                lotsize=lotsize,
                product_type=req.product_type,
                side=req.buy_or_sell,
                entry_qty=req.quantity,
                open_qty=req.quantity,
                avg_entry_price=req.price if req.price_type == "LMT" else 0.0,
                client_ref=client_ref,
                status="PENDING",
                description=(req.description or "").strip(),
                # Per-order target rides with this lot; it goes live once the lot
                # fills (a PENDING lot can't auto-exit — there's nothing open yet).
                target_enabled=bool(req.target_enabled and req.target_value > 0),
                target_value=req.target_value if req.target_enabled else 0.0,
                carried_pnl=carried_pnl,
                is_reentry=is_reentry,
                reentry_source_lot_id=req.reentry_source_lot_id if is_reentry else None,
                source_service=source_service,
            )
            db.add(lot)
        db.commit()
        db.refresh(lot)
    elif matched_lot is not None:
        # Route this order as a close against the existing opposite-side lot,
        # the same way POST /api/lots/{id}/exit does, instead of opening a new
        # independent lot that a later offset pass would have to net out.
        db = next(get_db())
        lot = db.query(OrderLot).filter_by(id=matched_lot.id).first()
        if lot is None or lot.status not in ("OPEN", "PARTIAL") or lot.open_qty < req.quantity:
            # Lost a race with a concurrent close/exit on the same lot since the
            # lookup above — refuse rather than silently over-closing it.
            db.close()
            raise HTTPException(status_code=409,
                                detail="The matching open position changed while placing this order — please retry.")
        exit_client_ref = f"lotexit{uuid.uuid4().hex[:10]}"
        exit_record = LotExit(lot_id=lot.id, exit_qty=req.quantity, client_ref=exit_client_ref, status="PENDING")
        db.add(exit_record)
        db.commit()
        db.refresh(exit_record)

    payload = req.dict()
    payload.pop("description", None)       # broker payload doesn't take these
    payload.pop("target_enabled", None)
    payload.pop("target_value", None)
    payload.pop("reentry_source_lot_id", None)
    if exit_record is not None:
        payload["remarks"] = exit_record.client_ref  # broker echoes this back in order book
    elif lot is not None:
        payload["remarks"] = lot.client_ref  # broker echoes this back in order book

    def _fail_lot():
        """Undo the DB row after a broker REJECTION — the broker answered, and
        said no, so no order exists. A recycled CLOSED lot is restored to its
        finished state (carried_pnl keeps the net P&L); a fresh lot never
        represented a real position, so it's just marked CANCELLED.

        Only for a definite rejection. See _unresolved_lot for the case where we
        never heard back.
        """
        if db is None:
            return
        if exit_record is not None:
            # matched_lot itself is an untouched existing position — only the
            # exit attempt failed, so only the LotExit row is written off.
            exit_record.status = "REJECTED"
            db.commit()
            return
        if lot is None:
            return
        if reentry_snapshot is not None:
            for field, value in reentry_snapshot.items():
                setattr(lot, field, value)
        else:
            lot.status = "CANCELLED"
        db.commit()

    def _unresolved_lot(why: str):
        """We did not hear back — the order may be resting or filled.

        Marking this CANCELLED is a lie the system cannot take back: the row is
        written off while the broker holds a real position, and because no order
        id was ever saved nothing links the two afterwards. Leave it PENDING and
        say so, so reconciliation and a human both still have something to find.
        """
        if db is None:
            return
        if exit_record is not None:
            logger.warning("Unresolved exit placement for lot %d (exit %d): %s — "
                            "verify at the broker before retrying", matched_lot.id, exit_record.id, why)
            return
        if lot is None:
            return
        lot.status = "PENDING"
        lot.description = ((lot.description or "") +
                           f" [unresolved placement: {why}. The order may be live at the broker —"
                           " verify before re-placing.]")[:2000]
        db.commit()

    try:
        result = await broker.placeOrder(token, credentials, payload)
        if isinstance(result, dict) and result.get("stat") != "Ok":
            _fail_lot()
            raise HTTPException(status_code=400, detail=result.get("emsg", "Order placement failed"))
        order_id = result.get("norenordno", "") if isinstance(result, dict) else str(result)

        if exit_record is not None and db is not None:
            exit_record.broker_exit_orderid = order_id
            db.commit()
        elif lot is not None and db is not None:
            lot.broker_entry_orderid = order_id
            db.commit()
            # The re-entry is live and the recycle has overwritten the finished
            # round-trip's row — preserve that round-trip in the history archive.
            # Best-effort and in its own transaction: an archive failure must
            # never roll back or fail an order the broker already accepted.
            if archive_data is not None:
                try:
                    db.add(ClosedTradeArchive(**archive_data))
                    db.commit()
                except Exception:
                    db.rollback()
                    logger.exception(
                        "Failed to archive closed round-trip (source lot %s) on re-entry",
                        archive_data.get("source_lot_id"),
                    )

            # A fresh position on this symbol must not inherit a symbol-level
            # target left enabled by a prior, now-closed position: symbol_targets
            # is keyed on (exch, tsym) and is never cleared on exit, so an old
            # day's target would silently re-arm this new entry and auto-exit a
            # position the user never set it for. When this is the only open leg
            # on the symbol (no other OPEN/PARTIAL lot exists), treat it as a
            # fresh position and disable any lingering target; scaling into a
            # live position (an open lot already exists) leaves it untouched.
            other_open = (
                db.query(OrderLot)
                .filter(OrderLot.owner_uid == auth.user_id,
                        OrderLot.exch == req.exchange,
                        OrderLot.tsym == req.tradingsymbol,
                        OrderLot.status.in_(("OPEN", "PARTIAL")),
                        OrderLot.id != lot.id)
                .first()
            )
            if other_open is None:
                st = db.query(SymbolTarget).filter_by(owner_uid=auth.user_id, exch=req.exchange, tsym=req.tradingsymbol).first()
                if st and (st.enabled or st.carried_pnl):
                    st.enabled = False
                    st.carried_pnl = 0.0   # keep target_value as the modal's remembered default
                    db.commit()
                    if tok:
                        state._symbol_targets.pop(f"{req.exchange}|{tok}", None)
                    logger.info("Cleared stale symbol target for %s|%s on fresh entry (lot %d)",
                                req.exchange, req.tradingsymbol, lot.id)

        sm = get_scripmaster()
        tok = sm.get_token(req.exchange, req.tradingsymbol)
        if tok:
            state._auto_exited_tokens.discard(f"{req.exchange}|{tok}")

        asyncio.create_task(_short_poll_reconcile(auth.user_id))
        return {"status": "success", "order_id": order_id,
                "lot_id": (exit_record.lot_id if exit_record is not None else (lot.id if lot else None))}
    except HTTPException:
        raise
    except BrokerError as e:
        # `raw` is set only when the broker itself answered and refused. Without
        # it we never reached a verdict — the request may have been received.
        if e.raw:
            _fail_lot()
            logger.error("Broker rejected order: %s | payload: %s", e, payload)
            raise HTTPException(status_code=400, detail=f"Shoonya rejected the order: {e}")
        _unresolved_lot(str(e))
        logger.error("Order placement UNRESOLVED (no verdict from broker): %s | payload: %s", e, payload)
        raise HTTPException(status_code=502,
                            detail=f"No response from the broker ({e}). The order may already be live — "
                                   "check the order book before placing it again.")
    except Exception as e:
        _unresolved_lot(str(e))
        logger.exception("Order placement UNRESOLVED (transport failure) | payload: %s", payload)
        raise HTTPException(status_code=502,
                            detail=f"Could not confirm the order ({e}). It may already be live — "
                                   "check the order book before placing it again.")
    finally:
        if db is not None:
            db.close()


@router.get("/api/orders")
async def get_orders():
    """Get all orders (order book)."""
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    token = SessionToken(
        token=auth.auth_token,
        broker_uid=auth.user_id,
        issued_at="",
        broker_name="shoonya",
    )
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id}

    try:
        raw_orders = await broker.getOrderBook(token, credentials)
        await _reconcile_lots_from_orderbook(raw_orders, auth.user_id)
        orders = [
            OrderItem(
                norenordno=o.get("norenordno", ""),
                tsym=o.get("tsym", ""),
                exch=o.get("exch", ""),
                prd=o.get("prd", ""),
                trantype=o.get("trantype", ""),
                qty=o.get("qty", "0"),
                price=o.get("prc", o.get("price", "0")),
                pricetype=o.get("prctyp", o.get("pricetype", "")),
                status=o.get("status", ""),
                orderid=o.get("norenordno", ""),
                pytime=o.get("exch_tm", o.get("pytime", "")),
                exch_orderid=o.get("exchordid", o.get("exch_orderid", "")),
                # Execution truth — see OrderItem. Without these a caller cannot
                # tell a filled order from an untouched one and re-places exits
                # that have already executed.
                fillshares=str(o.get("fillshares", "") or "0"),
                avgprc=str(o.get("avgprc", "") or "0"),
                prc=str(o.get("prc", o.get("price", "")) or ""),
                rejreason=str(o.get("rejreason", "") or o.get("emsg", "") or ""),
                remarks=str(o.get("remarks", "") or ""),
            )
            for o in raw_orders
        ]
        return OrderBookResponse(orders=orders)
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=f"Shoonya error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error fetching orders: {e}")


@router.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str):
    """Cancel an order."""
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    token = SessionToken(
        token=auth.auth_token,
        broker_uid=auth.user_id,
        issued_at="",
        broker_name="shoonya",
    )
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id,
                   **_active_broker_proxy("shoonya")}

    try:
        result = await broker.cancelOrder(token, credentials, order_id)
        if isinstance(result, dict) and result.get("stat") != "Ok":
            raise HTTPException(status_code=400, detail=result.get("emsg", "Order cancellation failed"))
        return {"status": "cancelled", "order_id": order_id}
    except HTTPException:
        raise
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=f"Shoonya error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error cancelling order: {e}")


@router.put("/api/orders/{order_id}")
async def modify_order(order_id: str, req: dict):
    """Modify an existing order."""
    auth = _require_legacy_auth()
    broker = get_broker("shoonya")
    token = SessionToken(
        token=auth.auth_token,
        broker_uid=auth.user_id,
        issued_at="",
        broker_name="shoonya",
    )
    credentials = {"user_id": auth.user_id, "account_id": auth.user_id,
                   **_active_broker_proxy("shoonya")}

    try:
        result = await broker.modifyOrder(token, credentials, order_id, req)
        if isinstance(result, dict) and result.get("stat") != "Ok":
            raise HTTPException(status_code=400, detail=result.get("emsg", "Order modification failed"))

        new_price = req.get("newprice")
        if new_price is not None:
            try:
                price_val = float(new_price)
                db = next(get_db())
                try:
                    lot = db.query(OrderLot).filter(
                        OrderLot.broker_entry_orderid == order_id,
                        OrderLot.status == "PENDING",
                    ).first()
                    if lot:
                        lot.avg_entry_price = price_val
                        db.commit()
                finally:
                    db.close()
            except Exception:
                logger.warning("modify_order: failed to sync price to lot for order %s", order_id)

        return {"status": "modified", "order_id": order_id}
    except HTTPException:
        raise
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=f"Shoonya error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error modifying order: {e}")
