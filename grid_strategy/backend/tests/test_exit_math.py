"""Target/stop math, pinned against the vectors the dashboard also asserts.

See ladder_math_vectors.json (this directory) for why this is shared.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.exits import compute_sl, compute_target, lot_pnl

VECTORS = json.loads(
    (Path(__file__).resolve().parent / "ladder_math_vectors.json").read_text())


@pytest.mark.parametrize("case", VECTORS["target"], ids=lambda c: c["name"])
def test_compute_target_matches_shared_vectors(case):
    tgt, anchor = compute_target(SimpleNamespace(**case["inst"]), case["entry"], case["prev"])
    assert tgt == pytest.approx(case["expect_target"])
    assert anchor == pytest.approx(case["expect_anchor"])


@pytest.mark.parametrize("case", VECTORS["sl"], ids=lambda c: c["name"])
def test_compute_sl_matches_shared_vectors(case):
    assert compute_sl(SimpleNamespace(**case["inst"]), case["entry"]) == pytest.approx(
        case["expect_sl"])


def test_a_chained_target_is_never_at_or_below_its_own_entry():
    """The clamp exists because a recycled level can refire ABOVE still-open
    lower rungs; without it the new rung exits instantly for ~nothing."""
    inst = SimpleNamespace(target_mode="points", target_value=200, target_chain_pct=100)
    for prev in (14000, 15000, 15389, 15390, 15391, 16000):
        tgt, _ = compute_target(inst, 15390, prev)
        assert tgt > 15390, f"target {tgt} would round-trip instantly (prev={prev})"


def test_disabled_stop_is_zero_because_the_engine_reads_zero_as_no_stop():
    inst = SimpleNamespace(sl_enabled=False, sl_mode="points", sl_value=200)
    assert compute_sl(inst, 15390) == 0.0


def test_lot_pnl_is_plain_arithmetic_in_both_directions():
    assert lot_pnl(15390, 15530, 1) == pytest.approx(140.0)
    assert lot_pnl(15390, 15300, 2) == pytest.approx(-180.0)
