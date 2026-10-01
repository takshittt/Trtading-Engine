"""Guards on re-entry, and on a fill the broker priced at nothing.

Re-entry was the only BUY in the system with no gates. An armed limit re-entry
fired through the master switch, a tripped breaker, a paused ladder and Flatten
— i.e. through every control an operator reaches for when they want everything
to stop.

Separately, a broker that reports a fill with no average price is reporting
MISSING DATA, not a sale at zero. Multiplying that zero out books the rung's
entire notional as a realised loss, which then feeds the circuit breaker.
"""

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.engine as eng_mod
from core.engine import GridEngine

ENGINE_SRC = Path(eng_mod.__file__).read_text()


def _bare():
    e = GridEngine.__new__(GridEngine)
    e.global_cb_tripped = False
    e.broker_connected = True
    return e


def _inst(**kw):
    d = dict(sym="GOLDPETAL", enabled=True, cb_tripped=False, cb_reason="",
             rollover_state="idle", exch="MCX")
    d.update(kw)
    return SimpleNamespace(**d)


def _gate(e, inst, engine_on=True, market_open=True, monkeypatch=None):
    monkeypatch.setattr(e, "engine_on", lambda: engine_on)
    monkeypatch.setattr(eng_mod, "market_open_now", lambda exch: market_open)
    return e._reentry_blocked(inst)


# ------------------------------------------------------------ re-entry ----

def test_reentry_is_blocked_by_the_master_switch(monkeypatch):
    assert "master switch" in _gate(_bare(), _inst(), engine_on=False, monkeypatch=monkeypatch)


def test_reentry_is_blocked_by_the_instrument_breaker(monkeypatch):
    e = _bare()
    assert "circuit breaker" in _gate(e, _inst(cb_tripped=True), monkeypatch=monkeypatch)


def test_reentry_is_blocked_by_the_global_breaker(monkeypatch):
    e = _bare(); e.global_cb_tripped = True
    assert "GLOBAL" in _gate(e, _inst(), monkeypatch=monkeypatch)


def test_reentry_is_blocked_when_the_instrument_is_disabled(monkeypatch):
    assert "disabled" in _gate(_bare(), _inst(enabled=False), monkeypatch=monkeypatch)


def test_reentry_is_blocked_when_the_market_is_shut(monkeypatch):
    assert "closed" in _gate(_bare(), _inst(), market_open=False, monkeypatch=monkeypatch)


def test_reentry_is_blocked_while_a_rollover_runs(monkeypatch):
    assert _gate(_bare(), _inst(rollover_state="rolling"), monkeypatch=monkeypatch)


def test_reentry_is_blocked_when_the_broker_is_down(monkeypatch):
    e = _bare(); e.broker_connected = False
    assert "broker session" in _gate(e, _inst(), monkeypatch=monkeypatch)


def test_reentry_proceeds_when_everything_is_healthy(monkeypatch):
    assert _gate(_bare(), _inst(), monkeypatch=monkeypatch) == ""


def test_reenter_now_actually_consults_the_gate():
    """The helper is worthless if the caller never invokes it."""
    src = inspect.getsource(GridEngine.reenter_now)
    assert "_reentry_blocked" in src
    assert src.index("_reentry_blocked") < src.index("om.execute"), \
        "the gate must be checked BEFORE the buy is sent"


def test_flatten_disarms_armed_reentries():
    """Flatten used to select only OPEN rungs, so a paused rung kept its
    trigger and bought back after the panic button."""
    src = inspect.getsource(GridEngine.flatten_instrument)
    assert "TEMP_EXITED" in src and "reenter_trigger_price" in src


# --------------------------------------------------- zero-price fills ----

def _assigns_guard(func_name: str, marker: str) -> bool:
    """Is there an `avg <= 0` style guard inside this function?"""
    tree = ast.parse(ENGINE_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            return marker in ast.get_source_segment(ENGINE_SRC, node)
    raise AssertionError(f"{func_name} not found")


def test_exit_path_refuses_to_price_a_fill_at_zero():
    assert _assigns_guard("_close_lot_inner", "avg <= 0")


def test_roll_basis_refuses_a_zero_sell_average():
    assert _assigns_guard("_roll_one_lot", "sold_avg <= 0")


@pytest.mark.parametrize("entry,qty", [(15480.0, 1), (22000.0, 75)])
def test_the_phantom_loss_this_prevents(entry, qty):
    """What the unguarded arithmetic produced."""
    phantom = (0.0 - entry) * qty
    assert phantom < 0 and abs(phantom) == entry * qty
    guarded = (entry - entry) * qty        # falls back to the entry price
    assert guarded == 0.0
