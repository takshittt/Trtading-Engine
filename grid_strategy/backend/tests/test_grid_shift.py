"""Grid ladder: frozen self-anchored targets + the compounding downward
shift + the B1-target pause.

The rule the user specified:

  * each level's target is FIXED at setup = its own price + offset. Filling
    cheaper does NOT move the target, so the profit on that rung widens.
  * when a level fills, every still-PENDING level below it slides down by
    (level price − actual fill). The next buy therefore sits one interval below
    where we ACTUALLY got in, and the shift compounds down the ladder:
        B1 fills 16280 → B2 = 16230; B2 fills 16210 → B3 = 16160.
  * the shift runs ONCE per level (a recycled re-buy must not ratchet again).
  * when B1 (the top rung) hits its target the whole grid has cashed out,
    so the ladder pauses (disarms) and waits for a manual re-arm.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import core.engine as eng_mod
from core.engine import GridEngine
from core.exits import compute_target

ENGINE_SRC = Path(eng_mod.__file__).read_text()


def _bare():
    return GridEngine.__new__(GridEngine)


def _inst(**kw):
    d = dict(id=1, sym="GOLDPETAL",
             target_mode="points", target_value=75, target_chain_pct=100)
    d.update(kw)
    return SimpleNamespace(**d)


def _level(no, price, status="PENDING", shift_applied=False):
    return SimpleNamespace(level_no=no, price=price, status=status,
                           shift_applied=shift_applied)


class _FakeQuery:
    """db.query(LadderLevel).filter(...).all() → the rows we were handed."""
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, below_rows):
        self._below = below_rows

    def query(self, *a, **k):
        return _FakeQuery(self._below)


@pytest.fixture(autouse=True)
def _silence_event_log(monkeypatch):
    monkeypatch.setattr(eng_mod.event_log, "write", lambda *a, **k: None)


# ---------------------------------------------------- frozen targets ----

def test_each_levels_target_is_self_anchored_own_price_plus_offset():
    """The user's numbers: 50-pt ladder, 75-pt offset → 16375 / 16325 / 16275,
    each = its OWN level + 75 (NOT chained off the rung above)."""
    inst = _inst()
    assert compute_target(inst, 16300, None) == (16375, 16300)
    assert compute_target(inst, 16250, None) == (16325, 16250)
    assert compute_target(inst, 16200, None) == (16275, 16200)


# ------------------------------------------------- the compounding shift ----

def test_shift_moves_pending_levels_below_by_the_fill_delta():
    e, inst = _bare(), _inst()
    b1 = _level(1, 16300)
    below = [_level(2, 16250), _level(3, 16200), _level(4, 16150)]
    e._apply_ladder_shift(_FakeDB(below), inst, b1, 16280.0)   # filled 20 cheaper
    assert [lv.price for lv in below] == [16230, 16180, 16130]
    assert b1.shift_applied is True


def test_the_shift_compounds_down_the_ladder_to_the_agreed_numbers():
    """16300 fills 16280 → B2 16230; B2 fills 16210 → B3 16160 (not 16170)."""
    e, inst = _bare(), _inst()
    b1 = _level(1, 16300)
    b2, b3, b4 = _level(2, 16250), _level(3, 16200), _level(4, 16150)
    e._apply_ladder_shift(_FakeDB([b2, b3, b4]), inst, b1, 16280.0)
    assert b2.price == 16230
    # now B2 (at 16230) fills 20 cheaper at 16210 → the levels below cascade again
    e._apply_ladder_shift(_FakeDB([b3, b4]), inst, b2, 16210.0)
    assert b3.price == 16160
    assert b4.price == 16110


def test_a_level_only_ever_shifts_once_even_if_it_re_buys():
    """A recycled re-buy after a target hit must NOT ratchet the ladder again."""
    e, inst = _bare(), _inst()
    b1 = _level(1, 16300, shift_applied=True)   # already shifted on its first fill
    below = [_level(2, 16230)]
    e._apply_ladder_shift(_FakeDB(below), inst, b1, 16280.0)
    assert below[0].price == 16230              # untouched


def test_a_fill_at_or_above_the_level_never_shifts_but_still_locks_the_level():
    """A limit buy can fill at the level or better only; an exact fill leaves the
    ladder as-is, and the level is still marked so a later re-buy can't shift."""
    e, inst = _bare(), _inst()
    b1 = _level(1, 16300)
    below = [_level(2, 16250)]
    e._apply_ladder_shift(_FakeDB(below), inst, b1, 16300.0)   # no gap
    assert below[0].price == 16250
    assert b1.shift_applied is True


# ------------------------------------------------ wiring / pause (source) ----

def test_the_shift_is_wired_into_the_resting_fill_path():
    assert "self._apply_ladder_shift(db, inst, lvl, actual)" in ENGINE_SRC


def test_b1_target_pause_disarms_the_ladder():
    """When the top rung hits target the settle path must disarm the ladder."""
    assert "inst_row.ladder_armed = False" in ENGINE_SRC
    assert "grid cycle" in ENGINE_SRC.lower()
