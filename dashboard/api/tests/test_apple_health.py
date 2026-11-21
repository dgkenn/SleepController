"""Apple Watch data pushed by Health Auto Export: parsed, stored idempotently, never published."""
import json

from app import apple_health, health_snapshot


def _payload():
    return {"data": {"metrics": [
        {"name": "heart_rate", "units": "count/min", "data": [
            {"date": "2026-11-21 03:14:00 -0500", "Min": 52, "Avg": 55, "Max": 58,
             "source": "Watch"},
            {"date": "2026-11-21 03:19:00 -0500", "qty": 54}]},
        {"name": "heart_rate_variability", "units": "ms", "data": [
            {"date": "2026-11-21 03:20:00 -0500", "qty": 61.5}]},
        {"name": "step_count", "data": [{"date": "2026-11-21 03:20:00 -0500", "qty": 3}]},
        {"name": "sleep_analysis", "data": [
            {"startDate": "2026-11-21 00:10:00 -0500", "endDate": "2026-11-21 00:40:00 -0500",
             "value": "Core", "source": "Watch"},
            {"startDate": "2026-11-21 00:40:00 -0500", "endDate": "2026-11-21 01:20:00 -0500",
             "value": "Deep"},
            {"date": "2026-11-21 07:00:00 -0500", "sleepStart": "2026-11-21 00:10:00 -0500",
             "sleepEnd": "2026-11-21 07:00:00 -0500", "core": 3.9, "deep": 1.1, "rem": 1.5,
             "awake": 0.3, "totalSleep": 6.5}]}]}}


def test_metrics_and_sleep_are_stored(auth_client):
    client = auth_client
    r = client.post("/apple/health", json=_payload())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["stored"]["heart_rate"] == 2 and out["stored"]["heart_rate_variability"] == 1
    assert out["sleep_segments"] == 2 and out["stored"]["sleep_totals"] == 5
    assert "step_count" in out["ignored_metrics"]


def test_a_resent_window_does_not_duplicate(auth_client):
    client = auth_client
    from app.db import get_repo
    client.post("/apple/health", json=_payload())
    client.post("/apple/health", json=_payload())
    conn = get_repo().conn
    assert conn.execute("SELECT COUNT(*) FROM apple_health WHERE metric='heart_rate'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM apple_sleep").fetchone()[0] == 2


def test_times_are_stored_in_utc():
    assert apple_health.parse_ts("2026-11-21 03:14:00 -0500") == "2026-11-21T08:14:00+00:00"
    assert apple_health.parse_ts("2026-11-21T03:14:00") is None   # no offset: unplaceable


def test_night_stages_returns_the_reference_hypnogram(auth_client):
    client = auth_client
    from app.db import get_repo
    client.post("/apple/health", json=_payload())
    segs = apple_health.night_stages(get_repo().conn, "2026-11-21T05:00:00+00:00",
                                     "2026-11-21T12:00:00+00:00")
    assert [s["stage"] for s in segs] == ["core", "deep"]


def test_a_payload_without_metrics_is_refused_cleanly(auth_client):
    client = auth_client
    assert client.post("/apple/health", json={"hello": 1}).json()["ok"] is False


def test_the_health_snapshot_carries_counts_but_no_values(auth_client):
    from app.db import get_repo
    client, repo = auth_client, get_repo()
    client.post("/apple/health", json=_payload())
    block = health_snapshot._apple_health_block(repo)
    assert block["sleep_nights_7d"] >= 1 and block["points_7d"]["heart_rate"] >= 2
    blob = json.dumps(block)
    assert "55" not in blob and "61.5" not in blob


def test_it_needs_no_login_on_the_box_by_default(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.delenv("SLEEPCTL_NO_LOGIN", raising=False)
    assert TestClient(app).post("/apple/health", json=_payload()).status_code == 200
