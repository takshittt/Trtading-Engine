"""Ceilings and backoff on the loops that place real orders.

Every automatic order here comes from a loop that re-asserts a desired state,
and such a loop only stops when reality agrees with it. When it cannot agree —
a misread fill, a broker that keeps refusing — it repeats, and each repetition
is a live order. One of those loops put out a real SELL per reconcile pass until
a long rung had become a short.

Entries and targets therefore get a hard ceiling. Exits deliberately do NOT:
refusing to retry an exit leaves a position that wanted out still open, which is
a worse failure than the one being prevented. They back off instead.
"""

import core.engine as eng_mod
from core.engine import (
    _ENTRY_REPLACE_MAX, _ENTRY_REPLACE_WINDOW, _EXIT_FAIL_ALARM_AFTER,
    _EXIT_RETRY_BASE, _EXIT_RETRY_MAX, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW,
    GridEngine,
)


def _bare_engine() -> GridEngine:
    """An engine object without running __init__ (which builds clients)."""
    e = GridEngine.__new__(GridEngine)
    e._rate_hist = {}
    e._rate_halted = set()
    e._exit_fails = {}
    return e


# ------------------------------------------------------------- ceilings ----

def test_target_placements_are_allowed_up_to_the_ceiling_then_stop():
    e = _bare_engine()
    for i in range(_TARGET_REPLACE_MAX):
        assert e._rate_allow("target", 1, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW,
                             halt_msg="halt"), f"placement {i + 1} should be allowed"
        e._rate_note("target", 1)
    assert not e._rate_allow("target", 1, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW,
                             halt_msg="halt"), "the loop must stop at the ceiling"


def test_a_halted_bucket_stays_halted():
    """Latched on purpose: a loop that resumes on its own resumes the damage."""
    e = _bare_engine()
    for _ in range(_TARGET_REPLACE_MAX):
        e._rate_allow("target", 1, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW, halt_msg="halt")
        e._rate_note("target", 1)
    assert not e._rate_allow("target", 1, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW, halt_msg="halt")
    e._rate_hist[("target", 1)] = []          # even with the history cleared
    assert not e._rate_allow("target", 1, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW, halt_msg="halt")


def test_the_ceiling_is_per_rung_not_global():
    """One runaway rung must not disarm the targets of every other rung."""
    e = _bare_engine()
    for _ in range(_TARGET_REPLACE_MAX):
        e._rate_allow("target", 1, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW, halt_msg="halt")
        e._rate_note("target", 1)
    assert not e._rate_allow("target", 1, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW, halt_msg="halt")
    assert e._rate_allow("target", 2, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW, halt_msg="halt")


def test_buckets_do_not_share_a_budget():
    e = _bare_engine()
    for _ in range(_TARGET_REPLACE_MAX):
        e._rate_allow("target", 7, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW, halt_msg="halt")
        e._rate_note("target", 7)
    assert not e._rate_allow("target", 7, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW, halt_msg="halt")
    assert e._rate_allow("entry", 7, _ENTRY_REPLACE_MAX, _ENTRY_REPLACE_WINDOW, halt_msg="halt")


def test_placements_outside_the_window_do_not_count():
    """A level legitimately re-places across pauses and day rollovers."""
    e = _bare_engine()
    stale = -(_TARGET_REPLACE_WINDOW + 10)
    e._rate_hist[("target", 1)] = [stale] * (_TARGET_REPLACE_MAX + 5)
    assert e._rate_allow("target", 1, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW, halt_msg="halt")


def test_a_failed_placement_does_not_consume_budget():
    """_rate_note is only called after an order reaches the broker, so a broker
    having a bad minute cannot disarm the loop for the whole window."""
    e = _bare_engine()
    for _ in range(_TARGET_REPLACE_MAX * 3):
        assert e._rate_allow("target", 1, _TARGET_REPLACE_MAX, _TARGET_REPLACE_WINDOW, halt_msg="halt")
        # no _rate_note — the placement errored


def test_entry_ceiling_is_looser_than_the_target_ceiling():
    """A rejected buy does not fill; a re-placed target did. Different risk."""
    assert _ENTRY_REPLACE_MAX > _TARGET_REPLACE_MAX


# -------------------------------------------------------------- backoff ----

def test_exit_retry_starts_fast_and_doubles():
    e = _bare_engine()
    assert e._exit_retry_wait(1) == _EXIT_RETRY_BASE
    e._note_exit_result(1, ok=False)
    assert e._exit_retry_wait(1) == _EXIT_RETRY_BASE * 2
    e._note_exit_result(1, ok=False)
    assert e._exit_retry_wait(1) == _EXIT_RETRY_BASE * 4


def test_exit_retry_is_capped_and_never_becomes_never():
    e = _bare_engine()
    for _ in range(50):
        e._note_exit_result(1, ok=False)
    wait = e._exit_retry_wait(1)
    assert wait == _EXIT_RETRY_MAX
    assert wait < float("inf"), "an exit must always keep retrying — a stop-loss that gives up is worse"


def test_a_successful_exit_clears_the_backoff():
    e = _bare_engine()
    for _ in range(4):
        e._note_exit_result(1, ok=False)
    assert e._exit_retry_wait(1) > _EXIT_RETRY_BASE
    e._note_exit_result(1, ok=True)
    assert e._exit_retry_wait(1) == _EXIT_RETRY_BASE


def test_exit_backoff_is_per_rung():
    e = _bare_engine()
    for _ in range(4):
        e._note_exit_result(1, ok=False)
    assert e._exit_retry_wait(2) == _EXIT_RETRY_BASE


def test_repeated_exit_failures_raise_an_alarm(monkeypatch):
    seen = []
    monkeypatch.setattr(eng_mod.event_log, "error",
                        lambda *a, **k: seen.append(a), raising=True)
    e = _bare_engine()
    for _ in range(_EXIT_FAIL_ALARM_AFTER):
        e._note_exit_result(9, ok=False)
    assert seen, "a rung that cannot exit must be reported, not merely slowed down"
