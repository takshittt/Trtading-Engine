"""The gateway↔engine contract: every field the engine reads must be declared.

This is the test that would have caught the incident of 18 Aug on day one.

The gateway serves several endpoints through pydantic `response_model`s, and a
response model does not merely describe the payload — it FILTERS it. Any key the
broker sent that the model does not declare is dropped silently, with no error
anywhere. `OrderItem` never declared `fillshares` or `avgprc`, so every filled
order reached the engine as `filled=0, avg=0`; a resting target that had already
executed read as unfilled, and the desired-state sync rested another live SELL
for it once per reconcile pass until a long rung had become a real short.

Nothing detected that. Not a type checker (both sides were internally
consistent), not a unit test on either side (each was correct in isolation), not
a log line. The only detector was money moving.

So the contract is asserted here, from the consumer's side, by reading the
gateway's schema source directly. Deliberately AST-based rather than importing
the gateway package: this must run in the engine's own environment, with no
broker SDK installed and no gateway dependencies, or it will be skipped in
exactly the situations where it matters.

Adding a field the engine relies on means adding it here too.
"""

import ast
from pathlib import Path

import pytest

GATEWAY = (Path(__file__).resolve().parents[3] / "Gateway" / "gateway_backend")

# model → the fields the engine genuinely reads off it, and where it reads them.
REQUIRED = {
    "OrderItem": {
        "file": "app/schemas/orders.py",
        "fields": {
            "norenordno",   # engine: find_order() matches on this
            "status",       # engine: TERMINAL check drives every settle path
            "fillshares",   # engine: core/orders._fillshares — did it fill, how much
            "avgprc",       # engine: core/orders._avgprc — at what price
            "trantype",     # engine: _find_untracked_order side match
            "tsym",         # engine: _find_untracked_order contract match
            "qty",          # engine: _find_untracked_order qty match
            "prd",          # engine: _find_untracked_order product match
        },
    },
    "QuoteResponse": {
        "file": "app/schemas/market.py",
        "fields": {
            "lp",           # engine: every price path
            "bp1", "sp1",   # engine: spread guard, marketable limits, tick validation
        },
    },
    "PositionItem": {
        "file": "app/schemas/positions.py",
        "fields": {
            "tsym", "exch",
            "prd",          # engine: lots are matched to the SAME product only
            "netqty",       # engine: the ghost-close comparison
            "token", "lp",  # engine: ref_prices, so a shut market still shows a price
        },
    },
    "FundsResponse": {
        "file": "app/schemas/positions.py",
        "fields": {"cash", "margin_used"},   # engine: _remaining_margin, margin gate
    },
    "ScripSearchResult": {
        "file": "app/schemas/market.py",
        "fields": {
            "tsym", "token",
            "instrumenttype",   # engine: _next_contract filters futures on this
            "expd",             # engine: picks the nearest LATER expiry
            "lotsize",          # engine: carried onto the rolled lot
            "sym",
        },
    },
}


def _declared_fields(path: Path, class_name: str) -> set[str]:
    """Field names declared on a pydantic model, read from source."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(f"{class_name} not found in {path}")


@pytest.mark.parametrize("model", sorted(REQUIRED))
def test_gateway_model_declares_fields_the_engine_reads(model):
    spec = REQUIRED[model]
    path = GATEWAY / spec["file"]
    if not path.exists():
        pytest.skip(f"gateway source not present at {path}")
    declared = _declared_fields(path, model)
    missing = spec["fields"] - declared
    assert not missing, (
        f"{model} does not declare {sorted(missing)}. A pydantic response_model "
        f"DROPS undeclared keys, so the engine will receive these as absent and "
        f"read their absence as fact — which is how a filled order became an "
        f"unfilled one and the engine shorted the account. Declare them in "
        f"{spec['file']} and populate them in the router."
    )


def test_order_item_fill_fields_are_not_silently_defaulted_away():
    """fillshares/avgprc must exist AND the router must populate them.

    Declaring the field is only half of it: a model field with a default that
    the router never sets is just as invisible to the engine.
    """
    router = GATEWAY / "app/routers/orders.py"
    if not router.exists():
        pytest.skip("gateway source not present")
    src = router.read_text()
    for field in ("fillshares", "avgprc"):
        assert f"{field}=" in src, (
            f"OrderItem.{field} is declared but /api/orders never populates it, "
            f"so it defaults to '0' for every order — indistinguishable from a "
            f"genuinely unfilled one."
        )
