"""Tests for the read-only interpretability surface: /insights/decisions + /insights/parameters."""

from __future__ import annotations

from datetime import datetime


def test_insights_requires_auth(client):
    from fastapi.testclient import TestClient
    from app.main import app
    fresh = TestClient(app)
    assert fresh.get("/insights/decisions").status_code == 401
    assert fresh.get("/insights/parameters").status_code == 401


def test_insights_decisions_shape_empty(auth_client):
    r = auth_client.get("/insights/decisions")
    assert r.status_code == 200
    body = r.json()
    assert "decisions" in body and "n" in body
    assert isinstance(body["decisions"], list)
    assert body["n"] == len(body["decisions"])


def test_insights_decisions_reflects_logged_rows(auth_client):
    from app.db import get_repo
    from sleepctl.models import (
        ControllerState, CorrectionAction, Decision, Intervention, NightObjective,
        ThermalIntent,
    )

    repo = get_repo()
    try:
        now = datetime.now()
        night_date = now.date().isoformat()
        decision = Decision(
            timestamp=now,
            state=ControllerState.MAINTENANCE,
            objective=NightObjective.OPTIMIZE,
            thermal_intent=ThermalIntent.DEEP_BIAS_COOL,
            target_temp_f=66.5,
            target_level=-40,
            action=CorrectionAction.COOLER,
            reason="cooling to protect deep sleep window",
            confidence=0.82,
        )
        repo.log_decision(decision, night_date)
        repo.log_intervention(
            Intervention(
                timestamp=now,
                state=ControllerState.MAINTENANCE,
                action=CorrectionAction.COOLER,
                magnitude_f=1.5,
                reason="cooling to protect deep sleep window",
            ),
            night_date,
        )
    finally:
        repo.close()

    r = auth_client.get("/insights/decisions?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["n"] >= 1
    last = body["decisions"][-1]
    assert last["state"] == "maintenance"
    assert last["intent"] == "deep_bias_cool"
    assert last["action"] == "cooler"
    assert last["target_temp_f"] == 66.5
    assert last["reason"] == "cooling to protect deep sleep window"
    assert last["confidence"] == 0.82
    # An intervention logged at the exact same timestamp means the bed actually moved.
    assert last["moved"] is True
    assert last["magnitude_f"] == 1.5


def test_insights_decisions_limit_respected(auth_client):
    r = auth_client.get("/insights/decisions?limit=2")
    assert r.status_code == 200
    assert len(r.json()["decisions"]) <= 2


def test_insights_parameters_shape(auth_client):
    r = auth_client.get("/insights/parameters")
    assert r.status_code == 200
    body = r.json()
    assert "parameters" in body and "n" in body
    params = body["parameters"]
    assert isinstance(params, list) and len(params) > 0
    assert body["n"] == len(params)
    names = {p["name"] for p in params}
    # The setpoint profile fields should always be present (seed data creates a setpoint version).
    assert "neutral_f" in names
    for p in params:
        assert set(p) >= {"name", "value", "source", "confidence", "what"}
        assert isinstance(p["what"], str) and len(p["what"]) > 0


def test_insights_parameters_includes_calibration_when_present(auth_client):
    from app.db import get_repo
    repo = get_repo()
    try:
        repo.save_thermal_calibration({
            "cool_levels_per_min": 2.0, "heat_levels_per_min": 1.5,
            "cool_f_per_min": 0.3, "heat_f_per_min": 0.2,
            "cool_lag_min": 8.0, "heat_lag_min": 10.0, "source": "self_test",
        })
        repo.save_comfort_profile({
            "neutral_f": 71.0, "cool_edge_f": 65.0, "warm_edge_f": 76.0,
            "ratings": [{"f": 71.0, "rating": 0}], "source": "comfort_cal",
        })
        repo.save_resting_baseline({
            "hr": 54.0, "hrv": 62.0, "rr": 13.5, "movement": 0.02,
            "n_samples": 300, "source": "self_test",
        })
    finally:
        repo.close()

    body = auth_client.get("/insights/parameters").json()
    names = {p["name"] for p in body["parameters"]}
    assert "cool_f_per_min" in names
    assert "heat_f_per_min" in names
    assert "comfort_neutral_f" in names
    assert "resting_hr_hrv" in names


# ---- /insights/wake-patterns: the 3AM WAKE targeted analysis --------------------------------

def test_wake_patterns_requires_auth():
    from fastapi.testclient import TestClient
    from app.main import app
    fresh = TestClient(app)
    assert fresh.get("/insights/wake-patterns").status_code == 401


def test_wake_patterns_shape(auth_client):
    r = auth_client.get("/insights/wake-patterns")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {
        "lookback_nights", "n_nights_available", "bin_minutes",
        "recurring_windows", "n_recurring_windows", "note",
    }
    assert isinstance(body["recurring_windows"], list)
    assert body["n_recurring_windows"] == len(body["recurring_windows"])
    # the shared seed only populates nightly_summaries/context, not raw_samples, so there's no
    # awakening history yet -- the report must degrade gracefully, not error.
    assert body["recurring_windows"] == []


def test_wake_patterns_lookback_param_is_clamped(auth_client):
    r = auth_client.get("/insights/wake-patterns?lookback_nights=999999")
    assert r.status_code == 200
    assert r.json()["lookback_nights"] == 365
    r2 = auth_client.get("/insights/wake-patterns?lookback_nights=0")
    assert r2.status_code == 200
    assert r2.json()["lookback_nights"] == 1


def test_wake_patterns_reflects_a_recurring_logged_awakening(auth_client):
    from datetime import datetime, timedelta

    from app.db import get_repo
    from sleepctl.models import SensorFrame, SleepStage

    repo = get_repo()
    try:
        base = datetime.now() - timedelta(days=5)
        for i in range(5):
            d = (base + timedelta(days=i)).date().isoformat()
            repo.conn.execute(
                "INSERT OR REPLACE INTO nightly_summaries (date) VALUES (?)", (d,))
            repo.conn.commit()
            wake_ts = datetime.fromisoformat(d) + timedelta(days=1, hours=3, minutes=10)
            pre = SensorFrame(timestamp=wake_ts - timedelta(minutes=5), stage=SleepStage.REM,
                              stage_confidence=0.85, heart_rate=55, hrv=60,
                              respiratory_rate=14, movement=0.05, presence=True,
                              bed_temp_f=70.0, room_temp_f=67.0, data_age_seconds=5.0)
            repo.log_sample(pre, "maintenance", False, d)
            wake = SensorFrame(timestamp=wake_ts, stage=SleepStage.AWAKE, stage_confidence=0.85,
                               heart_rate=68, hrv=45, respiratory_rate=16, movement=0.4,
                               presence=True, bed_temp_f=70.0, room_temp_f=67.0,
                               data_age_seconds=5.0)
            repo.log_sample(wake, "maintenance", True, d)
    finally:
        repo.close()

    body = auth_client.get("/insights/wake-patterns?lookback_nights=30").json()
    assert body["n_recurring_windows"] >= 1
    w = body["recurring_windows"][0]
    assert w["window"]["label"].startswith("03:00")
    assert w["window"]["stage_exited"] == "rem"
    assert w["window"]["nights_woke"] == 5
