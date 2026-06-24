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


def test_power_away_prime_controls(auth_client):
    for action, expect in [("power-on", "power_on"), ("power-off", "power_off"),
                           ("away-on", "away_on"), ("away-off", "away_off"),
                           ("prime", "prime")]:
        assert auth_client.post(f"/control/{action}").json()["queued"] == expect


def test_temp_nudge_queues(auth_client):
    assert auth_client.post("/tonight/temp/nudge", json={"delta_f": -1.0}).json()["queued"] \
        == "nudge_temp"


def test_wake_with_night_type_and_plan(auth_client):
    r = auth_client.post("/tonight/wake", json={"wake_time": "05:30", "window_min": 15,
                                                "vibration_power": 20, "night_type": "work"})
    assert r.status_code == 200 and r.json()["queued"] == "set_wake"
    plan = auth_client.get("/tonight/plan").json()
    assert plan["mode"] in ("normal", "constrained", "recovery")
    assert "objective" in plan and "targets" in plan and "strategy" in plan
    assert "deep_pct_min" in plan["targets"]


def test_status_has_perfect_sleep_and_mode(auth_client):
    body = auth_client.get("/status").json()
    assert "power_on" in body and "away" in body
    if body["last_night"]:
        assert "perfect_sleep" in body["last_night"]


def test_maintenance_summary(auth_client):
    m = auth_client.get("/maintenance").json()
    assert "recurring_wake_times" in m and "strategy" in m
    assert "avg_wake_events" in m and "recent" in m


def test_induce_and_nap_sessions(auth_client):
    assert auth_client.post("/tonight/induce").json()["queued"] == "induce_sleep"
    # nap needs a duration or wake time
    assert auth_client.post("/tonight/nap", json={}).status_code == 400
    assert auth_client.post("/tonight/nap", json={"duration_min": 20}).json()["queued"] \
        == "start_nap"
    # preview returns a literature-backed strategy without starting
    for mins, strat in [(20, "power"), (90, "cycle"), (45, "trap")]:
        p = auth_client.post("/tonight/nap/preview", json={"duration_min": mins}).json()
        assert p["strategy"] == strat and "advice" in p
    assert auth_client.post("/tonight/session/end").json()["queued"] == "end_session"


def test_checkin_status_and_submit(auth_client):
    st = auth_client.get("/checkin/status").json()
    assert "due" in st and "perfect_sleep" in st
    date = st.get("date")
    r = auth_client.post("/checkin", json={
        "date": date, "rested": 8, "grogginess": 3, "daytime_energy": 7,
        "awakenings_felt": 2, "onset_feel": "normal",
        "factors": {"caffeine": False, "alcohol": True, "stress": False},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["subjective"]["rested"] == 8
    assert "perfect_sleep" in body and "objective" in body and "insights" in body
    # after submitting, the check-in is no longer due
    assert auth_client.get("/checkin/status").json()["due"] is False
