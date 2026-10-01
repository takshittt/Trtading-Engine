"""How an order-book row becomes a fill decision.

Every incident so far has come from this translation, not from the strategy.
The rule the code has to hold is narrow and easy to state:

    a status the broker calls terminal must never be read as "nothing happened"
    merely because the numbers next to it are missing.

Missing data is missing data. Treating it as zero turns a filled exit into an
unfilled one, and the engine's response to an unfilled exit is to place another.
"""

from core.orders import TERMINAL, FillReport, OrderManager, _avgprc, _fillshares


def _row(**kw):
    row = {"norenordno": "26081800164037", "status": "COMPLETE",
           "fillshares": "1", "avgprc": "15530.00", "tsym": "GOLDPETAL31AUG26",
           "trantype": "S", "qty": "1", "prd": "M"}
    row.update(kw)
    return row


_OM = OrderManager(rest=None)   # _report_from_row is pure; no broker needed


def _report(row, requested=1):
    return _OM._report_from_row(row.get("norenordno", ""), row, requested)


# ---------------------------------------------------------------- parsing ----

def test_fill_fields_parse_from_broker_strings():
    assert _fillshares(_row()) == 1
    assert _avgprc(_row()) == 15530.0


def test_absent_fill_fields_parse_as_zero_not_crash():
    # This is what the gateway used to serve for EVERY order.
    assert _fillshares({"status": "COMPLETE"}) == 0
    assert _avgprc({"status": "COMPLETE"}) == 0.0


def test_malformed_fill_fields_do_not_raise():
    assert _fillshares(_row(fillshares="")) == 0
    assert _fillshares(_row(fillshares="oops")) == 0
    assert _avgprc(_row(avgprc=None)) == 0.0
    assert _fillshares(None) == 0


# --------------------------------------------------------------- verdicts ----

def test_complete_and_fully_filled_is_complete():
    r = _report(_row())
    assert r.status == "COMPLETE"
    assert r.ok and r.filled_qty == 1 and r.avg_price == 15530.0


def test_complete_without_fill_quantity_is_never_cancelled():
    """The regression that cost real money.

    COMPLETE means the broker executed it. With the quantity missing we do not
    know how much, but we DO know it is not zero — and reporting CANCELLED here
    is what made the caller re-place a sell that had already filled.
    """
    r = _report(_row(fillshares="0", avgprc="0"))
    assert r.status != "CANCELLED", (
        "a COMPLETE order reported as CANCELLED re-arms the level and re-places "
        "the order — this is the duplicate-sell bug"
    )
    assert r.status == "TIMEOUT"      # hand it to the reconciler, do not guess
    assert not r.ok                   # and never let a caller treat it as a clean fill


def test_partial_fill_is_reported_as_partial():
    r = _report(_row(status="COMPLETE", fillshares="1"), requested=2)
    assert r.status == "PARTIAL" and r.filled_qty == 1 and r.ok


def test_rejected_with_no_fill_is_rejected_and_carries_the_reason():
    r = _report(_row(status="REJECTED", fillshares="0", avgprc="0",
                     rejreason="ALGO_CHK: MKT not allowed"))
    assert r.status == "REJECTED" and not r.ok
    assert "ALGO_CHK" in r.error, "the broker's reason must survive to the operator"


def test_cancelled_after_a_partial_fill_still_reports_the_fill():
    """Units that traded before the cancel are a real position."""
    r = _report(_row(status="CANCELED", fillshares="1", avgprc="15479.00"), requested=2)
    assert r.status == "PARTIAL" and r.filled_qty == 1 and r.avg_price == 15479.0


def test_terminal_set_covers_both_broker_spellings():
    assert {"CANCELED", "CANCELLED"} <= TERMINAL


def test_ok_requires_an_actual_fill():
    assert not FillReport(order_id="x", status="COMPLETE", filled_qty=0).ok
    assert not FillReport(order_id="x", status="TIMEOUT", filled_qty=5).ok
    assert FillReport(order_id="x", status="PARTIAL", filled_qty=1).ok
