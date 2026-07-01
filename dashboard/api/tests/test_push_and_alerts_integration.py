"""Integration tests: health-monitor -> alerts table -> /alerts endpoint, and the
/push/subscribe + /push/vapid-public-key endpoints. Uses the same seeded TestClient DB
as the rest of the suite (conftest.py's ``auth_client``).
"""

from __future__ import annotations

from app import services
from app.bridge import write_runtime_state
from app.db import get_repo


def _set_runtime_extra(**extra_overrides):
    repo = get_repo()
    try:
        base_extra = {
            "thermal_health": {"state": "ok", "responding": True, "reason": "at setpoint"},
            "device": {"online": True, "has_water": True, "priming": False},
            "telemetry_stale": False,
        }
        base_extra.update(extra_overrides)
        write_runtime_state(repo.conn, {
            "state": "IDLE", "objective": "OPTIMIZE", "mode": "auto",
            "target_temp_f": 68.0, "bed_temp_f": 70.0, "room_temp_f": 68.0,
            "stage": "unknown", "confidence": 0.8, "target_level": -50,
            "daemon_alive": True, "extra": base_extra,
        })
    finally:
        repo.close()


def _clear_all_alerts():
    repo = get_repo()
    try:
        repo.conn.execute("UPDATE alerts SET acknowledged=1")
        repo.conn.commit()
    finally:
        repo.close()


def test_no_water_raises_a_critical_alert_via_status(auth_client):
    _clear_all_alerts()
    _set_runtime_extra(device={"online": True, "has_water": False, "priming": False})
    try:
        body = auth_client.get("/status").json()
        codes = [a["type"] for a in body["alerts"]]
        assert "health_no_water" in codes
        alert = next(a for a in body["alerts"] if a["type"] == "health_no_water")
        assert alert["severity"] == "critical"
    finally:
        _set_runtime_extra()  # restore healthy state for subsequent tests
        _clear_all_alerts()


def test_alert_clears_when_condition_resolves(auth_client):
    _clear_all_alerts()
    _set_runtime_extra(device={"online": True, "has_water": False, "priming": False})
    auth_client.get("/status")  # raises health_no_water

    _set_runtime_extra()  # water is back
    body = auth_client.get("/status").json()
    codes = [a["type"] for a in body["alerts"]]
    assert "health_no_water" not in codes
    _clear_all_alerts()


def test_repeated_polls_do_not_duplicate_the_same_alert(auth_client):
    _clear_all_alerts()
    _set_runtime_extra(device={"online": True, "has_water": False, "priming": False})
    try:
        auth_client.get("/status")
        auth_client.get("/status")
        auth_client.get("/status")
        repo = get_repo()
        try:
            n = repo.conn.execute(
                "SELECT COUNT(*) c FROM alerts WHERE type='health_no_water' AND acknowledged=0"
            ).fetchone()["c"]
        finally:
            repo.close()
        assert n == 1
    finally:
        _set_runtime_extra()
        _clear_all_alerts()


def test_thermal_stalled_alert_via_direct_service_call(auth_client):
    _clear_all_alerts()
    _set_runtime_extra(thermal_health={"state": "stalled", "responding": False, "reason": "no gap"})
    try:
        repo = get_repo()
        try:
            summary = services.evaluate_and_sync_health_alerts(repo)
        finally:
            repo.close()
        assert "thermal_stalled" in summary["newly_raised"]
    finally:
        _set_runtime_extra()
        _clear_all_alerts()


def test_vapid_public_key_endpoint_no_auth_required(client):
    r = client.get("/push/vapid-public-key")
    assert r.status_code == 200
    body = r.json()
    assert "public_key" in body and "configured" in body


def test_push_subscribe_requires_auth():
    # ``client``/``auth_client`` are session-scoped and share a login cookie once any
    # test authenticates, so use a fresh cookie-less TestClient here (same pattern as
    # test_api.py::test_auth_required).
    from fastapi.testclient import TestClient
    from app.main import app
    r = TestClient(app).post("/push/subscribe", json={
        "endpoint": "https://push.example/xyz",
        "keys": {"p256dh": "abc", "auth": "def"},
    })
    assert r.status_code == 401


def test_push_subscribe_persists_subscription(auth_client):
    r = auth_client.post("/push/subscribe", json={
        "endpoint": "https://push.example/subscribe-test",
        "keys": {"p256dh": "p256dh-value", "auth": "auth-value"},
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True

    repo = get_repo()
    try:
        row = repo.conn.execute(
            "SELECT * FROM push_subscriptions WHERE endpoint=?",
            ("https://push.example/subscribe-test",),
        ).fetchone()
    finally:
        repo.close()
    assert row is not None
    assert row["p256dh"] == "p256dh-value"


def test_push_subscribe_upserts_on_same_endpoint(auth_client):
    endpoint = "https://push.example/upsert-test"
    auth_client.post("/push/subscribe", json={
        "endpoint": endpoint, "keys": {"p256dh": "old", "auth": "old"}})
    auth_client.post("/push/subscribe", json={
        "endpoint": endpoint, "keys": {"p256dh": "new", "auth": "new"}})

    repo = get_repo()
    try:
        rows = repo.conn.execute(
            "SELECT * FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchall()
    finally:
        repo.close()
    assert len(rows) == 1
    assert rows[0]["p256dh"] == "new"


def test_push_unsubscribe_removes_row(auth_client):
    endpoint = "https://push.example/unsub-test"
    auth_client.post("/push/subscribe", json={
        "endpoint": endpoint, "keys": {"p256dh": "p", "auth": "a"}})
    r = auth_client.post("/push/unsubscribe", json={"endpoint": endpoint})
    assert r.status_code == 200

    repo = get_repo()
    try:
        row = repo.conn.execute(
            "SELECT * FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
    finally:
        repo.close()
    assert row is None
