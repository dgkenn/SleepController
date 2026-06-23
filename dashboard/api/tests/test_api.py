"""End-to-end API tests using FastAPI TestClient (DB + env set in conftest)."""

from __future__ import annotations


def test_health_no_auth(client):
    assert client.get("/health").json()["ok"] is True


def test_auth_required(client):
    # fresh client without cookie -> 401
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/status").status_code == 401


def test_login_and_status(auth_client):
    r = auth_client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert "state" in body and "recommendation" in body
    assert body["last_night"] is not None  # seeded


def test_nights_and_trends(auth_client):
    assert len(auth_client.get("/nights?limit=10").json()) > 0
    tr = auth_client.get("/analytics/trends?metric=wake_events&window=14").json()
    assert tr["metric"] == "wake_events" and len(tr["points"]) > 0


def test_ml_overview(auth_client):
    ov = auth_client.get("/ml/overview").json()
    assert "model_confidence" in ov and "recommendation" in ov and "setpoint" in ov


def test_manual_temp_logs_override_and_queues_command(auth_client):
    r = auth_client.post("/tonight/temp", json={"target_f": 64.0})
    assert r.status_code == 200 and r.json()["queued"] == "set_temp"
    from app.db import get_repo
    repo = get_repo()
    try:
        acts = [a for a in repo.recent_actions(10) if a.source == "manual"]
        assert acts and acts[-1].params.get("target_f") == 64.0
        pending = repo.conn.execute(
            "SELECT COUNT(*) c FROM commands WHERE type='set_temp' AND status='pending'"
        ).fetchone()["c"]
        assert pending >= 1
    finally:
        repo.close()


def test_emergency_stop_queues_command(auth_client):
    assert auth_client.post("/control/stop").json()["queued"] == "stop"


def test_notes_roundtrip(auth_client):
    auth_client.post("/notes", json={"date": "2026-06-23", "text": "felt groggy"})
    notes = auth_client.get("/notes?date=2026-06-23").json()
    assert any(n["text"] == "felt groggy" for n in notes)


def test_alerts_and_admin_health(auth_client):
    auth_client.get("/alerts")
    health = auth_client.get("/admin/health").json()
    assert "daemon" in health and "sources" in health
