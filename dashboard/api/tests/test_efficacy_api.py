"""API surface for the standing "does the controller help?" efficacy trial: config get/set
(default OFF) + the /efficacy status+analysis endpoint."""

from __future__ import annotations


def test_efficacy_config_defaults_off(auth_client):
    cfg = auth_client.get("/efficacy/config").json()
    assert cfg["enabled"] is False
    assert cfg["block_nights"] >= 1


def test_efficacy_config_update_and_persist(auth_client):
    r = auth_client.put("/efficacy/config", json={"enabled": True, "block_nights": 4})
    assert r.status_code == 200
    cfg = r.json()
    assert cfg["enabled"] is True and cfg["block_nights"] == 4
    # persisted
    assert auth_client.get("/efficacy/config").json()["enabled"] is True
    # turn back off so this test doesn't leak state that skews other tests in the session
    auth_client.put("/efficacy/config", json={"enabled": False})


def test_efficacy_status_shape_and_not_enough_data(auth_client):
    auth_client.put("/efficacy/config", json={"enabled": True})
    r = auth_client.get("/efficacy")
    assert r.status_code == 200
    body = r.json()
    assert "config" in body and "analysis" in body
    analysis = body["analysis"]
    assert analysis["enough_data"] is False
    assert set(analysis["metrics"].keys()) == {"wake_events", "deep_pct", "efficiency"}
    assert "verdict" in analysis and isinstance(analysis["verdict"], str)
    auth_client.put("/efficacy/config", json={"enabled": False})


def test_efficacy_requires_auth(client):
    # fresh client without a session cookie -> 401 (the shared `client` fixture may already be
    # authenticated by an earlier test in this session, so build an unauthenticated one).
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/efficacy").status_code == 401
