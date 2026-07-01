"""GET /learning/ledger: auth + response shape."""

from __future__ import annotations


def test_ledger_requires_auth(client):
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/learning/ledger").status_code == 401


def test_ledger_shape(auth_client):
    r = auth_client.get("/learning/ledger")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"entries", "contradictions"}
    assert isinstance(body["entries"], list)
    assert isinstance(body["contradictions"], list)
    assert len(body["entries"]) > 0

    for e in body["entries"]:
        for key in ("name", "phase", "value", "unit", "source", "maturity", "confidence", "note"):
            assert key in e
        assert e["phase"] in ("onset", "maintenance", "wake", "thermal")
        assert e["source"] in ("preset", "learned", "measured")
        assert 0.0 <= e["confidence"] <= 1.0

    for w in body["contradictions"]:
        for key in ("phase", "a", "b", "combined_spread_f", "message"):
            assert key in w
