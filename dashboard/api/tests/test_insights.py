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
