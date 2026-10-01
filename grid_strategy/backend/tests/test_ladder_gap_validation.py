"""A ladder must not be armable in a state where it can only fire once.

min_gap_points refuses to open a rung within that distance of an existing one.
Set it wider than the spacing between levels and the ladder still arms, still
shows every level as pending, and still buys exactly one rung ever — the rest
are refused for the life of the position, one log line at a time, while price
trades straight through them.

That happened on 18 Aug: levels 20 points apart with the gap left at 200 from a
previous configuration.
"""

import pytest


def tightest_gap(prices):
    """The spacing the ladder actually has — mirrors the check in ladder_confirm."""
    ps = sorted(prices)
    return min(b - a for a, b in zip(ps, ps[1:]))


def would_be_rejected(prices, min_gap):
    return min_gap > 0 and len(prices) > 1 and tightest_gap(prices) < min_gap


def test_the_configuration_that_silently_broke_the_ladder_is_rejected():
    assert would_be_rejected([15480, 15460, 15440], min_gap=200)


def test_a_ladder_spaced_wider_than_the_gap_is_fine():
    assert not would_be_rejected([15390, 15190, 14990], min_gap=200)


def test_spacing_exactly_equal_to_the_gap_is_allowed():
    """`gap` is a minimum, so meeting it exactly is a valid ladder."""
    assert not would_be_rejected([15400, 15200, 15000], min_gap=200)


def test_the_gap_check_uses_the_TIGHTEST_pair_not_the_average():
    """One cramped pair is enough to strand every level below it."""
    assert would_be_rejected([16000, 15500, 15490], min_gap=200)


def test_disabled_gap_never_rejects():
    assert not would_be_rejected([15480, 15460, 15440], min_gap=0)


def test_a_single_level_is_always_valid():
    """Nothing to be too close to yet."""
    assert not would_be_rejected([15480], min_gap=200)


@pytest.mark.parametrize("prices", [[100, 90], [90, 100]])
def test_ordering_of_the_submitted_levels_does_not_matter(prices):
    assert tightest_gap(prices) == 10
