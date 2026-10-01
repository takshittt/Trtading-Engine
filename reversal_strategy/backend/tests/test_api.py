"""HTTP surface: SSO gating and a buy → edit → exit round trip.

The client is used without its context manager on purpose, so FastAPI's
startup hook (which launches the price feed, reconciler and other background
loops) never runs. The schema is already built by conftest.
"""
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.state import STATE
from app.main import app

SYM = "RELIANCE-FUT"


def _auth(typ="user"):
    tok = jwt.encode({"sub": "1", "typ": typ, "exp": int(time.time()) + 60},
                     "test-secret-not-a-real-key-padded-to-32b", algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def client():
    STATE.prices[SYM] = 1000.0
    return TestClient(app)


def test_health_is_public(client):
    assert client.get("/api/health").json() == {"status": "ok", "service": "reversal-strategy"}


@pytest.mark.parametrize("path", ["/api/signals", "/api/positions", "/api/positions/orders"])
def test_dashboard_routes_require_a_user_token(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_auth(typ="service")).status_code == 401
    assert client.get(path, headers=_auth()).status_code == 200


def test_buy_edit_exit_round_trip(client):
    h = _auth()
    res = client.post("/api/positions/buy", headers=h,
                      json={"symbol": SYM.lower(), "lots": 2, "atr": 10.0}).json()
    assert res["ok"], res
    pid = res["position"]["id"]

    listed = client.get("/api/positions", headers=h).json()
    assert [p["id"] for p in listed] == [pid]

    edited = client.post(f"/api/positions/{pid}/edit", headers=h,
                         json={"stop_loss": 990.0}).json()
    assert edited["position"]["stop_loss"] == 990.0

    assert client.post(f"/api/positions/{pid}/partial", headers=h,
                       json={"lots": 1}).json()["position"]["lots"] == 1
    assert client.post(f"/api/positions/{pid}/exit", headers=h).json()["ok"]
    assert client.get("/api/positions", headers=h).json() == []

    orders = client.get("/api/positions/orders", headers=h).json()
    assert {o["intent"] for o in orders} == {"ENTRY", "PARTIAL", "MANUAL"}


def test_manual_add_then_ignore(client):
    h = _auth()
    sig = client.post("/api/signals/manual-add", headers=h, json={"symbol": SYM}).json()["signal"]
    assert [s["id"] for s in client.get("/api/signals", headers=h).json()] == [sig["id"]]
    assert client.post(f"/api/signals/{sig['id']}/ignore", headers=h).json() == {"ok": True}
    assert client.get("/api/signals", headers=h).json() == []
