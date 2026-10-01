"""Guards on a HAND-SET target or stop price.

compute_target/compute_sl derive exits from the instrument's offsets, and
_exit_config_error checks those before a rung is opened. A price the operator
TYPES bypasses both: it lands on the lot and the tick loop acts on it on the
next tick. Two edits were unguarded and both sell at market immediately —
a stop above the last price, and (on an unfilled rung) a target under the entry.

live=True is measured against the LAST PRICE, not the entry, on purpose: a stop
above entry but below the market is a trailing stop on a winner and must stay
allowed. Getting that wrong would block ordinary risk management.
"""

import pytest

from core.exits import exit_edit_error, level_override_error

ENTRY, LTP = 15480.0, 15520.0


def err(**kw):
    kw.setdefault("target", None); kw.setdefault("sl", None)
    kw.setdefault("entry", ENTRY); kw.setdefault("ltp", LTP); kw.setdefault("live", True)
    return exit_edit_error(**kw)[0]


def warns(**kw):
    kw.setdefault("target", None); kw.setdefault("sl", None)
    kw.setdefault("entry", ENTRY); kw.setdefault("ltp", LTP); kw.setdefault("live", True)
    return exit_edit_error(**kw)[1]


# ---------------------------------------------------------- stops, live ----

def test_stop_above_the_last_price_is_refused():
    """The bug: armed on price <= stop, so this is a market sell next tick."""
    e = err(sl=LTP + 1)
    assert e and "next tick" in e


def test_stop_exactly_at_the_last_price_is_refused():
    assert err(sl=LTP)


def test_trailing_stop_above_entry_but_below_market_is_ALLOWED():
    """Must not block ordinary risk management on a winning rung."""
    assert err(sl=ENTRY + 10) == ""      # 15490 < ltp 15520


def test_ordinary_stop_below_entry_is_allowed():
    assert err(sl=ENTRY - 100) == ""


def test_stop_just_under_the_market_warns_but_is_allowed():
    assert err(sl=LTP - 0.01) == ""
    assert any("almost immediately" in w for w in warns(sl=LTP - 0.01))


def test_stop_with_no_live_tick_is_allowed_but_flagged():
    assert err(sl=ENTRY + 500, ltp=0.0) == ""
    assert any("no live tick" in w for w in warns(sl=ENTRY + 500, ltp=0.0))


def test_clearing_the_stop_is_allowed_but_says_the_rung_is_unprotected():
    assert err(sl=0) == ""
    assert any("NO stop" in w for w in warns(sl=0))


def test_negative_stop_is_refused():
    assert err(sl=-5)


# ------------------------------------------------------- stops, unfilled ----

def test_stop_above_the_buy_price_on_an_unfilled_rung_is_refused():
    e = err(sl=ENTRY + 1, live=False)
    assert e and "the moment the buy fills" in e


def test_unfilled_rung_ignores_the_market_price():
    """A buy resting far below the market is the normal case for a dip ladder."""
    assert err(sl=ENTRY - 50, live=False, ltp=99999.0) == ""


# --------------------------------------------------------------- targets ----

def test_target_below_entry_on_an_unfilled_rung_is_refused():
    e = err(target=ENTRY - 50, live=False)
    assert e and "round-trip" in e


def test_target_below_entry_on_a_LIVE_rung_only_warns():
    """The units exist; the operator may legitimately be cutting a loser."""
    assert err(target=ENTRY - 50) == ""
    assert any("LOSS" in w for w in warns(target=ENTRY - 50))


def test_target_under_the_market_warns_that_it_is_not_a_resting_target():
    assert err(target=LTP - 10) == ""
    assert any("as soon as the sell fills" in w for w in warns(target=LTP - 10))


def test_zero_target_is_refused_because_it_disarms_the_target_exit():
    e = err(target=0)
    assert e and "switches the target exit OFF" in e


def test_a_normal_target_above_the_market_is_clean():
    assert err(target=LTP + 100) == ""
    assert warns(target=LTP + 100) == []


# ---------------------------------------------------------- ladder levels ----

def test_level_target_at_or_below_its_own_price_is_refused():
    assert level_override_error(15440.0, 15400.0, None)
    assert level_override_error(15440.0, 15440.0, None)


def test_level_stop_at_or_above_its_own_price_is_refused():
    assert level_override_error(15440.0, None, 15450.0)
    assert level_override_error(15440.0, None, 15440.0)


def test_a_sane_level_passes():
    assert level_override_error(15440.0, 15480.0, 15400.0) == ""


@pytest.mark.parametrize("v", [0, -1, None])
def test_non_positive_override_means_clear_to_auto_not_invalid(v):
    """These routes use <=0 as 'reset to the computed value'."""
    assert level_override_error(15440.0, v, v) == ""
