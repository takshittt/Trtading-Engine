"""Only USER actions may forget a level's retry cooldown.

A refusal to place a level stamps a cooldown, and outside the first half hour
the retry window is five minutes. That is correct for a level the broker keeps
rejecting — backing off is the point — but wrong the moment a human corrects
the setting that caused the refusal: they change the exact field the log
complained about, the dashboard fires a sync, and nothing happens for another
five minutes, which is indistinguishable from the fix not working.

So `clear_holds` belongs on config edits, arming, breaker resets and ladder
confirms, and must NOT leak onto the automatic resolvers — clearing the cooldown
there would retry a rejected order instantly and burn its whole placement budget
in seconds instead of backing off.

Asserted at source level because the distinction is about which CALLER is
allowed to ask, and that is exactly what would rot silently.
"""

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

# Automatic callers: these run on their own and must keep backing off.
AUTOMATIC_CONTEXTS = {
    "_resolve_entry_order", "_resolve_target_order", "_reconcile_once",
    "_check_ladder_on_price", "cancel_resting_entries",
}


def _sync_calls(path: Path):
    """(enclosing function, passes clear_holds) for each sync_resting_orders call."""
    tree = ast.parse(path.read_text())
    out = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.fn = "<module>"

        def visit_FunctionDef(self, node):
            prev, self.fn = self.fn, node.name
            self.generic_visit(node)
            self.fn = prev

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            name = getattr(node.func, "attr", None)
            if name == "sync_resting_orders":
                out.append((self.fn, any(k.arg == "clear_holds" for k in node.keywords)))
            self.generic_visit(node)

    V().visit(tree)
    return out


def test_automatic_resolvers_never_clear_the_cooldown():
    calls = _sync_calls(BACKEND / "core" / "engine.py")
    offenders = [fn for fn, clears in calls if clears and fn in AUTOMATIC_CONTEXTS]
    assert not offenders, (
        f"{offenders} clear the retry cooldown on an automatic path. A level the "
        f"broker keeps rejecting would then retry immediately and exhaust its "
        f"placement budget instead of backing off."
    )


def test_a_config_change_does_clear_the_cooldown():
    """The case that stranded a corrected ladder for five minutes."""
    calls = _sync_calls(BACKEND / "api" / "routes.py")
    by_fn = {fn: clears for fn, clears in calls}
    assert by_fn.get("update_instrument") is True, (
        "changing instrument config must forget the stale refusal, or fixing the "
        "setting that blocked a level appears to do nothing"
    )


@pytest.mark.parametrize("fn", ["ladder_arm", "set_circuit_breaker", "engine_start"])
def test_other_user_actions_clear_the_cooldown(fn):
    by_fn = {f: c for f, c in _sync_calls(BACKEND / "api" / "routes.py")}
    if fn not in by_fn:
        pytest.skip(f"{fn} does not sync")
    assert by_fn[fn] is True, f"{fn} is a deliberate user action; it should not wait out a stale cooldown"


def test_sync_resting_orders_still_defaults_to_not_clearing():
    """Default must stay conservative: the automatic callers rely on it."""
    tree = ast.parse((BACKEND / "core" / "engine.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "sync_resting_orders":
            defaults = dict(zip([a.arg for a in node.args.args[-len(node.args.defaults):]],
                                node.args.defaults))
            assert isinstance(defaults["clear_holds"], ast.Constant)
            assert defaults["clear_holds"].value is False
            return
    raise AssertionError("sync_resting_orders not found")
